from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..memory import KnowledgeService
from ..models import InboundEvent, OutboundMessage
from ..systems.searching import SearchContext

if TYPE_CHECKING:
    from ..app import WaifuService


class ReplyFlowService:
    def __init__(self, service: WaifuService) -> None:
        self.service = service

    async def handle_event_async_locked(
        self,
        event: InboundEvent,
        *,
        background_image_delivery: bool,
    ) -> OutboundMessage | None:
        svc = self.service
        svc._ensure_character_scope_bound()
        current_character = svc._active_character_id()
        text = event.command_text(svc.config.bot_account_id)
        live_runtime = svc._requires_live_llm()
        latest_message = svc._latest_message_text(event, text)
        svc._record_inbound(event, text)
        if not text and event.image_count == 0:
            return None
        if text and any(text.startswith(prefix) for prefix in svc.config.ignore_prefixes):
            return None
        if not svc.gate.should_reply(event):
            return None

        await svc.onboarding.aremember_directory_member(event)
        session = await svc.memory.asave_user_event(event, character_id=current_character)
        assistant_name = svc._resolve_assistant_name(event, session, character_id=current_character)
        session = await svc.persona.asanitize_session_state(session, assistant_name=assistant_name)
        await svc.persona.asanitize_member_state(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
            character_id=current_character,
        )
        address = await svc._resolve_address_async(event, session)
        inbound_behavior = svc.event_engine.capture_inbound(
            event,
            text=latest_message,
            character_id=current_character,
        )
        svc._record_behavior_event(inbound_behavior)

        repeat_reply = svc.gate.build_repeat_reply(event, session, address=address)
        if repeat_reply is not None:
            return await svc.emitter.aemit(event, repeat_reply, assistant_name=assistant_name)

        active_skills = svc.tool_orchestrator.prompt_skill_cards(latest_message)
        await svc._store_active_skills_async(session, active_skills)
        explicit_skill = svc.dispatcher.resolve_explicit_skill_request(latest_message)
        if explicit_skill is not None:
            skill, raw_args = explicit_skill
            return await svc.dispatcher.adispatch_explicit_skill(
                event,
                session,
                skill=skill,
                raw_args=raw_args,
                address=address,
                assistant_name=assistant_name,
                active_skills=active_skills,
                background_image_delivery=svc.config.background_image_delivery,
            )
        tool_context = svc.tool_orchestrator.exposure_context(
            event,
            session,
            latest_message=latest_message,
            address=address,
            assistant_name=assistant_name,
        )
        if live_runtime and not svc.generator.llm_ready:
            unavailable = OutboundMessage(
                launcher_id=event.launcher_id,
                launcher_type=event.launcher_type,
                text=svc.tool_orchestrator.unavailable_reply(address),
            )
            return await svc.emitter.aemit(event, unavailable, assistant_name=assistant_name)

        emotion = svc.emotions.analyze(event, session)
        search_context = SearchContext()
        await svc._store_search_context_async(session, search_context)
        conversation_view = await svc.memory.aformat_dialogue(
            event.launcher_id,
            event.launcher_type,
            assistant_name=assistant_name,
            limit=svc.config.history_window_messages,
            character_id=current_character,
        )
        recalled_memory_hints = await asyncio.to_thread(
            svc.knowledge_repository.recall_knowledge,
            scopes=svc.knowledge._knowledge_scopes(event),
            query=latest_message,
            limit=svc.config.memory_recall_limit,
            character_id=current_character,
        )
        knowledge_entries = await svc.knowledge._asession_knowledge_entries(event, latest_message)
        memory_hints = KnowledgeService._merge_memory_hints(
            recalled_memory_hints,
            [str(item.get("summary", "") or "").strip() for item in knowledge_entries],
            limit=svc.config.memory_recall_limit,
        )
        member_record = await svc.knowledge._amember_record(event)
        behavior_context = svc._behavior_context(event)
        graph_snapshot = svc.memory_graph.build(
            event=event,
            session=session,
            member=member_record,
            knowledge_entries=knowledge_entries,
            behavior_events=behavior_context,
        )
        speaker_notes = await svc.knowledge._adirectory_member_notes(event)
        for highlight in graph_snapshot.get("highlights", [])[:3] if isinstance(graph_snapshot, dict) else []:
            text_hint = str(highlight or "").strip()
            if text_hint:
                speaker_notes.append(text_hint)
        analysis_hint = await svc.thoughts.aanalyze(
            event,
            session,
            assistant_name=assistant_name,
            address=address,
            conversation_view=conversation_view,
            memory_hints=memory_hints,
            speaker_notes=speaker_notes,
            active_skills=active_skills,
            allow_fallback=not live_runtime,
        )
        generated_reply = await svc.generator.agenerate_reply_message(
            event,
            session,
            emotion,
            assistant_name=assistant_name,
            address_override=address,
            search_hint=search_context.summary,
            search_context=search_context.to_prompt_block(),
            conversation_view=conversation_view,
            memory_hints=memory_hints,
            speaker_notes=speaker_notes,
            analysis_hint=analysis_hint,
            active_skills=active_skills,
            tool_context=tool_context,
            allow_fallback=not live_runtime,
        )
        reply_text = generated_reply.text
        if "last_tool_calls" in session.metadata:
            await asyncio.to_thread(svc.memory.store.save, session)
        if live_runtime and not str(reply_text or "").strip() and not generated_reply.images:
            return None
        reply_text = await svc.onboarding.amaybe_append_soft_ask(
            event,
            session,
            latest_message=latest_message,
            assistant_name=assistant_name,
            reply_text=reply_text,
            character_id=current_character,
        )
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=reply_text,
            images=list(generated_reply.images),
        )
        emitted = await svc.emitter.aemit(
            event,
            message,
            assistant_name=assistant_name,
            emotion=emotion,
            search_used=bool(search_context.active),
            behavior_reason="reply",
            character_id=current_character,
        )
        await svc._schedule_knowledge_writeback_async(
            event,
            session=await svc.memory.aload(
                event.launcher_id,
                event.launcher_type,
                character_id=current_character,
            ),
            latest_message=latest_message,
            assistant_name=assistant_name,
            address=address,
            conversation_view=conversation_view,
        )
        return emitted
