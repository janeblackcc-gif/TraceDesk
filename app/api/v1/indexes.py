from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app.api.dependencies import authentication
from app.authz.policy import kb_access
from app.config import Settings
from app.db.models import ChunkEmbedding, IndexGeneration, Job, ModelProfile
from app.db.session import Database
from app.models.factory import create_bounded_provider
from app.services.auth_service import AuthService, Principal
from app.services.errors import DomainError
from app.services.index_service import IndexService


class IndexRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model_profile_id: UUID | None = None
    retrieval_profile: Literal['default-v1'] = 'default-v1'
    force_rebuild: bool = False


class IndexAccepted(BaseModel):
    generation_id: UUID
    job_id: UUID | None
    state: str


class IndexView(BaseModel):
    id: UUID
    kb_id: UUID
    status: str
    chunk_count: int
    embedded_count: int
    model_profile_id: UUID
    model_digest: str
    error_code: str | None


def build_index_router(database: Database, auth: AuthService, settings: Settings) -> APIRouter:
    router = APIRouter(prefix='/api/v1')
    service = IndexService(database)
    CurrentUser = Annotated[Principal, Depends(authentication(auth))]

    @router.post('/knowledge-bases/{kb_id}/index-generations', status_code=202, response_model=IndexAccepted)
    def create(kb_id: UUID, body: IndexRequest, request: Request, principal: CurrentUser) -> IndexAccepted:
        return IndexAccepted.model_validate(service.request(principal.user_id, kb_id,
            request.headers.get('Idempotency-Key', ''), request.state.request_id, create_bounded_provider(settings),
            profile_id=body.model_profile_id, force=body.force_rebuild))

    @router.get('/index-generations/{generation_id}', response_model=IndexView)
    def get(generation_id: UUID, principal: CurrentUser) -> IndexView:
        with database.transaction() as session:
            generation = session.get(IndexGeneration, generation_id)
            if generation is None:
                raise DomainError('NOT_FOUND', 404)
            kb_access(session, principal.user_id, generation.kb_id)
            profile = session.get(ModelProfile, generation.model_profile_id)
            job = session.scalar(select(Job).where(Job.generation_id == generation_id, Job.type.in_(['index', 'reindex']))
                .order_by(Job.created_at.desc()).limit(1))
            count = session.scalar(select(func.count()).select_from(ChunkEmbedding).where(ChunkEmbedding.generation_id == generation_id))
            return IndexView(id=generation.id, kb_id=generation.kb_id, status=generation.status,
                chunk_count=generation.chunk_count, embedded_count=count, model_profile_id=profile.id,
                model_digest=profile.model_digest, error_code=job.error_code if job else None)

    @router.post('/index-generations/{generation_id}/activate', status_code=204)
    def activate(generation_id: UUID, request: Request, principal: CurrentUser) -> Response:
        with database.transaction() as session:
            generation = session.get(IndexGeneration, generation_id)
            if generation is None:
                raise DomainError('NOT_FOUND', 404)
            kb_access(session, principal.user_id, generation.kb_id, 'admin')
            kb_id = generation.kb_id
        service.rollback(principal.user_id, kb_id, generation_id, request.state.request_id, require_current_manifest=True)
        return Response(status_code=204)

    @router.post('/index-generations/{generation_id}/rollback', status_code=204)
    def rollback(generation_id: UUID, request: Request, principal: CurrentUser) -> Response:
        with database.transaction() as session:
            generation = session.get(IndexGeneration, generation_id)
            if generation is None:
                raise DomainError('NOT_FOUND', 404)
            kb_access(session, principal.user_id, generation.kb_id, 'admin')
            kb_id = generation.kb_id
        service.rollback(principal.user_id, kb_id, generation_id, request.state.request_id)
        return Response(status_code=204)

    return router
