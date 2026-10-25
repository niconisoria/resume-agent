import logging

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import tracing
from app.main import app
from tests.helpers import frames

http = TestClient(app)
_memory = InMemorySpanExporter()
_installed = False


@pytest.fixture
def spans():
    """Global test tracer provider (set once per process) exporting to memory."""
    global _installed
    if not _installed:
        provider = tracing.build_provider(
            tracing.RedactingExporter(_memory), project="test", batch=False
        )
        trace.set_tracer_provider(provider)
        _installed = True
    _memory.clear()
    return _memory


def attrs(span):
    return dict(span.attributes or {})


def all_text(memory):
    return " ".join(
        str(v) for s in memory.get_finished_spans() for v in attrs(s).values()
    )


def by_name(memory, name):
    return [s for s in memory.get_finished_spans() if s.name == name]


def test_chat_is_one_trace_with_tool_steps(model, spans):
    model(
        turns=[
            {"tool_calls": [("search_knowledge", {"query": "ruby"})]},
            {"chunks": ["Six years."]},
        ]
    )
    final = frames(http.post("/chat", json={"message": "Ruby years?"}))[-1]
    (chat,) = by_name(spans, "chat")
    (tool,) = by_name(spans, "tool.search_knowledge")
    assert tool.context.trace_id == chat.context.trace_id
    assert tool.parent.span_id == chat.context.span_id
    assert attrs(chat)["openinference.span.kind"] == "CHAIN"
    assert attrs(chat)["session.id"] == final["conversation_id"]
    assert attrs(chat)["input.value"] == "Ruby years?"
    assert attrs(chat)["output.value"] == "Six years."
    assert attrs(tool)["openinference.span.kind"] == "TOOL"
    assert attrs(tool)["tool.name"] == "search_knowledge"
    assert '"ruby"' in attrs(tool)["input.value"]
    assert attrs(tool)["output.value"] == "No relevant knowledge found."


def test_emails_redacted_in_exported_spans(model, spans):
    args = {"summary": "s", "reason": "r", "contact_email": "ana@example.com"}
    model(turns=[{"tool_calls": [("escalate_to_human", args)]}, {"chunks": ["ok"]}])
    http.post("/chat", json={"message": "Email me at ana@example.com"})
    text = all_text(spans)
    assert "ana@example.com" not in text
    assert "[email]" in text


def test_no_ip_in_spans(model, spans):
    model(chunks=["hi"])
    http.post("/chat", json={"message": "hi"}, headers={"Fly-Client-IP": "203.0.113.9"})
    assert by_name(spans, "chat")
    assert "203.0.113.9" not in all_text(spans)


def test_failed_export_never_raises(caplog):
    class Broken:
        def export(self, spans):
            raise ConnectionError("phoenix down")

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    exporter = tracing.RedactingExporter(Broken())
    with caplog.at_level(logging.WARNING):
        assert exporter.export([]) == SpanExportResult.FAILURE
    assert "phoenix down" in caplog.text or "export failed" in caplog.text


def test_missing_settings_means_tracing_off(monkeypatch):
    monkeypatch.delenv("PHOENIX_COLLECTOR_ENDPOINT", raising=False)
    monkeypatch.delenv("PHOENIX_API_KEY", raising=False)
    assert tracing.setup() is False


def test_project_name_from_env(monkeypatch):
    monkeypatch.setenv("PHOENIX_PROJECT_NAME", "resume-agent")
    assert tracing.project_name() == "resume-agent"
    monkeypatch.delenv("PHOENIX_PROJECT_NAME")
    assert tracing.project_name() == "resume-agent-dev"
    provider = tracing.build_provider(InMemorySpanExporter(), project="p", batch=False)
    assert provider.resource.attributes["openinference.project.name"] == "p"


def test_emails_redacted_in_span_events(spans):
    with trace.get_tracer("t").start_as_current_span("boom") as span:
        span.add_event(
            "exception", {"exception.message": "bad contact ana@example.com"}
        )
    (boom,) = by_name(spans, "boom")
    assert boom.events[0].attributes["exception.message"] == "bad contact [email]"


def test_emails_redacted_in_span_status(spans):
    # An exception escaping a span sets status "<Type>: <message>" as well as the event.
    with pytest.raises(ValueError), trace.get_tracer("t").start_as_current_span("boom"):
        raise ValueError("bad contact ana@example.com")
    (boom,) = by_name(spans, "boom")
    assert boom.status.description == "ValueError: bad contact [email]"
    assert not boom.status.is_ok


def test_sdk_tracing_off_so_llm_calls_not_doubled():
    from app import agent

    assert agent.CLIENT_OPTIONS["open_telemetry"] is False


def test_health_checks_not_traced(spans):
    assert http.get("/health").status_code == 200
    names = [s.name for s in spans.get_finished_spans()]
    assert not any("health" in n for n in names), names


def test_fastapi_operation_spans_off(model, spans):
    model(chunks=["hi"])
    http.post("/chat", json={"message": "hi"})
    names = {s.name for s in spans.get_finished_spans()}
    assert "chat" in names
    assert not {"fastapi.endpoint", "fastapi.dependencies"} & names
