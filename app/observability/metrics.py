import shutil
from pathlib import Path

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, ProcessCollector, generate_latest
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import func, select, text

from app.db.models import AuditEvent, Job, JobAttempt
from app.db.session import Database

STAGES = ('queue_ms', 'retrieval_ms', 'context_ms', 'model_wait_ms', 'embedding_ms', 'generation_ms', 'citation_ms', 'total_ms')


class DatabaseCollector:
    def __init__(self, database: Database, objects: Path):
        self.database, self.objects = database, objects

    def collect(self):
        available = GaugeMetricFamily('tracedesk_metrics_database_available', 'DB metrics scrape succeeded', value=0)
        try:
            with self.database.transaction() as session:
                states = session.execute(select(Job.type, Job.state, func.count()).group_by(Job.type, Job.state)).all()
                expired = session.scalar(select(func.count()).select_from(Job).where(
                    Job.state == 'running', Job.lease_expires_at <= func.clock_timestamp()))
                failures = session.execute(select(JobAttempt.error_code, func.count()).where(
                    JobAttempt.finished_at > func.clock_timestamp() - text("INTERVAL '15 minutes'"),
                    JobAttempt.error_code.is_not(None)).group_by(JobAttempt.error_code)).all()
                denials = session.execute(select(AuditEvent.action, func.count()).where(AuditEvent.outcome == 'denied',
                    AuditEvent.occurred_at > func.clock_timestamp() - text("INTERVAL '15 minutes'"))
                    .group_by(AuditEvent.action)).all()
                timings = []
                for stage in STAGES:
                    # stage names are a constant allowlist; payloads never become SQL.
                    row = session.execute(text(f"""SELECT count(*),
                        percentile_cont(0.5) WITHIN GROUP (ORDER BY (latencies->>'{stage}')::float / 1000),
                        percentile_cont(0.95) WITHIN GROUP (ORDER BY (latencies->>'{stage}')::float / 1000),
                        percentile_cont(0.99) WITHIN GROUP (ORDER BY (latencies->>'{stage}')::float / 1000)
                        FROM query_runs WHERE created_at > clock_timestamp() - INTERVAL '24 hours'
                        AND jsonb_typeof(latencies->'{stage}') = 'number'""")).one()
                    timings.append((stage.removesuffix('_ms'), row))
            jobs = GaugeMetricFamily('tracedesk_jobs', 'Current jobs by bounded type and state', labels=['type', 'state'])
            for kind, state, count in states:
                jobs.add_metric([kind, state], count)
            yield jobs
            yield GaugeMetricFamily('tracedesk_jobs_expired_leases', 'Running jobs with an expired lease', value=expired)
            errors = GaugeMetricFamily('tracedesk_job_errors_15m', 'Attempt errors within 15 minutes', labels=['code'])
            for code, count in failures:
                errors.add_metric([code], count)
            yield errors
            denied = GaugeMetricFamily('tracedesk_audit_denials_15m', 'Security denials within 15 minutes', labels=['action'])
            for action, count in denials:
                denied.add_metric([action], count)
            yield denied
            latency = GaugeMetricFamily('tracedesk_query_latency_24h_seconds', 'Completed query stage quantiles over 24h', labels=['stage', 'quantile'])
            samples = GaugeMetricFamily('tracedesk_query_latency_24h_samples', 'Completed query timing samples over 24h', labels=['stage'])
            for stage, row in timings:
                samples.add_metric([stage], row[0])
                for quantile, value in zip(('0.5', '0.95', '0.99'), row[1:]):
                    if value is not None:
                        latency.add_metric([stage, quantile], value)
            yield latency
            yield samples
            available = GaugeMetricFamily('tracedesk_metrics_database_available', 'DB metrics scrape succeeded', value=1)
        except Exception:
            pass
        yield available
        try:
            disk = shutil.disk_usage(self.objects)
            yield GaugeMetricFamily('tracedesk_storage_free_bytes', 'Available bytes on the objects volume', value=disk.free)
            yield GaugeMetricFamily('tracedesk_storage_free_ratio', 'Available fraction of the objects volume', value=disk.free / disk.total)
        except OSError:
            yield GaugeMetricFamily('tracedesk_storage_probe_error', 'Objects volume cannot be inspected', value=1)


class Metrics:
    def __init__(self, database: Database, objects: Path):
        self.registry = CollectorRegistry()
        ProcessCollector(registry=self.registry)
        self.registry.register(DatabaseCollector(database, objects))
        self.requests = Counter('tracedesk_http_requests', 'HTTP requests', ['method', 'route', 'status'], registry=self.registry)
        self.latency = Histogram('tracedesk_http_request_seconds', 'HTTP handler latency', ['method', 'route'],
            buckets=(.01, .05, .1, .25, .5, 1, 2, 5, 15, 30, 120), registry=self.registry)
        self.inflight = Gauge('tracedesk_http_inflight', 'Current HTTP handlers', registry=self.registry)

    def render(self) -> bytes:
        return generate_latest(self.registry)
