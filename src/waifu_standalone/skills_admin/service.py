from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from ..skills import import_skill_bundle as import_skill_bundle_payload
from ..skills import (
    build_skill_pack_template,
    export_skill_pack as export_skill_pack_payload,
    import_skill_pack as import_skill_pack_payload,
)
from ..skills import build_skill_markdown_template
from ..skills import ToolExecutionResult
from ..skills import record_skill_error_event

if TYPE_CHECKING:
    from ..app import WaifuService


_RESTRICTED_TOOL_IDS = {"write-file", "exec-command"}
_BUILTIN_CAPABILITY_MAP: dict[str, dict[str, str]] = {
    "image": {"title": "生图", "summary": "生成图片并直接回传。", "category": "creative"},
    "image-caption": {"title": "生图交付", "summary": "为生成后的图片补交付文案。", "category": "creative"},
    "search": {"title": "联网搜索", "summary": "执行联网检索并整理结果。", "category": "network"},
    "search-links": {"title": "来源链接", "summary": "返回搜索结果对应的来源链接。", "category": "network"},
    "summary": {"title": "会话总结", "summary": "总结最近的对话上下文。", "category": "memory"},
    "summarize": {"title": "外部内容总结", "summary": "总结链接、视频或文件内容。", "category": "content"},
    "skill-list": {"title": "技能列表", "summary": "列出当前可用技能和触发方式。", "category": "orchestration"},
    "web-fetch": {"title": "抓取网页", "summary": "抓取 URL 文本并归一化输出。", "category": "network"},
    "read-file": {"title": "读取文件", "summary": "读取允许目录内的文件内容。", "category": "filesystem"},
    "list-files": {"title": "列出文件", "summary": "列出允许目录内的文件和目录。", "category": "filesystem"},
    "write-file": {"title": "写入文件", "summary": "写入允许目录内的文件。", "category": "filesystem"},
    "exec-command": {"title": "执行命令", "summary": "执行白名单命令。", "category": "runtime"},
}
_PROMPT_CAPABILITY_MAP: dict[str, dict[str, str]] = {
    "freshness-check": {"title": "时效性核验", "summary": "提醒模型优先核验时效性。", "category": "prompt"},
    "concise-answer": {"title": "简洁回答", "summary": "压缩回答长度，优先给结论。", "category": "prompt"},
    "image-handoff": {"title": "生图交付语气", "summary": "统一图片交付时的语气和文案。", "category": "prompt"},
}


