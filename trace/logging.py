"""Structured, correlated, redacted logging for the service (production hardening).

One JSON object per line, so any log stack (Loki, CloudWatch, a plain ``jq``)
can query it. Every record carries the correlation fields bound for the
current unit of work -- ``workflow_id``, ``step``, ``attempt``, ``request_id``,
``worker_id`` -- through ``contextvars``, so a line written deep inside the ERP
client still says which workflow it belongs to.

Redaction is applied to the rendered message and every extra field: ERPNext
``token key:secret`` headers, ``Bearer`` tokens, API keys and passwords in
``key=value`` form and in DSNs. Secrets reach this module only by accident;
the filter makes the accident harmless.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_context: ContextVar[dict[str, Any] | None] = ContextVar("veritas_log_context", default=None)

_REDACTIONS = [
    (re.compile(r"(token\s+)[^\s:]+:[^\s\"']+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(bearer\s+)[A-Za-z0-9._~+/=-]+", re.I), r"\1[REDACTED]"),
    (
        re.compile(
            r"((?:api[_-]?key|api[_-]?secret|secret|password|passwd|pwd|token)\s*[=:]\s*)"
            r"[^\s,;&\"']+",
            re.I,
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(://[^:/\s]+:)[^@/\s]+(@)"), r"\1[REDACTED]\2"),
]

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message"}


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Bind correlation fields for everything logged inside the block."""
    merged = {**(_context.get() or {}), **{k: v for k, v in fields.items() if v is not None}}
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)


def current_context() -> dict[str, Any]:
    return dict(_context.get() or {})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
            **current_context(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                out[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(out, default=str)


class PlainFormatter(logging.Formatter):
    """Human-readable for a terminal, still redacted and still correlated."""

    def format(self, record: logging.LogRecord) -> str:
        ctx = " ".join(f"{k}={v}" for k, v in current_context().items())
        line = f"{record.levelname:<7} {record.name}: {redact(record.getMessage())}"
        return f"{line}  [{ctx}]" if ctx else line


def setup_logging(level: str = "INFO", json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else PlainFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Dependencies log request lines that can carry headers; keep them quiet.
    for noisy in ("urllib3", "httpx", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
