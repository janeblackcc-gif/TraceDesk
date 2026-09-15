"""Rank comparison against frozen committed RC2 on the synthetic Atlas corpus."""
import hashlib
import json
import os
import subprocess
import types
from pathlib import Path

from sqlalchemy import select

from app.db.models import DocumentRevision, LogicalDocument, User
from app.migration.importer import import_snapshot
from app.migration.legacy import inspect_legacy
from app.rag.sparse import candidates, retrieve
from app.storage.object_store import ObjectStore
from app.store import Store

ROOT = Path(__file__).resolve().parents[2]


def test_atlas_migration_preserves_sparse_rankings(database, tmp_path):
    reference = subprocess.run(['git', 'show', 'ccc00c2f5e369bb50a7e459fda0404de1e74b4f3:app/retrieval.py'], cwd=ROOT,
        capture_output=True, text=True, encoding='utf-8', check=True).stdout
    # The reference comes from this repository's committed code, never a document.
    frozen = types.ModuleType('frozen_rc2_retrieval')
    exec(compile(reference, 'frozen_rc2_retrieval.py', 'exec'), frozen.__dict__)
    manifest = json.loads((ROOT / 'demo/manifest.json').read_text(encoding='utf-8'))
    questions = [json.loads(line) for line in (ROOT / 'eval/questions.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    source = tmp_path / 'atlas.db'
    old = Store(source)
    old_candidates = {}
    try:
        for document in manifest['documents']:
            old.import_document(document['filename'], (ROOT / 'demo' / document['file']).read_bytes(),
                                document['collection'], document['version'])
        for version in ('v1', 'v2'):
            old_candidates[version] = old.candidates('Atlas 演示项目', version)
    finally:
        old.close()
    with database.transaction() as session:
        admin = User(email='sparse@example.invalid', display_name='Synthetic rank comparison', is_system_admin=True)
        session.add(admin)
        session.flush()
        admin_id = admin.id
    import_snapshot(database, ObjectStore(tmp_path / 'objects'), inspect_legacy(source), admin_id)
    report = {'dataset_role': 'legacy_dev_synthetic', 'reference_sha256': hashlib.sha256(reference.encode()).hexdigest(),
              'questions_sha256': hashlib.sha256((ROOT / 'eval/questions.jsonl').read_bytes()).hexdigest(),
              'cases': [], 'scope_leaks': 0, 'rank_differences': 0}
    with database.transaction() as session:
        kbs = dict(session.execute(select(DocumentRevision.source_version_label, LogicalDocument.kb_id)
            .join(LogicalDocument, LogicalDocument.id == DocumentRevision.document_id)).all())
        for question in questions:
            version = question['version']
            rows = candidates(session, kbs[version])
            aliases = {row['id']: row['legacy_chunk_id'] for row in rows}
            assert {row['legacy_chunk_id'] for row in rows} == {row['id'] for row in old_candidates[version]}
            for method in ('bm25', 'hybrid'):
                expected = frozen.search(question['question'], old_candidates[version], method)
                actual = retrieve(session, kbs[version], None, [question['question']], method, context=False)
                before = [row['id'] for row in expected]
                after = [aliases[row['id']] for row in actual]
                report['rank_differences'] += before != after
                report['scope_leaks'] += sum(row['version'] != version for row in actual)
                report['cases'].append({'id': question['id'], 'method': method, 'before': before, 'after': after})
    output = Path(os.environ.get('TRACEDESK_TEST_ARTIFACT_DIR', str(tmp_path))) / 'sparse-rank-diff.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    assert report['scope_leaks'] == 0
    assert report['rank_differences'] == 0, f"{report['rank_differences']} rank differences; see {output}"
