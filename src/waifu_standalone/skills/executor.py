from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .registry import SkillManifest
from .messages import t
from .telemetry import get_skill_telemetry_summary, record_skill_telemetry_event
from .tool_registry import ToolExecutionResult, ToolInvocation

if TYPE_CHECKING:
    from .tool_registry import ToolRegistry


_DANGEROUS_TOOL_IDS: set[str] = {"write-file", "exec-command"}


@dataclass(slots=True)
class SkillExecutionContext:
    invocation: ToolInvocation
    trigger_source: str = "llm"
    caller: str = ""


@dataclass(slots=True)
class SkillExecutionError(Exception):
    skill_id: str
    error_code: str
    message: str
    trace_id: str
    details: dict[str, object] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


@dataclass(slots=True)
class SkillExecutionResult:
    skill_id: str
    tool_id: str
    status: str
    latency_ms: int
    trace_id: str
    result: ToolExecutionResult
    error_code: str = ""

    def as_telemetry(self) -> dict[str, object]:
        return {
            "skill_id": self.skill_id,
            "tool_id": self.tool_id,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "trace_id": self.trace_id,
            "error_code": self.error_code,
        }


class SkillExecutor:
    def __init__(self, tools: "ToolRegistry") -> None:
        self._tools = tools

    def run(self, manifest: SkillManifest, invocation: ToolInvocation) -> ToolExecutionResult:
        started = time.perf_counter()
        trace_id = uuid.uuid4().hex[:12]
        try:
            result = self._run_inner(manifest, invocation, trace_id=trace_id)
            self._record(manifest, invocation, result, started=started, trace_id=trace_id)
            return result
        except SkillExecutionError as exc:
            result = ToolExecutionResult(error=exc.message, text=exc.message)
            self._record(
                manifest,
                invocation,
                result,
                started=started,
                trace_id=trace_id,
                error_code=exc.error_code,
                details=exc.details,
            )
            return result
        except (RuntimeError, ValueError, TypeError, OSError) as exc:
            message = t("skill.unhandled_exception", error=str(exc))
            result = ToolExecutionResult(error=message, text=message)
            self._record(
                manifest,
                invocation,
                result,
                started=started,
                trace_id=trace_id,
                error_code="unhandled_exception",
                details={"exception_type": type(exc).__name__},
            )
            return result

    async def arun(self, manifest: SkillManifest, invocation: ToolInvocation) -> ToolExecutionResult:
        started = time.perf_counter()
        trace_id = uuid.uuid4().hex[:12]
        try:
            result = await asyncio.wait_for(
                self._arun_inner(manifest, invocation, trace_id=trace_id),
                timeout=max(1.0, float(manifest.policy.timeout_seconds)),
            )
            self._record(manifest, invocation, result, started=started, trace_id=trace_id)
            return result
        except asyncio.TimeoutError:
            result = ToolExecutionResult(
                error=t("skill.timeout", seconds=f"{manifest.policy.timeout_seconds:g}"),
                text=t("skill.timeout", seconds=f"{manifest.policy.timeout_seconds:g}"),
            )
            self._record(
                manifest,
                invocation,
                result,
                started=started,
                trace_id=trace_id,
                error_code="timeout",
            )
            return result
        except SkillExecutionError as exc:
            result = ToolExecutionResult(error=exc.message, text=exc.message)
            self._record(
                manifest,
                invocation,
                result,
                started=started,
                trace_id=trace_id,
                error_code=exc.error_code,
                details=exc.details,
            )
            return result
        except (RuntimeError, ValueError, TypeError, OSError) as exc:
            message = t("skill.unhandled_exception", error=str(exc))
            result = ToolExecutionResult(error=message, text=message)
            self._record(
                manifest,
                invocation,
                result,
                started=started,
                trace_id=trace_id,
                error_code="unhandled_exception",
                details={"exception_type": type(exc).__name__},
            )
            return result

    def _run_inner(self, manifest: SkillManifest, invocation: ToolInvocation, *, trace_id: str) -> ToolExecutionResult:
        if not manifest.enabled:
            raise self._error(manifest, "disabled", t("skill.disabled"), trace_id)
        if not manifest.eligible or manifest.validation_errors:
            raise self._error(manifest, "invalid_manifest", t("skill.invalid_manifest"), trace_id)
        if manifest.handler.type != "tool_id":
            raise self._error(
                manifest,
                "unsupported_handler",
                t("skill.unsupported_handler", handler=manifest.handler.type),
                trace_id,
            )
        tool_id = manifest.handler.target
        tool = self._tools.get(tool_id)
        if tool is None:
            raise self._error(manifest, "tool_not_registered", t("skill.tool_not_registered", tool_id=tool_id), trace_id)
        if not tool.model_callable or tool.model_handler is None:
            raise self._error(manifest, "tool_not_callable", t("skill.tool_not_callable", tool_id=tool_id), trace_id)
        self._validate_tool_authorization(manifest, tool_id, trace_id)
        next_invocation = self._with_manifest_args(manifest, invocation)
        self._validate_arguments(manifest, next_invocation.arguments, trace_id)
        next_invocation.tool_id = tool_id
        next_invocation.skill = manifest
        return self._limit_result(manifest, tool.model_handler(next_invocation))

    async def _arun_inner(self, manifest: SkillManifest, invocation: ToolInvocation, *, trace_id: str) -> ToolExecutionResult:
        if not manifest.enabled:
            raise self._error(manifest, "disabled", t("skill.disabled"), trace_id)
        if not manifest.eligible or manifest.validation_errors:
            raise self._error(manifest, "invalid_manifest", t("skill.invalid_manifest"), trace_id)
        if manifest.handler.type != "tool_id":
            raise self._error(
                manifest,
                "unsupported_handler",
                t("skill.unsupported_handler", handler=manifest.handler.type),
                trace_id,
            )
        tool_id = manifest.handler.target
        tool = self._tools.get(tool_id)
        if tool is None:
            raise self._error(manifest, "tool_not_registered", t("skill.tool_not_registered", tool_id=tool_id), trace_id)
        if not tool.model_callable:
            raise self._error(manifest, "tool_not_callable", t("skill.tool_not_callable", tool_id=tool_id), trace_id)
        self._validate_tool_authorization(manifest, tool_id, trace_id)
        next_invocation = self._with_manifest_args(manifest, invocation)
        self._validate_arguments(manifest, next_invocation.arguments, trace_id)
        next_invocation.tool_id = tool_id
        next_invocation.skill = manifest
        if tool.async_model_handler is not None:
            result = await tool.async_model_handler(next_invocation)
        elif tool.model_handler is not None:
            result = await asyncio.to_thread(tool.model_handler, next_invocation)
        else:
            raise self._error(manifest, "tool_not_callable", t("skill.tool_not_callable", tool_id=tool_id), trace_id)
        return self._limit_result(manifest, result)

    @staticmethod
    def _with_manifest_args(manifest: SkillManifest, invocation: ToolInvocation) -> ToolInvocation:
        merged_args: dict[str, object] = dict(manifest.default_args)
        merged_args.update(dict(invocation.arguments or {}))
        if "raw_args" not in merged_args and invocation.raw_args:
            merged_args["raw_args"] = invocation.raw_args
        return ToolInvocation(
            tool_id=manifest.handler.target,
            raw_args=invocation.raw_args,
            event=invocation.event,
            session=invocation.session,
            skill=manifest,
            address=invocation.address,
            assistant_name=invocation.assistant_name,
            active_skills=list(invocation.active_skills),
            arguments=merged_args,
        )

    @staticmethod
    def _limit_result(manifest: SkillManifest, result: ToolExecutionResult) -> ToolExecutionResult:
        limit = max(200, int(manifest.policy.max_output_chars))
        if result.text and len(result.text) > limit:
            result.text = result.text[: limit - 1].rstrip() + "…"
        return result

    @staticmethod
    def _validate_tool_authorization(manifest: SkillManifest, tool_id: str, trace_id: str) -> None:
        if tool_id not in _DANGEROUS_TOOL_IDS:
            return
        if manifest.policy.requires_authorization:
            return
        raise SkillExecutor._error(
            manifest,
            "permission_denied",
            t("skill.permission_denied", tool_id=tool_id),
            trace_id,
            details={"tool_id": tool_id, "required_policy": "requires_authorization"},
        )

    @staticmethod
    def _validate_arguments(manifest: SkillManifest, arguments: dict[str, object], trace_id: str) -> None:
        schema = manifest.input_schema if isinstance(manifest.input_schema, dict) else {}
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                name = str(key or "").strip()
                if not name:
                    continue
                value = arguments.get(name)
                if value is None or (isinstance(value, str) and not value.strip()):
                    raise SkillExecutor._error(
                        manifest,
                        "invalid_arguments",
                        t("skill.invalid_arguments", reason=f"missing required field: {name}"),
                        trace_id,
                        details={"field": name},
                    )
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return
        for name, spec in properties.items():
            if name not in arguments or not isinstance(spec, dict):
                continue
            expected = str(spec.get("type") or "").strip()
            if expected and not _argument_matches_type(arguments.get(name), expected):
                raise SkillExecutor._error(
                    manifest,
                    "invalid_arguments",
                    t("skill.invalid_arguments", reason=f"{name} must be {expected}"),
                    trace_id,
                    details={"field": str(name), "expected": expected},
                )

    @staticmethod
    def _error(
        manifest: SkillManifest,
        code: str,
        message: str,
        trace_id: str,
        *,
        details: dict[str, object] | None = None,
    ) -> SkillExecutionError:
        return SkillExecutionError(
            skill_id=manifest.skill_id,
            error_code=code,
            message=message,
            trace_id=trace_id,
            details=dict(details or {}),
        )

    @staticmethod
    def _record(
        manifest: SkillManifest,
        invocation: ToolInvocation,
        result: ToolExecutionResult,
        *,
        started: float,
        trace_id: str,
        error_code: str = "",
        details: dict[str, object] | None = None,
    ) -> None:
        latency_ms = int((time.perf_counter() - started) * 1000)
        status = "error" if result.error else "ok"
        trigger_source = str(invocation.arguments.get("_trigger_source") or "llm").strip() or "llm"
        payload = {
            "skill_id": manifest.skill_id,
            "tool_id": manifest.handler.target,
            "trigger_source": trigger_source,
            "latency_ms": latency_ms,
            "status": status,
            "error_code": error_code or ("tool_error" if result.error else ""),
            "error": str(result.error or "")[:600],
            "trace_id": trace_id,
            "caller": f"{invocation.event.launcher_type}:{invocation.event.launcher_id}:{invocation.event.sender_id}",
            "arg_summary": _summarize_json(invocation.arguments),
            "result_summary": str(result.text or result.error or "")[:1000],
            "details": dict(details or {}),
            "created_at": int(time.time()),
        }
        payload = record_skill_telemetry_event(payload)
        manifest.last_execution = payload
        invocation.session.metadata["last_skill_execution"] = payload
        recent = invocation.session.metadata.get("skill_telemetry")
        if not isinstance(recent, list):
            recent = []
        recent.append(payload)
        invocation.session.metadata["skill_telemetry"] = recent[-20:]


def _argument_matches_type(value: object, expected: str) -> bool:
    if value is None:
        return True
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def _summarize_json(payload: dict[str, object]) -> str:
    cleaned = {
        str(key): value
        for key, value in payload.items()
        if not str(key).lower().endswith(("key", "token", "secret", "password"))
    }
    try:
        return json.dumps(cleaned, ensure_ascii=False, default=str)[:1000]
    except (TypeError, ValueError):
        return str(cleaned)[:1000]
