import json
import sqlite3

import pytest

from app.migration.legacy import LegacyValidationError, file_hash, inspect_legacy
from app.store import Store


def fixture_database(path):
    store = Store(path)
    try:
        first = store.import_document('deploy.md', b'# Deployment\nPort 8088.', 'Atlas', 'v1')
        store.import_document('deploy.md', b'# Deployment\nPort 8099.', 'Atlas', 'v2')
        store.save_vectors({first['id'] + ':0': [1.0, 0.0]}, 'unknown-legacy-profile')
    finally:
        store.close()


def test_preflight_is_read_only_and_retains_scope_and_model_uncertainty(tmp_path):
    path = tmp_path / 'source.db'
    fixture_database(path)
    before = file_hash(path)
    snapshot = inspect_legacy(path)
    assert snapshot.report['source_unchanged'] and file_hash(path) == before
    assert snapshot.report['source_counts']['documents'] == 2
    assert snapshot.report['planned_kbs'] == [{'collection': 'Atlas', 'version': 'v1'}, {'collection': 'Atlas', 'version': 'v2'}]
    assert snapshot.report['vector_profiles'] == [{'model_key': 'unknown-legacy-profile', 'dimension': 2, 'count': 1}]
    assert snapshot.report['reindex_required']


@pytest.mark.parametrize(('sql', 'code'), [
    ("UPDATE documents SET pages='not-json'", 'INVALID_PAGES_JSON'),
    ("UPDATE chunks SET text='tampered'", 'INVALID_CHUNK_POSITION_OR_TEXT'),
    ("UPDATE vectors SET value='[true, 0]'", 'INVALID_VECTOR'),
    ("UPDATE vectors SET value='[NaN, 0]'", 'INVALID_VECTOR'),
    ("UPDATE chunks SET doc_id='missing'", 'ORPHAN_CHUNK'),
    ("UPDATE vectors SET chunk_id='missing'", 'ORPHAN_VECTOR'),
])
def test_invalid_sources_fail_without_modification(tmp_path, sql, code):
    path = tmp_path / 'source.db'
    fixture_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(sql)
    connection.close()
    before = file_hash(path)
    with pytest.raises(LegacyValidationError) as failure:
        inspect_legacy(path)
    assert code in {issue['code'] for issue in failure.value.report['issues']}
    assert file_hash(path) == before


def test_live_uncheckpointed_wal_is_rejected(tmp_path):
    path = tmp_path / 'source.db'
    fixture_database(path)
    store = Store(path)
    try:
        store.import_document('live.md', b'Live data', 'Atlas', 'v1')
        with pytest.raises(LegacyValidationError) as failure:
            inspect_legacy(path)
        assert failure.value.report['issues'][0]['code'] == 'SOURCE_REQUIRES_OFFLINE_CHECKPOINT'
        assert failure.value.report['source_unchanged'] is None
    finally:
        store.close()


def test_filename_normalization_collision_requires_operator_decision(tmp_path):
    path = tmp_path / 'source.db'
    fixture_database(path)
    store = Store(path)
    try:
        store.import_document('DEPLOY.md', b'Conflicting case', 'Atlas', 'v1')
    finally:
        store.close()
    with pytest.raises(LegacyValidationError) as failure:
        inspect_legacy(path)
    assert 'NORMALIZED_FILENAME_COLLISION' in {issue['code'] for issue in failure.value.report['issues']}
