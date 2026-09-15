from __future__ import annotations

import io
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.db.models import (Chunk, DocumentRevision, FileObject, Job, KnowledgeBase, LegacyAlias,
    LogicalDocument, MigrationCheckpoint, ParsedPage, ParseRun, User, Workspace, WorkspaceMember)
from app.db.session import Database
from app.storage.object_store import ObjectStore
from .legacy import LegacySnapshot, sha256


@dataclass(frozen=True)
class ImportResult:
    imported: int
    already_present: int
    remaining: int
    source_db_hash: str


def import_snapshot(database: Database, objects: ObjectStore, snapshot: LegacySnapshot,
                    admin_id: UUID, *, max_documents: int | None = None) -> ImportResult:
    if max_documents is not None and max_documents < 1:
        raise ValueError('max_documents must be positive')
    if not database.readiness().ready:
        raise ValueError('DATABASE_NOT_READY')
    if not snapshot.report['source_unchanged'] or any(issue['severity'] == 'error' for issue in snapshot.report['issues']):
        raise ValueError('LEGACY_VALIDATION_FAILED')
    with database.transaction() as session:
        admin = session.get(User, admin_id)
        if admin is None or admin.status != 'active' or not admin.is_system_admin:
            raise ValueError('MIGRATION_REQUIRES_EXISTING_ACTIVE_SYSTEM_ADMIN')
    imported = present = 0
    # A short transaction per document is both checkpoint and serialization unit.
    for document in snapshot.documents:
        if max_documents is not None and imported >= max_documents:
            break
        with database.transaction() as session:
            session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                            {'key': 'legacy-import:' + snapshot.source_hash})
            admin = session.get(User, admin_id, with_for_update=True)
            if admin is None or admin.status != 'active' or not admin.is_system_admin:
                raise ValueError('MIGRATION_ADMIN_REVOKED')
            checkpoint = session.get(MigrationCheckpoint, (snapshot.source_hash, document.id))
            if checkpoint is not None:
                revision = session.get(DocumentRevision, checkpoint.revision_id)
                if revision is None or revision.sha256 != document.sha256:
                    raise ValueError('MIGRATION_CHECKPOINT_MISMATCH')
                stored = session.execute(select(Chunk.legacy_chunk_id, Chunk.text_sha256).where(Chunk.revision_id == revision.id)).all()
                if dict(stored) != {chunk.id: sha256(chunk.text.encode()) for chunk in document.chunks}:
                    raise ValueError('MIGRATION_TEXT_MISMATCH')
                present += 1
                continue
            workspace_key = 'legacy:' + snapshot.source_hash
            workspace = session.scalar(select(Workspace).where(Workspace.migration_key == workspace_key))
            if workspace is None:
                workspace = Workspace(name='Migrated Local Workspace', slug='migrated-' + snapshot.source_hash[:16], migration_key=workspace_key)
                session.add(workspace)
                session.flush()
                session.add(WorkspaceMember(workspace_id=workspace.id, user_id=admin_id, role='admin'))
            scope_digest = sha256(json.dumps([document.collection, document.version], ensure_ascii=False).encode())
            kb_key = snapshot.source_hash + ':' + scope_digest
            kb = session.scalar(select(KnowledgeBase).where(KnowledgeBase.migration_key == kb_key))
            if kb is None:
                kb = KnowledgeBase(workspace_id=workspace.id, name=f'{document.collection} / {document.version}',
                                   slug='legacy-' + scope_digest[:24], migration_key=kb_key)
                session.add(kb)
                session.flush()
            existing = session.scalar(select(DocumentRevision).where(DocumentRevision.legacy_doc_id == document.id))
            if existing is not None:
                raise ValueError('LEGACY_ID_ALREADY_IMPORTED_FROM_DIFFERENT_SNAPSHOT')
            # This is explicitly extracted text, never a reconstructed original PDF.
            raw_pages = json.dumps(list(document.pages), ensure_ascii=False, separators=(',', ':')).encode()
            item = objects.put(io.BytesIO(raw_pages), max_bytes=max(len(raw_pages), 1))
            session.execute(insert(FileObject).values(sha256=item.sha256, size_bytes=item.size_bytes,
                storage_key=item.storage_key, detected_type='application/vnd.tracedesk.legacy-pages+json',
                original_extension='.json').on_conflict_do_nothing(index_elements=['sha256']))
            file_object = session.scalar(select(FileObject).where(FileObject.sha256 == item.sha256))
            if file_object is None or file_object.size_bytes != item.size_bytes or file_object.storage_key != item.storage_key:
                raise ValueError('FILE_OBJECT_METADATA_MISMATCH')
            logical = LogicalDocument(kb_id=kb.id, display_name=document.filename,
                normalized_key=unicodedata.normalize('NFC', document.filename).casefold(), created_by=admin_id)
            session.add(logical)
            session.flush()
            revision = DocumentRevision(document_id=logical.id, revision_no=1, file_object_id=file_object.id,
                sha256=document.sha256, status='quarantined' if document.status == 'quarantined' else 'parsed',
                parser_profile={'name': 'legacy_extracted_text', 'version': 'rc2'}, created_by=admin_id,
                created_at=datetime.fromtimestamp(document.created_at, timezone.utc),
                legacy_doc_id=document.id, migration_key=snapshot.source_hash + ':' + document.id,
                source_version_label=document.version, source_file_available=False)
            session.add(revision)
            session.flush()
            logical.desired_revision_id = revision.id
            parse_run = ParseRun(revision_id=revision.id, parser_name='legacy_extracted_text', parser_version='rc2',
                config_hash=sha256(b'legacy-extracted-text-v1'), status='succeeded', finished_at=datetime.now(timezone.utc))
            session.add(parse_run)
            session.flush()
            for page_number, page in enumerate(document.pages, 1):
                session.add(ParsedPage(parse_run_id=parse_run.id, page_no=page_number, text=page, text_sha256=sha256(page.encode())))
            session.add(LegacyAlias(kind='document', legacy_id=document.id, kb_id=kb.id,
                                    target_id=revision.id, revision_id=revision.id))
            for part in document.chunks:
                chunk = Chunk(parse_run_id=parse_run.id, revision_id=revision.id, ordinal=part.ordinal,
                    page_start=part.page, page_end=part.page, line_start=part.start_line, line_end=part.end_line,
                    heading=part.heading, text=part.text, text_sha256=sha256(part.text.encode()), legacy_chunk_id=part.id,
                    search_tsv=func.to_tsvector('simple', f'{document.filename} {part.heading} {part.text}'))
                session.add(chunk)
                session.flush()
                session.add(LegacyAlias(kind='chunk', legacy_id=part.id, kb_id=kb.id, target_id=chunk.id,
                                        revision_id=revision.id, text_sha256=chunk.text_sha256))
            if document.status != 'quarantined':
                payload = {'revision_id': str(revision.id), 'source': 'legacy_import'}
                session.add(Job(type='reindex', workspace_id=workspace.id, kb_id=kb.id, revision_id=revision.id,
                    idempotency_key=revision.migration_key, payload=payload,
                    payload_hash=sha256(json.dumps(payload, sort_keys=True).encode()), created_by=admin_id))
            session.add(MigrationCheckpoint(source_db_hash=snapshot.source_hash, legacy_doc_id=document.id, revision_id=revision.id))
        imported += 1
    return ImportResult(imported, present, len(snapshot.documents) - imported - present, snapshot.source_hash)
