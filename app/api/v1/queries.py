from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, JsonValue

from app.api.dependencies import authentication
from app.api.schemas import QueryRequest, QueryResponse
from app.db.session import Database
from app.services.auth_service import AuthService, Principal
from app.services.query_service import QueryService


class TraceView(BaseModel):
    query_id: UUID
    status: str
    details: dict[str, JsonValue]


def build_query_router(database: Database, auth: AuthService) -> APIRouter:
    router = APIRouter(prefix='/api/v1')
    service = QueryService(database)
    CurrentUser = Annotated[Principal, Depends(authentication(auth))]

    @router.post('/queries', response_model=QueryResponse, status_code=202)
    def query(body: QueryRequest, request: Request, principal: CurrentUser) -> QueryResponse:
        return service.create(principal.user_id, body, request.headers.get('Idempotency-Key', ''), request.state.request_id)

    @router.get('/queries/{query_id}', response_model=QueryResponse)
    def result(query_id: UUID, principal: CurrentUser) -> QueryResponse:
        return service.read(principal.user_id, query_id)

    @router.get('/queries/{query_id}/trace', response_model=TraceView)
    def trace(query_id: UUID, principal: CurrentUser) -> TraceView:
        return TraceView.model_validate(service.trace(principal.user_id, query_id))

    @router.get('/queries/{query_id}/export.md', response_class=Response)
    def export(query_id: UUID, request: Request, principal: CurrentUser) -> Response:
        content = service.export(principal.user_id, query_id, request.state.request_id)
        return Response(content, media_type='text/markdown; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="tracedesk-query.md"'})

    return router
