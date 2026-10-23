from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery
from fastapi.testclient import TestClient

from misakabot.config import Settings
from misakabot.webhook import build_webhook_app
from misakabot.repository import AuditRepository


class FakeSession:
    async def close(self) -> None:
        return None


class FakeBot:
    def __init__(self) -> None:
        self.session = FakeSession()
        self.webhooks: list[dict[str, object]] = []
        self.command_menus: list[dict[str, object]] = []
        self.deleted_command_scopes: list[dict[str, object]] = []

    async def set_webhook(self, url: str, **kwargs: object) -> None:
        self.webhooks.append({"url": url, **kwargs})

    async def set_my_commands(self, commands: object, **kwargs: object) -> None:
        self.command_menus.append({"commands": commands, **kwargs})

    async def delete_my_commands(self, **kwargs: object) -> None:
        self.deleted_command_scopes.append(kwargs)


class FakeDispatcher:
    def __init__(self) -> None:
        self.updates: list[int] = []

    def resolve_used_update_types(self) -> list[str]:
        return ["message"]

    async def feed_update(self, _: FakeBot, update: object) -> None:
        self.updates.append(update.update_id)  # type: ignore[attr-defined]


class FailingDispatcher(FakeDispatcher):
    async def feed_update(self, _: FakeBot, update: object) -> None:
        self.updates.append(update.update_id)  # type: ignore[attr-defined]
        raise RuntimeError("simulated handler failure")


class ExpiredCallbackDispatcher(FakeDispatcher):
    async def feed_update(self, _: FakeBot, update: object) -> None:
        self.updates.append(update.update_id)  # type: ignore[attr-defined]
        raise TelegramBadRequest(
            AnswerCallbackQuery(callback_query_id="expired"),
            "query is too old and response timeout expired or query ID is invalid",
        )


class WebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repository = AuditRepository(Path(self.tempdir.name) / "audit.sqlite3")
        self.repository.initialize()
        self.settings = Settings(
            telegram_bot_token="123:token",
            admin_user_ids=frozenset({1}),
            telegram_transport="webhook",
            telegram_webhook_url="https://dashboard.example/telegram/webhook",
            telegram_webhook_secret="expected-secret",
            allowed_group_ids=frozenset({-100}),
        )
        self.bot = FakeBot()
        self.dispatcher = FakeDispatcher()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_registers_webhook_and_accepts_valid_secret(self) -> None:
        app = build_webhook_app(self.settings, self.bot, self.dispatcher, self.repository)  # type: ignore[arg-type]
        with TestClient(app) as client:
            response = client.post(
                "/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "expected-secret"},
                json={"update_id": 99},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.dispatcher.updates, [99])
        self.assertEqual(self.bot.webhooks[0]["url"], self.settings.telegram_webhook_url)
        self.assertEqual(len(self.bot.command_menus), 2)
        deleted = {
            (item["scope"].type, item.get("language_code", ""))
            for item in self.bot.deleted_command_scopes
        }
        for language in ("", "zh", "en"):
            self.assertIn(("default", language), deleted)
            self.assertIn(("all_group_chats", language), deleted)
            self.assertIn(("chat", language), deleted)
        menu = self.bot.command_menus[1]
        self.assertEqual(menu["scope"].type, "chat_administrators")
        self.assertEqual(
            [command.command for command in menu["commands"]],
            ["audit", "unban", "join_guard", "memory"],
        )

    def test_rejects_an_invalid_secret(self) -> None:
        app = build_webhook_app(self.settings, self.bot, self.dispatcher, self.repository)  # type: ignore[arg-type]
        with TestClient(app) as client:
            response = client.post("/telegram/webhook", json={"update_id": 100})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.dispatcher.updates, [])

    def test_handler_failure_returns_500_so_telegram_retries(self) -> None:
        dispatcher = FailingDispatcher()
        app = build_webhook_app(self.settings, self.bot, dispatcher, self.repository)  # type: ignore[arg-type]
        with TestClient(app) as client:
            response = client.post(
                "/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "expected-secret"},
                json={"update_id": 101},
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(dispatcher.updates, [101])

    def test_expired_callback_is_acknowledged_without_webhook_retry(self) -> None:
        dispatcher = ExpiredCallbackDispatcher()
        app = build_webhook_app(
            self.settings, self.bot, dispatcher, self.repository  # type: ignore[arg-type]
        )
        callback_update = {
            "update_id": 103,
            "callback_query": {
                "id": "expired",
                "from": {"id": 1, "is_bot": False, "first_name": "Admin"},
                "chat_instance": "instance",
                "data": "vps_verify:old:0",
            },
        }
        headers = {"X-Telegram-Bot-Api-Secret-Token": "expected-secret"}
        with TestClient(app) as client:
            first = client.post("/telegram/webhook", headers=headers, json=callback_update)
            second = client.post("/telegram/webhook", headers=headers, json=callback_update)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(dispatcher.updates, [103])

    def test_processed_update_is_not_dispatched_twice(self) -> None:
        app = build_webhook_app(self.settings, self.bot, self.dispatcher, self.repository)  # type: ignore[arg-type]
        with TestClient(app) as client:
            headers = {"X-Telegram-Bot-Api-Secret-Token": "expected-secret"}
            first = client.post("/telegram/webhook", headers=headers, json={"update_id": 102})
            second = client.post("/telegram/webhook", headers=headers, json={"update_id": 102})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.dispatcher.updates, [102])

    def test_pruning_keeps_recent_webhook_deduplication_records(self) -> None:
        self.assertEqual(
            self.repository.claim_webhook_update(201, "2026-09-01T00:00:00+00:00", "2026-08-31T23:58:00+00:00"),
            "claimed",
        )
        self.repository.complete_webhook_update(201, "2026-09-01T00:00:01+00:00")
        self.assertEqual(
            self.repository.claim_webhook_update(202, "2026-09-08T00:00:00+00:00", "2026-09-07T23:58:00+00:00"),
            "claimed",
        )
        self.repository.complete_webhook_update(202, "2026-09-08T00:00:01+00:00")
        self.assertEqual(
            self.repository.claim_webhook_update(203, "2026-09-01T00:00:00+00:00", "2026-08-31T23:58:00+00:00"),
            "claimed",
        )

        self.assertEqual(
            self.repository.prune_webhook_updates(
                "2026-09-07T00:00:00+00:00", "2026-09-07T00:00:00+00:00"
            ),
            2,
        )
        self.assertEqual(
            self.repository.claim_webhook_update(201, "2026-09-08T00:00:02+00:00", "2026-09-08T00:00:00+00:00"),
            "claimed",
        )
        self.assertEqual(
            self.repository.claim_webhook_update(202, "2026-09-08T00:00:02+00:00", "2026-09-08T00:00:00+00:00"),
            "processed",
        )
        self.assertEqual(
            self.repository.claim_webhook_update(203, "2026-09-08T00:00:02+00:00", "2026-09-08T00:00:00+00:00"),
            "claimed",
        )
