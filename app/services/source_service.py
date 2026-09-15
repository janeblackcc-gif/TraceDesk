from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from app.authz.policy import kb_access
from app.db.models import DocumentRevision, GenerationRevision, LogicalDocument, ParsedPage, ParseRun
from app.db.session import Database
from .errors import DomainError


@dataclass(frozen=True)
class SourceContent:
    revision_id: UUID
    filename: str
    page: int
    text: str
    collection: str
    version: str


class SourceService:
    def __init__(self, database: Database):
        self.database = database

    def read(self, actor: UUID, revision_id: UUID, page: int) -> SourceContent:
        if page < 1:
            raise DomainError('INVALID_PAGE', 422)
        with self.database.transaction() as session:
            revision = session.get(DocumentRevision, revision_id)
            document = session.get(LogicalDocument, revision.document_id) if revision else None
            if document is None:
                raise DomainError('NOT_FOUND', 404)
            kb = kb_access(session, actor, document.kb_id)
            if (document.deleted_at is not None or revision.deleted_at is not None or
                    revision.status in {'deleted', 'superseded'} or
                    (document.active_revision_id is not None and document.active_revision_id != revision.id)):
                raise DomainError('SOURCE_GONE', 410)
            if revision.status == 'quarantined':
                raise DomainError('SOURCE_QUARANTINED', 409)
            membership = session.get(GenerationRevision, (kb.active_index_generation_id, revision.id)) if kb.active_index_generation_id else None
            run = session.get(ParseRun, membership.parse_run_id) if membership else session.scalar(select(ParseRun).where(
                ParseRun.revision_id == revision.id, ParseRun.status == 'succeeded').order_by(ParseRun.finished_at.desc(), ParseRun.id).limit(1))
            row = session.scalar(select(ParsedPage).where(ParsedPage.parse_run_id == run.id, ParsedPage.page_no == page)) if run else None
            if row is None:
                raise DomainError('SOURCE_NOT_READY', 404)
            return SourceContent(revision.id, document.display_name, page, row.text, kb.name,
                                 revision.source_version_label or str(revision.revision_no))
