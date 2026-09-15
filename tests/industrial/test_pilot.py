import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.operations.pilot import evaluate_pilot


def digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_pilot(root, *, high_error: bool = False) -> None:
    root.mkdir()
    start = datetime.now(timezone.utc).replace(microsecond=0)
    thresholds = root / "thresholds.json"
    thresholds.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "frozen",
                "created_at": (start - timedelta(minutes=2)).isoformat(),
                "frozen_at": (start - timedelta(minutes=1)).isoformat(),
                "minimum_task_success_rate": 0.8,
                "maximum_median_verification_seconds": 120.0,
                "minimum_tasks_per_user": 1,
                "maximum_high_severity_errors": 0,
            }
        ),
        encoding="utf-8",
    )
    participants = root / "participants.private.jsonl"
    participants.write_text(
        json.dumps(
            {
                "participant_id": "user-001",
                "role": "authorized tester",
                "consent_reference": "local-consent-record",
                "started_at": start.isoformat(),
                "real_user": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    tasks = root / "tasks.private.jsonl"
    tasks.write_text(
        json.dumps(
            {
                "timestamp": (start + timedelta(minutes=5)).isoformat(),
                "participant_id": "user-001",
                "task_id": "task-001",
                "outcome": "failure" if high_error else "success",
                "verification_seconds": 30.0,
                "high_severity_error": high_error,
                "severe_error_category": "wrong-scope" if high_error else None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "format_version": 1,
        "run_id": "fixture-pilot",
        "scope": "real-user-pilot",
        "started_at": start.isoformat(),
        "finished_at": (start + timedelta(minutes=10)).isoformat(),
        "authorized_real_materials": True,
        "authorization_reference": "local-authorization",
        "thresholds_path": thresholds.name,
        "thresholds_sha256": digest(thresholds),
        "participants_path": participants.name,
        "participants_sha256": digest(participants),
        "tasks_path": tasks.name,
        "tasks_sha256": digest(tasks),
        "responsible_signoff": ["owner-001"],
    }
    (root / "pilot_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_real_pilot_aggregate_passes_bounded_fixture(tmp_path) -> None:
    run = tmp_path / "pilot"
    write_pilot(run)
    report = evaluate_pilot(run)
    assert report["status"] == "passed"
    assert report["real_user_count"] == 1
    assert report["task_count"] == 1


def test_real_pilot_aggregate_rejects_high_severity_error(tmp_path) -> None:
    run = tmp_path / "pilot"
    write_pilot(run, high_error=True)
    report = evaluate_pilot(run)
    assert report["status"] == "failed"
    assert "HIGH_SEVERITY_ERROR_OBSERVED" in report["failures"]
