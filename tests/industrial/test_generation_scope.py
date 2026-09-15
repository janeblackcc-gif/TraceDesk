from uuid import uuid4

import pytest
from sqlalchemy import MetaData, text
from sqlalchemy.exc import IntegrityError

from app.db.models import (DocumentRevision, FileObject, GenerationRevision, IndexGeneration,
                           KnowledgeBase, LogicalDocument, ModelProfile, ParseRun, User, Workspace)
from conftest import upgrade


def seed(session):
    user = User(email='scope@example.invalid', display_name='Scope fixture')
    workspace = Workspace(name='Fixture', slug='scope')
    file = FileObject(sha256='a' * 64, size_bytes=1, storage_key='sha256/aa/' + 'a' * 64,
                      detected_type='text/plain', original_extension='.txt')
    profile = ModelProfile(provider='fixture', model_tag='fixture', model_digest='b' * 64, dimension=1024, input_profile='fixture')
    session.add_all([user, workspace, file, profile])
    session.flush()
    kbs = [KnowledgeBase(workspace_id=workspace.id, name=name, slug=name) for name in ['one', 'two']]
    session.add_all(kbs)
    session.flush()
    documents = [LogicalDocument(kb_id=kb.id, display_name='fixture.txt', normalized_key='fixture.txt', created_by=user.id) for kb in kbs]
    session.add_all(documents)
    session.flush()
    revisions = [DocumentRevision(document_id=doc.id, file_object_id=file.id, sha256=file.sha256,
                  revision_no=1, status='parsed', created_by=user.id) for doc in documents]
    session.add_all(revisions)
    session.flush()
    parses = [ParseRun(revision_id=revision.id, parser_name='fixture', parser_version='1', config_hash='c' * 64, status='succeeded') for revision in revisions]
    generation = IndexGeneration(kb_id=kbs[0].id, model_profile_id=profile.id, retrieval_config_hash='d' * 64,
                                  status='building', corpus_manifest_hash='e' * 64)
    session.add_all([*parses, generation])
    session.flush()
    return kbs, documents, revisions, parses, generation


def test_generation_cannot_include_other_kb_or_parse(database):
    with database.transaction() as session:
        kbs, documents, revisions, parses, generation = seed(session)
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(GenerationRevision(generation_id=generation.id, revision_id=revisions[1].id,
                kb_id=kbs[0].id, document_id=documents[1].id, parse_run_id=parses[1].id))
            session.flush()
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(GenerationRevision(generation_id=generation.id, revision_id=revisions[0].id,
                kb_id=kbs[0].id, document_id=documents[0].id, parse_run_id=parses[1].id))
            session.flush()
        with pytest.raises(IntegrityError), session.begin_nested():
            documents[0].active_revision_id = revisions[1].id
            session.flush()


def seed_0003(session):
    """Freeze the source schema fixture instead of inserting with today's ORM."""
    metadata = MetaData()
    metadata.reflect(session.connection())
    identifiers = {name: uuid4() for name in ('user', 'workspace', 'file', 'profile', 'kb', 'document', 'revision', 'parse', 'generation')}
    rows = [
        ('users', {'id': identifiers['user'], 'email': 'scope@example.invalid', 'display_name': 'Fixture'}),
        ('workspaces', {'id': identifiers['workspace'], 'name': 'Fixture', 'slug': 'scope'}),
        ('file_objects', {'id': identifiers['file'], 'sha256': 'a' * 64, 'size_bytes': 1,
                         'storage_key': 'sha256/aa/' + 'a' * 64, 'detected_type': 'text/plain', 'original_extension': '.txt'}),
        ('model_profiles', {'id': identifiers['profile'], 'provider': 'fixture', 'model_tag': 'fixture',
                            'model_digest': 'b' * 64, 'dimension': 1024, 'input_profile': 'fixture'}),
        ('knowledge_bases', {'id': identifiers['kb'], 'workspace_id': identifiers['workspace'], 'name': 'One', 'slug': 'one'}),
        ('logical_documents', {'id': identifiers['document'], 'kb_id': identifiers['kb'], 'display_name': 'fixture.txt',
                               'normalized_key': 'fixture.txt', 'created_by': identifiers['user']}),
        ('document_revisions', {'id': identifiers['revision'], 'document_id': identifiers['document'], 'file_object_id': identifiers['file'],
                               'sha256': 'a' * 64, 'revision_no': 1, 'status': 'parsed', 'created_by': identifiers['user']}),
        ('parse_runs', {'id': identifiers['parse'], 'revision_id': identifiers['revision'], 'parser_name': 'fixture',
                        'parser_version': '1', 'config_hash': 'c' * 64, 'status': 'succeeded'}),
        ('index_generations', {'id': identifiers['generation'], 'kb_id': identifiers['kb'], 'model_profile_id': identifiers['profile'],
                               'retrieval_config_hash': 'd' * 64, 'status': 'building', 'corpus_manifest_hash': 'e' * 64}),
    ]
    for table, values in rows:
        session.execute(metadata.tables[table].insert().values(**values))
    return identifiers['generation'], identifiers['revision'], identifiers['parse']


def test_upgrade_backfills_existing_unambiguous_generation(empty_database):
    upgrade(empty_database, '0003')
    with empty_database.transaction() as session:
        generation_id, revision_id, parse_id = seed_0003(session)
        session.execute(text('INSERT INTO generation_revisions (generation_id,revision_id) VALUES (:g,:r)'),
                        {'g': generation_id, 'r': revision_id})
    upgrade(empty_database)
    with empty_database.transaction() as session:
        row = session.get(GenerationRevision, (generation_id, revision_id))
        assert row.parse_run_id == parse_id


def test_ambiguous_backfill_fails_transactionally(empty_database):
    upgrade(empty_database, '0003')
    with empty_database.transaction() as session:
        generation_id, revision_id, _ = seed_0003(session)
        session.add(ParseRun(revision_id=revision_id, parser_name='fixture', parser_version='2', config_hash='f' * 64, status='succeeded'))
        session.execute(text('INSERT INTO generation_revisions (generation_id,revision_id) VALUES (:g,:r)'),
                        {'g': generation_id, 'r': revision_id})
    with pytest.raises(IntegrityError):
        upgrade(empty_database)
    with empty_database.engine.connect() as connection:
        assert connection.scalar(text('SELECT version_num FROM alembic_version')) == '0003'
        assert connection.scalar(text('SELECT count(*) FROM generation_revisions')) == 1
