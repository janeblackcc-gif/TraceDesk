from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


@dataclass(frozen=True)
class LegacyChunk:
    id: str
    doc_id: str
    ordinal: int
    page: int
    start_line: int
    end_line: int
    heading: str
    text: str


@dataclass(frozen=True)
class LegacyDocument:
    id: str
    filename: str
    collection: str
    version: str
    sha256: str
    status: str
    pages: tuple[str, ...]
    created_at: float
    chunks: tuple[LegacyChunk, ...]


@dataclass(frozen=True)
class LegacySnapshot:
    source_hash: str
    documents: tuple[LegacyDocument, ...]
    report: dict


class LegacyValidationError(ValueError):
    def __init__(self, report: dict):
        super().__init__('LEGACY_VALIDATION_FAILED; inspect the migration plan')
        self.report = report


def inspect_legacy(path: Path) -> LegacySnapshot:
    path = path.resolve(strict=True)
    before = file_hash(path)
    report = {'source_db_sha256': before, 'source_counts': {}, 'issues': [],
              'legacy_id_collisions': [], 'vector_profiles': [], 'planned_kbs': [],
              'reindex_required': True, 'source_unchanged': None, 'documents': [],
              'archive_policy': 'Conversations and traces remain in the restricted source backup; not assigned to users.'}
    issues = report['issues']

    def issue(code: str, identifier: str | None = None, *, warning: bool = False) -> None:
        item = {'code': code, 'severity': 'warning' if warning else 'error'}
        if identifier is not None:
            item['resource_id'] = identifier
        issues.append(item)

    # immutable=1 avoids writing a WAL/SHM sidecar, but is safe only on a frozen,
    # checkpointed database. Refuse potentially uncheckpointed sidecars.
    for suffix in ('-wal', '-journal'):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            issue('SOURCE_REQUIRES_OFFLINE_CHECKPOINT')
            raise LegacyValidationError(report)
    documents: list[LegacyDocument] = []
    try:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA query_only=ON')
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                issue('SQLITE_INTEGRITY_ERROR')
            expected = {'collections', 'documents', 'chunks', 'vectors', 'conversations', 'traces'}
            actual = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not expected <= actual:
                issue('MISSING_TABLES')
                raise LegacyValidationError(report)
            report['source_counts'] = {table: connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in sorted(expected)}
            collection_names = {row['name'] for row in connection.execute('SELECT * FROM collections')}
            doc_rows = connection.execute('SELECT * FROM documents ORDER BY id').fetchall()
            doc_ids = {row['id'] for row in doc_rows}
            chunk_rows = connection.execute('SELECT * FROM chunks ORDER BY id').fetchall()
            chunk_ids = {row['id'] for row in chunk_rows}
            if len(doc_ids) != len(doc_rows) or len(chunk_ids) != len(chunk_rows):
                issue('LEGACY_ID_COLLISION')
                report['legacy_id_collisions'] = ['duplicate primary identifiers']
            for row in chunk_rows:
                if row['doc_id'] not in doc_ids:
                    issue('ORPHAN_CHUNK', row['id'])
            chunks_by_doc: dict[str, list[sqlite3.Row]] = {}
            for row in chunk_rows:
                chunks_by_doc.setdefault(row['doc_id'], []).append(row)
            names: set[tuple[str, str, str]] = set()
            for row in doc_rows:
                identifier = row['id']
                if row['collection'] not in collection_names:
                    issue('ORPHAN_DOCUMENT', identifier)
                if not re.fullmatch('[0-9a-f]{32}', identifier) or not re.fullmatch('[0-9a-f]{64}', row['sha256']):
                    issue('INVALID_DOCUMENT_ID_OR_HASH', identifier)
                if row['status'] not in {'ready', 'quarantined'}:
                    issue('UNKNOWN_DOCUMENT_STATUS', identifier)
                if any(not isinstance(row[key], str) or not row[key].strip() or '\x00' in row[key]
                       for key in ('filename', 'collection', 'version')):
                    issue('INVALID_SCOPE_OR_NAME', identifier)
                    continue
                normalized = unicodedata.normalize('NFC', row['filename']).casefold()
                name_key = (row['collection'], row['version'], normalized)
                if name_key in names:
                    issue('NORMALIZED_FILENAME_COLLISION', identifier)
                names.add(name_key)
                if len(row['filename']) > 255 or max(len(row['collection']), len(row['version'])) > 60:
                    issue('SCOPE_OR_NAME_TOO_LONG', identifier)
                try:
                    pages = json.loads(row['pages'])
                    if not isinstance(pages, list) or not pages or any(not isinstance(page, str) or '\x00' in page for page in pages):
                        raise ValueError
                except (ValueError, TypeError):
                    issue('INVALID_PAGES_JSON', identifier)
                    continue
                chunk_items = []
                for part in chunks_by_doc.get(identifier, []):
                    try:
                        prefix, ordinal = part['id'].rsplit(':', 1)
                        ordinal = int(ordinal)
                        if prefix != identifier or ordinal < 0:
                            raise ValueError
                        page, start, end = part['page'], part['start_line'], part['end_line']
                        if not (type(page) is int and type(start) is int and type(end) is int and
                                1 <= page <= len(pages) and 1 <= start <= end <= len(pages[page - 1].split('\n'))):
                            raise ValueError
                        expected_text = '\n'.join(pages[page - 1].split('\n')[start - 1:end]).strip()
                        if part['text'] != expected_text or not expected_text:
                            raise ValueError
                        chunk_items.append(LegacyChunk(part['id'], identifier, ordinal, page, start, end, part['heading'], part['text']))
                    except (ValueError, TypeError, AttributeError):
                        issue('INVALID_CHUNK_POSITION_OR_TEXT', part['id'])
                if sorted(chunk.ordinal for chunk in chunk_items) != list(range(len(chunk_items))) or not chunk_items:
                    issue('INVALID_CHUNK_SEQUENCE', identifier)
                try:
                    created_at = float(row['created_at'])
                    if not math.isfinite(created_at) or not 0 <= created_at <= 253402300799:
                        raise ValueError
                except (ValueError, TypeError):
                    issue('INVALID_TIMESTAMP', identifier)
                    continue
                documents.append(LegacyDocument(identifier, row['filename'], row['collection'], row['version'], row['sha256'],
                    row['status'], tuple(pages), created_at, tuple(sorted(chunk_items, key=lambda chunk: chunk.ordinal))))
                report['documents'].append({'legacy_doc_id': identifier, 'source_sha256': row['sha256'],
                    'page_hashes': [sha256(page.encode()) for page in pages],
                    'chunks': [{'legacy_chunk_id': chunk.id, 'text_sha256': sha256(chunk.text.encode())} for chunk in chunk_items]})
                issue('MISSING_ORIGINAL_FILE', identifier, warning=True)
            profiles: dict[tuple[str, int], int] = {}
            for row in connection.execute('SELECT * FROM vectors'):
                if row['chunk_id'] not in chunk_ids:
                    issue('ORPHAN_VECTOR', row['chunk_id'])
                try:
                    vector = json.loads(row['value'])
                    if (not isinstance(vector, list) or not vector or
                            any(type(value) not in {int, float} or not math.isfinite(value) for value in vector)):
                        raise ValueError
                    key = (row['model_key'], len(vector))
                    profiles[key] = profiles.get(key, 0) + 1
                except (ValueError, TypeError):
                    issue('INVALID_VECTOR', row['chunk_id'])
            report['vector_profiles'] = [{'model_key': key, 'dimension': dimension, 'count': count} for (key, dimension), count in sorted(profiles.items())]
            report['planned_kbs'] = [{'collection': collection, 'version': version} for collection, version in
                                     sorted({(doc.collection, doc.version) for doc in documents})]
            for table in ('conversations', 'traces'):
                if report['source_counts'][table]:
                    issue('RESTRICTED_LEGACY_ARCHIVE_REQUIRED', warning=True)
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        issue('SQLITE_READ_ERROR')
    report['source_unchanged'] = file_hash(path) == before
    if not report['source_unchanged']:
        issue('SOURCE_CHANGED_DURING_SCAN')
    if any(item['severity'] == 'error' for item in issues):
        raise LegacyValidationError(report)
    return LegacySnapshot(before, tuple(documents), report)
