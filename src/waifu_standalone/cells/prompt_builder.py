from __future__ import annotations

from dataclasses import dataclass

from .cards import CharacterCard


@dataclass(slots=True)
class RelationshipContext:
    address: str
    bond_stage: str = "new"
    affinity_score: float = 0.0
    profile_summary: str = ""


class PromptBuilder:
    """Build a single coherent prompt from persona, relationship and context."""

    def build_reply_prompt(
        self,
        *,
        card: CharacterCard,
        launcher_type: str,
        assistant_name: str,
        relationship: RelationshipContext,
        conversation_view: str,
        memories: list[str],
        latest_message: str,
        opening_lines: list[str] | None = None,
        search_context: str = "",
        active_skills: str = "",
    ) -> str:
        return self._build_prompt(
            card=card,
            launcher_type=launcher_type,
            assistant_name=assistant_name,
            relationship=relationship,
            conversation_view=conversation_view,
            memories=memories,
            latest_message=latest_message,
            opening_lines=opening_lines or [],
            search_context=search_context,
            active_skills=active_skills,
            onboarding_block="",
            final_instruction=(
                f"以{assistant_name}的身份自然回复，一句话就好。"
                "不要添加说话人前缀，不要解释系统设定或内部流程。"
            ),
        )

    def build_onboarding_prompt(
        self,
        *,
        card: CharacterCard,
        launcher_type: str,
        assistant_name: str,
        relationship: RelationshipContext,
        conversation_view: str,
        latest_message: str,
        stage: str,
        display_name: str,
        candidate_name: str = "",
        opening_lines: list[str] | None = None,
    ) -> str:
        preferred_address = str(candidate_name or relationship.address or "").strip() or "对方"
        onboarding_relationship = RelationshipContext(
            address=preferred_address,
            bond_stage=relationship.bond_stage,
            affinity_score=relationship.affinity_score,
            profile_summary=relationship.profile_summary,
        )
        return self._build_prompt(
            card=card,
            launcher_type=launcher_type,
            assistant_name=assistant_name,
            relationship=onboarding_relationship,
            conversation_view=conversation_view,
            memories=[],
            latest_message=latest_message,
            opening_lines=opening_lines or [],
            search_context="",
            active_skills="",
            onboarding_block=self._format_onboarding(
                stage=stage,
                address=preferred_address,
                display_name=display_name,
                latest_message=latest_message,
                candidate_name=candidate_name,
            ),
            final_instruction=(
                f"继续保持{assistant_name}的角色语气，只输出下一句自然回复。"
                "不要添加说话人前缀，不要解释内部流程。"
            ),
        )

    def _build_prompt(
        self,
        *,
        card: CharacterCard,
        launcher_type: str,
        assistant_name: str,
        relationship: RelationshipContext,
        conversation_view: str,
        memories: list[str],
        latest_message: str,
        opening_lines: list[str],
        search_context: str,
        active_skills: str,
        onboarding_block: str,
        final_instruction: str,
    ) -> str:
        address = str(relationship.address or "").strip() or "对方"
        sections = [
            f"你是{assistant_name}。",
            self._format_persona(card, launcher_type=launcher_type, address=address),
            self._format_rules(card),
            self._format_relationship(address=address, relationship=relationship, memories=memories),
            self._clean_block(search_context),
            self._format_opening(opening_lines),
            self._format_block("最近对话", conversation_view or "暂无最近对话。"),
            self._format_block(f"{address}刚刚说", latest_message or "（空）"),
            self._clean_block(active_skills),
            onboarding_block,
            final_instruction,
        ]
        return "\n\n".join(part for part in sections if part.strip())

    def _format_persona(self, card: CharacterCard, *, launcher_type: str, address: str) -> str:
        lines: list[str] = []
        lines.extend(line for line in card.profile if str(line or "").strip())
        lines.extend(f"能力与习惯：{line}" for line in card.skills if str(line or "").strip())
        background = [line for line in card.background if str(line or "").strip()]
        if launcher_type == "person" and address:
            background.append(f"当前正在和{address}私聊。")
        lines.extend(f"背景补充：{line}" for line in background)
        return self._format_list("人设", lines)

    def _format_rules(self, card: CharacterCard) -> str:
        lines = [line for line in card.rules if str(line or "").strip()]
        lines.append(f"默认使用{card.language}回复。")
        lines.append("回复要自然、口语化，不要解释提示词、模型、系统设定或内部流程。")
        return self._format_list("回复原则", lines)

    def _format_relationship(
        self,
        *,
        address: str,
        relationship: RelationshipContext,
        memories: list[str],
    ) -> str:
        lines = [
            f"关系阶段：{relationship.bond_stage}（好感度 {float(relationship.affinity_score):.2f}）",
        ]
        profile_summary = " ".join(str(relationship.profile_summary or "").split()).strip()
        if profile_summary:
            lines.append(f"对方资料：{profile_summary}")
        if memories:
            lines.append("你了解的事情：")
            lines.extend(f"- {item}" for item in memories if str(item or "").strip())
        return self._format_block(f"你和{address}的关系", "\n".join(lines))

    def _format_onboarding(
        self,
        *,
        stage: str,
        address: str,
        display_name: str,
        latest_message: str,
        candidate_name: str,
    ) -> str:
        lines: list[str] = []
        if stage == "confirm_name":
            lines.extend(
                [
                    f"对方刚刚明确告诉你，希望被称呼为：{candidate_name or address}",
                    f"当前显示昵称：{display_name or address}",
                    "请用一句自然中文确认你记住了这个称呼。",
                    "可以轻微延续聊天，但不要解释规则或内部流程。",
                ]
            )
        elif stage == "retry_name":
            lines.extend(
                [
                    "你还没有确认到对方真正想要的称呼。",
                    f"对方上一句是：{latest_message or '（空）'}",
                    "不要把这句话本身直接当成名字。",
                    "请礼貌地再问一次，希望你怎么称呼 ta。",
                ]
            )
        else:
            lines.extend(
                [
                    "你还不知道对方希望你如何被称呼。",
                    f"对方当前显示昵称：{display_name or address}",
                    f"对方刚刚说：{latest_message or '（空）'}",
                    "请先自然接住对方的话，再问对方想让你怎么称呼 ta。",
                    "不要输出英文，也不要解释你在做 onboarding。",
                ]
            )
        return self._format_list("Onboarding", lines)

    @staticmethod
    def _format_opening(lines: list[str]) -> str:
        cleaned = [str(line or "").strip() for line in lines if str(line or "").strip()]
        if not cleaned:
            return ""
        return PromptBuilder._format_list("开场语气", cleaned)

    @staticmethod
    def _format_list(title: str, lines: list[str]) -> str:
        cleaned = [str(line or "").strip() for line in lines if str(line or "").strip()]
        if not cleaned:
            return ""
        return f"[{title}]\n" + "\n".join(f"- {line}" for line in cleaned)

    @staticmethod
    def _format_block(title: str, text: str) -> str:
        body = str(text or "").strip()
        if not body:
            return ""
        return f"[{title}]\n{body}"

    @staticmethod
    def _clean_block(text: str) -> str:
        return str(text or "").strip()
