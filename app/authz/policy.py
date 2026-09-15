from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import KBMember, KnowledgeBase, User, Workspace, WorkspaceMember
from app.services.errors import DomainError

Access = Literal['viewer', 'editor', 'admin']
RANK = {'viewer': 1, 'editor': 2, 'admin': 3}


def active_user(session: Session, user_id: UUID) -> User:
    user = session.get(User, user_id, populate_existing=True)
    if user is None or user.status != 'active':
        raise DomainError('AUTHENTICATION_REQUIRED', 401)
    return user


def workspace_access(session: Session, user_id: UUID, workspace_id: UUID, *, admin: bool = False) -> Workspace:
    user = active_user(session, user_id)
    workspace = session.get(Workspace, workspace_id, populate_existing=True)
    if workspace is None:
        raise DomainError('NOT_FOUND', 404)
    member = session.get(WorkspaceMember, (workspace_id, user_id), populate_existing=True)
    if not user.is_system_admin and member is None:
        raise DomainError('NOT_FOUND', 404)
    if admin and not user.is_system_admin and (member is None or member.role != 'admin'):
        raise DomainError('FORBIDDEN', 403)
    return workspace


def kb_access(session: Session, user_id: UUID, kb_id: UUID, access: Access = 'viewer') -> KnowledgeBase:
    user = active_user(session, user_id)
    kb = session.get(KnowledgeBase, kb_id, populate_existing=True)
    if kb is None or kb.deleted_at is not None:
        raise DomainError('NOT_FOUND', 404)
    member = session.get(WorkspaceMember, (kb.workspace_id, user_id), populate_existing=True)
    if user.is_system_admin or (member is not None and member.role == 'admin'):
        return kb
    grant = session.get(KBMember, (kb_id, user_id), populate_existing=True)
    if member is None or grant is None:
        raise DomainError('NOT_FOUND', 404)
    if RANK[grant.role] < RANK[access]:
        raise DomainError('FORBIDDEN', 403)
    return kb


def kb_role(session: Session, user_id: UUID, kb_id: UUID) -> Access:
    kb = kb_access(session, user_id, kb_id)
    user = active_user(session, user_id)
    member = session.get(WorkspaceMember, (kb.workspace_id, user_id))
    if user.is_system_admin or (member and member.role == 'admin'):
        return 'admin'
    grant = session.get(KBMember, (kb_id, user_id))
    if grant and grant.role == 'editor':
        return 'editor'
    return 'viewer'


@dataclass(frozen=True)
class AuthorizationSnapshot:
    user_id: UUID
    kb_id: UUID
    auth_version: int
    kb_permission_epoch: int
    workspace_permission_epoch: int
    data_epoch: int


def capture(session: Session, user_id: UUID, kb_id: UUID) -> AuthorizationSnapshot:
    kb = kb_access(session, user_id, kb_id)
    user = active_user(session, user_id)
    workspace = session.get(Workspace, kb.workspace_id, populate_existing=True)
    if workspace is None:
        raise DomainError('NOT_FOUND', 404)
    return AuthorizationSnapshot(user_id, kb_id, user.auth_version, kb.permission_epoch,
                                  workspace.permission_epoch, kb.data_epoch)


def recheck(session: Session, snapshot: AuthorizationSnapshot) -> None:
    try:
        current = capture(session, snapshot.user_id, snapshot.kb_id)
    except DomainError:
        raise DomainError('AUTHORIZATION_CHANGED', 409) from None
    if current != snapshot:
        raise DomainError('AUTHORIZATION_CHANGED', 409)
