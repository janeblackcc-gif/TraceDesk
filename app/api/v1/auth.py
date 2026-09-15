import re
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select

from app.authz.policy import active_user, kb_access, kb_role, workspace_access
from app.db.models import Job, KnowledgeBase, Workspace, WorkspaceMember
from app.db.session import Database
from app.services.auth_service import AuthService, Principal, csrf_for
from app.services.errors import DomainError
from app.services.document_service import DocumentService
from app.services.deletion_service import DeletionService
from app.jobs.repository import JobRepository
from app.ingest import MAX_BYTES
from app.api.dependencies import COOKIE, authentication



class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid')


class LoginBody(StrictBody):
    email: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator('email')
    @classmethod
    def email_address(cls, value: str) -> str:
        value = value.strip().casefold()
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            raise ValueError('Invalid email address')
        return value


class CreateUserBody(LoginBody):
    display_name: str = Field(min_length=1, max_length=120)


class BootstrapBody(CreateUserBody):
    token: SecretStr = Field(min_length=32, max_length=256)


class ChangeUserBody(StrictBody):
    password: SecretStr | None = Field(default=None, min_length=12, max_length=128)
    disabled: bool | None = None


class MemberBody(StrictBody):
    role: Literal['admin', 'member', 'editor', 'viewer']


class CreateKBBody(StrictBody):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=120, pattern=r'^[a-z0-9][a-z0-9-]*$')


class IdentifierResponse(BaseModel):
    id: UUID


class UserView(BaseModel):
    id: UUID
    email: str
    display_name: str
    is_system_admin: bool


class WorkspaceView(BaseModel):
    id: UUID
    name: str
    role: str


