from __future__ import annotations

import logging
import time
import uuid
from typing import Any

_LOGGER = logging.getLogger(__name__)
_SKILL_TELEMETRY: dict[str, list[dict[str, object]]] = {}
_SKILL_TELEMETRY_STORE: object | None = None


def set_skill_telemetry_store(store: object | None) -> None:
    global _SKILL_TELEMETRY_STORE
    _SKILL_TELEMETRY_STORE = store


def record_skill_telemetry_event(payload: dict[str, object]) -> dict[str, object]:
    normalized = _normalize_payload(payload)
    skill_id = str(normalized.get("skill_id") or "").strip() or "__unknown__"
    rows = _SKILL_TELEMETRY.setdefault(skill_id, [])
    rows.append(normalized)
    _SKILL_TELEMETRY[skill_id] = rows[-500:]

    store = _SKILL_TELEMETRY_STORE
    recorder = getattr(store, "record_skill_telemetry", None)
    if callable(recorder):
        try:
            recorder(normalized)
        except Exception:
            _LOGGER.exception("failed to persist skill telemetry event")
    return normalized


def record_skill_error_event(
    *,
    skill_id: str,
    error_code: str,
    message: str,
    source: str = "",
    tool_id: str = "",
    trigger_source: str = "system",
    caller: str = "",
    trace_id: str = "",
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    return record_skill_telemetry_event(
        {
            "skill_id": skill_id or "__system__",
            "tool_id": tool_id,
            "trigger_source": trigger_source,
            "latency_ms": 0,
            "status": "error",
            "error_code": error_code or "skill_error",
            "error": message,
            "trace_id": trace_id or uuid.uuid4().hex[:12],
            "caller": caller or source or "system",
            "details": dict(details or {}),
            "created_at": int(time.time()),
        }
    )


def get_skill_telemetry_summary(skill_id: str) -> dict[str, object]:
    target = str(skill_id or "").strip()
    store = _SKILL_TELEMETRY_STORE
    summarizer = getattr(store, "skill_telemetry_summary", None)
    if callable(summarizer):
        try:
            summary = summarizer(target)
        except Exception:
            _LOGGER.exception("failed to read persistent skill telemetry")
        else:
            if int(summary.get("calls") or 0) > 0:
                return _normalize_summary(summary)
    return _summary_from_rows(_SKILL_TELEMETRY.get(target, []))


def _normalize_payload(payload: dict[str, object]) -> dict[str, object]:
    created_at = payload.get("created_at", payload.get("timestamp", 0))
    try:
        resolved_created_at = int(float(created_at or 0))
    except (TypeError, ValueError):
        resolved_created_at = int(time.time())
    if resolved_created_at <= 0:
        resolved_created_at = int(time.time())
    return {
        "skill_id": str(payload.get("skill_id") or "__unknown__").strip() or "__unknown__",
        "tool_id": str(payload.get("tool_id") or "").strip(),
        "trigger_source": str(payload.get("trigger_source") or "system").strip() or "system",
        "latency_ms": _safe_int(payload.get("latency_ms"), 0),
        "status": str(payload.get("status") or "error").strip() or "error",
        "error_code": str(payload.get("error_code") or "").strip(),
        "error": str(payload.get("error") or "")[:1000],
        "trace_id": str(payload.get("trace_id") or uuid.uuid4().hex[:12]).strip(),
        "caller": str(payload.get("caller") or "").strip(),
        "arg_summary": str(payload.get("arg_summary") or "")[:1000],
        "result_summary": str(payload.get("result_summary") or "")[:1000],
        "details": dict(payload.get("details") or {}) if isinstance(payload.get("details"), dict) else {},
        "created_at": resolved_created_at,
    }


def _summary_from_rows(rows: list[dict[str, object]]) -> dict[str, object]:
    ordered = sorted(rows, key=lambda item: int(item.get("created_at") or 0))
    calls = len(ordered)
    success = sum(1 for row in ordered if row.get("status") == "ok")
    failure = max(0, calls - success)
    last = dict(ordered[-1]) if ordered else {}
    return {
        "calls": calls,
        "success": success,
        "failure": failure,
        "success_rate": round(success / calls, 4) if calls else 0.0,
        "last": last,
        "never_succeeded": calls > 0 and success == 0,
    }


def _normalize_summary(summary: dict[str, object]) -> dict[str, object]:
    calls = _safe_int(summary.get("calls"), 0)
    success = _safe_int(summary.get("success"), 0)
    failure = _safe_int(summary.get("failure"), max(0, calls - success))
    last = summary.get("last")
    return {
        "calls": calls,
        "success": success,
        "failure": failure,
        "success_rate": round(success / calls, 4) if calls else 0.0,
        "last": dict(last) if isinstance(last, dict) else {},
        "never_succeeded": calls > 0 and success == 0,
    }


def _safe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)
