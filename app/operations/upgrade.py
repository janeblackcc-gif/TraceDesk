"""Guarded schema upgrade bound to a verified backup of the exact database."""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import select, text

from app.db.models import Job
from app.db.session import Database, migration_config
from app.operations.backup import identity, verify_backup
from app.services.errors import DomainError


def schema_revisions(connection) -> list[str]:
    return list(connection.scalars(text('SELECT version_num FROM alembic_version ORDER BY version_num')))


def upgrade(database: Database, archive: Path, *, apply: bool = False) -> dict[str, object]:
    """Validate an operator backup and optionally migrate its source database to head.

    The verified backup is the rollback boundary. The exclusive maintenance lock
    fences cooperative application writes while the source revision is rechecked
    and PostgreSQL applies the transactional DDL.
    """
    manifest = verify_backup(archive)
    config = migration_config()
    target_revisions = sorted(ScriptDirectory.from_config(config).get_heads())
    if len(target_revisions) != 1:
        raise DomainError('UPGRADE_TARGET_INVALID', 409)
    target = target_revisions[0]
    with database.maintenance() as connection:
        if identity(connection) != manifest.source_identity_sha256:
            raise DomainError('UPGRADE_BACKUP_DATABASE_MISMATCH', 409)
        source_revisions = schema_revisions(connection)
        if source_revisions != manifest.schema_revision or len(source_revisions) != 1:
            raise DomainError('UPGRADE_BACKUP_SCHEMA_MISMATCH', 409)
        if connection.scalar(select(Job.id).where(Job.state == 'running').limit(1)):
            raise DomainError('UPGRADE_JOBS_RUNNING', 409, retryable=True)
        source = source_revisions[0]
        script = ScriptDirectory.from_config(config)
        try:
            revisions = [item.revision for item in script.iterate_revisions(target, source)] if source != target else []
        except Exception:
            raise DomainError('UPGRADE_PATH_INVALID', 409) from None
        report: dict[str, object] = {
            'status': 'planned' if not apply else 'running',
            'source_revision': source,
            'target_revision': target,
            'upgrade_revisions': list(reversed(revisions)),
            'backup_app_ref': manifest.app_ref,
            'backup_dump_sha256': manifest.dump.sha256,
        }
        if not apply:
            return report
        config.attributes['connection'] = connection
        command.upgrade(config, 'head')
        actual = schema_revisions(connection)
        if actual != [target]:
            raise DomainError('UPGRADE_SCHEMA_MISMATCH', 409)
        report['status'] = 'migrated'
    status = database.readiness()
    if not status.ready:
        raise DomainError('UPGRADE_NOT_READY', 409)
    report['status'] = 'passed'
    report['readiness'] = status.code
    return report