class SkillsAdminService:
    def __init__(self, service: WaifuService) -> None:
        self.service = service

    def list_skills(self) -> dict[str, object]:
        return self.service.skills.describe()

    def list_tools(self) -> dict[str, object]:
        return self.service.tools.describe()

    def get_skills_panel(self) -> dict[str, object]:
        svc = self.service
        skills = self.list_skills()
        tools = self.list_tools()
        return {
            "skills": skills,
            "tools": tools,
            "capabilities": self._normalized_capabilities(skills, tools),
            "skill_groups": self._grouped_skills(skills),
            "tool_groups": self._grouped_tools(tools),
            "safety": self._tool_policy_panel(),
            "claw_runtime": svc.get_claw_runtime_panel(refresh=True),
            "marketplace": svc.marketplace.describe(),
        }

    def search_marketplace(self, query: str, *, source_id: str = "", limit: int = 12) -> dict[str, object]:
        return self.service.marketplace.search(query, source_id=source_id, limit=limit)

    def import_marketplace_skill(
        self,
        *,
        source_id: str,
        github_url: str,
    ) -> dict[str, object]:
        svc = self.service
        with tempfile.TemporaryDirectory() as tmpdir:
            prepared = svc.marketplace.prepare_skill_bundle(source_id, github_url, tmpdir)
            result = self.import_skill_bundle(
                prepared["path"],
                source_metadata={
                    "source_id": source_id,
                    "source_url": prepared.get("source_url", github_url),
                    "bundle_url": prepared.get("bundle_url", ""),
                    "page_url": prepared.get("page_url", ""),
                },
            )
        result["source_id"] = source_id
        result["source_url"] = prepared.get("source_url", github_url)
        result["bundle_url"] = prepared.get("bundle_url", "")
        result["page_url"] = prepared.get("page_url", "")
        return result

    def skill_pack_template(self) -> dict[str, object]:
        return build_skill_pack_template()

    def export_skill_pack(
        self,
        *,
        skill_ids: list[str] | None = None,
        include_builtin: bool = False,
        name: str = "",
        description: str = "",
    ) -> dict[str, object]:
        return export_skill_pack_payload(
            self.service.skills,
            skill_ids=skill_ids,
            include_builtin=include_builtin,
            name=name,
            description=description,
        )

    def import_skill_pack(
        self,
        payload: dict[str, object] | str,
        *,
        overwrite: bool = True,
    ) -> dict[str, object]:
        return import_skill_pack_payload(self.service.skills, payload, overwrite=overwrite)

    def import_skill_bundle(
        self,
        source: str | Path,
        *,
        overwrite: bool = True,
        source_metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        svc = self.service
        result = import_skill_bundle_payload(svc.skills, source, overwrite=overwrite)
        if svc.config.claw_runtime.enabled:
            try:
                plugin = svc.claw_runtime.install_plugin(
                    source,
                    overwrite=overwrite,
                    metadata=source_metadata or {},
                )
            except Exception as exc:
                record_skill_error_event(
                    skill_id="claw_runtime:install_plugin",
                    error_code="claw_runtime_install_failed",
                    message=str(exc),
                    source="skills_admin.import_skill_bundle",
                )
                result["claw_runtime"] = {
                    "status": "error",
                    "reason": str(exc),
                }
            else:
                result["claw_runtime"] = {
                    "status": "ok",
                    "plugin": plugin,
                }
                self._register_claw_runtime_tools()
        return result

    def _register_claw_runtime_tools(self) -> None:
        svc = self.service
        try:
            runtime_tools = svc.claw_runtime.list_tools().get("items", [])
        except Exception as exc:
            record_skill_error_event(
                skill_id="claw_runtime:list_tools",
                error_code="claw_runtime_list_tools_failed",
                message=str(exc),
                source="skills_admin.register_tools",
            )
            return
        for runtime_tool in runtime_tools if isinstance(runtime_tools, list) else []:
            if not isinstance(runtime_tool, dict):
                continue
            tool_id = str(runtime_tool.get("id", "") or "").strip()
            if not tool_id:
                continue
            if svc.tools.get(tool_id) is not None:
                continue
            name = str(runtime_tool.get("name", "") or "").strip() or tool_id
            description = str(runtime_tool.get("description", "") or "").strip() or "ClawRuntime tool."
            parameters = runtime_tool.get("input_schema") or runtime_tool.get("inputSchema") or {}
            if not isinstance(parameters, dict):
                parameters = {}

            def model_handler(invocation, *, _tool_id: str = tool_id):  # type: ignore[no-untyped-def]
                if not svc.dispatcher._claw_runtime_allows_execution():
                    return ToolExecutionResult(error="claw runtime routing is disabled", text="ClawRuntime 路由未启用。")
                try:
                    payload = svc.claw_runtime.invoke_tool(
                        _tool_id,
                        svc.dispatcher._claw_runtime_payload(invocation),
                    )
                except Exception as exc:
                    record_skill_error_event(
                        skill_id=f"claw_runtime:{_tool_id}",
                        tool_id=_tool_id,
                        error_code="claw_runtime_tool_failed",
                        message=str(exc),
                        source="skills_admin.model_handler",
                    )
                    return ToolExecutionResult(error=str(exc), text=f"ClawRuntime 工具执行失败：{exc}")
                return svc.dispatcher._tool_result_from_claw_runtime(payload)

            async def async_model_handler(invocation, *, _handler=model_handler):  # type: ignore[no-untyped-def]
                return await asyncio.to_thread(_handler, invocation)

            def handler(invocation, *, _handler=model_handler):  # type: ignore[no-untyped-def]
                return svc.dispatcher._emit_tool_execution_result(invocation, _handler(invocation))

            async def async_handler(invocation, *, _handler=model_handler):  # type: ignore[no-untyped-def]
                result = await asyncio.to_thread(_handler, invocation)
                return await svc.dispatcher._aemit_tool_execution_result(invocation, result)

            svc.tools.register(
                tool_id,
                name=name,
                description=description,
                handler=handler,
                async_handler=async_handler,
                parameters=parameters,
                model_handler=model_handler,
                async_model_handler=async_model_handler,
                risk_level="command",
                skill_source="claw_runtime",
            )

    def get_skill_detail(self, skill_id: str) -> dict[str, object] | None:
        return self._skill_detail_with_markdown(skill_id)

    def set_skill_enabled(self, skill_id: str, enabled: bool) -> dict[str, object] | None:
        skill = self.service.skills.set_enabled(skill_id, enabled)
        if skill is None:
            return None
        return skill.as_dict()

    def install_skill(self, markdown: str, filename: str | None = None) -> dict[str, object]:
        skill = self.service.skills.install_workspace_skill(markdown, filename=filename)
        detail = skill.as_dict()
        detail["markdown"] = self.service.skills.get_skill_markdown(skill.skill_id) or markdown
        return detail

    def save_skill(self, skill_id: str, markdown: str) -> dict[str, object] | None:
        skill = self.service.skills.save_workspace_skill(skill_id, markdown)
        if skill is None:
            return None
        detail = skill.as_dict()
        detail["markdown"] = self.service.skills.get_skill_markdown(skill.skill_id) or markdown
        return detail

    def delete_skill(self, skill_id: str) -> bool:
        return self.service.skills.delete_workspace_skill(skill_id)

    def reload_skills(self) -> dict[str, object]:
        return self.service.skills.reload()

    def new_skill_template(self) -> dict[str, str]:
        return {
            "markdown": build_skill_markdown_template(
                skill_id="custom-skill",
                name="自定义技能",
                description="描述这个技能的职责。",
                triggers=["触发词"],
                priority=4,
                body="在这里写下给模型看的能力说明。工具型技能请在 handler 里绑定 tool_id。",
            )
        }

    def _skill_detail_with_markdown(self, skill_id: str) -> dict[str, object] | None:
        skill = self.service.skills.get_skill(skill_id)
        if skill is None:
            return None
        detail = skill.as_dict()
        detail["markdown"] = self.service.skills.get_skill_markdown(skill_id) or ""
        return detail

    def _normalized_capabilities(
        self,
        skills_payload: dict[str, object],
        tools_payload: dict[str, object],
    ) -> list[dict[str, object]]:
        skill_items = [
            dict(item)
            for item in list(skills_payload.get("items", []))
            if isinstance(item, dict)
        ]
        tool_items = [
            dict(item)
            for item in list(tools_payload.get("items", []))
            if isinstance(item, dict)
        ]
        by_tool: dict[str, list[dict[str, object]]] = {}
        for skill in skill_items:
            manifest = skill.get("manifest", {})
            manifest = manifest if isinstance(manifest, dict) else {}
            handler = manifest.get("handler", {})
            handler = handler if isinstance(handler, dict) else {}
            tool_id = str(handler.get("target", "") or "").strip() if handler.get("type") == "tool_id" else ""
            if tool_id:
                by_tool.setdefault(tool_id, []).append(skill)
        items: list[dict[str, object]] = []
        for tool in tool_items:
            tool_id = str(tool.get("id", "") or "").strip()
            meta = _BUILTIN_CAPABILITY_MAP.get(tool_id, {})
            attached_skills = by_tool.get(tool_id, [])
            source_kind = "builtin" if tool_id in _BUILTIN_CAPABILITY_MAP else "runtime"
            if attached_skills:
                source_kind = str(attached_skills[0].get("source_kind", source_kind) or source_kind)
            items.append(
                {
                    "id": tool_id,
                    "title": meta.get("title", tool.get("name") or tool_id),
                    "summary": meta.get("summary", tool.get("description") or ""),
                    "category": meta.get("category", "runtime"),
                    "type": "tool",
                    "source_kind": source_kind,
                    "model_callable": bool(tool.get("model_callable")),
                    "restricted": tool_id in _RESTRICTED_TOOL_IDS,
                    "aliases": list(tool.get("aliases", [])),
                    "dispatch_skill_ids": [str(skill.get("id", "") or "") for skill in attached_skills],
                    "dispatch_skill_count": len(attached_skills),
                }
            )
        for skill in skill_items:
            manifest = skill.get("manifest", {})
            manifest = manifest if isinstance(manifest, dict) else {}
            handler = manifest.get("handler", {})
            handler = handler if isinstance(handler, dict) else {}
            if str(handler.get("type", "") or "").strip() == "tool_id":
                continue
            skill_id = str(skill.get("id", "") or "").strip()
            meta = _PROMPT_CAPABILITY_MAP.get(skill_id, {})
            items.append(
                {
                    "id": skill_id,
                    "title": meta.get("title", skill.get("name") or skill_id),
                    "summary": meta.get("summary", skill.get("description") or ""),
                    "category": meta.get("category", "prompt"),
                    "type": "prompt",
                    "source_kind": str(skill.get("source_kind", "workspace") or "workspace"),
                    "model_callable": False,
                    "restricted": False,
                    "aliases": list((manifest.get("metadata", {}) or {}).get("aliases", []) if isinstance(manifest.get("metadata", {}), dict) else []),
                    "dispatch_skill_ids": [skill_id],
                    "dispatch_skill_count": 1,
                }
            )
        items.sort(
            key=lambda item: (
                0 if str(item.get("source_kind")) == "builtin" else 1,
                0 if str(item.get("type")) == "tool" else 1,
                str(item.get("title", "")).lower(),
            )
        )
        return items

    def _grouped_skills(self, skills_payload: dict[str, object]) -> dict[str, list[dict[str, object]]]:
        skill_items = [
            dict(item)
            for item in list(skills_payload.get("items", []))
            if isinstance(item, dict)
        ]
        groups = {
            "builtin": [],
            "workspace": [],
            "plugin": [],
            "prompt": [],
            "tool_dispatch": [],
            "restricted": [],
            "invalid": [],
        }
        for skill in skill_items:
            source_kind = str(skill.get("source_kind", "workspace") or "workspace")
            manifest = skill.get("manifest", {})
            manifest = manifest if isinstance(manifest, dict) else {}
            handler = manifest.get("handler", {})
            handler = handler if isinstance(handler, dict) else {}
            command_dispatch = str(handler.get("type", "") or "").strip()
            command_tool = str(handler.get("target", "") or "").strip()
            if source_kind in groups:
                groups[source_kind].append(skill)
            else:
                groups["workspace"].append(skill)
            if str(skill.get("status", "") or "") == "invalid" or skill.get("validation_errors"):
                groups["invalid"].append(skill)
            if command_dispatch == "tool_id":
                groups["tool_dispatch"].append(skill)
                if command_tool in _RESTRICTED_TOOL_IDS:
                    groups["restricted"].append(skill)
            else:
                groups["prompt"].append(skill)
        return groups

    def _grouped_tools(self, tools_payload: dict[str, object]) -> dict[str, list[dict[str, object]]]:
        tool_items = [
            dict(item)
            for item in list(tools_payload.get("items", []))
            if isinstance(item, dict)
        ]
        groups = {
            "builtin": [],
            "runtime": [],
            "model_callable": [],
            "restricted": [],
            "aliased": [],
        }
        for tool in tool_items:
            tool_id = str(tool.get("id", "") or "").strip()
            if tool_id in _BUILTIN_CAPABILITY_MAP:
                groups["builtin"].append(tool)
            else:
                groups["runtime"].append(tool)
            if bool(tool.get("model_callable")):
                groups["model_callable"].append(tool)
            if tool_id in _RESTRICTED_TOOL_IDS:
                groups["restricted"].append(tool)
            if list(tool.get("aliases", [])):
                groups["aliased"].append(tool)
        return groups

    def _tool_policy_panel(self) -> dict[str, object]:
        policy = self.service.config.tool_policy
        return {
            "enabled": policy.enabled,
            "allowed_roots": list(policy.allowed_roots),
            "write_enabled": policy.write_enabled,
            "write_allowed_roots": list(policy.write_allowed_roots),
            "exec_enabled": policy.exec_enabled,
            "exec_allowed_roots": list(policy.exec_allowed_roots),
            "exec_allowlist": list(policy.exec_allowlist),
            "resolved_allowed_roots": self._resolve_policy_roots(policy.allowed_roots),
            "resolved_write_allowed_roots": self._resolve_policy_roots(policy.write_allowed_roots),
            "resolved_exec_allowed_roots": self._resolve_policy_roots(policy.exec_allowed_roots),
            "restricted_tools": sorted(_RESTRICTED_TOOL_IDS),
        }

    def _resolve_policy_roots(self, raw_roots: list[str]) -> list[str]:
        config_path = str(self.service.config.config_path or "").strip()
        if config_path:
            try:
                base_dir = Path(config_path).resolve().parent
            except OSError:
                base_dir = Path.cwd().resolve()
        else:
            base_dir = Path.cwd().resolve()
        roots: list[str] = []
        for item in raw_roots:
            token = str(item or "").strip()
            lowered = token.replace("\\", "/").strip().lower()
            if lowered in {"data", "./data", ".\\data"}:
                path = Path(self.service.config.data_root or ".").expanduser()
            elif lowered in {".", "./", ".\\"}:
                path = base_dir
            else:
                path = Path(token).expanduser()
                if not path.is_absolute():
                    path = base_dir / path
            try:
                resolved = str(path.resolve(strict=False))
            except OSError:
                continue
            if resolved not in roots:
                roots.append(resolved)
        return roots
