"""Migrations use explicit credentials and never initialize the legacy store."""
from alembic import context
from sqlalchemy import create_engine, pool

from app.config import Settings
from app.db.base import Base
from app.db import models  # noqa: F401 -- register mapped tables
from app.db.session import validate_database_url

target_metadata = Base.metadata


def run() -> None:
    supplied = context.config.attributes.get('connection')
    if supplied is not None:
        context.configure(connection=supplied, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    database_url = Settings.load().database_url
    if database_url is None:
        raise RuntimeError('DATABASE_URL is required for migrations')
    url = validate_database_url(database_url)
    if context.is_offline_mode():
        context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True,
                           connect_args={'connect_timeout': 5})
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run()
