from __future__ import annotations

import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from aiogram.exceptions import TelegramNetworkError
from aiogram.enums import ChatMemberStatus
from aiogram.methods import GetChatMember
from aiogram.types import ChatMemberMember, User

from misakabot.gateway import AiogramGateway, is_active_chat_member
from misakabot.repository import AuditRepository


class BanDurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_timed_ban_passes_utc_expiration_to_telegram(self) -> None:
        bot = Mock()
        bot.ban_chat_member = AsyncMock()
        gateway = AiogramGateway(bot, Mock())
        now = datetime(2026, 10, 23, tzinfo=timezone.utc)
        with patch("misakabot.gateway.datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            await gateway.ban_member(-100123, 42, duration_hours=1)
        bot.ban_chat_member.assert_awaited_once_with(
            -100123, 42, until_date=now + timedelta(hours=1)
        )

    async def test_zero_and_existing_bans_remain_permanent(self) -> None:
        bot = Mock()
        bot.ban_chat_member = AsyncMock()
        gateway = AiogramGateway(bot, Mock())
        await gateway.ban_member(-100123, 42, duration_hours=0)
        bot.ban_chat_member.assert_awaited_once_with(-100123, 42)
        bot.ban_chat_member.reset_mock()
        await gateway.ban_member(-100123, 42)
        bot.ban_chat_member.assert_awaited_once_with(-100123, 42)


class MemberInfoKeyboardTests(unittest.TestCase):
    def test_member_without_username_uses_admin_callback(self) -> None:
        button = AiogramGateway._member_info_keyboard(42).inline_keyboard[0][0]

        self.assertEqual(button.text, "查看用户信息")
        self.assertEqual(button.callback_data, "user_info:42")
        self.assertIsNone(button.url)

    def test_member_with_username_uses_profile_link(self) -> None:
        button = AiogramGateway._member_info_keyboard(42, "member_name").inline_keyboard[0][0]

        self.assertEqual(button.text, "查看用户资料")
        self.assertEqual(button.url, "https://t.me/member_name?profile")
        self.assertIsNone(button.callback_data)


class MemberLookupRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_restricted_user_with_is_member_false_is_not_a_group_member(self) -> None:
        bot = Mock()
        bot.get_chat_member = AsyncMock(
            return_value=Mock(status=ChatMemberStatus.RESTRICTED, is_member=False)
        )
        gateway = AiogramGateway(bot, Mock())

        self.assertFalse(await gateway.is_group_member(-100123, 42))

    async def test_restricted_user_with_is_member_true_is_a_group_member(self) -> None:
        member = Mock(status=ChatMemberStatus.RESTRICTED, is_member=True)
        self.assertTrue(is_active_chat_member(member))

    async def test_transient_network_error_is_retried(self) -> None:
        bot = Mock()
        method = GetChatMember(chat_id=-100123, user_id=42)
        member = ChatMemberMember(
            status=ChatMemberStatus.MEMBER,
            user=User(id=42, is_bot=False, first_name="A"),
        )
        bot.get_chat_member = AsyncMock(
            side_effect=[TelegramNetworkError(method, "connection reset"), member]
        )
        gateway = AiogramGateway(bot, Mock())
        gateway._MEMBER_LOOKUP_RETRY_DELAYS_SECONDS = (0.0,)

        with patch("misakabot.gateway.asyncio.sleep", new=AsyncMock()) as sleep:
            self.assertFalse(await gateway.is_group_administrator(-100123, 42))

        self.assertEqual(bot.get_chat_member.await_count, 2)
        sleep.assert_awaited_once_with(0.0)


class GroupChallengeDisplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_username_uses_cleaned_nickname(self) -> None:
        bot = Mock()
        bot.send_message = AsyncMock(return_value=Mock(message_id=12))
        bot.get_chat_member = AsyncMock(return_value=Mock(
            user=User(id=42, is_bot=False, first_name="昵称\u202e\n测试")
        ))
        gateway = AiogramGateway(bot, Mock())
        await gateway.send_group_vps_challenge(
            -100123, 42, None, "题目？", (("答案", "vps_verify:token:0"),), 2
        )
        text = bot.send_message.await_args.args[1]
        self.assertIn("<code>昵称测试</code>（ID：42）", text)
        self.assertNotIn("\u202e", text)

    async def test_name_lookup_failure_still_sends_challenge_with_id(self) -> None:
        bot = Mock()
        bot.send_message = AsyncMock(return_value=Mock(message_id=12))
        gateway = AiogramGateway(bot, Mock())
        bot.get_chat_member = AsyncMock(side_effect=RuntimeError("unavailable"))
        await gateway.send_group_vps_challenge(
            -100123, 42, None, "题目？", (("答案", "vps_verify:token:0"),), 2
        )
        self.assertIn("新成员 <code>42</code>", bot.send_message.await_args.args[1])
        # A single short lookup: no retries while a join flood is in progress.
        self.assertEqual(bot.get_chat_member.await_count, 1)

    async def test_known_display_name_skips_lookup_and_drops_links(self) -> None:
        bot = Mock()
        bot.send_message = AsyncMock(return_value=Mock(message_id=12))
        bot.get_chat_member = AsyncMock()
        gateway = AiogramGateway(bot, Mock())
        await gateway.send_group_vps_challenge(
            -100123, 42, None, "题目<b>？", (("答案", "vps_verify:token:0"),), 2,
            display_name="免费机场 t.me/spam_channel @spam <b>",
        )
        bot.get_chat_member.assert_not_awaited()
        text = bot.send_message.await_args.args[1]
        self.assertEqual(bot.send_message.await_args.kwargs["parse_mode"], "HTML")
        self.assertIn("<code>免费机场 spam &lt;b&gt;</code>（ID：42）", text)
        self.assertIn("题目&lt;b&gt;？", text)
        self.assertNotIn("t.me", text)

    async def test_challenge_displays_username_and_numeric_id_when_enabled(self) -> None:
        bot = Mock()
        bot.send_message = AsyncMock(return_value=Mock(message_id=12))
        gateway = AiogramGateway(bot, Mock(), show_secondary_verification_username=True)

        await gateway.send_group_vps_challenge(
            -100123,
            42,
            "member_name",
            "题目？",
            (("答案", "vps_verify:token:0"),),
            2,
        )

        text = bot.send_message.await_args.args[1]
        self.assertIn("<code>@member_name</code>（ID：42）", text)
        self.assertIn("入群二次验证", text)
        self.assertNotIn("黑话", text)
        self.assertNotIn("交易安全", text)

    async def test_challenge_can_hide_username(self) -> None:
        bot = Mock()
        bot.send_message = AsyncMock(return_value=Mock(message_id=12))
        gateway = AiogramGateway(bot, Mock(), show_secondary_verification_username=False)

        await gateway.send_group_vps_challenge(
            -100123,
            42,
            "member_name",
            "题目？",
            (("答案", "vps_verify:token:0"),),
            2,
        )

        text = bot.send_message.await_args.args[1]
        self.assertIn("新成员 <code>42</code>", text)
        self.assertNotIn("@member_name", text)


class GroupChallengeRotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rotation_edits_card_with_new_buttons_without_member_lookup(self) -> None:
        bot = Mock()
        bot.edit_message_text = AsyncMock()
        bot.get_chat_member = AsyncMock()
        gateway = AiogramGateway(bot, Mock())

        await gateway.update_group_vps_challenge(
            -100123, 77, 42, None, "新题？",
            (("甲", "vps_verify:newtoken:0"), ("乙", "vps_verify:newtoken:1")), 2, 2,
            display_name="成员",
        )

        bot.get_chat_member.assert_not_awaited()
        kwargs = bot.edit_message_text.await_args.kwargs
        self.assertEqual(kwargs["message_id"], 77)
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertIn("已换一道新题，还可尝试 2 次", kwargs["text"])
        self.assertIn("<code>成员</code>（ID：42）", kwargs["text"])
        callbacks = [
            button.callback_data
            for row in kwargs["reply_markup"].inline_keyboard for button in row
        ]
        self.assertIn("vps_verify:newtoken:1", callbacks)
        self.assertIn("vps_admin_approve:newtoken", callbacks)

    async def test_rotation_displays_absolute_deadline_instead_of_new_two_minutes(self) -> None:
        bot = Mock(edit_message_text=AsyncMock())
        gateway = AiogramGateway(bot, Mock())
        await gateway.update_group_vps_challenge(
            -100, 77, 42, "member", "题目？", (("甲", "vps_verify:new:0"),), 2, 2,
            expires_at="2026-10-23T17:03:01+00:00",
        )
        text = bot.edit_message_text.await_args.kwargs["text"]
        self.assertIn("10-24 01:03:01", text)
        self.assertNotIn("2 分钟内", text)


class GroupChallengeOrphanGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_unregistered_card_has_durable_cleanup_and_registered_card_cancels_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = AuditRepository(Path(directory) / "guard.sqlite3")
            repository.initialize()
            bot = Mock(send_message=AsyncMock(return_value=Mock(message_id=77)))
            gateway = AiogramGateway(bot, repository)
            now = datetime.now(timezone.utc)
            repository.create_group_challenge(
                chat_id=-100, user_id=42, username="member", token_hash="active",
                answer_index=0, expires_at=(now + timedelta(minutes=2)).isoformat(),
                now=now.isoformat(), verification_flow="direct_join",
            )
            message_id = await gateway.send_group_vps_challenge(
                -100, 42, "member", "题目？", (("甲", "vps_verify:active:0"),), 2
            )
            future = (now + timedelta(hours=1)).isoformat()
            self.assertEqual(repository.due_message_deletions(future), [(-100, 77)])
            self.assertFalse(repository.set_group_challenge_message_id(
                -100, 42, message_id, now.isoformat(), (now + timedelta(minutes=2)).isoformat(),
                token_hash="stale",
            ))
            self.assertEqual(repository.due_message_deletions(future), [(-100, 77)])
            self.assertTrue(repository.set_group_challenge_message_id(
                -100, 42, message_id, now.isoformat(), (now + timedelta(minutes=2)).isoformat(),
                token_hash="active",
            ))
            self.assertEqual(repository.due_message_deletions(future), [])
