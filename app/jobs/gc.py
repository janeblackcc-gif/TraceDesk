"""Two-phase collection: durable DB tombstones, then retryable OS recycling."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, func, select, text, update

from app.db.models import (Chunk, ChunkEmbedding, DocumentDeletion, DocumentRevision, FileObject,
                           LogicalDocument, ParsedPage, ParseRun, QueryRun, QuerySource, TraceRecord)
from app.db.session import Database
from app.services.errors import DomainError
from app.storage.object_store import ObjectStore
from .repository import JobRepository, Lease


class GarbageCollector:
    def __init__(self, database: Database, objects: ObjectStore):
        self.database, self.objects = database, objects

    def prepare(self, queue: JobRepository, lease: Lease, deletion_id: UUID) -> list[str] | None:
        with self.database.transaction() as session:
            session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                            {'key': 'kb-mutation:' + str(lease.kb_id)})
            job = queue._owned(session, lease)
            if job is None:
                return None
            if job.cancel_requested_at is not None:
                queue._terminal(session, job, 'cancelled')
                return None
            deletion = session.get(DocumentDeletion, deletion_id, with_for_update=True)
            document = session.get(LogicalDocument, deletion.document_id) if deletion else None
            if (deletion is None or document is None or document.kb_id != lease.kb_id or
                    deletion.restored_at is not None or document.deleted_at != deletion.deleted_at):
                queue._terminal(session, job, 'superseded')
                return None
            if deletion.purged_at is not None:
                keys = deletion.snapshot.get('object_keys')
                if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
                    raise DomainError('GC_RECORD_INVALID', 409)
                return [key for key in keys if isinstance(key, str)]
            now = session.scalar(select(func.clock_timestamp()))
            if now < deletion.restore_before:
                raise DomainError('GC_RETENTION_NOT_ELAPSED', 409)
            revisions = session.scalars(select(DocumentRevision).where(DocumentRevision.document_id == document.id)).all()
            identifiers = [row.id for row in revisions]
            file_ids = {row.file_object_id for row in revisions if row.file_object_id is not None}
            files = session.scalars(select(FileObject).where(FileObject.id.in_(file_ids)).order_by(FileObject.sha256)).all()
            keys = [file.storage_key for file in files]
            chunks = select(Chunk.id).where(Chunk.revision_id.in_(identifiers))
            query_ids = list(session.scalars(select(QuerySource.query_run_id).where(QuerySource.chunk_id.in_(chunks)).distinct()))
            # Purge only affected query payloads, retaining unrelated audit/history.
            if query_ids:
                session.execute(update(QueryRun).where(QueryRun.id.in_(query_ids)).values(response=None, status='superseded'))
                session.execute(delete(TraceRecord).where(TraceRecord.query_run_id.in_(query_ids)))
            session.execute(delete(QuerySource).where(QuerySource.chunk_id.in_(chunks)))
            session.execute(delete(ChunkEmbedding).where(ChunkEmbedding.revision_id.in_(identifiers)))
            session.execute(delete(Chunk).where(Chunk.revision_id.in_(identifiers)))
            runs = select(ParseRun.id).where(ParseRun.revision_id.in_(identifiers))
            session.execute(delete(ParsedPage).where(ParsedPage.parse_run_id.in_(runs)))
            session.execute(update(ParseRun).where(ParseRun.revision_id.in_(identifiers)).values(metrics={}))
            for revision in revisions:
                revision.file_object_id = None
                revision.source_file_available = False
                revision.purged_at = now
            session.flush()
            for file in files:
                session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                                {'key': 'object:' + file.sha256})
                if not session.scalar(select(DocumentRevision.id).where(DocumentRevision.file_object_id == file.id).limit(1)):
                    file.gc_pending_at = now
            deletion.purged_at = now
            deletion.snapshot = {'object_keys': keys}
            return keys

    def collect_object(self, key: str) -> None:
        # The digest lock is also held by upload. A concurrent reuse either keeps
        # this object alive or republishes bytes after collection commits.
        path = self.objects.path_for(key)
        with self.database.transaction() as session:
            session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                            {'key': 'object:' + path.name})
            file = session.scalar(select(FileObject).where(FileObject.storage_key == key).with_for_update())
            if file is None or file.gc_pending_at is None:
                return
            if session.scalar(select(DocumentRevision.id).where(DocumentRevision.file_object_id == file.id).limit(1)):
                file.gc_pending_at = None
                return
            self.objects.recycle(file.storage_key)
            session.delete(file)

    def run(self, queue: JobRepository, lease: Lease) -> str:
        identifier = lease.payload.get('deletion_id')
        if not isinstance(identifier, str):
            return queue.fail(lease, 'GC_RECORD_INVALID')
        try:
            deletion_id = UUID(identifier)
            keys = self.prepare(queue, lease, deletion_id)
            if keys is None:
                with self.database.transaction() as session:
                    from app.db.models import Job
                    job = session.get(Job, lease.job_id)
                    return job.state if job and job.state in {'cancelled', 'superseded'} else 'lease_lost'
            for key in keys:
                if not queue.heartbeat(lease):
                    return queue.finish(lease, lambda session, job: {})
                self.collect_object(key)
            return queue.finish(lease, lambda session, job: {'deletion_id': identifier, 'state': 'purged'})
        except DomainError as exc:
            return queue.fail(lease, exc.code, retryable=exc.retryable)
        except OSError:
            return queue.fail(lease, 'OBJECT_RECYCLE_FAILED', retryable=True)
        except ValueError:
            return queue.fail(lease, 'GC_RECORD_INVALID')
