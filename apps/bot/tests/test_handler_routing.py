from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Message, Update

from misakabot.domain import Action, ModerationOutcome, ModerationVerdict
from misakabot.gateway import AiogramGateway
from misakabot.handlers import build_dispatcher
from misakabot.handlers.messages import review_reason
from misakabot.llm import KimiCodingGroupReplyClient, RuleBasedModerationClient
from misakabot.onboarding import OnboardingService
from misakabot.repository import AuditRepository
from misakabot.service import ModerationService
from misakabot.signals import detect_suspicion
from misakabot.normalizer import normalize_message


class HandlerRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.repository = AuditRepository(Path(self.directory.name) / "routing.sqlite3")
        self.repository.initialize()
        self.session = AsyncMock(spec=BaseSession)
        self.bot = Bot("123456:offline-test-token", session=self.session)
        self.session.return_value = Message(
            message_id=100, date=datetime.now(timezone.utc),
            chat={"id": -100, "type": "supergroup"}, text="reply",
        )
        self.gateway = AiogramGateway(self.bot, self.repository)
        self.gateway.is_group_administrator = AsyncMock(return_value=False)
        self.service = ModerationService(
            self.repository, self.gateway, RuleBasedModerationClient()
        )
        self.service.moderate = AsyncMock(return_value=ModerationOutcome(
            Action.ALLOW, None, detect_suspicion(normalize_message("你好")), 1,
        ))
        self.onboarding = OnboardingService(self.repository, self.gateway)
        self.onboarding.start_direct_join = AsyncMock()
        self.onboarding.start = AsyncMock()
        self.client = AsyncMock(spec=KimiCodingGroupReplyClient)
        self.client.reply.return_value = "测试回答"
        self.dispatcher = build_dispatcher(
            self.bot, self.service, self.onboarding, frozenset({-100}), frozenset(),
            group_reply_client=self.client, bot_user_id=self.bot.id, bot_username="TestBot",
        )

    async def asyncTearDown(self) -> None:
        await self.bot.session.close()
        self.directory.cleanup()

    async def test_review_card_reason_prefers_ai_verdict(self) -> None:
        outcome = ModerationOutcome(
            Action.NEEDS_REVIEW,
            ModerationVerdict(True, "account_trade", 0.92, (), "持续提供账号代付服务"),
            detect_suspicion(normalize_message("代付")),
            1,
        )
        self.assertEqual(review_reason(outcome), "AI 判定「account_trade」92%：持续提供账号代付服务")

    async def feed_message(self, text: str, *, chat_id: int = -100, **extra: object) -> None:
        message = {
            "message_id": 1, "date": int(datetime.now(timezone.utc).timestamp()),
            "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
            "from": {"id": 42, "is_bot": False, "first_name": "Member"},
            "text": text, **extra,
        }
        update = Update.model_validate({"update_id": 1, "message": message})
        await self.dispatcher.feed_update(self.bot, update)

    async def test_memory_command_does_not_reach_moderation_or_llm(self) -> None:
        await self.feed_message("/memory on")
        self.assertTrue(self.repository.assistant_memory_enabled(-100, 42))
        self.service.moderate.assert_not_awaited()
        self.client.reply.assert_not_awaited()

    async def test_new_member_service_message_routes_to_onboarding(self) -> None:
        await self.feed_message("", new_chat_members=[
            {"id": 43, "is_bot": False, "first_name": "New member"}
        ])
        self.onboarding.start_direct_join.assert_awaited_once()
        self.service.moderate.assert_not_awaited()

    async def test_private_and_disabled_group_messages_never_invoke_llm(self) -> None:
        await self.feed_message("猫猫 你好", chat_id=42)
        await self.feed_message("猫猫 你好", chat_id=-200)
        self.client.reply.assert_not_awaited()
        self.service.moderate.assert_not_awaited()

    async def test_allowed_mention_is_reviewed_then_replied_and_saved(self) -> None:
        await self.feed_message("猫猫 你好")
        self.service.moderate.assert_awaited_once()
        self.assertTrue(self.service.moderate.await_args.kwargs["force_llm_review"])
        self.client.reply.assert_awaited_once()
        self.assertEqual(self.client.reply.await_args.args[0], "你好")
        self.assertEqual(self.repository.assistant_context(-100, 42),
                         [("user", "你好"), ("assistant", "测试回答")])

    async def test_flagged_message_does_not_receive_ai_reply(self) -> None:
        self.service.moderate.return_value = ModerationOutcome(
            Action.NEEDS_REVIEW, None, detect_suspicion(normalize_message("你好")), 1,
        )
        await self.feed_message("猫猫 你好")
        self.client.reply.assert_not_awaited()

    async def test_admin_can_address_bot_without_moderation(self) -> None:
        self.gateway.is_group_administrator.return_value = True
        await self.feed_message("@TestBot 你好")
        self.service.moderate.assert_not_awaited()
        self.client.reply.assert_awaited_once()

    async def test_unaddressed_message_is_reviewed_without_ai_reply(self) -> None:
        await self.feed_message("普通聊天")
        self.service.moderate.assert_awaited_once()
        self.client.reply.assert_not_awaited()

    async def test_member_update_invalidates_cache_and_is_subscribed(self) -> None:
        self.assertIn("chat_member", self.dispatcher.resolve_used_update_types())
        self.gateway._admin_cache[(-100, 42)] = (True, float("inf"))
        user = {"id": 42, "is_bot": False, "first_name": "Member"}
        update = Update.model_validate({"update_id": 2, "chat_member": {
            "chat": {"id": -100, "type": "supergroup"}, "from": user,
            "date": int(datetime.now(timezone.utc).timestamp()),
            "old_chat_member": {"status": "member", "user": user},
            "new_chat_member": {"status": "left", "user": user},
        }})
        await self.dispatcher.feed_update(self.bot, update)
        self.assertNotIn((-100, 42), self.gateway._admin_cache)
        self.service.moderate.assert_not_awaited()
