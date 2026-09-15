import json
import logging
from datetime import datetime, timezone
from uuid import UUID

LOGGER = logging.getLogger('tracedesk.operations')


def event(name: str, *, service: str = 'web', request_id: str | None = None, user_id: UUID | None = None,
          route: str | None = None, status: int | None = None, latency_ms: float | None = None,
          error_code: str | None = None, job_id: UUID | None = None, trace_id: str | None = None,
          span_id: str | None = None, parent_span_id: str | None = None) -> None:
    """Explicit fields prevent accidentally passing prompts/configs/exception text."""
    record = {'timestamp': datetime.now(timezone.utc).isoformat(), 'level': 'INFO', 'service': service,
              'version': '0.3.0-dev', 'event': name, 'request_id': request_id,
              'user_id': str(user_id) if user_id else None, 'route': route, 'status': status,
              'latency_ms': round(latency_ms, 3) if latency_ms is not None else None,
              'error_code': error_code, 'job_id': str(job_id) if job_id else None,
              'trace_id': trace_id, 'span_id': span_id, 'parent_span_id': parent_span_id}
    try:
        LOGGER.info(json.dumps({key: value for key, value in record.items() if value is not None}, ensure_ascii=True))
    except Exception:
        # Telemetry failures are not a reason to fail a business request. Security
        # audit events use database transactions and deliberately fail closed.
        pass


def configure() -> None:
    if not LOGGER.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(message)s'))
        LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
