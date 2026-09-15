"""Consistent PostgreSQL + immutable object backup, restored only to empty targets."""
from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.engine import Connection, URL

from app.db.base import Base
from app.db.models import FileObject, Job, ModelProfile
from app.db.session import Database
from app.services.errors import DomainError
from app.storage.object_store import KEY, ObjectStore, StoredObject

IDENTITY_SQL = """SELECT system_identifier::text || ':' || current_database() || ':' ||
    (SELECT oid::text FROM pg_database WHERE datname=current_database()) FROM pg_control_system()"""


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class FileRecord(Record):
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    size_bytes: int = Field(ge=0)
    storage_key: str


class TableRecord(Record):
    rows: int = Field(ge=0)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class BackupManifest(Record):
    format_version: int = Field(ge=1, le=1)
    created_at: str
    source_identity_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    app_ref: str = Field(min_length=1, max_length=300)
    schema_revision: list[str]
    extensions: dict[str, str]
    model_profiles: list[dict[str, str | int | None]]
    dump: FileRecord
    objects: list[FileRecord]
    tables: dict[str, TableRecord]


def checksum(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def identity(connection: Connection) -> str:
    return hashlib.sha256(str(connection.scalar(text(IDENTITY_SQL))).encode()).hexdigest()


class PostgresTools:
    """Use native PG18 tools, or exec in an explicitly selected PG18 container.

    A server identity check prevents a host/container port mismatch from backing
    up a different database. Passwords travel in the child environment, never argv.
    """
    def __init__(self, url: URL, *, container: str | None = None, timeout: int = 600):
        self.url, self.container, self.timeout = url, container, timeout

    def run(self, tool: str, args: list[str], *, source: BinaryIO | None = None,
            destination: BinaryIO | None = None) -> bytes:
        environment = dict(os.environ, PGPASSWORD=self.url.password or '', PGCONNECT_TIMEOUT='5')
        environment.pop('PGOPTIONS', None)
        prefix = (['docker', 'exec', '-i', '-e', 'PGPASSWORD', '-e', 'PGCONNECT_TIMEOUT', self.container]
                  if self.container else [])
        command = [*prefix, tool, '--no-password', '--host', '127.0.0.1' if self.container else (self.url.host or 'localhost'),
                   '--port', str(5432 if self.container else (self.url.port or 5432)),
                   '--username', self.url.username or '', '--dbname', self.url.database or '', *args]
        try:
            result = subprocess.run(command, env=environment, stdin=source, stdout=destination or subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise DomainError('BACKUP_TOOL_UNAVAILABLE', 503) from None
        if result.returncode:
            # stderr can contain connection details; surface only the stable code.
            raise DomainError('BACKUP_TOOL_FAILED', 503)
        return result.stdout or b''

    def verify_connection(self, connection: Connection) -> None:
        actual = self.run('psql', ['--no-psqlrc', '--tuples-only', '--no-align', '--command', IDENTITY_SQL])
        if hashlib.sha256(actual.strip()).hexdigest() != identity(connection):
            raise DomainError('BACKUP_CONNECTION_MISMATCH', 409)


def fingerprints(connection: Connection) -> dict[str, TableRecord]:
    result = {}
    # Table/column names come only from our SQLAlchemy metadata, never the archive.
    for name, table in sorted(Base.metadata.tables.items()):
        order = ', '.join('"' + column.name + '"' for column in table.primary_key)
        digest, count = hashlib.sha256(), 0
        rows = connection.execution_options(yield_per=100).execute(
            text(f'SELECT to_jsonb(t)::text FROM "{name}" t ORDER BY {order}'))
        for row in rows:
            digest.update(row[0].encode('utf-8') + b'\n')
            count += 1
        rows.close()
        connection.execution_options(yield_per=0, stream_results=False)
        result[name] = TableRecord(rows=count, sha256=digest.hexdigest())
    return result


def backup(database: Database, objects: ObjectStore, output: Path, tools: PostgresTools,
           *, app_ref: str) -> BackupManifest:
    if not app_ref.strip() or len(app_ref) > 300:
        raise DomainError('APP_REFERENCE_REQUIRED', 400)
    target = output.resolve()
    if target.is_relative_to(objects.root) or objects.root.is_relative_to(target):
        raise DomainError('BACKUP_PATH_OVERLAP', 400)
    if not database.readiness().ready:
        raise DomainError('DATABASE_NOT_READY', 503)
    with database.maintenance() as connection:
        if connection.scalar(select(Job.id).where(Job.state == 'running').limit(1)):
            raise DomainError('BACKUP_JOBS_RUNNING', 409, retryable=True)
        tools.verify_connection(connection)
        target.mkdir(parents=True, exist_ok=False, mode=0o700)
        destination = ObjectStore(target / 'objects')
        records = []
        for row in connection.execute(select(FileObject.sha256, FileObject.size_bytes, FileObject.storage_key)
                                      .order_by(FileObject.sha256)):
            item = StoredObject(row.sha256, row.size_bytes, row.storage_key)
            objects.verify(item)
            with objects.open(item.storage_key) as stream:
                copied = destination.put(stream, max_bytes=item.size_bytes)
            if copied != item:
                raise DomainError('BACKUP_OBJECT_MISMATCH', 409)
            records.append(FileRecord(sha256=item.sha256, size_bytes=item.size_bytes, storage_key=item.storage_key))
        dump_path = target / 'database.dump'
        with dump_path.open('xb') as stream:
            tools.run('pg_dump', ['--format=custom', '--no-owner', '--no-acl', '--lock-wait-timeout=5s'], destination=stream)
            stream.flush()
            os.fsync(stream.fileno())
        profiles = [dict(row._mapping) for row in connection.execute(select(ModelProfile.provider, ModelProfile.model_tag,
            ModelProfile.model_digest, ModelProfile.dimension, ModelProfile.input_profile, ModelProfile.prompt_profile))]
        manifest = BackupManifest(format_version=1, created_at=datetime.now(timezone.utc).isoformat(),
            source_identity_sha256=identity(connection), app_ref=app_ref,
            schema_revision=list(connection.scalars(text('SELECT version_num FROM alembic_version ORDER BY version_num'))),
            extensions=dict(connection.execute(text("SELECT extname, extversion FROM pg_extension WHERE extname IN ('vector','citext')")).tuples().all()),
            model_profiles=profiles, dump=FileRecord(sha256=checksum(dump_path), size_bytes=dump_path.stat().st_size,
                storage_key='database.dump'), objects=records, tables=fingerprints(connection))
        # Presence of this final, fsynced file is the completion marker. Interrupted
        # directories remain evidence and are never accepted as complete backups.
        with (target / 'manifest.json').open('x', encoding='utf-8') as stream:
            stream.write(manifest.model_dump_json(indent=2) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        return manifest


def verify_backup(directory: Path) -> BackupManifest:
    if directory.is_symlink() or (directory / 'manifest.json').is_symlink():
        raise DomainError('BACKUP_PATH_INVALID', 400)
    manifest = BackupManifest.model_validate_json((directory / 'manifest.json').read_bytes())
    dump = directory / 'database.dump'
    if (manifest.dump.storage_key != 'database.dump' or dump.is_symlink() or
            dump.stat().st_size != manifest.dump.size_bytes or checksum(dump) != manifest.dump.sha256):
        raise DomainError('BACKUP_DUMP_MISMATCH', 409)
    root = directory / 'objects'
    if not root.is_dir() or root.is_symlink():
        raise DomainError('BACKUP_PATH_INVALID', 400)
    seen = set()
    for item in manifest.objects:
        match = KEY.fullmatch(item.storage_key)
        if (not match or match[1] != item.sha256[:2] or match[2] != item.sha256 or item.storage_key in seen):
            raise DomainError('BACKUP_OBJECT_KEY_INVALID', 400)
        seen.add(item.storage_key)
        path = root / item.storage_key
        if any(part.is_symlink() for part in [root / 'sha256', path.parent, path]):
            raise DomainError('BACKUP_PATH_INVALID', 400)
        if path.stat().st_size != item.size_bytes or checksum(path) != item.sha256:
            raise DomainError('BACKUP_OBJECT_MISMATCH', 409)
    return manifest


def restore(database: Database, object_root: Path, archive: Path, tools: PostgresTools) -> BackupManifest:
    """Restore a trusted operator backup; never overwrite or clean any destination."""
    manifest = verify_backup(archive)
    destination = object_root.resolve()
    if (object_root.exists() or object_root.is_symlink() or destination.is_relative_to(archive.resolve()) or
            archive.resolve().is_relative_to(destination)):
        raise DomainError('RESTORE_REQUIRES_NEW_DIRECTORY', 409)
    with database.maintenance() as connection:
        if identity(connection) == manifest.source_identity_sha256:
            raise DomainError('RESTORE_REQUIRES_NEW_DATABASE', 409)
        if connection.scalar(text("""SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%')
                OR EXISTS(SELECT 1 FROM pg_namespace WHERE nspname NOT IN ('public','pg_catalog','information_schema')
                AND nspname NOT LIKE 'pg_%')""")):
            raise DomainError('RESTORE_REQUIRES_EMPTY_DATABASE', 409)
        tools.verify_connection(connection)
        objects = ObjectStore(destination)
        for item in manifest.objects:
            with (archive / 'objects' / item.storage_key).open('rb') as stream:
                copied = objects.put(stream, max_bytes=item.size_bytes)
            if (copied.sha256, copied.size_bytes, copied.storage_key) != (item.sha256, item.size_bytes, item.storage_key):
                raise DomainError('BACKUP_OBJECT_MISMATCH', 409)
        # The external restore connection does not use application transactions.
        # Our maintenance gate fences web/workers throughout the restoration.
        with (archive / 'database.dump').open('rb') as stream:
            tools.run('pg_restore', ['--single-transaction', '--exit-on-error', '--no-owner', '--no-acl'], source=stream)
        if fingerprints(connection) != manifest.tables:
            raise DomainError('RESTORE_DATABASE_MISMATCH', 409)
        restored_schema = list(connection.scalars(text('SELECT version_num FROM alembic_version ORDER BY version_num')))
        if restored_schema != manifest.schema_revision:
            raise DomainError('RESTORE_SCHEMA_MISMATCH', 409)
        # All old cookies must reauthenticate in the recovered environment.
        connection.execute(text('UPDATE sessions SET revoked_at=clock_timestamp() WHERE revoked_at IS NULL'))
    if not database.readiness().ready:
        raise DomainError('RESTORE_SCHEMA_REQUIRES_MATCHING_APP', 409)
    return manifest
