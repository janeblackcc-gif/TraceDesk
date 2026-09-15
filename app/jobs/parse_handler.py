from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import Chunk, DocumentRevision, FileObject, Job, KnowledgeBase, ParsedPage, ParseRun
from app.db.session import Database
from app.ingest import SUSPICIOUS
from app.parsing.worker import DockerParser, ParseResult
from app.services.errors import DomainError
from app.storage.object_store import ObjectStore, StoredObject
from .repository import JobRepository, Lease


class ParseHandler:
    def __init__(self, database: Database, objects: ObjectStore, parser: DockerParser):
        self.database, self.objects, self.parser = database, objects, parser

    def run(self, queue: JobRepository, lease: Lease) -> str:
        from app.db.models import LogicalDocument
        with self.database.transaction() as session:
            revision = session.get(DocumentRevision, lease.revision_id)
            if revision is None or not revision.source_file_available:
                return queue.fail(lease, 'ORIGINAL_FILE_UNAVAILABLE')
            document = session.get(LogicalDocument, revision.document_id)
            file = session.get(FileObject, revision.file_object_id)
            if document is None or file is None:
                return queue.fail(lease, 'SOURCE_UNAVAILABLE')
            name, object_key = document.display_name, file.storage_key
            item = StoredObject(file.sha256, file.size_bytes, object_key)
        cancel = threading.Event()
        finished = threading.Event()

        def heartbeat() -> None:
            while not finished.wait(min(10, queue.lease_seconds / 3)):
                try:
                    if not queue.heartbeat(lease):
                        cancel.set()
                        return
                except Exception:
                    cancel.set()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            self.objects.verify(item)
            result = self.parser.parse(self.objects.path_for(object_key), name, cancel)
            return queue.finish(lease, lambda session, job: self.persist(session, job, result))
        except DomainError as exc:
            return queue.fail(lease, exc.code, retryable=exc.retryable, effect=self.record_failure)
        except (OSError, ValueError):
            return queue.fail(lease, 'SOURCE_OBJECT_UNAVAILABLE', effect=self.record_failure)
        finally:
            finished.set()
            thread.join(timeout=15)

    @staticmethod
    def record_failure(session: Session, job: Job) -> None:
        from app.db.models import LogicalDocument
        revision = session.get(DocumentRevision, job.revision_id)
        document = session.get(LogicalDocument, revision.document_id) if revision else None
        if revision is not None and document is not None and document.desired_revision_id == revision.id and document.deleted_at is None:
            if job.state in {'failed', 'cancelled'}:
                revision.status = 'failed'
            session.add(ParseRun(revision_id=revision.id, parser_name='rc2-bounded', parser_version='2-unknown',
                config_hash=hashlib.sha256(b'rc2-bounded-container-v2').hexdigest(),
                status='cancelled' if job.state == 'cancelled' else 'failed', finished_at=datetime.now(timezone.utc),
                error_code=job.error_code or 'PARSE_CANCELLED'))

    @staticmethod
    def persist(session: Session, job: Job, result: ParseResult) -> dict:
        revision = session.get(DocumentRevision, job.revision_id)
        quarantined = any(SUSPICIOUS.search(page) for page in result.pages)
        profile = 'rc2-bounded-container-v2/pypdf-' + result.parser_version
        run = ParseRun(revision_id=revision.id, parser_name='rc2-bounded', parser_version='2/' + result.parser_version,
            config_hash=hashlib.sha256(profile.encode()).hexdigest(), status='succeeded', finished_at=datetime.now(timezone.utc))
        session.add(run)
        session.flush()
        for page_no, page in enumerate(result.pages, 1):
            session.add(ParsedPage(parse_run_id=run.id, page_no=page_no, text=page, text_sha256=hashlib.sha256(page.encode()).hexdigest()))
        for ordinal, chunk in enumerate(result.chunks):
            session.add(Chunk(parse_run_id=run.id, revision_id=revision.id, ordinal=ordinal, page_start=chunk.page,
                page_end=chunk.page, line_start=chunk.start_line, line_end=chunk.end_line, heading=chunk.heading,
                text=chunk.text, text_sha256=hashlib.sha256(chunk.text.encode()).hexdigest(),
                search_tsv=func.to_tsvector('simple', f'{result.filename} {chunk.heading} {chunk.text}')))
        revision.status = 'quarantined' if quarantined else 'parsed'
        session.get(KnowledgeBase, job.kb_id).data_epoch += 1
        session.flush()
        from app.services.index_service import IndexService
        IndexService.after_parse(session, job)
        return {'parse_run_id': str(run.id), 'pages': len(result.pages), 'chunks': len(result.chunks), 'status': revision.status}
