import logging
import os
from collections.abc import Sequence

from openinference.instrumentation.anthropic import AnthropicInstrumentor
from openinference.semconv.resource import ResourceAttributes
from openinference.semconv.trace import SpanAttributes
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from app.handoff import redact_emails

log = logging.getLogger(__name__)

tracer = trace.get_tracer("resume-agent")

# OpenInference attribute names Phoenix groups and renders by.
KIND = SpanAttributes.OPENINFERENCE_SPAN_KIND
SESSION_ID = SpanAttributes.SESSION_ID
INPUT = SpanAttributes.INPUT_VALUE
OUTPUT = SpanAttributes.OUTPUT_VALUE
TOOL_NAME = SpanAttributes.TOOL_NAME


def project_name() -> str:
    return os.environ.get("PHOENIX_PROJECT_NAME", "resume-agent-dev")


def _redact(value):
    if isinstance(value, str):
        return redact_emails(value)
    if isinstance(value, tuple | list):
        return type(value)(_redact(v) for v in value)
    return value


def _redact_attrs(attributes) -> dict:
    return {k: _redact(v) for k, v in (attributes or {}).items()}


class RedactingExporter(SpanExporter):
    """Wraps an exporter: emails → [email] in span attributes; export errors never raise."""

    def __init__(self, inner: SpanExporter):
        self.inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            return self.inner.export([self._redacted(s) for s in spans])
        except Exception:
            log.warning("trace export failed", exc_info=True)
            return SpanExportResult.FAILURE

    @staticmethod
    def _redacted(span: ReadableSpan) -> ReadableSpan:
        return ReadableSpan(
            name=span.name,
            context=span.context,
            parent=span.parent,
            resource=span.resource,
            attributes=_redact_attrs(span.attributes),
            # Events carry exception messages, which can quote visitor text too.
            events=[
                Event(e.name, _redact_attrs(e.attributes), e.timestamp)
                for e in span.events
            ],
            links=span.links,
            kind=span.kind,
            status=span.status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


def build_provider(
    exporter: SpanExporter, project: str, batch: bool = True
) -> TracerProvider:
    provider = TracerProvider(
        resource=Resource({ResourceAttributes.PROJECT_NAME: project})
    )
    # Batch: export runs on a background thread, so a slow Phoenix never slows a reply.
    processor = BatchSpanProcessor(exporter) if batch else SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    return provider


def setup() -> bool:
    """Send traces to Phoenix Cloud; without settings, tracing stays off (no-op)."""
    endpoint = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "").rstrip("/")
    api_key = os.environ.get("PHOENIX_API_KEY")
    if not endpoint or not api_key:
        log.info("tracing off: PHOENIX_COLLECTOR_ENDPOINT / PHOENIX_API_KEY not set")
        return False
    if not endpoint.endswith("/v1/traces"):
        endpoint += "/v1/traces"
    exporter = OTLPSpanExporter(
        endpoint=endpoint, headers={"authorization": f"Bearer {api_key}"}, timeout=10
    )
    provider = build_provider(RedactingExporter(exporter), project_name())
    trace.set_tracer_provider(provider)
    AnthropicInstrumentor().instrument(tracer_provider=provider)
    return True


def shutdown() -> None:
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()  # flushes pending spans
