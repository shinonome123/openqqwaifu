from __future__ import annotations

import asyncio
import hashlib
from copy import deepcopy

from ..config import AppConfig
from ..infra.llm_clients import LLMClientError, UnsupportedMessageRoles, build_llm_client
from ..models import OutboundMessage
from ..runtime_v2.contracts import ReplyProposal
from .context import BUILDER_VERSION, ContextBuilder, ImagesUnavailable
from .memory_output import RESPONSE_CONTRACT, parse_response


class ReplyComposer:
    """One structured model request, with no local tools or hidden fallback call."""

    def __init__(self, config: AppConfig, *, capture: bool, client=None, context_observer=None):
        self.config = deepcopy(config)
        self.capture = capture
        if type(self.config.runtime_v2.structured_memory_enabled) is not bool:
            raise ValueError("structured_memory_enabled must be a boolean")
        self.builder = ContextBuilder(self.config.runtime_v2, multimodal_enabled=config.multimodal_enabled)
        if self.config.runtime_v2.openai_instruction_role not in {"system", "developer"}:
            raise ValueError("invalid openai_instruction_role")
        if self.config.runtime_v2.openai_output_token_parameter not in {"max_completion_tokens", "max_tokens"}:
            raise ValueError("invalid openai_output_token_parameter")
        self.client = client or build_llm_client(self.config)
        self.context_observer = context_observer
        if self.config.llm.enabled and not getattr(self.client, "supports_message_roles", False):
            if client is None:
                self.client.close()
            raise UnsupportedMessageRoles("runtime v2 requires a backend that preserves message roles; Dify's string-query interface is not supported")

    async def _record(self, context, report):
        if self.context_observer is not None:
            await asyncio.to_thread(self.context_observer, context, report)

    async def generate(self, context, event, history):
        text = event.command_text(self.config.bot_account_id)
        if not text and not event.image_count:
            return None
        if text.startswith("/skill"):
            return OutboundMessage(event.launcher_id, "person", "当前会话处于新版私聊试运行，技能命令尚未接入。")
        if any(text.startswith(prefix) for prefix in self.config.ignore_prefixes):
            return None
        try:
            bundle = self.builder.build(context, event, history,
                response_contract=RESPONSE_CONTRACT if self.config.runtime_v2.structured_memory_enabled else "")
        except ValueError as exc:
            await self._record(context, {"builder_version": BUILDER_VERSION, "status": "context_rejected",
                                         "error_type": type(exc).__name__})
            raise
        report = bundle.report()
        report.update(backend=str(self.config.llm.backend), status="prepared")
        if not self.config.llm.enabled:
            report["status"] = "offline_preview" if self.capture else "model_disabled"
            await self._record(context, report)
            if self.capture:
                # A deterministic preview is explicitly identified; it must not
                # be presented as evidence that character quality was evaluated.
                return OutboundMessage(event.launcher_id, "person", "[离线预览] 已构建角色与对话上下文，未调用模型。")
            raise LLMClientError("model is disabled")
        if not getattr(self.client, "enabled", False):
            report["status"] = "model_unconfigured"
            await self._record(context, report)
            raise LLMClientError("model is not configured")
        if report["image_count"] and not getattr(self.client, "supports_images", False):
            report["status"] = "images_unsupported"
            await self._record(context, report)
            raise ImagesUnavailable("configured backend does not support images")
        await self._record(context, report)
        # Namespace provider identity by account, character, conversation AND
        # simulation/live mode, without placing raw QQ identifiers in that key.
        identity = [context.binding.bot_account_id, context.binding.character.character_id,
                    context.binding.conversation_id, "capture" if self.capture else "live", str(context.data_generation)]
        user_key = "waifu-v2-" + hashlib.sha256("\0".join(identity).encode()).hexdigest()[:40]
        try:
            output_options = {"json_object": True} if (self.config.runtime_v2.structured_memory_enabled
                and getattr(self.client, "supports_json_object", False)) else {}
            answer = await self.client.ainvoke_messages(bundle.wire_messages(), user=user_key,
                max_output_tokens=self.config.runtime_v2.max_output_tokens,
                instruction_role=self.config.runtime_v2.openai_instruction_role,
                output_token_parameter=self.config.runtime_v2.openai_output_token_parameter, **output_options)
            if not isinstance(answer, str) or not answer.strip():
                raise LLMClientError("model returned no text")
            changes = ()
            if self.config.runtime_v2.structured_memory_enabled:
                answer, changes = parse_response(answer, event, set(bundle.memory_references))
        except Exception as exc:
            report.update(status="model_failed", error_type=type(exc).__name__)
            await self._record(context, report)
            raise
        report["status"] = "generated"
        await self._record(context, report)
        message = OutboundMessage(event.launcher_id, "person", answer.strip())
        return ReplyProposal(message, changes) if changes else message

    def close(self):
        self.client.close()

    async def generate_followup(self, context, before_call):
        from .followup_draft import BUILDER_VERSION as DRAFT_VERSION, build_context, parse_result
        bundle = build_context(context, self.config.runtime_v2)
        report = bundle.report()
        report.update(builder_version=DRAFT_VERSION, backend=str(self.config.llm.backend), status="prepared")
        if not self.config.llm.enabled or not getattr(self.client, "enabled", False):
            raise LLMClientError("draft model is disabled or unconfigured")
        # Persist redacted provenance and revalidate after context construction,
        # immediately before the single provider call. No preview substitution.
        await before_call(report)
        identity = [context.binding.bot_account_id, context.binding.character.character_id,
                    context.binding.conversation_id, "capture" if self.capture else "live", str(context.data_generation), "followup-draft"]
        answer = await self.client.ainvoke_messages(bundle.wire_messages(),
            user="waifu-v2-" + hashlib.sha256("\0".join(identity).encode()).hexdigest()[:40],
            max_output_tokens=self.config.runtime_v2.max_output_tokens,
            instruction_role=self.config.runtime_v2.openai_instruction_role,
            output_token_parameter=self.config.runtime_v2.openai_output_token_parameter,
            **({"json_object": True} if getattr(self.client, "supports_json_object", False) else {}))
        return parse_result(answer)
