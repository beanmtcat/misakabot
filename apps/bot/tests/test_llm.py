import asyncio
import json
import unittest

import httpx

from misakabot.llm import KimiCodingGroupReplyClient, KimiCodingModerationClient, telegram_plain_text
from misakabot.normalizer import normalize_message
from misakabot.signals import detect_suspicion


class KimiModerationClientTests(unittest.TestCase):
    def test_kimi_accepts_numeric_confidence_boundaries_and_boolean_false(self) -> None:
        for confidence in (0, 1, 0.95):
            with self.subTest(confidence=confidence):
                def handler(_: httpx.Request) -> httpx.Response:
                    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
                        "is_ad": False, "confidence": confidence,
                    })}}]})

                client = KimiCodingModerationClient(
                    api_key="test-key", model="kimi-coding", transport=httpx.MockTransport(handler)
                )
                message = normalize_message("普通消息")
                verdict = asyncio.run(client.judge(message, detect_suspicion(message)))
                self.assertIs(verdict.is_ad, False)
                self.assertEqual(verdict.confidence, confidence)
                self.assertEqual(verdict.evidence, ())

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

    def test_kimi_rejects_malformed_verdict_fields_instead_of_coercing_to_ad(self) -> None:
        for invalid in (
            {"is_ad": "false", "confidence": 0.95},
            {"is_ad": "true", "confidence": 0.95},
            {"is_ad": 1, "confidence": 0.95},
            {"is_ad": False, "confidence": True},
            {"is_ad": False, "confidence": 0.95, "evidence": "不是数组"},
            {"is_ad": False, "confidence": 0.95, "evidence": [42]},
            {"is_ad": False, "confidence": float("nan")},
            {"is_ad": False, "confidence": float("inf")},
        ):
            with self.subTest(invalid=invalid):
                def handler(_: httpx.Request) -> httpx.Response:
                    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(invalid)}}]})

                client = KimiCodingModerationClient(
                    api_key="test-key", model="kimi-coding", transport=httpx.MockTransport(handler)
                )
                message = normalize_message("普通消息")
                with self.assertRaisesRegex(ValueError, "moderation schema"):
                    asyncio.run(client.judge(message, detect_suspicion(message)))


class KimiGroupReplyClientTests(unittest.TestCase):
    def test_kimi_reply_rejects_missing_choices_and_non_text_content(self) -> None:
        for invalid in ({}, {"choices": []}, {"choices": [{"message": {"content": None}}]}):
            with self.subTest(response=invalid):
                client = KimiCodingGroupReplyClient(
                    api_key="test-key", model="kimi-coding",
                    transport=httpx.MockTransport(lambda _: httpx.Response(200, json=invalid)),
                )
                with self.assertRaisesRegex(ValueError, "reply content"):
                    asyncio.run(client.reply("你好"))

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
