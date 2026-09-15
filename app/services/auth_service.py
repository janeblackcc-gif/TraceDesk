from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import case, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.audit.service import audit
from app.auth.passwords import HASHER, hash_password, verify_password
from app.authz.policy import active_user, kb_access, workspace_access
from app.db.models import (AuthRateLimit, BootstrapState, KBMember, KnowledgeBase, User,
                           UserSession, Workspace, WorkspaceMember)
from app.db.session import Database
from .errors import DomainError


def token_hash(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def csrf_for(token: str) -> str:
    return hashlib.sha256(b'csrf:' + token.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    session_id: UUID
    auth_version: int


@dataclass(frozen=True, repr=False)
class LoginResult:
    token: str
    csrf: str


class AuthService:
    def __init__(self, database: Database, bootstrap_token: str | None = None):
        self.database = database
        self._bootstrap_hash = token_hash(bootstrap_token) if bootstrap_token is not None else None

    def throttle(self, client: str, email: str) -> None:
        now = datetime.now(timezone.utc)
        exceeded = False
        with self.database.transaction() as session:
            session.execute(delete(AuthRateLimit).where(AuthRateLimit.window_started_at < now - timedelta(days=1)))
            for bucket, maximum in [('client:' + client, 30), ('email:' + email.casefold(), 10)]:
                stale = AuthRateLimit.window_started_at < now - timedelta(minutes=1)
                hits = session.scalar(insert(AuthRateLimit).values(bucket_key=token_hash(bucket).hex(), window_started_at=now, hits=1)
                    .on_conflict_do_update(index_elements=['bucket_key'], set_={
                        'window_started_at': case((stale, now), else_=AuthRateLimit.window_started_at),
                        'hits': case((stale, 1), else_=AuthRateLimit.hits + 1)})
                    .returning(AuthRateLimit.hits))
                exceeded = exceeded or hits > maximum
        if exceeded:
            raise DomainError('AUTH_RATE_LIMITED', 429, retryable=True)

    def bootstrap(self, supplied: str, email: str, password: str, display_name: str, request_id: str) -> UUID:
        if self._bootstrap_hash is None or not hmac.compare_digest(token_hash(supplied), self._bootstrap_hash):
            raise DomainError('BOOTSTRAP_UNAVAILABLE', 403)
        try:
            encoded = hash_password(password)
        except ValueError:
            raise DomainError('PASSWORD_POLICY', 422) from None
        with self.database.transaction() as session:
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('system-bootstrap', 0))"))
            if session.get(BootstrapState, 1) is not None or session.scalar(select(func.count()).select_from(User)):
                raise DomainError('BOOTSTRAP_CONSUMED', 409)
            user = User(email=email.casefold(), display_name=display_name, password_hash=encoded, is_system_admin=True)
            workspace = Workspace(name='Team Workspace', slug='team')
            session.add_all([user, workspace, BootstrapState(id=1)])
            session.flush()
            session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role='admin'))
            audit(session, 'bootstrap', actor=user.id, request_id=request_id, workspace_id=workspace.id)
            return user.id

    def login(self, email: str, password: str, client: str, request_id: str, previous_token: str | None = None) -> LoginResult:
        self.throttle(client, email)
        now = datetime.now(timezone.utc)
        result = None
        with self.database.transaction() as session:
            user = session.scalar(select(User).where(User.email == email.casefold()).with_for_update())
            valid = verify_password(password, user.password_hash if user else None)
            if user is None or not valid or user.status != 'active':
                audit(session, 'login', actor=None, request_id=request_id, outcome='denied')
            else:
                if HASHER.check_needs_rehash(user.password_hash):
                    user.password_hash = hash_password(password)
                if previous_token:
                    previous = session.scalar(select(UserSession).where(UserSession.token_hash == token_hash(previous_token)))
                    if previous:
                        previous.revoked_at = now
                token = secrets.token_urlsafe(32)
                csrf = csrf_for(token)
                session.add(UserSession(user_id=user.id, token_hash=token_hash(token), csrf_hash=token_hash(csrf),
                    expires_at=now + timedelta(hours=8), last_seen_at=now, auth_version_at_issue=user.auth_version))
                user.last_login_at = now
                audit(session, 'login', actor=user.id, request_id=request_id)
                result = LoginResult(token, csrf)
        if result is None:
            raise DomainError('INVALID_CREDENTIALS', 401)
        return result

    def authenticate(self, token: str | None, csrf: str | None = None, *, write: bool = False) -> Principal:
        if token is None or len(token) > 128:
            raise DomainError('AUTHENTICATION_REQUIRED', 401)
        now = datetime.now(timezone.utc)
        with self.database.transaction() as session:
            record = session.scalar(select(UserSession).where(UserSession.token_hash == token_hash(token)).with_for_update())
            if record is None or record.revoked_at is not None or record.expires_at <= now or record.last_seen_at <= now - timedelta(minutes=30):
                raise DomainError('AUTHENTICATION_REQUIRED', 401)
            user = active_user(session, record.user_id)
            if record.auth_version_at_issue != user.auth_version:
                raise DomainError('AUTHENTICATION_REQUIRED', 401)
            if write and (csrf is None or len(csrf) > 128 or not hmac.compare_digest(token_hash(csrf), record.csrf_hash)):
                raise DomainError('CSRF_REJECTED', 403)
            record.last_seen_at = now
            return Principal(user.id, record.id, user.auth_version)

    def logout(self, principal: Principal, request_id: str) -> None:
        with self.database.transaction() as session:
            record = session.get(UserSession, principal.session_id, with_for_update=True)
            if record:
                record.revoked_at = datetime.now(timezone.utc)
                audit(session, 'logout', actor=principal.user_id, request_id=request_id)

    def create_user(self, actor: UUID, email: str, password: str, display_name: str, request_id: str) -> UUID:
        with self.database.transaction() as session:
            if not active_user(session, actor).is_system_admin:
                raise DomainError('FORBIDDEN', 403)
            if session.scalar(select(User.id).where(User.email == email.casefold())):
                raise DomainError('USER_EXISTS', 409)
            try:
                encoded = hash_password(password)
            except ValueError:
                raise DomainError('PASSWORD_POLICY', 422) from None
            user = User(email=email.casefold(), display_name=display_name, password_hash=encoded)
            session.add(user)
            session.flush()
            audit(session, 'user.create', actor=actor, request_id=request_id, resource_type='user', resource_id=str(user.id))
            return user.id

    def change_user(self, actor: UUID, target_id: UUID, request_id: str, *, password: str | None = None, disabled: bool | None = None) -> None:
        with self.database.transaction() as session:
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('system-users', 0))"))
            if not active_user(session, actor).is_system_admin:
                raise DomainError('FORBIDDEN', 403)
            target = session.get(User, target_id, with_for_update=True)
            if target is None:
                raise DomainError('NOT_FOUND', 404)
            if disabled and target.is_system_admin:
                count = session.scalar(select(func.count()).select_from(User).where(User.is_system_admin.is_(True), User.status == 'active'))
                if count <= 1:
                    raise DomainError('LAST_ADMIN_REQUIRED', 409)
            if password is not None:
                try:
                    target.password_hash = hash_password(password)
                except ValueError:
                    raise DomainError('PASSWORD_POLICY', 422) from None
            if disabled is not None:
                target.status = 'disabled' if disabled else 'active'
            target.auth_version += 1
            audit(session, 'user.change', actor=actor, request_id=request_id, resource_type='user', resource_id=str(target_id))

    def set_workspace_member(self, actor: UUID, workspace_id: UUID, target_id: UUID, role: str | None, request_id: str) -> None:
        if role not in {None, 'admin', 'member'}:
            raise DomainError('INVALID_ROLE', 422)
        with self.database.transaction() as session:
            workspace = session.get(Workspace, workspace_id, with_for_update=True)
            workspace_access(session, actor, workspace_id, admin=True)
            active_user(session, target_id)
            member = session.get(WorkspaceMember, (workspace_id, target_id))
            if role is None:
                if member:
                    session.delete(member)
            elif member:
                member.role = role
            else:
                session.add(WorkspaceMember(workspace_id=workspace_id, user_id=target_id, role=role))
            workspace.permission_epoch += 1
            audit(session, 'workspace.member.change', actor=actor, request_id=request_id, workspace_id=workspace_id, resource_type='user', resource_id=str(target_id))

    def set_kb_member(self, actor: UUID, kb_id: UUID, target_id: UUID, role: str | None, request_id: str) -> None:
        if role not in {None, 'editor', 'viewer'}:
            raise DomainError('INVALID_ROLE', 422)
        with self.database.transaction() as session:
            session.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id).with_for_update())
            kb = kb_access(session, actor, kb_id, 'admin')
            active_user(session, target_id)
            if session.get(WorkspaceMember, (kb.workspace_id, target_id)) is None:
                raise DomainError('WORKSPACE_MEMBERSHIP_REQUIRED', 409)
            member = session.get(KBMember, (kb_id, target_id))
            if role is None:
                if member:
                    session.delete(member)
            elif member:
                member.role = role
            else:
                session.add(KBMember(kb_id=kb_id, user_id=target_id, role=role))
            kb.permission_epoch += 1
            audit(session, 'kb.member.change', actor=actor, request_id=request_id, workspace_id=kb.workspace_id,
                  kb_id=kb_id, resource_type='user', resource_id=str(target_id))
