"""OpenTelemetry GenAI semantic-convention spans.

Follows https://opentelemetry.io/docs/specs/semconv/gen-ai/ (v1.28+):
span name "chat {model}", attrs `gen_ai.*`.

Extensions (gateway-specific, prefixed `gen_ai.routing.*` / `gen_ai.cot.*`)
are applied to describe routing decisions and CoT-E engagement.

If `OTEL_EXPORTER_OTLP_ENDPOINT` is unset, `init_tracer()` is a no-op and
`llm_span()` returns a no-op context manager — zero cost in dev/test.
"""
import logging
import os
from contextlib import contextmanager
from typing import Any, Optional

logger = logging.getLogger(__name__)

_tracer = None  # set by init_tracer() when OTLP endpoint configured


class _NoopSpan:
    def set_attribute(self, *_args, **_kwargs): pass
    def set_status(self, *_args, **_kwargs): pass
    def record_exception(self, *_args, **_kwargs): pass
    def end(self): pass


NOOP_SPAN = _NoopSpan()


def init_tracer(service_name: str = "llm-proxy", version: str = "0") -> None:
    """Configure OTLP HTTP exporter if endpoint env var is set; else no-op."""
    global _tracer

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        logger.info("otel.disabled — OTEL_EXPORTER_OTLP_ENDPOINT unset")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        resource = Resource.create({
            "service.name": service_name,
            "service.version": version,
        })
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(service_name)
        logger.info("otel.enabled endpoint=%s", endpoint)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("otel.init_failed %s", exc)


@contextmanager
def llm_span(
    *,
    operation: str,
    provider_type: str,
    requested_model: str,
    resolved_model: str,
    lmrh_hint: Optional[str] = None,
    cot_engaged: bool = False,
    unmet_hints: Optional[list[str]] = None,
    extra: Optional[dict[str, Any]] = None,
):
    """Emit a GenAI span. Usage:
        with llm_span(...) as span:
            ...
            span.set_attribute("gen_ai.usage.input_tokens", 42)
    """
    if _tracer is None:
        yield NOOP_SPAN
        return

    span_name = f"{operation} {resolved_model}"
    with _tracer.start_as_current_span(span_name) as span:
        span.set_attribute("gen_ai.operation.name", operation)
        span.set_attribute("gen_ai.provider.name", provider_type)
        span.set_attribute("gen_ai.request.model", requested_model or resolved_model)
        span.set_attribute("gen_ai.response.model", resolved_model)
        if lmrh_hint:
            span.set_attribute("gen_ai.routing.lmrh_hint", lmrh_hint)
        if unmet_hints:
            span.set_attribute("gen_ai.routing.unmet_hints", list(unmet_hints))
        span.set_attribute("gen_ai.cot.engaged", cot_engaged)
        if extra:
            for k, v in extra.items():
                span.set_attribute(k, v)
        yield span
