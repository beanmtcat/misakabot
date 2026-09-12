import asyncio
import json
import unittest

import httpx

from misakabot.llm import KimiCodingGroupReplyClient, KimiCodingModerationClient, telegram_plain_text
from misakabot.normalizer import normalize_message
from misakabot.signals import detect_suspicion


class KimiModerationClientTests(unittest.TestCase):
    def test_kimi_retries_transient_provider_error(self) -> None:
        attempts = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, json={"error": "temporarily unavailable"})
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
                "is_ad": False, "category": "normal", "confidence": 0.8,
            })}}]})

        message = normalize_message("普通消息")
        client = KimiCodingModerationClient(
            api_key="test-key", model="kimi-coding", transport=httpx.MockTransport(handler)
        )
        verdict = asyncio.run(client.judge(message, detect_suspicion(message)))

        self.assertEqual(attempts, 2)
        self.assertFalse(verdict.is_ad)

    def test_kimi_request_includes_sender_profile_and_parses_verdict(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["authorization"] = request.headers["authorization"]
            captured["payload"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "choices": [{
                        "message": {"content": json.dumps({
                            "is_ad": True,
                            "category": "photography_promotion",
                            "confidence": 0.93,
                            "evidence": ["昵称包含拍照服务和引流词"],
                            "reason": "广告服务推广",
                        })},
                    }],
                },
            )

        message = normalize_message("大家好", sender_name="手机拍照一百张寻我简介")
        client = KimiCodingModerationClient(
            api_key="test-key",
            model="kimi-coding",
            transport=httpx.MockTransport(handler),
        )
        verdict = asyncio.run(client.judge(message, detect_suspicion(message)))

        self.assertEqual(captured["url"], "https://api.kimi.com/coding/v1/chat/completions")
        self.assertEqual(captured["authorization"], "Bearer test-key")
        payload = captured["payload"]
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["temperature"], 1)
        prompt_data = json.loads(payload["messages"][1]["content"])
        self.assertEqual(prompt_data["sender_display_name"], "手机拍照一百张寻我简介")
        self.assertEqual(prompt_data["normalized_sender_display_name"], "手机拍照一百张寻我简介")
        self.assertTrue(verdict.is_ad)
        self.assertEqual(verdict.confidence, 0.93)

    def test_kimi_rejects_invalid_verdict_confidence(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"is_ad": true, "confidence": 1.2}'}}]},
            )

        message = normalize_message("普通消息")
        client = KimiCodingModerationClient(
            api_key="test-key",
            model="kimi-coding",
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(ValueError):
            asyncio.run(client.judge(message, detect_suspicion(message)))


class KimiGroupReplyClientTests(unittest.TestCase):
    def test_telegram_plain_text_removes_markdown_syntax(self) -> None:
        self.assertEqual(
            telegram_plain_text("## 标题\n**重点**：看 `控制台`\n[官方文档](https://docs.example.com)"),
            "标题\n重点：看 控制台\n官方文档（https://docs.example.com）",
        )

    def test_kimi_reply_has_bounded_context_and_returns_text(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["authorization"] = request.headers["authorization"]
            captured["payload"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "可以。"}}]})

        client = KimiCodingGroupReplyClient(
            api_key="test-key",
            model="kimi-coding",
            transport=httpx.MockTransport(handler),
        )
        answer = asyncio.run(
            client.reply(
                "这个怎么用？",
                "这是 Bot 上一条说明。",
                knowledge=("DMIT 官方文档：遇到未覆盖问题请提交工单。",),
            )
        )

        self.assertEqual(answer, "可以。")
        self.assertEqual(captured["url"], "https://api.kimi.com/coding/v1/chat/completions")
        self.assertEqual(captured["authorization"], "Bearer test-key")
        payload = captured["payload"]
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["temperature"], 1)
        prompt_data = json.loads(payload["messages"][1]["content"])
        self.assertEqual(prompt_data["user_message"], "这个怎么用？")
        self.assertEqual(prompt_data["bot_message_being_replied_to"], "这是 Bot 上一条说明。")
        self.assertEqual(prompt_data["approved_knowledge"], ["DMIT 官方文档：遇到未覆盖问题请提交工单。"])

    def test_kimi_reply_rejects_empty_response(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]})

        client = KimiCodingGroupReplyClient(
            api_key="test-key",
            model="kimi-coding",
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(ValueError):
            asyncio.run(client.reply("你好"))


if __name__ == "__main__":
    unittest.main()
