"""Authenticated read adapters for RC2; writes require the async-capable client."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select

from app.api.dependencies import authentication
from app.authz.policy import kb_access
from app.db.models import Chunk, DocumentRevision, KnowledgeBase, LegacyAlias, LogicalDocument
from app.db.session import Database
from app.services.auth_service import AuthService, Principal
from app.services.errors import DomainError
from app.services.query_service import QueryService
from app.services.source_service import SourceService


def build_legacy_router(database: Database, auth: AuthService) -> APIRouter:
    router = APIRouter(prefix='/api')
    CurrentUser = Annotated[Principal, Depends(authentication(auth))]

    def deprecated(response: Response) -> None:
        response.headers['Deprecation'] = 'true'
        response.headers['Link'] = '</api/v1>; rel="successor-version"'

    @router.get('/library')
    def library(response: Response, principal: CurrentUser) -> dict:
        deprecated(response)
        documents, collections = [], []
        with database.transaction() as session:
            for kb in session.scalars(select(KnowledgeBase).where(KnowledgeBase.deleted_at.is_(None)).order_by(KnowledgeBase.id)):
                try:
                    kb_access(session, principal.user_id, kb.id)
                except DomainError as exc:
                    if exc.status == 404:
                        continue
                    raise
                rows = session.execute(select(LogicalDocument, DocumentRevision).join(DocumentRevision,
                    DocumentRevision.id == func.coalesce(LogicalDocument.active_revision_id, LogicalDocument.desired_revision_id))
                    .where(LogicalDocument.kb_id == kb.id, LogicalDocument.deleted_at.is_(None)).limit(201)).all()
                if len(rows) > 200:
                    raise DomainError('CLIENT_UPGRADE_REQUIRED', 409)
                versions = set()
                for document, revision in rows:
                    version = revision.source_version_label or str(revision.revision_no)
                    versions.add(version)
                    documents.append({'id': revision.legacy_doc_id or str(revision.id), 'filename': document.display_name,
                        'collection': kb.name, 'version': version, 'status': revision.status, 'sha256': revision.sha256,
                        'created_at': revision.created_at.timestamp(), 'chunks': session.scalar(select(func.count()).select_from(Chunk)
                            .where(Chunk.revision_id == revision.id))})
                collections.append({'name': kb.name, 'active_version': sorted(versions)[-1] if versions else '', 'versions': sorted(versions)})
        return {'documents': documents, 'collections': collections}

    @router.get('/sources/{doc_id}')
    def source(doc_id: str, response: Response, principal: CurrentUser, page: int = 1) -> dict:
        deprecated(response)
        with database.transaction() as session:
            alias = session.get(LegacyAlias, ('document', doc_id))
            if alias:
                kb_access(session, principal.user_id, alias.kb_id)
                if alias.deleted_at:
                    raise DomainError('SOURCE_GONE', 410)
                identifier = alias.revision_id
            else:
                try:
                    identifier = UUID(doc_id)
                except ValueError:
                    raise DomainError('NOT_FOUND', 404) from None
        result = SourceService(database).read(principal.user_id, identifier, page)
        return {'filename': result.filename, 'collection': result.collection, 'version': result.version,
                'page': result.page, 'lines': result.text.split('\n')}

    @router.get('/traces/{trace_id}')
    def trace(trace_id: UUID, response: Response, principal: CurrentUser) -> dict:
        deprecated(response)
        return QueryService(database).trace(principal.user_id, trace_id)

    def upgrade(request: Request, principal: CurrentUser) -> Response:
        raise DomainError('CLIENT_UPGRADE_REQUIRED', 409)

    for route, methods in [('/ask', ['POST']), ('/index', ['POST']), ('/documents', ['POST']),
                           ('/documents/{doc_id}', ['DELETE']), ('/active-version', ['POST']),
                           ('/demo', ['POST']), ('/evaluate', ['POST'])]:
        router.add_api_route(route, upgrade, methods=methods)
    return router
