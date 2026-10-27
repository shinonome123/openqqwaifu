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
        # "/skill <name> ..." is a command even though "/" is an ignored prefix.
        explicit_skill = svc.dispatcher.resolve_explicit_skill_request(text)
        if explicit_skill is None and text and any(text.startswith(prefix) for prefix in svc.config.ignore_prefixes):
            return None
        # Keep ambient group chatter until the bot is addressed, so the next reply
        # has the surrounding conversation instead of only the @ message.
        svc._remember_ambient_event(event, character_id=current_character)
        if not svc.gate.should_reply(event):
            return None
        # Card commands cost no model call; the reply budget does not hold them back.
        if not svc.character_switcher.is_command(event, text) and not svc.gate.acquire_group_reply_slot(event):
            return None

        await svc.onboarding.aremember_directory_member(event)
        ambient_context = svc._peek_ambient_history(event, character_id=current_character)
        session = await svc.memory.asave_user_event(
            event,
            character_id=current_character,
            context_lines=ambient_context,
            bot_account_id=svc.config.bot_account_id,
        )
        svc._clear_ambient_history(event, character_id=current_character)
        # "切换人物卡" lists the cards, "切换<名字>" switches to one.
        handled, switch_reply = await svc.character_switcher.ahandle(event, text)
        if handled:
            return switch_reply
        # The bot is always the character on the card. What this person calls it (their
        # alias for it) goes into a speaker note; it must not replace the bot's own name.
        assistant_alias = svc._assistant_alias_for_user(event.sender_id, character_id=current_character)
        assistant_name = (
            svc.generator.resolve_assistant_name(event.launcher_type, session)
            or assistant_alias
            or svc.config.assistant_name
        )
        session = await svc.persona.asanitize_session_state(session, assistant_name=assistant_name)
        await svc.persona.asanitize_member_state(
            group_id=event.launcher_id if event.launcher_type == "group" else "",
            user_id=event.sender_id,
            character_id=current_character,
        )
        address = await svc._resolve_address_async(event, session)
        dialogue_assistant_name = assistant_name
        inbound_behavior = svc.event_engine.capture_inbound(
            event,
            text=latest_message,
            character_id=current_character,
        )
        svc._record_behavior_event(inbound_behavior)

        repeat_reply = svc.gate.build_repeat_reply(event, session, address=address)
        if repeat_reply is not None:
            return await svc.emitter.aemit(event, repeat_reply, assistant_name=dialogue_assistant_name)

        active_skills = svc.tool_orchestrator.prompt_skill_cards(latest_message)
        await svc._store_active_skills_async(session, active_skills)
        if explicit_skill is None:
            # "生图 一只猫" generates the image directly instead of asking the model first.
            explicit_skill = svc.dispatcher.resolve_image_command(text)
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
                background_image_delivery=background_image_delivery,
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
            return await svc.emitter.aemit(event, unavailable, assistant_name=dialogue_assistant_name)

        emotion = svc.emotions.analyze(event, session)
        search_context = SearchContext()
        await svc._store_search_context_async(session, search_context)
        conversation_view = await svc.memory.aformat_dialogue(
            event.launcher_id,
            event.launcher_type,
            assistant_name=dialogue_assistant_name,
            limit=svc.config.history_window_messages,
            max_chars=svc.config.history_window_chars,
            exclude_last=True,
            character_id=current_character,
        )
        # The same window as chat turns, sent as user/assistant messages.
        history_turns = await svc.memory.adialogue_turns(
            event.launcher_id,
            event.launcher_type,
            limit=svc.config.history_window_messages,
            max_chars=svc.config.history_window_chars,
            exclude_last=True,
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
        speaker_notes = await svc.knowledge._adirectory_member_notes(event)
        if assistant_alias and assistant_alias != assistant_name:
            speaker_notes.insert(
                0,
                f"{address}管你叫「{assistant_alias}」，这是对方给你起的称呼；你本人仍是{assistant_name}。",
            )
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
        sticker_hint = await asyncio.to_thread(svc.stickers.prompt_section, event)
        generated_reply = await svc.generator.agenerate_reply_message(
            event,
            session,
            emotion,
            assistant_name=assistant_name,
            address_override=address,
            search_hint=search_context.summary,
            search_context=search_context.to_prompt_block(),
            conversation_view=conversation_view,
            history_turns=history_turns,
            memory_hints=memory_hints,
            speaker_notes=speaker_notes,
            analysis_hint=analysis_hint,
            active_skills=active_skills,
            tool_context=tool_context,
            sticker_hint=sticker_hint,
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
        reply_text, sticker = await asyncio.to_thread(svc.stickers.take, reply_text, event)
        if not reply_text and not generated_reply.images:
            return None
        message = OutboundMessage(
            launcher_id=event.launcher_id,
            launcher_type=event.launcher_type,
            text=reply_text,
            images=list(generated_reply.images),
            sticker=sticker,
            # Without a model the reply is a canned fallback; it goes out whole.
            conversational=svc.generator.llm_ready,
        )
        emitted = await svc.emitter.aemit(
            event,
            message,
            # The emitter only uses this to label the rolling summary.
            assistant_name=dialogue_assistant_name,
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
            assistant_name=dialogue_assistant_name,
            address=address,
            conversation_view=conversation_view,
        )
        return emitted
