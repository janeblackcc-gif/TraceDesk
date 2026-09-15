from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from prometheus_client import CONTENT_TYPE_LATEST
from sqlalchemy import select

from app.api.dependencies import authentication
from app.audit.service import audit
from app.authz.policy import active_user, workspace_access
from app.db.models import AuditEvent
from app.db.session import Database
from app.observability.metrics import Metrics
from app.services.auth_service import AuthService, Principal
from app.services.errors import DomainError


class AuditView(BaseModel):
    id: int
    occurred_at: datetime
    actor_user_id: UUID | None
    workspace_id: UUID | None
    kb_id: UUID | None
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    request_id: str


class AuditPage(BaseModel):
    items: list[AuditView]
    next_before_id: int | None


def build_operations_router(database: Database, auth: AuthService, metrics: Metrics) -> APIRouter:
    router = APIRouter(prefix='/api/v1')
    Current = Annotated[Principal, Depends(authentication(auth))]

    @router.get('/audit-events', response_model=AuditPage)
    def events(request: Request, principal: Current, workspace_id: UUID | None = None,
               before_id: Annotated[int | None, Query(ge=1)] = None,
               limit: Annotated[int, Query(ge=1, le=200)] = 50) -> AuditPage:
        with database.transaction() as session:
            if workspace_id is not None:
                workspace_access(session, principal.user_id, workspace_id, admin=True)
            elif not active_user(session, principal.user_id).is_system_admin:
                raise DomainError('FORBIDDEN', 403)
            statement = select(AuditEvent)
            if workspace_id is not None:
                statement = statement.where(AuditEvent.workspace_id == workspace_id)
            if before_id is not None:
                statement = statement.where(AuditEvent.id < before_id)
            rows = session.scalars(statement.order_by(AuditEvent.id.desc()).limit(limit + 1)).all()
            items = [AuditView.model_validate(row, from_attributes=True) for row in rows[:limit]]
            audit(session, 'audit.read', actor=principal.user_id, request_id=request.state.request_id,
                  workspace_id=workspace_id)
            return AuditPage(items=items, next_before_id=items[-1].id if len(rows) > limit else None)

    @router.get('/operations/metrics', response_class=Response)
    def operational_metrics(principal: Current) -> Response:
        with database.transaction() as session:
            if not active_user(session, principal.user_id).is_system_admin:
                raise DomainError('FORBIDDEN', 403)
        return Response(content=metrics.render(), media_type=CONTENT_TYPE_LATEST)

    return router
