"""Export traces to OpenTelemetry (OTLP/JSON) and configure structured logging."""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from typing import Any

from cairn.observability.trace import Span


def _hex_id(value: str, size: int) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[: size * 2]


def to_otlp(root: Span, service_name: str = "cairn") -> dict[str, Any]:
    """Convert a span tree to the OTLP/JSON trace format.

    The output can be POSTed to any collector's ``/v1/traces`` endpoint, so
    Cairn traces show up in Jaeger, Tempo, Honeycomb and similar backends
    without a hard dependency on the OpenTelemetry SDK.
    """
    trace_id = _hex_id(root.span_id, 16)
    spans: list[dict[str, Any]] = []

    def visit(span: Span, parent: str | None) -> None:
        span_id = _hex_id(f"{root.span_id}/{span.span_id}", 8)
        end = span.end if span.end is not None else span.start
        spans.append({
            "traceId": trace_id,
            "spanId": span_id,
            **({"parentSpanId": parent} if parent else {}),
            "name": span.name,
            "kind": 1,
            "startTimeUnixNano": str(int(span.start * 1e9)),
            "endTimeUnixNano": str(int(end * 1e9)),
            "status": {"code": 2 if span.status in ("error", "failed", "cancelled", "interrupted") else 1},
            "attributes": [
                {"key": f"cairn.{k}", "value": {"stringValue": json.dumps(v, default=str)
                                                if not isinstance(v, str) else v}}
                for k, v in {"kind": span.kind, "status": span.status, **span.attributes}.items()
                if v is not None
            ],
        })
        for child in span.children:
            visit(child, span_id)

    visit(root, None)
    return {
        "resourceSpans": [{
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service_name}}]},
            "scopeSpans": [{"scope": {"name": "cairn"}, "spans": spans}],
        }]
    }


class JsonFormatter(logging.Formatter):
    """One JSON object per line: machine-parseable logs for production."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 6),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("run_id", "node_id", "job_id", "worker_id"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if json_logs else logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("cairn")
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    root.propagate = False
