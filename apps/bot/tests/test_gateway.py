from __future__ import annotations

import unittest

from misakabot.gateway import AiogramGateway


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

