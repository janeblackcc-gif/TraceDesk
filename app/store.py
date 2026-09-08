from __future__ import annotations
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from .ingest import parse, chunks, SUSPICIOUS, InputError

class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS collections(name TEXT PRIMARY KEY, active_version TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS documents(
          id TEXT PRIMARY KEY, filename TEXT NOT NULL, collection TEXT NOT NULL REFERENCES collections(name),
          version TEXT NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL, pages TEXT NOT NULL,
          created_at REAL NOT NULL, UNIQUE(collection,version,filename));
        CREATE TABLE IF NOT EXISTS chunks(
          id TEXT PRIMARY KEY, doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
          page INTEGER NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
          heading TEXT NOT NULL, text TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS vectors(
          chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
          model_key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(chunk_id,model_key));
        CREATE TABLE IF NOT EXISTS conversations(
          id TEXT PRIMARY KEY, collection TEXT NOT NULL, version TEXT NOT NULL, last_question TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS traces(id TEXT PRIMARY KEY, created_at REAL NOT NULL, payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_scope ON documents(collection,version,status);
        CREATE INDEX IF NOT EXISTS idx_chunk_doc ON chunks(doc_id);
        ''')

    def close(self):
        self.db.close()

    def import_document(self, name: str, data: bytes, collection: str, version: str) -> dict:
        if not collection.strip() or not version.strip():
            raise InputError('知识库和版本不能为空。')
        if max(len(collection), len(version)) > 60:
            raise InputError('知识库与版本各不超过 60 字符。')
        name, pages = parse(name, data)
        digest = hashlib.sha256(data).hexdigest()
        old = self.db.execute('SELECT * FROM documents WHERE collection=? AND version=? AND filename=?',
                              (collection, version, name)).fetchone()
        if old and old['sha256'] == digest:
            return {'id': old['id'], 'status': old['status'], 'duplicate': True, 'replaced': False}
        parts = chunks(pages)
        total = self.db.execute('SELECT count(*) FROM chunks').fetchone()[0]
        old_count = self.db.execute('SELECT count(*) FROM chunks WHERE doc_id=?', (old['id'],)).fetchone()[0] if old else 0
        if total - old_count + len(parts) > 12000:
            raise InputError('首版知识库总量限制为 12000 块；请删除不需要的资料。')
        status = 'quarantined' if any(SUSPICIOUS.search(p) for p in pages) else 'ready'
        doc_id = hashlib.sha256(f'{collection}\0{version}\0{name}\0{digest}'.encode()).hexdigest()[:32]
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO collections VALUES (?,?)', (collection, version))
            if old:
                self.db.execute('DELETE FROM documents WHERE id=?', (old['id'],))
            self.db.execute('INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)',
                            (doc_id, name, collection, version, digest, status,
                             json.dumps(pages, ensure_ascii=False), time.time()))
            for i, c in enumerate(parts):
                self.db.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?,?)',
                                (f'{doc_id}:{i}', doc_id, c['page'], c['start_line'], c['end_line'], c['heading'], c['text']))
        return {'id': doc_id, 'status': status, 'duplicate': False, 'replaced': bool(old), 'chunks': len(parts)}

    def library(self) -> dict:
        docs = [dict(r) for r in self.db.execute('''SELECT d.id,d.filename,d.collection,d.version,d.sha256,
          d.status,d.created_at,count(c.id) AS chunks FROM documents d LEFT JOIN chunks c ON c.doc_id=d.id
          GROUP BY d.id ORDER BY d.collection,d.version,d.filename''')]
        collections = [dict(r) for r in self.db.execute('SELECT * FROM collections ORDER BY name')]
        for collection in collections:
            collection['versions'] = sorted({d['version'] for d in docs if d['collection'] == collection['name']})
        return {'documents': docs, 'collections': collections}

    def resolve_version(self, collection: str, version: str | None) -> str:
        row = self.db.execute('SELECT active_version FROM collections WHERE name=?', (collection,)).fetchone()
        if not row:
            raise InputError('知识库不存在，请先导入文档或加载演示。')
        chosen = version or row['active_version']
        if not self.db.execute('SELECT 1 FROM documents WHERE collection=? AND version=?', (collection, chosen)).fetchone():
            raise InputError('该知识库没有所选版本，请重新选择。')
        return chosen

    def activate(self, collection: str, version: str):
        self.resolve_version(collection, version)
        with self.db:
            self.db.execute('UPDATE collections SET active_version=? WHERE name=?', (version, collection))
            self.db.execute('DELETE FROM conversations WHERE collection=?', (collection,))

    def candidates(self, collection: str, version: str) -> list[dict]:
        return [dict(r) for r in self.db.execute('''SELECT c.*,d.filename,d.version,d.collection FROM chunks c
          JOIN documents d ON d.id=c.doc_id WHERE d.collection=? AND d.version=? AND d.status='ready'
          ORDER BY c.id''', (collection, version))]

    def source(self, doc_id: str, page: int) -> dict:
        row = self.db.execute('SELECT filename,pages,version,collection FROM documents WHERE id=?', (doc_id,)).fetchone()
        if not row:
            raise InputError('来源已删除或已替换，请重新提问。')
        pages = json.loads(row['pages'])
        if page < 1 or page > len(pages):
            raise InputError('来源页码无效。')
        return {'filename': row['filename'], 'version': row['version'], 'collection': row['collection'],
                'page': page, 'lines': pages[page - 1].split('\n')}

    def delete(self, doc_id: str):
        with self.db:
            row = self.db.execute('SELECT collection FROM documents WHERE id=?', (doc_id,)).fetchone()
            if not row:
                raise InputError('文档不存在。')
            self.db.execute('DELETE FROM documents WHERE id=?', (doc_id,))
            self.db.execute('DELETE FROM conversations WHERE collection=?', (row['collection'],))
            # Query text and source summaries are not retained after library mutation.
            self.db.execute('DELETE FROM traces')

    def get_vectors(self, ids: list[str], key: str) -> dict[str, list[float]]:
        selected = set(ids)
        return {r['chunk_id']: json.loads(r['value']) for r in self.db.execute(
            'SELECT * FROM vectors WHERE model_key=?', (key,)) if r['chunk_id'] in selected}

    def save_vectors(self, vectors: dict[str, list[float]], key: str):
        with self.db:
            self.db.executemany('INSERT OR REPLACE INTO vectors VALUES (?,?,?)',
                                [(cid, key, json.dumps(value)) for cid, value in vectors.items()])

    def previous(self, conversation: str, collection: str, version: str) -> str | None:
        row = self.db.execute('SELECT * FROM conversations WHERE id=? AND collection=? AND version=?',
                              (conversation, collection, version)).fetchone()
        return row['last_question'] if row else None

    def remember(self, cid: str, collection: str, version: str, question: str):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO conversations VALUES (?,?,?,?)', (cid, collection, version, question))
            # Bounded single-user session history.
            self.db.execute('DELETE FROM conversations WHERE rowid NOT IN (SELECT rowid FROM conversations ORDER BY rowid DESC LIMIT 100)')

    def trace(self, payload: dict) -> str:
        trace_id = uuid.uuid4().hex
        with self.db:
            self.db.execute('INSERT INTO traces VALUES (?,?,?)', (trace_id, time.time(), json.dumps(payload, ensure_ascii=False)))
            self.db.execute('DELETE FROM traces WHERE id NOT IN (SELECT id FROM traces ORDER BY created_at DESC LIMIT 200)')
        return trace_id
