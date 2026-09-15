from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.services.errors import DomainError

ROOT = Path(__file__).resolve().parents[2]
MAINTENANCE_KEY = 87442318509431


def validate_database_url(value: str) -> URL:
    try:
        url = make_url(value)
        if url.drivername != 'postgresql+psycopg' or not url.database or not url.username:
            raise ValueError
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError
    except (SQLAlchemyError, ValueError, TypeError):
        raise ValueError('DATABASE_URL must specify a PostgreSQL psycopg database and user') from None
    return url


def migration_config() -> Config:
    config = Config(str(ROOT / 'alembic.ini'))
    config.set_main_option('script_location', str(ROOT / 'alembic'))
    return config


@dataclass(frozen=True)
class DatabaseStatus:
    ready: bool
    code: str


class Database:
    def __init__(self, url: str):
        self.engine: Engine = create_engine(validate_database_url(url), pool_pre_ping=True,
            pool_size=5, max_overflow=5, pool_timeout=5, hide_parameters=True,
            connect_args={'connect_timeout': 5, 'options': '-c statement_timeout=10000 -c timezone=UTC'})
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with self.sessions.begin() as session:
            if not session.scalar(text('SELECT pg_try_advisory_xact_lock_shared(:key)'), {'key': MAINTENANCE_KEY}):
                raise DomainError('MAINTENANCE_IN_PROGRESS', 503, retryable=True)
            yield session

    @contextmanager
    def maintenance(self) -> Iterator[Connection]:
        """Fail fast until existing transactions finish; release on every exit."""
        with self.engine.begin() as connection:
            if not connection.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': MAINTENANCE_KEY}):
                raise DomainError('DATABASE_BUSY', 409, retryable=True)
            yield connection

    def readiness(self) -> DatabaseStatus:
        try:
            with self.engine.connect() as connection:
                if not connection.scalar(text('SELECT pg_try_advisory_xact_lock_shared(:key)'), {'key': MAINTENANCE_KEY}):
                    return DatabaseStatus(False, 'MAINTENANCE_IN_PROGRESS')
                connection.execute(text('SELECT 1'))
                actual = set(MigrationContext.configure(connection).get_current_heads())
                expected = set(ScriptDirectory.from_config(migration_config()).get_heads())
                if not expected or actual != expected:
                    return DatabaseStatus(False, 'SCHEMA_MISMATCH')
                extensions = set(connection.execute(text(
                    "SELECT extname FROM pg_extension WHERE extname IN ('vector', 'citext')")).scalars())
                if extensions != {'vector', 'citext'}:
                    return DatabaseStatus(False, 'EXTENSION_MISSING')
        except SQLAlchemyError:
            return DatabaseStatus(False, 'DATABASE_UNAVAILABLE')
        return DatabaseStatus(True, 'READY')

    def close(self) -> None:
        self.engine.dispose()
