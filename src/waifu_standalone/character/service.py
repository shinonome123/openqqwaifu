from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import InboundEvent, MessageSegment, SessionMemory

if TYPE_CHECKING:
    from ..app import WaifuService


class CharacterService:
    def __init__(self, service: WaifuService) -> None:
        self.service = service

    def active_character_id(self) -> str:
        svc = self.service
        return str(svc.cards.active_character() or svc.config.character or "default").strip() or "default"

    def activate_character(self, character: str, *, reset_sessions: bool = True) -> str:
        svc = self.service
        active_character = svc.cards.set_active_character(character)
        svc.config.character = active_character
        svc._rebind_character_scoped_storage(active_character, reset_sessions=reset_sessions)
        return active_character

    def get_character_panel(self, character: str = "") -> dict[str, object]:
        svc = self.service
        target = str(character or svc.cards.active_character()).strip() or "default"
        bundle = svc.cards.get_editor_bundle(target)
        return {
            "current_character": svc.cards.active_character(),
            **bundle,
        }

    def save_character_panel(self, payload: dict[str, object]) -> dict[str, object]:
        svc = self.service
        character = str(payload.get("character") or svc.config.character or "default").strip() or "default"
        set_active = bool(payload.get("set_active", True))
        person = str(payload.get("person_content") or "")
        group = str(payload.get("group_content") or "")
        person_fields = payload.get("person_fields")
        group_fields = payload.get("group_fields")
        shared_fields = payload.get("shared_fields")
        portrait = payload.get("portrait")
        bundle = svc.cards.save_editor_bundle(
            character,
            person,
            group,
            person_fields=person_fields if isinstance(person_fields, dict) else None,
            group_fields=group_fields if isinstance(group_fields, dict) else None,
            shared_fields=shared_fields if isinstance(shared_fields, dict) else None,
            portrait=portrait if isinstance(portrait, dict) else None,
        )
        if isinstance(portrait, dict) and bool(portrait.get("generate", portrait.get("auto_generate", False))):
            bundle["portrait"] = self.generate_character_portrait(character, bundle, portrait)
        if set_active:
            self.activate_character(character, reset_sessions=True)
        svc._persist_config()
        return {
            "current_character": svc.cards.active_character(),
            **bundle,
        }

    def get_character_portrait(self, character: str) -> tuple[bytes, str] | None:
        return self.service.cards.load_portrait_asset(character)

    def preview_character_panel(self, payload: dict[str, object]) -> dict[str, object]:
        svc = self.service
        launcher_type = str(payload.get("launcher_type") or "person").strip().lower()
        if launcher_type not in {"person", "group"}:
            raise ValueError("launcher_type must be 'person' or 'group'")
        message = str(payload.get("message") or "").strip()
        if not message:
            raise ValueError("message is required")

        shared_fields = payload.get("shared_fields") if isinstance(payload.get("shared_fields"), dict) else {}
        person_fields = payload.get("person_fields") if isinstance(payload.get("person_fields"), dict) else {}
        group_fields = payload.get("group_fields") if isinstance(payload.get("group_fields"), dict) else {}
        variant_fields = person_fields if launcher_type == "person" else group_fields
        card = svc.cards.build_preview_card(
            shared_fields=shared_fields if isinstance(shared_fields, dict) else None,
            variant_fields=variant_fields if isinstance(variant_fields, dict) else None,
        )

        user_name = str(payload.get("user_name") or card.user_name or "User").strip() or card.user_name or "User"
        history = svc._normalize_preview_history(payload.get("history"), assistant_name=card.assistant_name)
        session = SessionMemory(
            launcher_id=f"preview-{launcher_type}",
            launcher_type=launcher_type,
            history=svc._preview_history_lines(history),
            metadata={},
        )
        event = InboundEvent(
            launcher_id=session.launcher_id,
            launcher_type=launcher_type,
            sender_id="preview-user",
            sender_name=user_name,
            segments=[MessageSegment(kind="text", text=message)],
        )
        emotion = svc.emotions.analyze(event, session)
        conversation_view = svc._preview_conversation_view(
            session.history,
            assistant_name=card.assistant_name,
            limit=svc.config.history_window_messages,
        )
        analysis_hint = svc.generator.generate_analysis(
            event,
            session,
            assistant_name=card.assistant_name,
            conversation_view=conversation_view,
            memory_hints=[],
            speaker_notes=[],
            active_skills=[],
            address_override=user_name,
            card_override=card,
        )
        reply_text = svc.generator.generate_reply(
            event,
            session,
            emotion,
            assistant_name=card.assistant_name,
            address_override=user_name,
            card_override=card,
            conversation_view=conversation_view,
            memory_hints=[],
            speaker_notes=[],
            analysis_hint=analysis_hint,
            active_skills=[],
        )
        transcript = [
            *history,
            {"role": "user", "text": message},
            {"role": "assistant", "text": reply_text},
        ]
        return {
            "launcher_type": launcher_type,
            "assistant_name": card.assistant_name,
            "user_name": user_name,
            "reply_text": reply_text,
            "analysis_hint": analysis_hint,
            "transcript": transcript,
            "llm_ready": svc.generator.llm_ready,
        }

    def generate_character_portrait(
        self,
        character: str,
        bundle: dict[str, object],
        portrait_payload: dict[str, object],
    ) -> dict[str, object]:
        svc = self.service
        current = dict(bundle.get("portrait", {}) if isinstance(bundle.get("portrait"), dict) else {})
        shared_fields = bundle.get("shared", {}) if isinstance(bundle.get("shared"), dict) else {}
        person = bundle.get("person", {}) if isinstance(bundle.get("person"), dict) else {}
        group = bundle.get("group", {}) if isinstance(bundle.get("group"), dict) else {}
        person_fields = person.get("fields", {}) if isinstance(person.get("fields"), dict) else {}
        group_fields = group.get("fields", {}) if isinstance(group.get("fields"), dict) else {}
        style = str(portrait_payload.get("style") or current.get("style") or "neon-pixel")
        prompt_suffix = str(portrait_payload.get("prompt_suffix") or current.get("prompt_suffix") or "")
        auto_generate = bool(portrait_payload.get("auto_generate", current.get("auto_generate", True)))
        prompt = svc.cards.build_portrait_prompt(
            character,
            shared_fields=shared_fields,
            person_fields=person_fields,
            group_fields=group_fields,
            portrait={"style": style, "prompt_suffix": prompt_suffix},
        )
        if not svc.generator.image_ready:
            current.update(
                {
                    "style": style,
                    "prompt_suffix": prompt_suffix,
                    "auto_generate": auto_generate,
                    "last_prompt": prompt,
                    "notice": "image provider is not configured",
                }
            )
            return current
        try:
            generated = svc.generator.generate_image(prompt)
            image_bytes, content_type = svc.generator.resolve_generated_image(generated.image_ref)
            saved = svc.cards.save_portrait_asset(
                character,
                image_bytes,
                content_type,
                prompt=generated.prompt,
                style=style,
                prompt_suffix=prompt_suffix,
                auto_generate=auto_generate,
            )
            saved.pop("notice", None)
            saved.pop("error", None)
            return saved
        except Exception as exc:
            current.update(
                {
                    "style": style,
                    "prompt_suffix": prompt_suffix,
                    "auto_generate": auto_generate,
                    "last_prompt": prompt,
                    "error": str(exc) or "portrait generation failed",
                }
            )
            current.pop("notice", None)
            return current
