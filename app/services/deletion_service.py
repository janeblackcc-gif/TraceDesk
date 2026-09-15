from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import func, select, text, update

from app.audit.service import audit
from app.authz.policy import kb_access
from app.db.models import (DocumentDeletion, DocumentRevision, Job, JsonValue, LegacyAlias,
                           LogicalDocument, ParseRun)
from app.db.session import Database
from app.jobs.repository import JobRepository, TERMINAL, enqueue
from .errors import DomainError

RETENTION = timedelta(days=7)


class RevisionState(BaseModel):
    status: str
    deleted_at: datetime | None


class DeletionSnapshot(BaseModel):
    active_revision_id: UUID | None
    revisions: dict[UUID, RevisionState]


class DeletionService:
    def __init__(self, database: Database):
        self.database = database

    def delete(self, actor: UUID, document_id: UUID, request_id: str) -> dict[str, JsonValue]:
        with self.database.transaction() as session:
            document = session.get(LogicalDocument, document_id)
            if document is None:
                raise DomainError('NOT_FOUND', 404)
            session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                            {'key': 'kb-mutation:' + str(document.kb_id)})
            kb = kb_access(session, actor, document.kb_id, 'editor')
            session.refresh(document, with_for_update=True)
            existing = session.scalar(select(DocumentDeletion).where(
                DocumentDeletion.document_id == document.id, DocumentDeletion.restored_at.is_(None)))
            if existing is not None:
                return self.view(existing)
            now = session.scalar(select(func.clock_timestamp()))
            revisions = session.scalars(select(DocumentRevision).where(DocumentRevision.document_id == document.id)).all()
            snapshot = DeletionSnapshot(active_revision_id=document.active_revision_id,
                revisions={row.id: RevisionState(status=row.status, deleted_at=row.deleted_at) for row in revisions})
            deletion = DocumentDeletion(document_id=document.id, deleted_by=actor, deleted_at=now,
                restore_before=now + RETENTION, snapshot=snapshot.model_dump(mode='json'))
            session.add(deletion)
            session.flush()
            document.deleted_at, document.active_revision_id = now, None
            kb.data_epoch += 1
            revision_ids = [row.id for row in revisions]
            for row in revisions:
                row.status, row.deleted_at = 'deleted', row.deleted_at or now
            session.execute(update(LegacyAlias).where(LegacyAlias.revision_id.in_(revision_ids),
                LegacyAlias.deleted_at.is_(None)).values(deleted_at=now))
            # A KB-wide index/query/eval may hold this document in its snapshot.
            jobs = session.scalars(select(Job).where(Job.kb_id == kb.id, ~Job.state.in_(TERMINAL),
                (Job.revision_id.in_(revision_ids)) | Job.type.in_(['index', 'reindex', 'query', 'eval']))
                .order_by(Job.id).with_for_update()).all()
            for job in jobs:
                job.cancel_requested_at = job.cancel_requested_at or now
                if job.state in {'queued', 'retry_wait'}:
                    JobRepository._terminal(session, job, 'cancelled', 'DOCUMENT_DELETED')
            gc = enqueue(session, kind='gc', workspace_id=kb.workspace_id, kb_id=kb.id, created_by=actor,
                key='gc:' + str(deletion.id), payload={'deletion_id': str(deletion.id)})
            gc.available_at = deletion.restore_before
            audit(session, 'document.delete', actor=actor, request_id=request_id, workspace_id=kb.workspace_id,
                  kb_id=kb.id, resource_type='document', resource_id=str(document.id))
            return self.view(deletion)

    def restore(self, actor: UUID, document_id: UUID, request_id: str) -> dict[str, JsonValue]:
        with self.database.transaction() as session:
            document = session.get(LogicalDocument, document_id)
            if document is None:
                raise DomainError('NOT_FOUND', 404)
            session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                            {'key': 'kb-mutation:' + str(document.kb_id)})
            kb = kb_access(session, actor, document.kb_id, 'admin')
            session.refresh(document, with_for_update=True)
            if document.deleted_at is None:
                return {'document_id': str(document.id), 'state': 'restored'}
            deletion = session.scalar(select(DocumentDeletion).where(DocumentDeletion.document_id == document.id,
                DocumentDeletion.restored_at.is_(None)).with_for_update())
            now = session.scalar(select(func.clock_timestamp()))
            if deletion is None or deletion.purged_at is not None or now >= deletion.restore_before:
                raise DomainError('RESTORE_WINDOW_EXPIRED', 410)
            conflict = session.scalar(select(LogicalDocument.id).where(LogicalDocument.kb_id == kb.id,
                LogicalDocument.normalized_key == document.normalized_key, LogicalDocument.deleted_at.is_(None)))
            if conflict is not None:
                raise DomainError('DOCUMENT_NAME_CONFLICT', 409)
            snapshot = DeletionSnapshot.model_validate(deletion.snapshot)
            for identifier, state in snapshot.revisions.items():
                revision = session.get(DocumentRevision, identifier)
                if revision is None or revision.purged_at is not None:
                    raise DomainError('RESTORE_DATA_UNAVAILABLE', 410)
                revision.status, revision.deleted_at = state.status, state.deleted_at
            document.deleted_at, document.active_revision_id = None, snapshot.active_revision_id
            deletion.restored_at = now
            kb.data_epoch += 1
            session.execute(update(LegacyAlias).where(LegacyAlias.revision_id.in_(snapshot.revisions),
                LegacyAlias.deleted_at == deletion.deleted_at).values(deleted_at=None))
            jobs = session.scalars(select(Job).where(Job.type == 'gc',
                Job.idempotency_key == 'gc:' + str(deletion.id)).with_for_update()).all()
            for job in jobs:
                job.cancel_requested_at = now
                if job.state not in TERMINAL:
                    JobRepository._terminal(session, job, 'cancelled', 'DOCUMENT_RESTORED')
            desired = session.get(DocumentRevision, document.desired_revision_id) if document.desired_revision_id else None
            if desired is not None and desired.status in {'uploaded', 'parsing', 'indexing'}:
                parsed = session.scalar(select(ParseRun.id).where(ParseRun.revision_id == desired.id, ParseRun.status == 'succeeded'))
                desired.status = 'parsed' if parsed else 'uploaded'
                if not parsed:
                    enqueue(session, kind='parse', workspace_id=kb.workspace_id, kb_id=kb.id, created_by=actor,
                        revision_id=desired.id, key='restore-parse:' + str(deletion.id), payload={'revision_id': str(desired.id)})
            audit(session, 'document.restore', actor=actor, request_id=request_id, workspace_id=kb.workspace_id,
                  kb_id=kb.id, resource_type='document', resource_id=str(document.id))
            return {'document_id': str(document.id), 'state': 'restored'}

    @staticmethod
    def view(deletion: DocumentDeletion) -> dict[str, JsonValue]:
        return {'document_id': str(deletion.document_id), 'deletion_id': str(deletion.id),
                'state': 'purged' if deletion.purged_at else 'deleted',
                'restore_before': deletion.restore_before.isoformat()}
