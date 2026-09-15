from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import select, text

from app.api.dependencies import authentication
from app.api.v1.auth import KBView
from app.audit.service import audit
from app.authz.policy import active_user, kb_access, kb_role, workspace_access
from app.db.models import (DocumentRevision, FileObject, Job, KBMember, KnowledgeBase, LegacyAlias,
                           LogicalDocument, User, WorkspaceMember)
from app.db.session import Database
from app.jobs.repository import JobRepository, TERMINAL, enqueue, payload_hash
from app.ingest import MAX_BYTES
from app.services.auth_service import AuthService, Principal
from app.services.document_service import DocumentService
from app.services.errors import DomainError
from app.services.legacy_resolver import resolve_legacy


class DocumentView(BaseModel):
    id: UUID
    name: str
    active_revision_id: UUID | None
    desired_revision_id: UUID | None
    state: str
    deleted_at: datetime | None
    revision_no: int | None
    source_file_available: bool


class DocumentPage(BaseModel):
    items: list[DocumentView]
    next_cursor: UUID | None


class JobItem(BaseModel):
    id: UUID
    type: str
    state: str
    created_at: datetime
    heartbeat_at: datetime | None
    attempt: int
    cancel_requested: bool
    error_code: str | None


class JobPage(BaseModel):
    items: list[JobItem]
    next_cursor: UUID | None


class KBPage(BaseModel):
    items: list[KBView]
    next_cursor: UUID | None


class ReparseView(BaseModel):
    revision_id: UUID
    job_id: UUID
    state: str


class MemberView(BaseModel):
    user_id: UUID
    display_name: str
    role: str


class LegacyView(BaseModel):
    revision_id: UUID


class UserItem(BaseModel):
    id: UUID
    email: str
    display_name: str
    status: str
    workspace_role: str | None


class UserPage(BaseModel):
    items: list[UserItem]
    next_cursor: UUID | None


