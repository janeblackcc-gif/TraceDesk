from __future__ import annotations

import hashlib
import io
import unicodedata
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.audit.service import audit
from app.authz.policy import kb_access
from app.db.models import (ApiReceipt, DocumentRevision, FileObject, Job, JsonValue, LogicalDocument)
from app.db.session import Database
from app.ingest import MAX_BYTES
from app.jobs.repository import enqueue, payload_hash
from app.storage.object_store import ObjectStore
from .errors import DomainError


def validate_name(name: str) -> str:
    name = unicodedata.normalize('NFC', name).strip()
    if (not name or len(name) > 180 or name.endswith(('.', ' ')) or
            any(character in name for character in '\\/:*?"<>|') or any(ord(character) < 32 for character in name)):
        raise DomainError('INVALID_FILENAME', 422)
    if Path(name).stem.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(i) for i in range(1, 10)), *('LPT' + str(i) for i in range(1, 10))}:
        raise DomainError('INVALID_FILENAME', 422)
    if Path(name).suffix.lower() not in {'.md', '.txt', '.pdf'}:
        raise DomainError('UNSUPPORTED_FILE_TYPE', 422)
    return name


class DocumentService:
    def __init__(self, database: Database, objects: ObjectStore):
        self.database, self.objects = database, objects

    def upload(self, actor: UUID, kb_id: UUID, name: str, data: bytes, key: str, request_id: str) -> dict[str, JsonValue]:
        if not 1 <= len(key) <= 200:
            raise DomainError('IDEMPOTENCY_KEY_REQUIRED', 422)
        with self.database.transaction() as session:
            session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'), {'key': 'kb-mutation:' + str(kb_id)})
            kb = kb_access(session, actor, kb_id, 'editor')
            name = validate_name(name)
            if not data or len(data) > MAX_BYTES:
                raise DomainError('FILE_SIZE_LIMIT', 413)
            suffix = Path(name).suffix.lower()
            if suffix == '.pdf' and not data.startswith(b'%PDF-'):
                raise DomainError('INVALID_FILE_SIGNATURE', 422)
            checksum = hashlib.sha256(data).hexdigest()
            digest = payload_hash({'name': name, 'sha256': checksum})
            operation = 'upload:' + str(kb_id)
            receipt = session.get(ApiReceipt, (actor, operation, key))
            if receipt is not None:
                if receipt.payload_hash != digest:
                    raise DomainError('IDEMPOTENCY_CONFLICT', 409)
                return dict(receipt.result)
            document = session.scalar(select(LogicalDocument).where(LogicalDocument.kb_id == kb_id,
                LogicalDocument.normalized_key == name.casefold(), LogicalDocument.deleted_at.is_(None)))
            current = session.get(DocumentRevision, document.desired_revision_id) if document and document.desired_revision_id else None
            if current is not None and current.sha256 == checksum:
                job = session.scalar(select(Job).where(Job.revision_id == current.id, Job.type == 'parse').order_by(Job.created_at.desc()).limit(1))
                result = {'document_id': str(document.id), 'revision_id': str(current.id),
                          'job_id': str(job.id) if job else None, 'state': current.status, 'duplicate': True}
            else:
                session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                                {'key': 'object:' + checksum})
                storage_key = f'sha256/{checksum[:2]}/{checksum}'
                session.execute(insert(FileObject).values(sha256=checksum, size_bytes=len(data), storage_key=storage_key,
                    detected_type='application/pdf' if suffix == '.pdf' else 'text/plain', original_extension=suffix)
                    .on_conflict_do_nothing(index_elements=['sha256']))
                file_object = session.scalar(select(FileObject).where(FileObject.sha256 == checksum))
                if file_object is None or file_object.size_bytes != len(data) or file_object.storage_key != storage_key:
                    raise DomainError('OBJECT_METADATA_MISMATCH', 409)
                file_object.gc_pending_at = None
                if document is None:
                    document = LogicalDocument(kb_id=kb_id, display_name=name, normalized_key=name.casefold(), created_by=actor)
                    session.add(document)
                    session.flush()
                last = session.scalar(select(func.max(DocumentRevision.revision_no)).where(DocumentRevision.document_id == document.id)) or 0
                revision = DocumentRevision(document_id=document.id, revision_no=last + 1, file_object_id=file_object.id,
                    sha256=checksum, status='uploaded', parser_profile={'name': 'rc2-bounded', 'version': '2'}, created_by=actor)
                session.add(revision)
                session.flush()
                document.desired_revision_id = revision.id
                # Admission precedes file publication. A full queue leaves no object behind.
                job = enqueue(session, kind='parse', workspace_id=kb.workspace_id, kb_id=kb.id, created_by=actor,
                    key='parse:' + str(revision.id), revision_id=revision.id, payload={'revision_id': str(revision.id)})
                self.objects.put(io.BytesIO(data), max_bytes=MAX_BYTES)
                result = {'document_id': str(document.id), 'revision_id': str(revision.id),
                          'job_id': str(job.id), 'state': 'queued', 'duplicate': False}
            session.add(ApiReceipt(user_id=actor, operation=operation, key=key, payload_hash=digest, result=result))
            audit(session, 'document.upload', actor=actor, request_id=request_id, workspace_id=kb.workspace_id,
                  kb_id=kb.id, resource_type='document', resource_id=result['document_id'])
            return result
