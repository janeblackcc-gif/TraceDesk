"""Validation and aggregation for privacy-preserving real-user pilot evidence."""
from __future__ import annotations

import hashlib
import statistics
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SHA256 = r"^[0-9a-f]{64}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PilotParticipant(StrictModel):
    participant_id: str = Field(min_length=1, max_length=100)
    role: str = Field(min_length=1, max_length=100)
    consent_reference: str = Field(min_length=1, max_length=300)
    started_at: datetime
    real_user: Literal[True]

    @model_validator(mode="after")
    def timestamp_has_timezone(self) -> PilotParticipant:
        if self.started_at.utcoffset() is None:
            raise ValueError("Participant timestamp must include a timezone")
        return self


class PilotTask(StrictModel):
    timestamp: datetime
    participant_id: str = Field(min_length=1, max_length=100)
    task_id: str = Field(min_length=1, max_length=200)
    outcome: Literal["success", "failure", "abandoned"]
    verification_seconds: float = Field(ge=0, le=86400)
    high_severity_error: bool
    severe_error_category: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def coherent(self) -> PilotTask:
        if self.timestamp.utcoffset() is None:
            raise ValueError("Pilot task timestamp must include a timezone")
        if self.high_severity_error != (self.severe_error_category is not None):
            raise ValueError("High-severity errors require exactly one category marker")
        return self


class PilotThresholds(StrictModel):
    schema_version: Literal[1]
    status: Literal["frozen"]
    created_at: datetime
    frozen_at: datetime
    minimum_task_success_rate: float = Field(ge=0, le=1)
    maximum_median_verification_seconds: float = Field(gt=0, le=86400)
    minimum_tasks_per_user: int = Field(ge=1)
    maximum_high_severity_errors: Literal[0]

    @model_validator(mode="after")
    def coherent(self) -> PilotThresholds:
        if self.created_at.utcoffset() is None or self.frozen_at.utcoffset() is None:
            raise ValueError("Pilot threshold timestamps must include a timezone")
        if self.frozen_at < self.created_at:
            raise ValueError("Pilot threshold freeze cannot precede creation")
        return self


class PilotManifest(StrictModel):
    format_version: Literal[1]
    run_id: str = Field(min_length=1, max_length=200)
    scope: Literal["real-user-pilot"]
    started_at: datetime
    finished_at: datetime
    authorized_real_materials: Literal[True]
    authorization_reference: str = Field(min_length=1, max_length=300)
    thresholds_path: str
    thresholds_sha256: str = Field(pattern=SHA256)
    participants_path: str
    participants_sha256: str = Field(pattern=SHA256)
    tasks_path: str
    tasks_sha256: str = Field(pattern=SHA256)
    responsible_signoff: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent(self) -> PilotManifest:
        timestamps = (self.started_at, self.finished_at)
        if any(value.utcoffset() is None for value in timestamps):
            raise ValueError("Pilot timestamps must include a timezone")
        if self.finished_at <= self.started_at:
            raise ValueError("Pilot finish must follow its start")
        if len(set(self.responsible_signoff)) != len(self.responsible_signoff):
            raise ValueError("Responsible signoffs must be distinct")
        for value in (self.thresholds_path, self.participants_path, self.tasks_path):
            path = PurePosixPath(value)
            if not value or "\\" in value or path.is_absolute() or ".." in path.parts:
                raise ValueError("Pilot evidence paths must be normalized and relative")
        if len({self.thresholds_path, self.participants_path, self.tasks_path}) != 3:
            raise ValueError("Threshold, participant and task evidence must be separate")
        return self


def _file(root: Path, relative: str) -> Path:
    path = root / relative
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Pilot evidence cannot use symlinks")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Pilot evidence file is missing or outside the run")
    return path


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _jsonl(path: Path, model: type[PilotParticipant] | type[PilotTask]) -> list[PilotParticipant | PilotTask]:
    rows = [model.model_validate_json(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if not rows:
        raise ValueError("Pilot evidence cannot be empty")
    return rows


def evaluate_pilot(directory: Path) -> dict[str, object]:
    root = directory.resolve()
    if directory.is_symlink() or not root.is_dir():
        raise ValueError("Pilot run must be a real directory")
    manifest = PilotManifest.model_validate_json((root / "pilot_manifest.json").read_text(encoding="utf-8-sig"))
    thresholds_path = _file(root, manifest.thresholds_path)
    participants_path = _file(root, manifest.participants_path)
    tasks_path = _file(root, manifest.tasks_path)
    if (
        _sha256(thresholds_path) != manifest.thresholds_sha256
        or _sha256(participants_path) != manifest.participants_sha256
        or _sha256(tasks_path) != manifest.tasks_sha256
    ):
        raise ValueError("Pilot evidence hash mismatch")
    thresholds = PilotThresholds.model_validate_json(thresholds_path.read_text(encoding="utf-8-sig"))
    if thresholds.frozen_at >= manifest.started_at:
        raise ValueError("Pilot thresholds must be frozen before the pilot starts")
    participant_rows = _jsonl(participants_path, PilotParticipant)
    task_rows = _jsonl(tasks_path, PilotTask)
    participants = [row for row in participant_rows if isinstance(row, PilotParticipant)]
    tasks = [row for row in task_rows if isinstance(row, PilotTask)]
    participant_ids = {row.participant_id for row in participants}
    if len(participant_ids) != len(participants):
        raise ValueError("Pilot participant IDs must be unique")
    if any(row.participant_id not in participant_ids for row in tasks):
        raise ValueError("Pilot task references an unknown participant")
    start = manifest.started_at.astimezone(timezone.utc)
    finish = manifest.finished_at.astimezone(timezone.utc)
    if any(not start <= row.timestamp.astimezone(timezone.utc) <= finish for row in tasks):
        raise ValueError("Pilot task falls outside the declared window")
    counts = {identifier: sum(row.participant_id == identifier for row in tasks) for identifier in participant_ids}
    task_successes = sum(row.outcome == "success" for row in tasks)
    success_rate = task_successes / len(tasks)
    median_verification = statistics.median(row.verification_seconds for row in tasks)
    high_errors = sum(row.high_severity_error for row in tasks)
    failures = []
    if min(counts.values()) < thresholds.minimum_tasks_per_user:
        failures.append("TASKS_PER_USER_BELOW_THRESHOLD")
    if success_rate < thresholds.minimum_task_success_rate:
        failures.append("TASK_SUCCESS_RATE_BELOW_THRESHOLD")
    if median_verification > thresholds.maximum_median_verification_seconds:
        failures.append("MEDIAN_VERIFICATION_TIME_EXCEEDED")
    if high_errors:
        failures.append("HIGH_SEVERITY_ERROR_OBSERVED")
    return {
        "status": "failed" if failures else "passed",
        "scope": manifest.scope,
        "run_id": manifest.run_id,
        "thresholds_frozen_before_start": True,
        "real_user_count": len(participants),
        "task_count": len(tasks),
        "successful_tasks": task_successes,
        "task_success_rate": round(success_rate, 6),
        "median_verification_seconds": median_verification,
        "high_severity_errors": high_errors,
        "responsible_signoff": manifest.responsible_signoff,
        "failures": sorted(failures),
    }
