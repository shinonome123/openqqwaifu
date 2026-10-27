"""Run the small synthetic role-experience set with an explicitly supplied model.

Credentials come only from WAIFU_EVAL_API_KEY. No application config, .env,
user database, QQ gateway, search tool or image provider is opened.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waifu_standalone.config import AppConfig, RuntimeV2Config
from waifu_standalone.conversation.composer import ReplyComposer
from waifu_standalone.infra.llm_clients import OpenAILLMClient
from waifu_standalone.models import InboundEvent, MessageSegment
from waifu_standalone.runtime_v2.contracts import DeliveryReceipt, EventEnvelope
from waifu_standalone.runtime_v2.engine import ConversationEngine
from waifu_standalone.runtime_v2.integration import snapshot_character
from waifu_standalone.runtime_v2.store import RuntimeStore


class ModelBudgetExceeded(RuntimeError):
    pass


class RecordedClient(OpenAILLMClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.usage_records = []
        self.output_records = []

    async def _apost_json(self, *args, **kwargs):
        raw = await super()._apost_json(*args, **kwargs)
        data = json.loads(raw)
        usage = data.get("usage") or {}
        self.usage_records.append({"model_returned": data.get("model", ""),
            "tokens": {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
                       if type(usage.get(k)) is int}})
        choice = (data.get("choices") or [{}])[0]
        self.output_records.append({"content": (choice.get("message") or {}).get("content", ""),
                                    "finish_reason": choice.get("finish_reason")})
        return raw


class LimitedClient:
    enabled = supports_message_roles = True
    supports_images = False

    def __init__(self, client, limit):
        self.client, self.limit, self.calls = client, limit, 0

    @property
    def supports_json_object(self):
        return getattr(self.client, "supports_json_object", False)

    async def ainvoke_messages(self, *args, **kwargs):
        if self.calls >= self.limit:
            raise ModelBudgetExceeded("evaluation model-call limit reached")
        self.calls += 1
        return await self.client.ainvoke_messages(*args, **kwargs)

    def close(self):
        # One shared client is closed by the runner after all scenarios.
        pass


class CaptureSender:
    async def send(self, message):
        return DeliveryReceipt("captured")


async def run_experience(output_dir, plan, client, model, *, max_calls=24, max_output_tokens=900,
                         output_token_parameter="max_completion_tokens", structured_memory=True):
    if not 1 <= max_calls <= 24:
        raise ValueError("max_calls must be between 1 and 24")
    if not 128 <= max_output_tokens <= 8192 or output_token_parameter not in {"max_completion_tokens", "max_tokens"}:
        raise ValueError("invalid output token limit or parameter")
    if type(structured_memory) is not bool:
        raise ValueError("structured_memory must be a boolean")
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    bounded = LimitedClient(client, max_calls)
    report = {"mode": "real_model_synthetic_dialogues", "model_requested": model,
        "provider_model_identity": "reported name not independently verified", "real_qq_sends": 0,
        "max_model_calls": max_calls, "max_output_tokens_per_call": max_output_tokens,
        "output_token_parameter": output_token_parameter,
        "structured_memory_enabled": structured_memory,
        "sampling": "provider defaults; no temperature or seed override",
        "started_at": time.time(), "scenarios": [], "human_review": "not_yet_performed"}

    def checkpoint():
        report["model_calls"] = bounded.calls
        report["usage_records"] = getattr(client, "usage_records", [])
        (output_dir / "transcripts.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    try:
        with tempfile.TemporaryDirectory(prefix="waifu-role-evaluation-") as root:
            for role in plan["characters"]:
                for scenario in plan["scenarios"]:
                    if bounded.calls >= max_calls:
                        report["stop_reason"] = "model_call_budget_exhausted"
                        break
                    case_root = Path(root) / role / scenario["id"]
                    config = AppConfig(data_root=str(case_root), bot_account_id="evaluation-account", character=role,
                        runtime_v2=RuntimeV2Config(enabled=True, structured_memory_enabled=structured_memory,
                            reminder_timezone="Asia/Shanghai", max_output_tokens=max_output_tokens,
                            openai_output_token_parameter=output_token_parameter))
                    config.llm.enabled, config.llm.backend, config.llm.model = True, "openai", model
                    card = snapshot_character(config, role)
                    store = RuntimeStore(case_root / "runtime.sqlite3", "evaluation-account", "capture", reminder_timezone="Asia/Shanghai")
                    composer = ReplyComposer(config, capture=True, client=bounded, context_observer=store.record_context)
                    engine = ConversationEngine(store, composer, CaptureSender(), "role-experience-v1")
                    case = {"id": scenario["id"], "title": scenario["title"], "character": role,
                        "character_version": card.version, "review_questions": scenario["review"], "turns": []}
                    report["scenarios"].append(case)
                    try:
                        for number, text in enumerate(scenario["turns"], 1):
                            if bounded.calls >= max_calls:
                                report["stop_reason"] = "model_call_budget_exhausted"
                                break
                            incoming = InboundEvent("evaluation-user", "person", "evaluation-user", "阿林",
                                [MessageSegment("text", text=text)], message_id=str(number), source_account_id="evaluation-account")
                            store.admit(EventEnvelope.from_inbound("evaluation-account", incoming), card)
                            started, before = time.monotonic(), bounded.calls
                            output_count = len(getattr(client, "output_records", []))
                            await engine.step("evaluation-user")
                            new_outputs = getattr(client, "output_records", [])[output_count:]
                            item = store.conversation_detail("evaluation-user")["items"][-1]
                            case["turns"].append({"user": text,
                                "assistant": (item.get("assistant_message") or {}).get("content", ""),
                                "state": item["state"], "error_code": item["error_code"],
                                "model_called": bounded.calls > before, "elapsed_seconds": round(time.monotonic() - started, 3),
                                "provider_output": new_outputs[-1] if new_outputs else None,
                                "context_report": (item.get("turn") or {}).get("context_report"),
                                "outbox_state": (item.get("outbox") or {}).get("state")})
                            checkpoint()
                            print(json.dumps({"character": role, "scenario": scenario["id"], "turn": number,
                                "state": item["state"], "model_calls": bounded.calls}, ensure_ascii=False), flush=True)
                            if item["state"] != "completed":
                                # A shared provider outage must not consume the
                                # remaining scenario budget with repeated calls.
                                report["stop_reason"] = "turn_failed"
                                return report
                    finally:
                        case["memory_records"] = store.memories("evaluation-user", role)["records"]
                        case["scheduled_followups"] = store._db.execute("SELECT count(*) FROM followup_candidates WHERE due_at IS NOT NULL").fetchone()[0]
                        case["reminder_tasks"] = store._db.execute("SELECT count(*) FROM reminder_tasks").fetchone()[0]
                        composer.close()
                        store.close()
                    checkpoint()
    finally:
        report["finished_at"] = time.time()
        checkpoint()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output-dir", required=True, help="a new output directory")
    parser.add_argument("--max-calls", type=int, default=24)
    parser.add_argument("--max-output-tokens", type=int, default=900)
    parser.add_argument("--output-token-parameter", choices=("max_completion_tokens", "max_tokens"), default="max_completion_tokens")
    parser.add_argument("--scenario", action="append", help="run only these scenario IDs; repeat to select several")
    parser.add_argument("--reply-only", action="store_true", help="diagnostic only: disable structured memory extraction")
    args = parser.parse_args()
    key = os.environ.get("WAIFU_EVAL_API_KEY", "")
    if not key:
        raise SystemExit("WAIFU_EVAL_API_KEY is required; no .env is loaded")
    plan = json.loads((ROOT / "examples/runtime-v2-experience-scenarios.json").read_text(encoding="utf-8"))
    if args.scenario:
        if set(args.scenario) - {s["id"] for s in plan["scenarios"]}:
            raise SystemExit("unknown scenario ID")
        plan["scenarios"] = [s for s in plan["scenarios"] if s["id"] in args.scenario]
    client = RecordedClient(base_url=args.base_url, api_key=key, model=args.model, timeout_seconds=45)
    try:
        report = asyncio.run(run_experience(args.output_dir, plan, client, args.model, max_calls=args.max_calls,
            max_output_tokens=args.max_output_tokens, output_token_parameter=args.output_token_parameter,
            structured_memory=not args.reply_only))
        print(json.dumps({"model_calls": report["model_calls"], "stop_reason": report.get("stop_reason", "completed")}), flush=True)
        if report.get("stop_reason"):
            raise SystemExit(2)
    except Exception as exc:
        # Provider/transport exceptions may contain URLs, credentials or bodies.
        print(json.dumps({"status": "evaluation_failed", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
    finally:
        client.close()


if __name__ == "__main__":
    main()
