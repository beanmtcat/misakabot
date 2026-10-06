from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiogram.exceptions import TelegramNetworkError
from aiogram.enums import ChatMemberStatus
from aiogram.methods import GetChatMember
from aiogram.types import ChatMemberMember, User

from misakabot.gateway import AiogramGateway, is_active_chat_member


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
