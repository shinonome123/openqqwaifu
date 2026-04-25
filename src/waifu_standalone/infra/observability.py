from __future__ import annotations

from collections import deque
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit


_ACCESS_LOGGER = logging.getLogger("waifu_standalone.http")
_LOG_BUFFER_LIMIT = 400
_LOG_BUFFER: deque[dict[str, object]] = deque(maxlen=_LOG_BUFFER_LIMIT)
_LOG_BUFFER_LOCK = threading.Lock()
_LOG_BUFFER_HANDLER: logging.Handler | None = None
_ACTIVE_METRICS_REGISTRY: MetricsRegistry | None = None
_ACTIVE_METRICS_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class TransportMetricsScope:
    kind: str
    target: str


class _InMemoryLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        with _LOG_BUFFER_LOCK:
            _LOG_BUFFER.append(
                {
                    "timestamp": float(getattr(record, "created", time.time()) or time.time()),
                    "level": str(getattr(record, "levelname", "INFO") or "INFO"),
                    "logger": str(getattr(record, "name", "") or ""),
                    "message": str(message or ""),
                }
            )


def ensure_log_buffer_handler() -> None:
    global _LOG_BUFFER_HANDLER
    if _LOG_BUFFER_HANDLER is not None:
        return
    with _LOG_BUFFER_LOCK:
        if _LOG_BUFFER_HANDLER is not None:
            return
        handler = _InMemoryLogHandler(level=logging.NOTSET)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(handler)
        _LOG_BUFFER_HANDLER = handler


def recent_log_entries(*, limit: int = 120, minimum_level: str = "") -> list[dict[str, object]]:
    ensure_log_buffer_handler()
    threshold = _coerce_level_number(minimum_level)
    with _LOG_BUFFER_LOCK:
        items = [dict(item) for item in list(_LOG_BUFFER)]
    if threshold > 0:
        items = [
            item
            for item in items
            if _coerce_level_number(str(item.get("level", "") or "")) >= threshold
        ]
    return items[-max(1, int(limit)) :][::-1]


