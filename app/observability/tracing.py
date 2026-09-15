from collections.abc import Sequence

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

from .logging import event


class OperationalSpanExporter(SpanExporter):
    """Export only correlation/timing, never exception events or arbitrary attrs."""
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        for span in spans:
            context = span.context
            if context is None or span.start_time is None or span.end_time is None:
                continue
            event('span', service='telemetry', route=span.name,
                  latency_ms=(span.end_time - span.start_time) / 1_000_000,
                  trace_id=f'{context.trace_id:032x}', span_id=f'{context.span_id:016x}',
                  parent_span_id=f'{span.parent.span_id:016x}' if span.parent else None)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


def provider(service: str) -> TracerProvider:
    result = TracerProvider(resource=Resource({'service.name': service, 'service.version': '0.3.0-dev'}))
    result.add_span_processor(BatchSpanProcessor(OperationalSpanExporter(), max_queue_size=1024,
        max_export_batch_size=128, schedule_delay_millis=1000, export_timeout_millis=2000))
    return result