def build_catalog_router(database: Database, auth: AuthService, documents: DocumentService) -> APIRouter:
    router = APIRouter(prefix='/api/v1')
    CurrentUser = Annotated[Principal, Depends(authentication(auth))]
    Limit = Annotated[int, Query(ge=1, le=200)]

    @router.get('/workspaces/{workspace_id}/users', response_model=UserPage)
    def users(workspace_id: UUID, principal: CurrentUser, cursor: UUID | None = None, limit: Limit = 50) -> UserPage:
        with database.transaction() as session:
            workspace_access(session, principal.user_id, workspace_id, admin=True)
            actor = active_user(session, principal.user_id)
            query = select(User, WorkspaceMember.role).outerjoin(WorkspaceMember,
                (WorkspaceMember.user_id == User.id) & (WorkspaceMember.workspace_id == workspace_id))
            if not actor.is_system_admin:
                query = query.where(WorkspaceMember.workspace_id == workspace_id)
            if cursor:
                query = query.where(User.id > cursor)
            rows = session.execute(query.order_by(User.id).limit(limit + 1)).all()
            return UserPage(items=[UserItem(id=user.id, email=user.email, display_name=user.display_name,
                status=user.status, workspace_role=role) for user, role in rows[:limit]],
                next_cursor=rows[limit - 1][0].id if len(rows) > limit else None)

    @router.get('/workspaces/{workspace_id}/knowledge-bases', response_model=KBPage)
    def knowledge_bases(workspace_id: UUID, principal: CurrentUser, cursor: UUID | None = None, limit: Limit = 50) -> KBPage:
        with database.transaction() as session:
            workspace_access(session, principal.user_id, workspace_id)
            user = active_user(session, principal.user_id)
            member = session.get(WorkspaceMember, (workspace_id, user.id))
            query = select(KnowledgeBase).where(KnowledgeBase.workspace_id == workspace_id, KnowledgeBase.deleted_at.is_(None))
            if not user.is_system_admin and member.role != 'admin':
                query = query.join(KBMember, KBMember.kb_id == KnowledgeBase.id).where(KBMember.user_id == user.id)
            if cursor:
                query = query.where(KnowledgeBase.id > cursor)
            rows = session.scalars(query.order_by(KnowledgeBase.id).limit(limit + 1)).all()
            return KBPage(items=[KBView(id=row.id, workspace_id=row.workspace_id, name=row.name, slug=row.slug,
                role=kb_role(session, user.id, row.id), active_index_generation_id=row.active_index_generation_id,
                data_epoch=row.data_epoch) for row in rows[:limit]], next_cursor=rows[limit - 1].id if len(rows) > limit else None)

    @router.get('/knowledge-bases/{kb_id}/documents', response_model=DocumentPage)
    def list_documents(kb_id: UUID, principal: CurrentUser, cursor: UUID | None = None, limit: Limit = 50,
                       include_deleted: bool = False) -> DocumentPage:
        with database.transaction() as session:
            kb_access(session, principal.user_id, kb_id, 'admin' if include_deleted else 'viewer')
            query = select(LogicalDocument).where(LogicalDocument.kb_id == kb_id)
            if not include_deleted:
                query = query.where(LogicalDocument.deleted_at.is_(None))
            if cursor:
                query = query.where(LogicalDocument.id > cursor)
            rows = session.scalars(query.order_by(LogicalDocument.id).limit(limit + 1)).all()
            result = []
            for row in rows[:limit]:
                revision = session.get(DocumentRevision, row.desired_revision_id) if row.desired_revision_id else None
                result.append(DocumentView(id=row.id, name=row.display_name, active_revision_id=row.active_revision_id,
                    desired_revision_id=row.desired_revision_id, state='deleted' if row.deleted_at else revision.status if revision else 'empty',
                    deleted_at=row.deleted_at, revision_no=revision.revision_no if revision else None,
                    source_file_available=revision.source_file_available if revision else False))
            return DocumentPage(items=result, next_cursor=rows[limit - 1].id if len(rows) > limit else None)

    @router.get('/jobs', response_model=JobPage)
    def jobs(kb_id: UUID, principal: CurrentUser, state: Literal['queued', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled', 'superseded'] | None = None,
             cursor: UUID | None = None, limit: Limit = 50) -> JobPage:
        with database.transaction() as session:
            role = kb_role(session, principal.user_id, kb_id)
            query = select(Job).where(Job.kb_id == kb_id)
            if role != 'admin':
                query = query.where((Job.type != 'query') | (Job.created_by == principal.user_id))
            if state:
                query = query.where(Job.state == state)
            if cursor:
                previous = session.scalar(select(Job).where(Job.id == cursor, Job.kb_id == kb_id))
                if previous is None:
                    raise DomainError('INVALID_CURSOR', 422)
                query = query.where((Job.created_at < previous.created_at) |
                    ((Job.created_at == previous.created_at) & (Job.id < previous.id)))
            rows = session.scalars(query.order_by(Job.created_at.desc(), Job.id.desc()).limit(limit + 1)).all()
            return JobPage(items=[JobItem(id=job.id, type=job.type, state=job.state, created_at=job.created_at,
                heartbeat_at=job.heartbeat_at, attempt=job.attempt_count, cancel_requested=job.cancel_requested_at is not None,
                error_code=job.error_code) for job in rows[:limit]], next_cursor=rows[limit - 1].id if len(rows) > limit else None)

    @router.get('/knowledge-bases/{kb_id}/members/{user_id}', response_model=MemberView)
    def member(kb_id: UUID, user_id: UUID, principal: CurrentUser) -> MemberView:
        with database.transaction() as session:
            kb_access(session, principal.user_id, kb_id, 'admin')
            row, user = session.get(KBMember, (kb_id, user_id)), session.get(User, user_id)
            if row is None or user is None:
                raise DomainError('NOT_FOUND', 404)
            return MemberView(user_id=user.id, display_name=user.display_name, role=row.role)

    @router.post('/document-revisions/{revision_id}/reparse', status_code=202, response_model=ReparseView)
    def reparse(revision_id: UUID, request: Request, principal: CurrentUser) -> ReparseView:
        key = request.headers.get('Idempotency-Key', '')
        if not 1 <= len(key) <= 200:
            raise DomainError('IDEMPOTENCY_KEY_REQUIRED', 422)
        with database.transaction() as session:
            revision = session.get(DocumentRevision, revision_id)
            document = session.get(LogicalDocument, revision.document_id) if revision else None
            if document is None:
                raise DomainError('NOT_FOUND', 404)
            session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'), {'key': 'kb-mutation:' + str(document.kb_id)})
            kb = kb_access(session, principal.user_id, document.kb_id, 'editor')
            session.refresh(document)
            session.refresh(revision)
            if document.deleted_at or revision.deleted_at or document.desired_revision_id != revision.id:
                raise DomainError('SOURCE_GONE', 410)
            if not revision.source_file_available:
                raise DomainError('ORIGINAL_FILE_UNAVAILABLE', 409)
            job_key = 'reparse:' + payload_hash({'actor': str(principal.user_id), 'revision': str(revision.id), 'key': key})
            existing = session.scalar(select(Job).where(Job.type == 'parse', Job.idempotency_key == job_key))
            if existing:
                return ReparseView(revision_id=revision.id, job_id=existing.id, state=existing.state)
            for pending in session.scalars(select(Job).where(Job.type == 'parse', Job.revision_id == revision.id,
                    ~Job.state.in_(TERMINAL)).with_for_update()):
                JobRepository._terminal(session, pending, 'superseded', 'REPARSE_REQUESTED')
            job = enqueue(session, kind='parse', workspace_id=kb.workspace_id, kb_id=kb.id, created_by=principal.user_id,
                revision_id=revision.id, key=job_key,
                payload={'revision_id': str(revision.id)})
            audit(session, 'document.reparse', actor=principal.user_id, request_id=request.state.request_id,
                  workspace_id=kb.workspace_id, kb_id=kb.id, resource_type='revision', resource_id=str(revision.id))
            return ReparseView(revision_id=revision.id, job_id=job.id, state=job.state)

    @router.get('/document-revisions/{revision_id}/file', response_class=Response)
    def original_file(revision_id: UUID, request: Request, principal: CurrentUser) -> Response:
        with database.transaction() as session:
            revision = session.get(DocumentRevision, revision_id)
            document = session.get(LogicalDocument, revision.document_id) if revision else None
            if document is None:
                raise DomainError('NOT_FOUND', 404)
            kb = kb_access(session, principal.user_id, document.kb_id)
            if document.deleted_at or revision.deleted_at or revision.purged_at:
                raise DomainError('SOURCE_GONE', 410)
            if revision.status == 'quarantined':
                kb_access(session, principal.user_id, document.kb_id, 'admin')
            if not revision.source_file_available:
                raise DomainError('ORIGINAL_FILE_UNAVAILABLE', 409)
            file = session.get(FileObject, revision.file_object_id)
            if file is None:
                raise DomainError('SOURCE_GONE', 410)
            with documents.objects.open(file.storage_key) as stream:
                content = stream.read(MAX_BYTES + 1)
            import hashlib
            if len(content) != file.size_bytes or len(content) > MAX_BYTES or hashlib.sha256(content).hexdigest() != file.sha256:
                raise DomainError('SOURCE_OBJECT_CORRUPT', 503)
            audit(session, 'document.download', actor=principal.user_id, request_id=request.state.request_id,
                  workspace_id=kb.workspace_id, kb_id=kb.id, resource_type='revision', resource_id=str(revision.id))
            return Response(content, media_type='application/octet-stream',
                headers={'Content-Disposition': f'attachment; filename="source-{revision.id}{file.original_extension}"'})

    @router.get('/legacy/sources/{legacy_doc_id}', response_model=LegacyView)
    def legacy_source(legacy_doc_id: str, principal: CurrentUser) -> LegacyView:
        with database.transaction() as session:
            alias = session.get(LegacyAlias, ('document', legacy_doc_id))
            if alias is None:
                raise DomainError('NOT_FOUND', 404)
            kb_access(session, principal.user_id, alias.kb_id)
            result = resolve_legacy(session, 'document', legacy_doc_id, authorized_kb_id=alias.kb_id)
            if result.status != 200 or result.target_id is None:
                raise DomainError('SOURCE_GONE' if result.status == 410 else 'NOT_FOUND', result.status)
            return LegacyView(revision_id=result.target_id)

    return router
