from __future__ import annotations

import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app import WaifuService


class ClawRuntimeAdminService:
    def __init__(self, service: WaifuService) -> None:
        self.service = service

    def get_claw_runtime_panel(self, *, refresh: bool = False) -> dict[str, object]:
        svc = self.service
        panel = svc.claw_runtime.describe(refresh=refresh)
        if refresh and svc.config.claw_runtime.enabled:
            sync_result = self.sync_workspace_bundles()
            if sync_result["imported_count"] or sync_result["errors"]:
                panel["workspace_sync"] = sync_result
            try:
                plugins = svc.claw_runtime.list_plugins()
            except Exception as exc:
                panel["plugins"] = {"items": [], "summary": {}, "error": str(exc)}
            else:
                panel["plugins"] = plugins
            try:
                tools = svc.claw_runtime.list_tools()
            except Exception as exc:
                panel["tools"] = {"items": [], "error": str(exc)}
            else:
                panel["tools"] = tools
        return panel

    def sync_workspace_bundles(self) -> dict[str, object]:
        svc = self.service
        if not svc.config.claw_runtime.enabled:
            return {"items": [], "imported_count": 0, "skipped_count": 0, "errors": []}
        return svc.claw_runtime.sync_workspace_bundles(svc.skills.workspace_root, overwrite=False)

    def list_claw_plugins(self) -> dict[str, object]:
        self.sync_workspace_bundles()
        return self.service.claw_runtime.list_plugins()

    def inspect_claw_plugin(self, plugin_id: str) -> dict[str, object] | None:
        return self.service.claw_runtime.inspect_plugin(plugin_id)

    def check_claw_plugins(self) -> dict[str, object]:
        self.sync_workspace_bundles()
        return self.service.claw_runtime.check_plugins()

    def update_claw_plugin(self, plugin_id: str) -> dict[str, object]:
        svc = self.service
        detail = self.inspect_claw_plugin(plugin_id)
        if detail is None:
            raise ValueError("claw plugin not found")
        source_id = str(detail.get("source_id", "") or "").strip()
        source_url = str(detail.get("source_url", "") or "").strip()
        if not source_id or not source_url:
            raise ValueError("claw plugin is missing marketplace source metadata")
        with tempfile.TemporaryDirectory() as tmpdir:
            prepared = svc.marketplace.prepare_skill_bundle(source_id, source_url, tmpdir)
            return svc.import_skill_bundle(
                prepared["path"],
                overwrite=True,
                source_metadata={
                    "source_id": source_id,
                    "source_url": prepared.get("source_url", source_url),
                    "bundle_url": prepared.get("bundle_url", ""),
                    "page_url": prepared.get("page_url", ""),
                    "plugin_id": str(detail.get("id", plugin_id) or plugin_id),
                },
            )
