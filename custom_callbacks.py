"""LiteLLM custom callback that logs every LLM call to a JSONL file.

The logger writes structured records to /logs/{TASK_ID}/requests.jsonl.
Secrets are recursively redacted before serialization.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from litellm.integrations.custom_logger import CustomLogger


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

REDACT_KEYS = {
    "authorization",
    "api_key",
    "api-key",
    "apikey",
    "openai_api_key",
    "cookie",
    "set-cookie",
    "token",
    "bearer",
    "master_key",
    "litellm_master_key",
    "password",
    "secret",
    "x-api-key",
    "proxy-authorization",
}

# Strings that look like API keys / bearer tokens.
_SECRET_PATTERNS = [
    re.compile(r"^sk-[A-Za-z0-9_\-]{8,}$"),
    re.compile(r"^Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"^xoxb-[A-Za-z0-9\-]+$"),
    re.compile(r"^ghp_[A-Za-z0-9]{20,}$"),
]

REDACTED = "***REDACTED***"


def _looks_like_secret(value: str) -> bool:
    if len(value) < 12:
        return False
    return any(p.search(value) for p in _SECRET_PATTERNS)


def _redact(obj: Any, _depth: int = 0) -> Any:
    """Recursively redact secret-looking keys and values.

    The depth guard protects against pathological / cyclic objects.
    """
    if _depth > 20:
        return "***TRUNCATED_DEPTH***"

    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            key_str = str(k)
            if key_str.lower() in REDACT_KEYS:
                out[key_str] = REDACTED
            else:
                out[key_str] = _redact(v, _depth + 1)
        return out

    if isinstance(obj, (list, tuple)):
        return [_redact(v, _depth + 1) for v in obj]

    if isinstance(obj, str):
        return REDACTED if _looks_like_secret(obj) else obj

    return obj


# ---------------------------------------------------------------------------
# Safe JSON serialization
# ---------------------------------------------------------------------------

def _to_jsonable(obj: Any, _depth: int = 0) -> Any:
    """Best-effort conversion of arbitrary objects into JSON-serializable forms."""
    if _depth > 20:
        return "***TRUNCATED_DEPTH***"

    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj

    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v, _depth + 1) for k, v in obj.items()}

    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_to_jsonable(v, _depth + 1) for v in obj]

    if isinstance(obj, datetime):
        return obj.isoformat()

    # Pydantic v2 / v1
    for attr in ("model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return _to_jsonable(fn(), _depth + 1)
            except Exception:
                pass

    # Generic objects: fall back to __dict__ or repr.
    if hasattr(obj, "__dict__"):
        try:
            return _to_jsonable(vars(obj), _depth + 1)
        except Exception:
            pass

    try:
        return json.loads(json.dumps(obj, default=str))
    except Exception:
        return repr(obj)


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class RolloutLogger(CustomLogger):
    """Append one JSON record per LLM call to /logs/{TASK_ID}/requests.jsonl."""

    def __init__(self) -> None:
        super().__init__()
        self.task_id = os.environ.get("TASK_ID", "task_001")
        self.session_id = os.environ.get("SESSION_ID", "session_001")
        self.log_dir = Path(os.environ.get("LOG_DIR", "/logs")) / self.task_id
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / "requests.jsonl"
        self._lock = threading.Lock()

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _iso(ts: Any) -> str | None:
        if ts is None:
            return None
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts.isoformat()
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        return str(ts)

    @staticmethod
    def _latency_ms(start: Any, end: Any) -> float | None:
        if isinstance(start, datetime) and isinstance(end, datetime):
            return (end - start).total_seconds() * 1000.0
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            return (end - start) * 1000.0
        return None

    def _write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()

    def _build_record(
        self,
        kwargs: dict[str, Any] | None,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
        status: str,
        error: str | None,
    ) -> dict[str, Any]:
        kwargs = kwargs or {}

        litellm_params = kwargs.get("litellm_params") or {}
        metadata = (
            litellm_params.get("metadata")
            or kwargs.get("metadata")
            or {}
        )
        request_id = (
            metadata.get("request_id")
            or kwargs.get("litellm_call_id")
            or kwargs.get("id")
            or str(uuid.uuid4())
        )

        usage: Any = None
        response_cost = kwargs.get("response_cost")
        if response_obj is not None:
            usage_attr = getattr(response_obj, "usage", None)
            if usage_attr is None and isinstance(response_obj, dict):
                usage_attr = response_obj.get("usage")
            usage = _to_jsonable(usage_attr) if usage_attr is not None else None

        record = {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "request_id": request_id,
            "timestamp_start": self._iso(start_time),
            "timestamp_end": self._iso(end_time),
            "latency_ms": self._latency_ms(start_time, end_time),
            "model": kwargs.get("model"),
            "call_type": kwargs.get("call_type"),
            "kwargs": _redact(_to_jsonable(kwargs)),
            "response_obj": _redact(_to_jsonable(response_obj)) if status == "success" else None,
            "status": status,
            "error": error,
            "usage": usage if status == "success" else None,
            "response_cost": response_cost if status == "success" else None,
        }
        return record

    def _safe_log(
        self,
        kwargs: dict[str, Any] | None,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
        status: str,
        error: str | None = None,
    ) -> None:
        try:
            record = self._build_record(kwargs, response_obj, start_time, end_time, status, error)
            self._write(record)
        except Exception as e:
            # Never let logging break the proxy.
            try:
                fallback = {
                    "task_id": self.task_id,
                    "session_id": self.session_id,
                    "status": "logger_error",
                    "error": f"{type(e).__name__}: {e}",
                    "timestamp_end": datetime.now(tz=timezone.utc).isoformat(),
                }
                self._write(fallback)
            except Exception:
                pass

    # -- LiteLLM hook methods --------------------------------------------

    def log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: D401
        self._safe_log(kwargs, response_obj, start_time, end_time, "success")

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):  # noqa: D401
        error = kwargs.get("exception") if isinstance(kwargs, dict) else None
        self._safe_log(kwargs, None, start_time, end_time, "failure", error=str(error) if error else None)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._safe_log(kwargs, response_obj, start_time, end_time, "success")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        error = kwargs.get("exception") if isinstance(kwargs, dict) else None
        self._safe_log(kwargs, None, start_time, end_time, "failure", error=str(error) if error else None)


# LiteLLM looks up this attribute from litellm_config.yaml.
proxy_handler_instance = RolloutLogger()
