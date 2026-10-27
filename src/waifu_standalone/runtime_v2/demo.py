"""Offline vertical slice: deterministic generation, durable recovery, no network."""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from ..character.cards import CharacterCard
from ..models import InboundEvent, MessageSegment, OutboundMessage
from .contracts import CharacterSnapshot, DeliveryReceipt, EventEnvelope
from .engine import ConversationEngine
from .store import RuntimeStore


class _DemoGenerator:
    async def generate(self, context, event, history):
        name = json.loads(context.binding.character.card_json)["assistant_name"]
        return OutboundMessage(event.launcher_id, "person", f"{name}：收到「{event.plain_text}」（此前 {len(history)} 条记录）")


class _DemoSender:
    def __init__(self):
        self.sent = []
        self.fail_next = False

    async def send(self, message):
        self.sent.append(asdict(message))
        if self.fail_next:
            self.fail_next = False
            raise TimeoutError("simulated ambiguous send")
        return DeliveryReceipt("captured")


def run_demo(output_dir: str | Path) -> dict:
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    return asyncio.run(_run(root))


async def _run(root: Path) -> dict:
    path = root / "runtime-v2-preview.sqlite3"
    sender = _DemoSender()
    alpha = CharacterSnapshot.create("alpha", asdict(CharacterCard(assistant_name="小夏")))
    beta = CharacterSnapshot.create("beta", asdict(CharacterCard(assistant_name="阿秋")))

    def envelope(mid, text):
        incoming = InboundEvent("demo-user", "person", "demo-user", "演示用户",
                                [MessageSegment("text", text=text)], message_id=mid)
        return EventEnvelope.from_inbound("demo-account", incoming)

    store = RuntimeStore(path, "demo-account", "capture")
    first = envelope("demo-1", "你好")
    try:
        store.admit(first, alpha)
    finally:
        store.close()
    # Restart before processing any work; recover from the same durable inbox.
    store = RuntimeStore(path, "demo-account", "capture")
    try:
        store.recover()
        engine = ConversationEngine(store, _DemoGenerator(), sender, "offline-demo")
        await engine.step("demo-user")
        duplicate = store.admit(first, alpha)
        store.enqueue_binding("demo-user", beta, "switch-demo")
        store.admit(envelope("demo-2", "换个角色继续聊"), alpha)
        await engine.step("demo-user")
        await engine.step("demo-user")
        sender.fail_next = True
        store.admit(envelope("demo-3", "模拟发送结果不明"), alpha)
        await engine.step("demo-user")
        before = len(sender.sent)
        await engine.step("demo-user")
        unknown = store.snapshot()["outbox"][0]
        report = {
            "network_used": False,
            "recovered_pending_input": True,
            "duplicate_suppressed": duplicate.duplicate,
            "ambiguous_delivery_not_retried": len(sender.sent) == before,
            "unknown_delivery_id": unknown["outbox_id"],
            "captured_attempts": sender.sent,
            "runtime": store.snapshot(),
            "database": str(path),
        }
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    finally:
        store.close()
