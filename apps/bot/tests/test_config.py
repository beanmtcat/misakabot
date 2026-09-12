from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from misakabot.config import Settings


class SettingsTests(unittest.TestCase):
    def test_parses_allowed_group_ids(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "123:token",
                "ALLOWED_GROUP_IDS": "-100123, -200456",
            },
            clear=True,
        ):
            settings = Settings.from_environment()
        self.assertEqual(settings.allowed_group_ids, frozenset({-100123, -200456}))

    def test_parses_dmit_knowledge_group_ids(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "123:token",
                "DMIT_KNOWLEDGE_GROUP_IDS": "-100123, -200456",
            },
            clear=True,
        ):
            settings = Settings.from_environment()
        self.assertEqual(settings.dmit_knowledge_group_ids, frozenset({-100123, -200456}))

    def test_parses_dmit_channel_group_ids(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "123:token",
                "DMIT_CHANNEL_GROUP_IDS": "-100123",
            },
            clear=True,
        ):
            settings = Settings.from_environment()
        self.assertEqual(settings.dmit_channel_group_ids, frozenset({-100123}))

    def test_no_allowed_group_keeps_bot_inactive(self) -> None:
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:token"}, clear=True):
            settings = Settings.from_environment()
        self.assertEqual(settings.allowed_group_ids, frozenset())

    def test_bot_reply_can_be_disabled(self) -> None:
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "123:token", "BOT_REPLY_ENABLED": "false"},
            clear=True,
        ):
            settings = Settings.from_environment()
        self.assertFalse(settings.bot_reply_enabled)

    def test_join_verification_default_timeout_is_ten_minutes(self) -> None:
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:token"}, clear=True):
            settings = Settings.from_environment()
        self.assertEqual(settings.verification_ttl_minutes, 5)
        self.assertEqual(settings.secondary_verification_ttl_minutes, 2)

    def test_parses_postgres_database_url(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "123:token",
                "DATABASE_URL": "postgresql://misakabot:password@postgres:5432/misakabot",
            },
            clear=True,
        ):
            settings = Settings.from_environment()
        self.assertEqual(settings.database_url, "postgresql://misakabot:password@postgres:5432/misakabot")
