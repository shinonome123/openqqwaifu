from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_role_experience import LimitedClient, ModelBudgetExceeded, run_experience


class Client:
    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    async def ainvoke_messages(self, *args, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("SECRET provider details")
        return json.dumps({"reply": "模拟回复", "memories": []}, ensure_ascii=False)


class RoleExperienceRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_limit_is_checked_before_contacting_provider(self):
        client = Client()
        bounded = LimitedClient(client, 2)
        await bounded.ainvoke_messages([])
        await bounded.ainvoke_messages([])
        with self.assertRaises(ModelBudgetExceeded):
            await bounded.ainvoke_messages([])
        self.assertEqual(client.calls, 2)

    async def test_provider_failure_stops_entire_run_and_exports_no_exception_body(self):
        plan = {"characters": ["quiet-reader", "curious-maker"], "scenarios": [
            {"id": "first", "title": "first", "turns": ["你好", "后来呢"], "review": ["连续性"]},
            {"id": "second", "title": "second", "turns": ["另一个场景"], "review": ["语气"]}]}
        with tempfile.TemporaryDirectory() as root:
            client = Client(fail=True)
            client.output_records = []
            output = Path(root) / "results"
            result = await run_experience(output, plan, client, "fake-model")
            self.assertEqual(client.calls, 1)
            self.assertEqual(result["stop_reason"], "turn_failed")
            saved = (output / "transcripts.json").read_text(encoding="utf-8")
            self.assertNotIn("SECRET", saved)
            self.assertEqual(json.loads(saved)["model_calls"], 1)
            self.assertEqual(result["scenarios"][0]["turns"][0]["error_code"], "generation_RuntimeError")
            self.assertIsNone(result["scenarios"][0]["turns"][0]["provider_output"])
            self.assertEqual(result["scenarios"][0]["memory_records"], [])

    async def test_failed_call_does_not_reuse_previous_provider_output(self):
        class FailsSecond(Client):
            def __init__(self):
                super().__init__()
                self.output_records = []

            async def ainvoke_messages(self, *args, **kwargs):
                if self.calls:
                    self.fail = True
                answer = await super().ainvoke_messages(*args, **kwargs)
                self.output_records.append({"content": answer, "finish_reason": "stop"})
                return answer

        plan = {"characters": ["quiet-reader"], "scenarios": [
            {"id": "first", "title": "first", "turns": ["你好", "后来呢"], "review": []}]}
        with tempfile.TemporaryDirectory() as root:
            result = await run_experience(Path(root) / "results", plan, FailsSecond(), "fake-model")
        turns = result["scenarios"][0]["turns"]
        self.assertIsNotNone(turns[0]["provider_output"])
        self.assertIsNone(turns[1]["provider_output"])
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["stop_reason"], "turn_failed")

    async def test_reply_only_is_explicit_and_does_not_request_json_or_create_memory(self):
        class PlainClient:
            supports_json_object = True

            async def ainvoke_messages(inner, messages, **kwargs):
                self.assertNotIn("json_object", kwargs)
                return "这是纯对话对照。"

        plan = {"characters": ["quiet-reader"], "scenarios": [
            {"id": "plain", "title": "plain", "turns": ["明天面试"], "review": []}]}
        with tempfile.TemporaryDirectory() as root:
            result = await run_experience(Path(root) / "results", plan, PlainClient(), "fake-model", structured_memory=False)
        self.assertFalse(result["structured_memory_enabled"])
        self.assertEqual(result["scenarios"][0]["turns"][0]["state"], "completed")
        self.assertEqual(result["scenarios"][0]["memory_records"], [])


if __name__ == "__main__":
    unittest.main()
