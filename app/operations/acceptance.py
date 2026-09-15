"""Evidence-backed release acceptance aggregation with explicit external gates."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.operations.capacity import CapacityEnvironment

SHA256 = r'^[0-9a-f]{64}$'
SENSITIVE_KEYS = {'password', 'token', 'session', 'question', 'quote', 'database_url', 'prompt'}
PINNED_IMAGE = re.compile(r'^\S+@sha256:[0-9a-f]{64}$')


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Artifact(StrictModel):
    kind: Literal['environment', 'integration', 'deployment', 'browser', 'model', 'migration', 'restore',
                  'upgrade', 'capacity', 'quality', 'pilot', 'other']
    path: str
    sha256: str = Field(pattern=SHA256)

    @model_validator(mode='after')
    def path_is_safe(self) -> Artifact:
        value = PurePosixPath(self.path)
        if not self.path or '\\' in self.path or value.is_absolute() or '..' in value.parts:
            raise ValueError('Artifact paths must be repository-relative and normalized')
        return self


class AcceptanceGate(StrictModel):
    fa_id: str = Field(pattern=r'^FA-(?:0[1-9]|1[0-9]|2[01])$')
    status: Literal['PASS', 'FAIL', 'SKIPPED-BLOCKED']
    requirement_ids: list[str] = Field(min_length=1)
    task_ids: list[str] = Field(min_length=1)
    artifacts: list[Artifact] = Field(default_factory=list)
    blocker: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=1000)

    @model_validator(mode='after')
    def status_has_evidence(self) -> AcceptanceGate:
        if len(set(self.requirement_ids)) != len(self.requirement_ids) or len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError('Requirement and task IDs must be unique')
        if self.status in {'PASS', 'FAIL'} and not self.artifacts:
            raise ValueError('PASS and FAIL gates require preserved evidence')
        if self.status == 'SKIPPED-BLOCKED' and not self.blocker:
            raise ValueError('Blocked gates require a concrete blocker')
        if self.status != 'SKIPPED-BLOCKED' and self.blocker is not None:
            raise ValueError('Only blocked gates may carry a blocker')
        return self


class AcceptanceManifest(StrictModel):
    format_version: Literal[1]
    run_id: str = Field(min_length=1, max_length=200)
    scope: Literal['local-readiness', 'target-release']
    created_at: datetime
    environment: Artifact
    gates: list[AcceptanceGate] = Field(min_length=21, max_length=21)

    @model_validator(mode='after')
    def coherent(self) -> AcceptanceManifest:
        if self.created_at.utcoffset() is None:
            raise ValueError('Acceptance timestamp must include a timezone')
        if self.environment.kind != 'environment':
            raise ValueError('The environment record must use kind=environment')
        if len({gate.fa_id for gate in self.gates}) != len(self.gates):
            raise ValueError('Acceptance gate IDs must be unique')
        return self


def acceptance_mapping(path: Path) -> dict[str, dict[str, set[str]]]:
    mapping: dict[str, dict[str, set[str]]] = {}
    with path.open(encoding='utf-8', newline='') as stream:
        for row in csv.DictReader(stream):
            for acceptance in row['acceptance_ids'].split(';'):
                target = mapping.setdefault(acceptance, {'tasks': set(), 'requirements': set()})
                target['tasks'].add(row['task_id'])
                target['requirements'].update(row['requirement_ids'].split(';'))
    return mapping


def _artifact(repository: Path, artifact: Artifact) -> Path:
    path = repository / artifact.path
    current = repository
    for part in PurePosixPath(artifact.path).parts:
        current /= part
        if current.is_symlink():
            raise ValueError('Acceptance evidence cannot use symlinks')
    if not path.is_file() or not path.resolve().is_relative_to(repository.resolve()):
        raise ValueError('Acceptance artifact is missing or outside the repository')
    with path.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != artifact.sha256:
        raise ValueError('Acceptance artifact hash mismatch')
    return path


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('Acceptance JSON evidence must be an object')
    return value


def _contains_sensitive_key(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in SENSITIVE_KEYS or any(lowered.endswith('_' + name) for name in SENSITIVE_KEYS):
                return True
            if _contains_sensitive_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _kind_report(paths: dict[str, list[Path]], kind: str) -> dict:
    candidates = paths.get(kind, [])
    if not candidates:
        raise ValueError(f'Target gate requires {kind} evidence')
    return _json(candidates[0])


def _target_gate(gate: AcceptanceGate, paths: dict[str, list[Path]]) -> None:
    if gate.fa_id == 'FA-01':
        report = _kind_report(paths, 'deployment')
        if report.get('status') != 'passed' or report.get('scope') != 'production':
            raise ValueError('FA-01 requires a passed production deployment report')
    elif gate.fa_id == 'FA-14':
        report = _kind_report(paths, 'migration')
        if report.get('status') != 'passed' or report.get('source_role') != 'authorized_real_copy':
            raise ValueError('FA-14 requires an authorized real-copy migration report')
    elif gate.fa_id == 'FA-15':
        report = _kind_report(paths, 'restore')
        if (report.get('status') != 'passed' or report.get('fixture') in {None, 'synthetic'} or
                not isinstance(report.get('rto_seconds'), (int, float)) or
                not isinstance(report.get('rpo_seconds'), (int, float))):
            raise ValueError('FA-15 requires a target restore drill with measured RTO and RPO')
    elif gate.fa_id == 'FA-16':
        report = _kind_report(paths, 'upgrade')
        required = {'previous_image', 'new_image', 'migration_failure_recovered', 'application_failure_recovered'}
        if (report.get('status') != 'passed' or report.get('scope') != 'target' or not required <= set(report) or
                not report['migration_failure_recovered'] or not report['application_failure_recovered'] or
                not PINNED_IMAGE.fullmatch(str(report['previous_image'])) or
                not PINNED_IMAGE.fullmatch(str(report['new_image']))):
            raise ValueError('FA-16 requires a target two-image upgrade and rollback drill')
    elif gate.fa_id == 'FA-18':
        report = _kind_report(paths, 'capacity')
        if report.get('status') != 'passed' or report.get('formal') is not True or report.get('duration_seconds', 0) < 1800:
            raise ValueError('FA-18 requires a passed formal target capacity report')
    elif gate.fa_id == 'FA-19':
        report = _kind_report(paths, 'quality')
        if (report.get('status') != 'passed' or report.get('dataset_role') != 'real_holdout' or
                report.get('thresholds_frozen_before_run') is not True or report.get('scope_leaks') != 0 or
                report.get('severe_errors') != 0):
            raise ValueError('FA-19 requires a threshold-frozen real holdout report')
    elif gate.fa_id == 'FA-20':
        report = _kind_report(paths, 'browser')
        if report.get('status') != 'passed':
            raise ValueError('FA-20 requires passed browser evidence')
    elif gate.fa_id == 'FA-21':
        report = _kind_report(paths, 'pilot')
        if (report.get('status') != 'passed' or report.get('thresholds_frozen_before_start') is not True or
                report.get('real_user_count', 0) < 1 or report.get('task_count', 0) < 1 or
                report.get('high_severity_errors') != 0 or not report.get('responsible_signoff')):
            raise ValueError('FA-21 requires real-user outcomes and responsible signoff')


def validate_acceptance(manifest_path: Path, repository: Path, traceability: Path) -> dict[str, object]:
    manifest = AcceptanceManifest.model_validate_json(manifest_path.read_text(encoding='utf-8-sig'))
    mapping = acceptance_mapping(traceability)
    expected = {f'FA-{number:02}' for number in range(1, 22)}
    if set(mapping) != expected or {gate.fa_id for gate in manifest.gates} != expected:
        raise ValueError('Acceptance manifest must cover FA-01 through FA-21 exactly once')
    environment_path = _artifact(repository, manifest.environment)
    environment = _json(environment_path)
    if _contains_sensitive_key(environment):
        raise ValueError('Environment evidence contains a sensitive field name')
    environment_record = CapacityEnvironment.model_validate_json(environment_path.read_text(encoding='utf-8-sig'))
    if environment_record.scope != manifest.scope:
        raise ValueError('Environment and acceptance scopes differ')
    statuses = {}
    artifact_count = 1
    for gate in manifest.gates:
        expected_tasks = mapping[gate.fa_id]['tasks']
        allowed_requirements = mapping[gate.fa_id]['requirements']
        if set(gate.task_ids) != expected_tasks:
            raise ValueError(f'{gate.fa_id} task mapping differs from traceability.csv')
        if not set(gate.requirement_ids) <= allowed_requirements:
            raise ValueError(f'{gate.fa_id} requirement mapping differs from traceability.csv')
        paths: dict[str, list[Path]] = {}
        for artifact in gate.artifacts:
            paths.setdefault(artifact.kind, []).append(_artifact(repository, artifact))
            artifact_count += 1
        if manifest.scope == 'target-release' and gate.status == 'PASS':
            _target_gate(gate, paths)
        statuses[gate.fa_id] = gate.status
    failed = sorted(identifier for identifier, status in statuses.items() if status == 'FAIL')
    blocked = sorted(identifier for identifier, status in statuses.items() if status == 'SKIPPED-BLOCKED')
    if manifest.scope == 'local-readiness' and 'LOCAL_SCOPE_NOT_RELEASE' not in blocked:
        blocked.append('LOCAL_SCOPE_NOT_RELEASE')
    status = 'failed' if failed else ('blocked' if blocked else 'passed')
    return {'status': status, 'scope': manifest.scope, 'run_id': manifest.run_id,
            'passed': sum(value == 'PASS' for value in statuses.values()), 'failed': failed,
            'blocked': blocked, 'artifact_count': artifact_count,
            'environment_sha256': manifest.environment.sha256, 'gates': statuses}