class KBView(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    slug: str
    role: Literal['admin', 'editor', 'viewer']
    active_index_generation_id: UUID | None = None
    data_epoch: int = 1


class MeResponse(BaseModel):
    user: UserView
    workspaces: list[WorkspaceView]
    knowledge_bases: list[KBView]


class UploadResponse(BaseModel):
    document_id: UUID
    revision_id: UUID
    job_id: UUID | None
    state: str
    duplicate: bool


class SourceResponse(BaseModel):
    revision_id: UUID
    filename: str
    page: int
    text: str


class JobView(BaseModel):
    id: UUID
    type: str
    state: str
    attempt: int
    cancel_requested: bool
    error_code: str | None


class DeletionView(BaseModel):
    document_id: UUID
    deletion_id: UUID
    state: Literal['deleted', 'purged']
    restore_before: datetime


class RestoreView(BaseModel):
    document_id: UUID
    state: Literal['restored']


def build_router(database: Database, service: AuthService, documents: DocumentService) -> APIRouter:
    router = APIRouter(prefix='/api/v1')

    CurrentUser = Annotated[Principal, Depends(authentication(service))]

    @router.post('/auth/bootstrap', status_code=201, response_model=IdentifierResponse)
    def bootstrap(body: BootstrapBody, request: Request) -> IdentifierResponse:
        service.throttle(request.client.host if request.client else 'unknown', 'bootstrap')
        return IdentifierResponse(id=service.bootstrap(body.token.get_secret_value(), body.email,
            body.password.get_secret_value(), body.display_name, request.state.request_id))

    @router.post('/auth/login', status_code=204)
    def login(body: LoginBody, request: Request) -> Response:
        result = service.login(body.email, body.password.get_secret_value(), request.client.host if request.client else 'unknown',
                               request.state.request_id, request.cookies.get(COOKIE))
        response = Response(status_code=204, headers={'X-CSRF-Token': result.csrf})
        response.set_cookie(COOKIE, result.token, max_age=8 * 3600, secure=True, httponly=True, samesite='strict', path='/')
        return response

    @router.get('/auth/csrf')
    def csrf(request: Request, principal: CurrentUser) -> dict[str, str]:
        return {'csrf_token': csrf_for(request.cookies[COOKIE])}

    @router.post('/auth/logout', status_code=204)
    def logout(request: Request, principal: CurrentUser) -> Response:
        service.logout(principal, request.state.request_id)
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='strict', path='/')
        return response

    @router.get('/me', response_model=MeResponse)
    def me(principal: CurrentUser) -> MeResponse:
        with database.transaction() as session:
            user = active_user(session, principal.user_id)
            workspaces = []
            for workspace in session.scalars(select(Workspace).order_by(Workspace.id)):
                membership = session.get(WorkspaceMember, (workspace.id, user.id))
                if user.is_system_admin or membership:
                    workspaces.append(WorkspaceView(id=workspace.id, name=workspace.name,
                        role='admin' if user.is_system_admin else membership.role))
            kbs = []
            for kb in session.scalars(select(KnowledgeBase).where(KnowledgeBase.deleted_at.is_(None)).order_by(KnowledgeBase.id)):
                try:
                    kb_access(session, user.id, kb.id)
                except DomainError as exc:
                    if exc.status == 404:
                        continue
                    raise
                kbs.append(KBView(id=kb.id, workspace_id=kb.workspace_id, name=kb.name, slug=kb.slug,
                    role=kb_role(session, user.id, kb.id), active_index_generation_id=kb.active_index_generation_id, data_epoch=kb.data_epoch))
            return MeResponse(user=UserView(id=user.id, email=user.email, display_name=user.display_name,
                is_system_admin=user.is_system_admin), workspaces=workspaces, knowledge_bases=kbs)

    @router.post('/users', status_code=201, response_model=IdentifierResponse)
    def create_user(body: CreateUserBody, request: Request, principal: CurrentUser) -> IdentifierResponse:
        return IdentifierResponse(id=service.create_user(principal.user_id, body.email, body.password.get_secret_value(), body.display_name, request.state.request_id))

    @router.patch('/users/{user_id}', status_code=204)
    def change_user(user_id: UUID, body: ChangeUserBody, request: Request, principal: CurrentUser) -> Response:
        service.change_user(principal.user_id, user_id, request.state.request_id,
                            password=body.password.get_secret_value() if body.password else None, disabled=body.disabled)
        return Response(status_code=204)

    @router.put('/workspaces/{workspace_id}/members/{user_id}', status_code=204)
    def workspace_member(workspace_id: UUID, user_id: UUID, body: MemberBody, request: Request, principal: CurrentUser) -> Response:
        service.set_workspace_member(principal.user_id, workspace_id, user_id, body.role, request.state.request_id)
        return Response(status_code=204)

    @router.delete('/workspaces/{workspace_id}/members/{user_id}', status_code=204)
    def remove_workspace_member(workspace_id: UUID, user_id: UUID, request: Request, principal: CurrentUser) -> Response:
        service.set_workspace_member(principal.user_id, workspace_id, user_id, None, request.state.request_id)
        return Response(status_code=204)

    @router.post('/workspaces/{workspace_id}/knowledge-bases', status_code=201, response_model=IdentifierResponse)
    def create_kb(workspace_id: UUID, body: CreateKBBody, request: Request, principal: CurrentUser) -> IdentifierResponse:
        from app.audit.service import audit
        with database.transaction() as session:
            workspace_access(session, principal.user_id, workspace_id, admin=True)
            kb = KnowledgeBase(workspace_id=workspace_id, name=body.name, slug=body.slug)
            session.add(kb)
            session.flush()
            audit(session, 'kb.create', actor=principal.user_id, request_id=request.state.request_id,
                  workspace_id=workspace_id, kb_id=kb.id, resource_type='kb', resource_id=str(kb.id))
            return IdentifierResponse(id=kb.id)

    @router.get('/knowledge-bases/{kb_id}', response_model=KBView)
    def get_kb(kb_id: UUID, principal: CurrentUser) -> KBView:
        with database.transaction() as session:
            kb = kb_access(session, principal.user_id, kb_id)
            return KBView(id=kb.id, workspace_id=kb.workspace_id, name=kb.name, slug=kb.slug,
                role=kb_role(session, principal.user_id, kb.id), active_index_generation_id=kb.active_index_generation_id, data_epoch=kb.data_epoch)

    @router.put('/knowledge-bases/{kb_id}/members/{user_id}', status_code=204)
    def kb_member(kb_id: UUID, user_id: UUID, body: MemberBody, request: Request, principal: CurrentUser) -> Response:
        service.set_kb_member(principal.user_id, kb_id, user_id, body.role, request.state.request_id)
        return Response(status_code=204)

    @router.delete('/knowledge-bases/{kb_id}/members/{user_id}', status_code=204)
    def remove_kb_member(kb_id: UUID, user_id: UUID, request: Request, principal: CurrentUser) -> Response:
        service.set_kb_member(principal.user_id, kb_id, user_id, None, request.state.request_id)
        return Response(status_code=204)

    @router.post('/knowledge-bases/{kb_id}/documents', status_code=202, response_model=UploadResponse)
    def upload(kb_id: UUID, request: Request, principal: CurrentUser, file: UploadFile = File(...),
               display_name: str | None = Form(default=None)) -> UploadResponse:
        try:
            with database.transaction() as session:
                kb_access(session, principal.user_id, kb_id, 'editor')
            data = file.file.read(MAX_BYTES + 1)
            result = documents.upload(principal.user_id, kb_id, display_name or file.filename or '', data,
                request.headers.get('Idempotency-Key', ''), request.state.request_id)
            return UploadResponse.model_validate(result)
        finally:
            file.file.close()

    @router.get('/document-revisions/{revision_id}/source', response_model=SourceResponse)
    def source(revision_id: UUID, principal: CurrentUser, page: int = 1) -> SourceResponse:
        from app.services.source_service import SourceService
        result = SourceService(database).read(principal.user_id, revision_id, page)
        return SourceResponse(revision_id=result.revision_id, filename=result.filename, page=result.page, text=result.text)

    @router.delete('/documents/{document_id}', response_model=DeletionView)
    def delete_document(document_id: UUID, request: Request, principal: CurrentUser) -> DeletionView:
        return DeletionView.model_validate(DeletionService(database).delete(principal.user_id, document_id, request.state.request_id))

    @router.post('/documents/{document_id}/restore', response_model=RestoreView)
    def restore_document(document_id: UUID, request: Request, principal: CurrentUser) -> RestoreView:
        return RestoreView.model_validate(DeletionService(database).restore(principal.user_id, document_id, request.state.request_id))

    @router.get('/jobs/{job_id}', response_model=JobView)
    def get_job(job_id: UUID, principal: CurrentUser) -> JobView:
        with database.transaction() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise DomainError('NOT_FOUND', 404)
            kb_access(session, principal.user_id, job.kb_id,
                      'admin' if job.type == 'query' and job.created_by != principal.user_id else 'viewer')
            return JobView(id=job.id, type=job.type, state=job.state, attempt=job.attempt_count,
                           cancel_requested=job.cancel_requested_at is not None, error_code=job.error_code)

    @router.post('/jobs/{job_id}/cancel', response_model=JobView)
    def cancel_job(job_id: UUID, request: Request, principal: CurrentUser) -> JobView:
        def authorize(session, job):
            access = ('viewer' if job.created_by == principal.user_id else 'admin') if job.type == 'query' else 'editor'
            if job.type in {'gc', 'eval'}:
                access = 'admin'
            kb_access(session, principal.user_id, job.kb_id, access)
        JobRepository(database).cancel(job_id, authorize)
        return get_job(job_id, principal)

    return router
