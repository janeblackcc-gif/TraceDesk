from uuid import uuid4

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.application import create_application
from app.config import Settings
from app.db.models import (FileObject, IndexGeneration, KnowledgeBase, LogicalDocument,
                           ModelProfile, User, Workspace, WorkspaceMember)
from app.db.session import migration_config
from conftest import upgrade


def seed(session):
    user = User(email='test@example.invalid', display_name='Test operator')
    workspace = Workspace(name='Fixture', slug='fixture')
    session.add_all([user, workspace])
    session.flush()
    kb = KnowledgeBase(workspace_id=workspace.id, name='Fixture KB', slug='fixture')
    session.add(kb)
    session.flush()
    return user, workspace, kb


def test_empty_stale_repeat_and_metadata_match(empty_database):
    db = empty_database
    assert db.readiness().code == 'SCHEMA_MISMATCH'
    upgrade(db, '0001')
    assert db.readiness().ready is False
    upgrade(db)
    assert db.readiness().ready
    before = set(inspect(db.engine).get_table_names())
    upgrade(db)
    assert set(inspect(db.engine).get_table_names()) == before
    assert len(before) >= 29
    with db.engine.begin() as connection:
        config = migration_config()
        config.attributes['connection'] = connection
        command.check(config)


def test_readiness_rejects_mismatch_without_leaking_credentials(database, tmp_path):
    app = create_application(Settings(data_dir=tmp_path, database_url=database.engine.url.render_as_string(hide_password=False)))
    with TestClient(app, base_url='https://localhost') as client:
        assert client.get('/readyz').status_code == 200
        with database.engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num = 'obsolete'"))
        response = client.get('/readyz')
        assert response.status_code == 503
        assert response.json() == {'ready': False, 'code': 'SCHEMA_MISMATCH'}
        assert client.get('/api/library').status_code == 401
    with pytest.raises(RuntimeError, match='SCHEMA_MISMATCH'):
        with TestClient(create_application(Settings(data_dir=tmp_path, database_url=database.engine.url.render_as_string(hide_password=False))), base_url='https://localhost'):
            pass


def test_memberships_email_and_orphan_constraints(database):
    with database.transaction() as session:
        user, workspace, _ = seed(session)
        session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role='admin'))
        session.flush()
        for operation in (
            lambda: session.add(User(email='TEST@example.invalid', display_name='Duplicate')),
            lambda: session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role='member')),
            lambda: session.add(WorkspaceMember(workspace_id=workspace.id, user_id=uuid4(), role='member')),
        ):
            with pytest.raises(IntegrityError), session.begin_nested():
                operation()
                session.flush()


def test_only_one_active_generation_and_no_cross_kb_pointer(database):
    with database.transaction() as session:
        _, workspace, kb = seed(session)
        profile = ModelProfile(provider='fixture', model_tag='test', model_digest='a' * 64,
                               dimension=1024, input_profile='fixture')
        other = KnowledgeBase(workspace_id=workspace.id, name='Other', slug='other')
        session.add_all([profile, other])
        session.flush()
        generation = IndexGeneration(kb_id=kb.id, model_profile_id=profile.id, retrieval_config_hash='b' * 64,
                                     status='active', corpus_manifest_hash='c' * 64)
        session.add(generation)
        session.flush()
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(IndexGeneration(kb_id=kb.id, model_profile_id=profile.id, retrieval_config_hash='b' * 64,
                                        status='active', corpus_manifest_hash='d' * 64))
            session.flush()
        with pytest.raises(IntegrityError), session.begin_nested():
            other.active_index_generation_id = generation.id
            session.flush()


def test_file_digest_and_live_document_name_constraints(database):
    with database.transaction() as session:
        user, _, kb = seed(session)
        session.add(LogicalDocument(kb_id=kb.id, display_name='deploy.md', normalized_key='deploy.md', created_by=user.id))
        session.flush()
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(LogicalDocument(kb_id=kb.id, display_name='Deploy.md', normalized_key='deploy.md', created_by=user.id))
            session.flush()
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(FileObject(sha256='invalid', size_bytes=-1, storage_key='invalid', detected_type='text/plain', original_extension='.txt'))
            session.flush()


def test_all_foreign_keys_have_leading_indexes(database):
    inspector = inspect(database.engine)
    for table in inspector.get_table_names():
        indexes = [item['column_names'] for item in inspector.get_indexes(table)]
        indexes += [inspector.get_pk_constraint(table)['constrained_columns']]
        indexes += [item['column_names'] for item in inspector.get_unique_constraints(table)]
        for fk in inspector.get_foreign_keys(table):
            columns = fk['constrained_columns']
            assert any(index[:len(columns)] == columns for index in indexes), (table, columns)
