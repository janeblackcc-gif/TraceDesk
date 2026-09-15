import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import ProgrammingError

from app.operations.backup import PostgresTools, backup
from app.operations.upgrade import upgrade as guarded_upgrade
from app.db.session import migration_config
from app.services.errors import DomainError
from app.storage.object_store import ObjectStore
from conftest import disposable_database, upgrade


def pg_tools(database):
    return PostgresTools(database.engine.url, container=os.environ.get('TRACEDESK_BACKUP_TEST_CONTAINER'))


def test_guarded_upgrade_requires_backup_from_the_exact_database(database, tmp_path):
    objects = ObjectStore(tmp_path / 'objects')
    archive = tmp_path / 'backup'
    backup(database, objects, archive, pg_tools(database), app_ref='dev:upgrade-fixture')
    plan = guarded_upgrade(database, archive)
    assert plan['status'] == 'planned'
    assert plan['source_revision'] == plan['target_revision'] == '0006'
    assert guarded_upgrade(database, archive, apply=True)['readiness'] == 'READY'
    with disposable_database() as other:
        upgrade(other)
        with pytest.raises(DomainError, match='UPGRADE_BACKUP_DATABASE_MISMATCH'):
            guarded_upgrade(other, archive)


def test_real_0005_to_0006_upgrade_is_atomic_and_preserves_existing_rows(empty_database, tmp_path):
    upgrade(empty_database, '0005')
    user_id = uuid4()
    with empty_database.engine.begin() as connection:
        connection.execute(text("INSERT INTO users (id,email,display_name) VALUES (:id,'upgrade@example.invalid','Upgrade fixture')"),
                           {'id': user_id})
    with empty_database.engine.begin() as connection:
        config = migration_config()
        config.attributes['connection'] = connection
        command.upgrade(config, 'head')
    inspector = inspect(empty_database.engine)
    assert {'gc_pending_at'} <= {item['name'] for item in inspector.get_columns('file_objects')}
    assert {'purged_at'} <= {item['name'] for item in inspector.get_columns('document_revisions')}
    assert 'document_deletions' in inspector.get_table_names()
    with empty_database.engine.connect() as connection:
        assert connection.scalar(text('SELECT display_name FROM users WHERE id=:id'), {'id': user_id}) == 'Upgrade fixture'
    assert empty_database.readiness().ready

    with disposable_database() as broken:
        upgrade(broken, '0005')
        with broken.engine.begin() as connection:
            connection.execute(text('ALTER TABLE file_objects ADD COLUMN gc_pending_at timestamptz'))
        with pytest.raises(ProgrammingError):
            with broken.engine.begin() as connection:
                config = migration_config()
                config.attributes['connection'] = connection
                command.upgrade(config, 'head')
        with broken.engine.connect() as connection:
            assert connection.scalar(text('SELECT version_num FROM alembic_version')) == '0005'
        broken_tables = inspect(broken.engine).get_table_names()
        broken_revision_columns = {item['name'] for item in inspect(broken.engine).get_columns('document_revisions')}
        assert 'document_deletions' not in broken_tables and 'purged_at' not in broken_revision_columns
        assert broken.readiness().code == 'SCHEMA_MISMATCH'

    report = {
        'status': 'passed',
        'fixture': 'isolated_postgresql',
        'successful_path': {'from': '0005', 'to': '0006', 'existing_row_preserved': True, 'ready': True},
        'failure_path': {'version_after_failure': '0005', 'partial_0006_objects': False, 'ready': False},
        'application_rollback': 'requires previous immutable image and target deployment environment',
    }
    output = Path(os.environ.get('TRACEDESK_TEST_ARTIFACT_DIR', str(tmp_path))) / 'upgrade-drill.json'
    output.write_text(json.dumps(report, indent=2), encoding='utf-8')
