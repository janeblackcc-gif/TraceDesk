"""Every integration test uses a new database, never the operator's data."""
import os
from contextlib import contextmanager
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine

from app.db.session import Database, migration_config, validate_database_url


@contextmanager
def disposable_database():
    value = os.environ.get('TRACEDESK_TEST_DATABASE_URL')
    if not value:
        pytest.skip('PostgreSQL integration requires TRACEDESK_TEST_DATABASE_URL')
    url = validate_database_url(value)
    name = 'tracedesk_test_' + uuid4().hex
    admin = create_engine(url, isolation_level='AUTOCOMMIT', hide_parameters=True)
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    database = Database(url.set(database=name).render_as_string(hide_password=False))
    try:
        yield database
    finally:
        database.close()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()


@pytest.fixture
def empty_database():
    with disposable_database() as database:
        yield database


def upgrade(database: Database, revision: str = 'head') -> None:
    with database.engine.begin() as connection:
        config = migration_config()
        config.attributes['connection'] = connection
        command.upgrade(config, revision)


@pytest.fixture
def database(empty_database):
    upgrade(empty_database)
    return empty_database