def configure_logging(*, service_name: str = "openqqwaifu", level: str | None = None) -> None:
    raw_level = str(level or os.getenv("OPENQQWAIFU_LOG_LEVEL") or "INFO").strip().upper() or "INFO"
    resolved_level = getattr(logging, raw_level, logging.INFO)
    root_logger = logging.getLogger()
    if not getattr(configure_logging, "_configured", False):
        logging.basicConfig(
            level=resolved_level,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
        configure_logging._configured = True  # type: ignore[attr-defined]
    ensure_log_buffer_handler()
    root_logger.setLevel(resolved_level)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger(__name__).info(
        "logging configured service=%s level=%s",
        str(service_name or "openqqwaifu").strip() or "openqqwaifu",
        logging.getLevelName(resolved_level),
    )


def logging_is_configured() -> bool:
    return bool(getattr(configure_logging, "_configured", False))


def set_active_metrics_registry(metrics: "MetricsRegistry | None") -> None:
    global _ACTIVE_METRICS_REGISTRY
    with _ACTIVE_METRICS_LOCK:
        _ACTIVE_METRICS_REGISTRY = metrics


def active_metrics_registry() -> "MetricsRegistry | None":
    with _ACTIVE_METRICS_LOCK:
        return _ACTIVE_METRICS_REGISTRY


def normalize_metrics_path(path: str) -> str:
    raw_path = str(path or "").strip() or "/"
    if raw_path.startswith("/assets/"):
        return "/assets/{asset}"
    if raw_path.startswith("/api/skills/") and raw_path.endswith("/toggle"):
        return "/api/skills/{skill_id}/toggle"
    if raw_path.startswith("/api/skills/") and raw_path.endswith("/save"):
        return "/api/skills/{skill_id}/save"
    if raw_path.startswith("/api/memory/sessions/") and raw_path.endswith("/save"):
        return "/api/memory/sessions/{launcher_type}/{launcher_id}/save"
    if raw_path.startswith("/api/sessions/"):
        return "/api/sessions/{launcher_type}/{launcher_id}"
    if raw_path.startswith("/api/knowledge/"):
        return "/api/knowledge/{entry_id}"
    if raw_path.startswith("/api/skills/"):
        return "/api/skills/{skill_id}"
    return raw_path


def record_http_exchange(
    metrics: "MetricsRegistry | None",
    *,
    method: str,
    path: str,
    status_code: int,
    duration_seconds: float,
    remote_addr: str = "",
) -> None:
    if metrics is not None:
        metrics.record_http_request(
            method=method,
            path=path,
            status_code=status_code,
            duration_seconds=duration_seconds,
        )
    _ACCESS_LOGGER.info(
        '%s "%s" status=%s duration_ms=%.2f remote=%s',
        str(method or "GET").upper(),
        str(path or "").strip() or "/",
        int(status_code),
        max(0.0, float(duration_seconds)) * 1000.0,
        str(remote_addr or "-").strip() or "-",
    )


def record_transport_exchange(
    *,
    scope: TransportMetricsScope | None,
    method: str,
    url: str,
    status: object,
    outcome: str,
    duration_seconds: float,
) -> None:
    metrics = active_metrics_registry()
    if metrics is None or scope is None:
        return
    metrics.record_upstream_request(
        kind=scope.kind,
        target=scope.target,
        method=method,
        url=url,
        status=status,
        outcome=outcome,
        duration_seconds=duration_seconds,
    )


@dataclass(slots=True)
class MetricsRegistry:
    service_name: str = "openqqwaifu"
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _created_at_wall: float = field(default_factory=time.time, init=False, repr=False)
    _http_requests_total: dict[tuple[str, str, str], int] = field(default_factory=dict, init=False, repr=False)
    _http_request_duration_sum: dict[tuple[str, str], float] = field(default_factory=dict, init=False, repr=False)
    _http_request_duration_count: dict[tuple[str, str], int] = field(default_factory=dict, init=False, repr=False)
    _onebot_events_total: dict[tuple[str, str], int] = field(default_factory=dict, init=False, repr=False)
    _onebot_event_duration_sum: dict[tuple[str, str], float] = field(default_factory=dict, init=False, repr=False)
    _onebot_event_duration_count: dict[tuple[str, str], int] = field(default_factory=dict, init=False, repr=False)
    _upstream_requests_total: dict[tuple[str, str, str, str, str, str], int] = field(default_factory=dict, init=False, repr=False)
    _upstream_request_duration_sum: dict[tuple[str, str, str, str, str], float] = field(default_factory=dict, init=False, repr=False)
    _upstream_request_duration_count: dict[tuple[str, str, str, str, str], int] = field(default_factory=dict, init=False, repr=False)
    _skill_calls_total: dict[tuple[str, str, str, str, str], int] = field(default_factory=dict, init=False, repr=False)
    _skill_call_duration_sum: dict[tuple[str, str, str, str], float] = field(default_factory=dict, init=False, repr=False)
    _skill_call_duration_count: dict[tuple[str, str, str, str], int] = field(default_factory=dict, init=False, repr=False)

    def record_http_request(
        self,
        *,
        method: str,
        path: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        method_label = str(method or "GET").upper()
        path_label = normalize_metrics_path(path)
        status_label = str(int(status_code))
        duration = max(0.0, float(duration_seconds))
        request_key = (method_label, path_label, status_label)
        duration_key = (method_label, path_label)
        with self._lock:
            self._http_requests_total[request_key] = self._http_requests_total.get(request_key, 0) + 1
            self._http_request_duration_sum[duration_key] = (
                self._http_request_duration_sum.get(duration_key, 0.0) + duration
            )
            self._http_request_duration_count[duration_key] = (
                self._http_request_duration_count.get(duration_key, 0) + 1
            )

    def record_onebot_event(
        self,
        *,
        post_type: str,
        outcome: str,
        duration_seconds: float,
    ) -> None:
        post_type_label = str(post_type or "message").strip() or "message"
        outcome_label = str(outcome or "unknown").strip() or "unknown"
        duration = max(0.0, float(duration_seconds))
        key = (post_type_label, outcome_label)
        with self._lock:
            self._onebot_events_total[key] = self._onebot_events_total.get(key, 0) + 1
            self._onebot_event_duration_sum[key] = self._onebot_event_duration_sum.get(key, 0.0) + duration
            self._onebot_event_duration_count[key] = self._onebot_event_duration_count.get(key, 0) + 1

    def record_upstream_request(
        self,
        *,
        kind: str,
        target: str,
        method: str,
        url: str,
        status: object,
        outcome: str,
        duration_seconds: float,
    ) -> None:
        kind_label = str(kind or "external").strip() or "external"
        target_label = str(target or "unknown").strip() or "unknown"
        method_label = str(method or "GET").upper()
        host_label = _normalize_host(url)
        status_label = _normalize_status(status)
        outcome_label = str(outcome or "unknown").strip() or "unknown"
        duration = max(0.0, float(duration_seconds))
        request_key = (kind_label, target_label, method_label, host_label, status_label, outcome_label)
        duration_key = (kind_label, target_label, method_label, host_label, outcome_label)
        with self._lock:
            self._upstream_requests_total[request_key] = self._upstream_requests_total.get(request_key, 0) + 1
            self._upstream_request_duration_sum[duration_key] = (
                self._upstream_request_duration_sum.get(duration_key, 0.0) + duration
            )
            self._upstream_request_duration_count[duration_key] = (
                self._upstream_request_duration_count.get(duration_key, 0) + 1
            )

    def record_skill_call(
        self,
        *,
        skill_id: str,
        tool_id: str = "",
        trigger_source: str = "system",
        status: str,
        error_code: str = "",
        latency_ms: object = 0,
    ) -> None:
        skill_label = str(skill_id or "__unknown__").strip() or "__unknown__"
        tool_label = str(tool_id or "-").strip() or "-"
        trigger_label = str(trigger_source or "system").strip() or "system"
        status_label = str(status or "error").strip() or "error"
        error_label = str(error_code or "").strip() or "-"
        try:
            duration = max(0.0, float(latency_ms or 0) / 1000.0)
        except (TypeError, ValueError):
            duration = 0.0
        call_key = (skill_label, tool_label, trigger_label, status_label, error_label)
        duration_key = (skill_label, tool_label, trigger_label, status_label)
        with self._lock:
            self._skill_calls_total[call_key] = self._skill_calls_total.get(call_key, 0) + 1
            self._skill_call_duration_sum[duration_key] = (
                self._skill_call_duration_sum.get(duration_key, 0.0) + duration
            )
            self._skill_call_duration_count[duration_key] = (
                self._skill_call_duration_count.get(duration_key, 0) + 1
            )

    def snapshot(self, service: Any, *, log_limit: int = 120, row_limit: int = 60) -> dict[str, Any]:
        with self._lock:
            http_requests_total = dict(self._http_requests_total)
            http_request_duration_sum = dict(self._http_request_duration_sum)
            http_request_duration_count = dict(self._http_request_duration_count)
            onebot_events_total = dict(self._onebot_events_total)
            onebot_event_duration_sum = dict(self._onebot_event_duration_sum)
            onebot_event_duration_count = dict(self._onebot_event_duration_count)
            upstream_requests_total = dict(self._upstream_requests_total)
            upstream_request_duration_sum = dict(self._upstream_request_duration_sum)
            upstream_request_duration_count = dict(self._upstream_request_duration_count)
            skill_calls_total = dict(self._skill_calls_total)
            skill_call_duration_sum = dict(self._skill_call_duration_sum)
            skill_call_duration_count = dict(self._skill_call_duration_count)
            created_at_wall = float(self._created_at_wall)

        runtime_stats = _runtime_stats_snapshot(service)
        pending_background_tasks = _pending_background_tasks(service)

        http_rows: list[dict[str, object]] = []
        for (method, path, status), total in sorted(
            http_requests_total.items(),
            key=lambda item: (-item[1], item[0]),
        )[: max(1, int(row_limit))]:
            sum_value = http_request_duration_sum.get((method, path), 0.0)
            count_value = http_request_duration_count.get((method, path), 0)
            avg_ms = (sum_value / count_value * 1000.0) if count_value else 0.0
            http_rows.append(
                {
                    "method": method,
                    "path": path,
                    "status": status,
                    "total": total,
                    "avg_ms": round(avg_ms, 3),
                }
            )

        onebot_rows: list[dict[str, object]] = []
        for (post_type, outcome), total in sorted(
            onebot_events_total.items(),
            key=lambda item: (-item[1], item[0]),
        )[: max(1, int(row_limit))]:
            sum_value = onebot_event_duration_sum.get((post_type, outcome), 0.0)
            count_value = onebot_event_duration_count.get((post_type, outcome), 0)
            avg_ms = (sum_value / count_value * 1000.0) if count_value else 0.0
            onebot_rows.append(
                {
                    "post_type": post_type,
                    "outcome": outcome,
                    "total": total,
                    "avg_ms": round(avg_ms, 3),
                }
            )

        upstream_rows: list[dict[str, object]] = []
        for (kind, target, method, host, status, outcome), total in sorted(
            upstream_requests_total.items(),
            key=lambda item: (-item[1], item[0]),
        )[: max(1, int(row_limit))]:
            duration_key = (kind, target, method, host, outcome)
            sum_value = upstream_request_duration_sum.get(duration_key, 0.0)
            count_value = upstream_request_duration_count.get(duration_key, 0)
            avg_ms = (sum_value / count_value * 1000.0) if count_value else 0.0
            upstream_rows.append(
                {
                    "kind": kind,
                    "target": target,
                    "method": method,
                    "host": host,
                    "status": status,
                    "outcome": outcome,
                    "total": total,
                    "avg_ms": round(avg_ms, 3),
                }
            )

        target_rollups: dict[tuple[str, str, str], dict[str, object]] = {}
        for (kind, target, method, host, status, outcome), total in upstream_requests_total.items():
            rollup = target_rollups.setdefault(
                (kind, target, host),
                {
                    "kind": kind,
                    "target": target,
                    "host": host,
                    "total": 0,
                    "error_total": 0,
                    "duration_sum": 0.0,
                    "duration_count": 0,
                    "methods": set(),
                    "statuses": set(),
                },
            )
            rollup["total"] = int(rollup["total"]) + total
            if outcome != "ok":
                rollup["error_total"] = int(rollup["error_total"]) + total
            rollup["methods"].add(method)
            rollup["statuses"].add(status)
            duration_key = (kind, target, method, host, outcome)
            rollup["duration_sum"] = float(rollup["duration_sum"]) + upstream_request_duration_sum.get(duration_key, 0.0)
            rollup["duration_count"] = int(rollup["duration_count"]) + upstream_request_duration_count.get(duration_key, 0)

        upstream_targets: list[dict[str, object]] = []
        for row in sorted(target_rollups.values(), key=lambda item: (-int(item["total"]), str(item["target"])))[
            : max(1, int(row_limit))
        ]:
            duration_count = int(row["duration_count"])
            avg_ms = (float(row["duration_sum"]) / duration_count * 1000.0) if duration_count else 0.0
            upstream_targets.append(
                {
                    "kind": row["kind"],
                    "target": row["target"],
                    "host": row["host"],
                    "total": int(row["total"]),
                    "error_total": int(row["error_total"]),
                    "avg_ms": round(avg_ms, 3),
                    "methods": sorted(str(item) for item in row["methods"]),
                    "statuses": sorted(str(item) for item in row["statuses"]),
                }
            )

        skill_rows: list[dict[str, object]] = []
        for (skill_id, tool_id, trigger_source, status, error_code), total in sorted(
            skill_calls_total.items(),
            key=lambda item: (-item[1], item[0]),
        )[: max(1, int(row_limit))]:
            duration_key = (skill_id, tool_id, trigger_source, status)
            sum_value = skill_call_duration_sum.get(duration_key, 0.0)
            count_value = skill_call_duration_count.get(duration_key, 0)
            avg_ms = (sum_value / count_value * 1000.0) if count_value else 0.0
            skill_rows.append(
                {
                    "skill_id": skill_id,
                    "tool_id": tool_id,
                    "trigger_source": trigger_source,
                    "status": status,
                    "error_code": "" if error_code == "-" else error_code,
                    "total": total,
                    "avg_ms": round(avg_ms, 3),
                }
            )

        skill_rollups: dict[str, dict[str, object]] = {}
        for (skill_id, tool_id, trigger_source, status, error_code), total in skill_calls_total.items():
            rollup = skill_rollups.setdefault(
                skill_id,
                {
                    "skill_id": skill_id,
                    "total": 0,
                    "error_total": 0,
                    "duration_sum": 0.0,
                    "duration_count": 0,
                    "tools": set(),
                    "triggers": set(),
                    "errors": set(),
                },
            )
            rollup["total"] = int(rollup["total"]) + total
            if status != "ok":
                rollup["error_total"] = int(rollup["error_total"]) + total
            if tool_id != "-":
                rollup["tools"].add(tool_id)
            rollup["triggers"].add(trigger_source)
            if error_code != "-":
                rollup["errors"].add(error_code)
            duration_key = (skill_id, tool_id, trigger_source, status)
            rollup["duration_sum"] = float(rollup["duration_sum"]) + skill_call_duration_sum.get(duration_key, 0.0)
            rollup["duration_count"] = int(rollup["duration_count"]) + skill_call_duration_count.get(duration_key, 0)

        skill_targets: list[dict[str, object]] = []
        for row in sorted(skill_rollups.values(), key=lambda item: (-int(item["total"]), str(item["skill_id"])))[
            : max(1, int(row_limit))
        ]:
            duration_count = int(row["duration_count"])
            avg_ms = (float(row["duration_sum"]) / duration_count * 1000.0) if duration_count else 0.0
            skill_targets.append(
                {
                    "skill_id": row["skill_id"],
                    "total": int(row["total"]),
                    "error_total": int(row["error_total"]),
                    "avg_ms": round(avg_ms, 3),
                    "tools": sorted(str(item) for item in row["tools"]),
                    "triggers": sorted(str(item) for item in row["triggers"]),
                    "errors": sorted(str(item) for item in row["errors"]),
                }
            )

        return {
            "generated_at": time.time(),
            "process_started_at": created_at_wall,
            "runtime": {
                **runtime_stats,
                "background_tasks": pending_background_tasks,
            },
            "logs": recent_log_entries(limit=log_limit),
            "http": {
                "total": sum(http_requests_total.values()),
                "rows": http_rows,
            },
            "onebot": {
                "total": sum(onebot_events_total.values()),
                "rows": onebot_rows,
            },
            "upstream": {
                "total": sum(upstream_requests_total.values()),
                "error_total": sum(
                    value
                    for (kind, target, method, host, status, outcome), value in upstream_requests_total.items()
                    if outcome != "ok"
                ),
                "rows": upstream_rows,
                "targets": upstream_targets,
            },
            "skills": {
                "total": sum(skill_calls_total.values()),
                "error_total": sum(
                    value
                    for (skill_id, tool_id, trigger_source, status, error_code), value in skill_calls_total.items()
                    if status != "ok"
                ),
                "rows": skill_rows,
                "targets": skill_targets,
            },
        }

    def render_prometheus(self, service: Any) -> str:
        with self._lock:
            http_requests_total = dict(self._http_requests_total)
            http_request_duration_sum = dict(self._http_request_duration_sum)
            http_request_duration_count = dict(self._http_request_duration_count)
            onebot_events_total = dict(self._onebot_events_total)
            onebot_event_duration_sum = dict(self._onebot_event_duration_sum)
            onebot_event_duration_count = dict(self._onebot_event_duration_count)
            upstream_requests_total = dict(self._upstream_requests_total)
            upstream_request_duration_sum = dict(self._upstream_request_duration_sum)
            upstream_request_duration_count = dict(self._upstream_request_duration_count)
            skill_calls_total = dict(self._skill_calls_total)
            skill_call_duration_sum = dict(self._skill_call_duration_sum)
            skill_call_duration_count = dict(self._skill_call_duration_count)
            created_at_wall = float(self._created_at_wall)

        runtime_stats = _runtime_stats_snapshot(service)
        pending_background_tasks = _pending_background_tasks(service)

        lines: list[str] = []
        lines.extend(
            [
                "# HELP openqqwaifu_service_up Embedded HTTP service health flag.",
                "# TYPE openqqwaifu_service_up gauge",
                "openqqwaifu_service_up 1",
                "# HELP openqqwaifu_process_start_time_seconds Process start time in unix seconds.",
                "# TYPE openqqwaifu_process_start_time_seconds gauge",
                f"openqqwaifu_process_start_time_seconds {_format_metric_value(created_at_wall)}",
                "# HELP openqqwaifu_http_requests_total Total HTTP requests handled by the embedded server.",
                "# TYPE openqqwaifu_http_requests_total counter",
            ]
        )
        for (method, path, status), value in sorted(http_requests_total.items()):
            lines.append(
                f'openqqwaifu_http_requests_total{{method="{_escape_label_value(method)}",path="{_escape_label_value(path)}",status="{_escape_label_value(status)}"}} {value}'
            )

        lines.extend(
            [
                "# HELP openqqwaifu_http_request_duration_seconds End-to-end HTTP handler duration.",
                "# TYPE openqqwaifu_http_request_duration_seconds summary",
            ]
        )
        for (method, path), value in sorted(http_request_duration_sum.items()):
            count = http_request_duration_count.get((method, path), 0)
            label_block = (
                f'method="{_escape_label_value(method)}",path="{_escape_label_value(path)}"'
            )
            lines.append(
                f"openqqwaifu_http_request_duration_seconds_sum{{{label_block}}} {_format_metric_value(value)}"
            )
            lines.append(f"openqqwaifu_http_request_duration_seconds_count{{{label_block}}} {count}")

        lines.extend(
            [
                "# HELP openqqwaifu_onebot_events_total Total inbound OneBot event handling attempts.",
                "# TYPE openqqwaifu_onebot_events_total counter",
            ]
        )
        for (post_type, outcome), value in sorted(onebot_events_total.items()):
            lines.append(
                f'openqqwaifu_onebot_events_total{{outcome="{_escape_label_value(outcome)}",post_type="{_escape_label_value(post_type)}"}} {value}'
            )

        lines.extend(
            [
                "# HELP openqqwaifu_onebot_event_duration_seconds OneBot event handling duration.",
                "# TYPE openqqwaifu_onebot_event_duration_seconds summary",
            ]
        )
        for (post_type, outcome), value in sorted(onebot_event_duration_sum.items()):
            count = onebot_event_duration_count.get((post_type, outcome), 0)
            label_block = (
                f'outcome="{_escape_label_value(outcome)}",post_type="{_escape_label_value(post_type)}"'
            )
            lines.append(
                f"openqqwaifu_onebot_event_duration_seconds_sum{{{label_block}}} {_format_metric_value(value)}"
            )
            lines.append(f"openqqwaifu_onebot_event_duration_seconds_count{{{label_block}}} {count}")

        lines.extend(
            [
                "# HELP openqqwaifu_upstream_requests_total Total outbound provider and sidecar HTTP requests.",
                "# TYPE openqqwaifu_upstream_requests_total counter",
            ]
        )
        for (kind, target, method, host, status, outcome), value in sorted(upstream_requests_total.items()):
            lines.append(
                f'openqqwaifu_upstream_requests_total{{kind="{_escape_label_value(kind)}",target="{_escape_label_value(target)}",method="{_escape_label_value(method)}",host="{_escape_label_value(host)}",status="{_escape_label_value(status)}",outcome="{_escape_label_value(outcome)}"}} {value}'
            )

        lines.extend(
            [
                "# HELP openqqwaifu_upstream_request_duration_seconds Outbound provider and sidecar HTTP request duration.",
                "# TYPE openqqwaifu_upstream_request_duration_seconds summary",
            ]
        )
        for (kind, target, method, host, outcome), value in sorted(upstream_request_duration_sum.items()):
            count = upstream_request_duration_count.get((kind, target, method, host, outcome), 0)
            label_block = (
                f'kind="{_escape_label_value(kind)}",target="{_escape_label_value(target)}",method="{_escape_label_value(method)}",host="{_escape_label_value(host)}",outcome="{_escape_label_value(outcome)}"'
            )
            lines.append(
                f"openqqwaifu_upstream_request_duration_seconds_sum{{{label_block}}} {_format_metric_value(value)}"
            )
            lines.append(f"openqqwaifu_upstream_request_duration_seconds_count{{{label_block}}} {count}")

        lines.extend(
            [
                "# HELP openqqwaifu_skill_calls_total Total skill executor calls.",
                "# TYPE openqqwaifu_skill_calls_total counter",
            ]
        )
        for (skill_id, tool_id, trigger_source, status, error_code), value in sorted(skill_calls_total.items()):
            lines.append(
                f'openqqwaifu_skill_calls_total{{skill_id="{_escape_label_value(skill_id)}",tool_id="{_escape_label_value(tool_id)}",trigger_source="{_escape_label_value(trigger_source)}",status="{_escape_label_value(status)}",error_code="{_escape_label_value(error_code)}"}} {value}'
            )

        lines.extend(
            [
                "# HELP openqqwaifu_skill_call_duration_seconds Skill executor call duration.",
                "# TYPE openqqwaifu_skill_call_duration_seconds summary",
            ]
        )
        for (skill_id, tool_id, trigger_source, status), value in sorted(skill_call_duration_sum.items()):
            count = skill_call_duration_count.get((skill_id, tool_id, trigger_source, status), 0)
            label_block = (
                f'skill_id="{_escape_label_value(skill_id)}",tool_id="{_escape_label_value(tool_id)}",trigger_source="{_escape_label_value(trigger_source)}",status="{_escape_label_value(status)}"'
            )
            lines.append(
                f"openqqwaifu_skill_call_duration_seconds_sum{{{label_block}}} {_format_metric_value(value)}"
            )
            lines.append(f"openqqwaifu_skill_call_duration_seconds_count{{{label_block}}} {count}")

        lines.extend(
            [
                "# HELP openqqwaifu_logs_buffered Recent in-memory log entries retained for the console.",
                "# TYPE openqqwaifu_logs_buffered gauge",
                f"openqqwaifu_logs_buffered {len(recent_log_entries(limit=_LOG_BUFFER_LIMIT))}",
                "# HELP openqqwaifu_uptime_seconds Service uptime in seconds.",
                "# TYPE openqqwaifu_uptime_seconds gauge",
                f'openqqwaifu_uptime_seconds {_format_metric_value(runtime_stats.get("uptime_seconds", 0.0))}',
                "# HELP openqqwaifu_recent_inbound Recent inbound events kept in memory.",
                "# TYPE openqqwaifu_recent_inbound gauge",
                f'openqqwaifu_recent_inbound {int(runtime_stats.get("recent_inbound", 0) or 0)}',
                "# HELP openqqwaifu_recent_outbound Recent outbound events kept in memory.",
                "# TYPE openqqwaifu_recent_outbound gauge",
                f'openqqwaifu_recent_outbound {int(runtime_stats.get("recent_outbound", 0) or 0)}',
                "# HELP openqqwaifu_recent_behavior Recent behavior events kept in memory.",
                "# TYPE openqqwaifu_recent_behavior gauge",
                f'openqqwaifu_recent_behavior {int(runtime_stats.get("recent_behavior", 0) or 0)}',
                "# HELP openqqwaifu_active_followups Active group follow-up windows.",
                "# TYPE openqqwaifu_active_followups gauge",
                f'openqqwaifu_active_followups {int(runtime_stats.get("active_followups", 0) or 0)}',
                "# HELP openqqwaifu_total_events Total in-memory event sequence count.",
                "# TYPE openqqwaifu_total_events gauge",
                f'openqqwaifu_total_events {int(runtime_stats.get("total_events", 0) or 0)}',
                "# HELP openqqwaifu_background_tasks Pending background futures.",
                "# TYPE openqqwaifu_background_tasks gauge",
                f"openqqwaifu_background_tasks {int(pending_background_tasks)}",
            ]
        )
        return "\n".join(lines) + "\n"

def _runtime_stats_snapshot(service: Any) -> dict[str, Any]:
    stats_getter = getattr(service, "runtime_stats", None)
    if not callable(stats_getter):
        return {}
    try:
        return dict(stats_getter())
    except Exception:
        return {}


def _pending_background_tasks(service: Any) -> int:
    pending_tasks = getattr(service, "_background_tasks", None)
    if isinstance(pending_tasks, set):
        return len(pending_tasks)
    return 0


def _normalize_host(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    host = str(parsed.netloc or parsed.hostname or "").strip()
    return host or "-"


def _normalize_status(value: object) -> str:
    text = str(value or "").strip()
    return text or "error"


def _coerce_level_number(level: str) -> int:
    if not level:
        return 0
    return int(getattr(logging, str(level).strip().upper(), 0) or 0)


def _escape_label_value(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_metric_value(value: object) -> str:
    try:
        numeric = float(value or 0.0)
    except (TypeError, ValueError):
        numeric = 0.0
    return format(numeric, ".17g")
