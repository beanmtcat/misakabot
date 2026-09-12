import hashlib
import hmac
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from misakabot.audit_api import create_app
from misakabot.config import Settings
from misakabot.domain import Action, MessageInput, ReviewStatus
from misakabot.normalizer import normalize_message
from misakabot.repository import AuditRepository
from misakabot.signals import detect_suspicion


def signed_init_data(token: str, user_id: int) -> str:
    values = {
        "auth_date": str(int(time.time())),
        "query_id": "test-query",
        "user": json.dumps({"id": user_id, "username": "admin"}, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class AuditApiTests(unittest.TestCase):
    def _settings(self, database_path: Path) -> Settings:
        return Settings(
            telegram_bot_token="123:token",
            admin_user_ids=frozenset({1}),
            allowed_group_ids=frozenset({-1}),
            audit_database_path=database_path,
        )

    def test_admin_can_read_events_and_others_cannot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = AuditRepository(Path(directory) / "audit.sqlite3")
            repository.initialize()
            incoming = MessageInput(
                chat_id=-1,
                message_id=99,
                user_id=88,
                text="出微信号 可加好友",
                sent_at=datetime.now(timezone.utc),
            )
            normalized = normalize_message(incoming.text)
            repository.record(
                incoming,
                normalized,
                detect_suspicion(normalized),
                None,
                Action.NEEDS_REVIEW,
                ReviewStatus.PENDING,
            )
            repository.record(
                MessageInput(
                    chat_id=-1,
                    message_id=100,
                    user_id=89,
                    text="低价出号，联系我",
                    sent_at=datetime.now(timezone.utc),
                ),
                normalize_message("低价出号，联系我"),
                detect_suspicion(normalize_message("低价出号，联系我")),
                None,
                Action.NEEDS_REVIEW,
                ReviewStatus.PENDING,
            )
            settings = Settings(
                telegram_bot_token="123:token",
                admin_user_ids=frozenset({1}),
                allowed_group_ids=frozenset({-1}),
                audit_database_path=Path(repository.database_path),
            )
            async def is_group_admin(user_id: int, chat_id: int) -> bool:
                return user_id == 2 and chat_id == -1

            client = TestClient(create_app(settings, repository, is_group_admin))
            self.assertEqual(client.get("/v1/audit/events").status_code, 422)
            headers = {"X-Telegram-Init-Data": signed_init_data("123:token", 2)}
            self.assertEqual(client.get("/v1/audit/events?chat_id=-1", headers=headers).status_code, 200)
            self.assertEqual(client.get("/v1/audit/events?chat_id=-2", headers=headers).status_code, 403)
            headers = {"X-Telegram-Init-Data": signed_init_data("123:token", 3)}
            self.assertEqual(client.get("/v1/audit/events?chat_id=-1", headers=headers).status_code, 403)
            headers = {"X-Telegram-Init-Data": signed_init_data("123:token", 1)}
            response = client.get("/v1/audit/events?chat_id=-1", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {item["raw_text"] for item in response.json()["items"]},
            {"出微信号 可加好友", "低价出号，联系我"},
        )

    def test_events_support_cursor_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = AuditRepository(Path(directory) / "audit.sqlite3")
            repository.initialize()
            for message_id in range(1, 3):
                incoming = MessageInput(
                    chat_id=-1,
                    message_id=message_id,
                    user_id=message_id,
                    text=f"低价出售 {message_id}",
                    sent_at=datetime.now(timezone.utc),
                )
                normalized = normalize_message(incoming.text)
                repository.record(incoming, normalized, detect_suspicion(normalized), None, Action.NEEDS_REVIEW, ReviewStatus.PENDING)
            settings = Settings(
                telegram_bot_token="123:token",
                admin_user_ids=frozenset({1}),
                allowed_group_ids=frozenset({-1}),
                audit_database_path=Path(repository.database_path),
            )
            client = TestClient(create_app(settings, repository))
            headers = {"X-Telegram-Init-Data": signed_init_data("123:token", 1)}
            first = client.get("/v1/audit/events?chat_id=-1&limit=1", headers=headers).json()
            second = client.get(
                f"/v1/audit/events?chat_id=-1&limit=1&before_id={first['next_before_id']}",
                headers=headers,
            ).json()
        self.assertEqual(len(first["items"]), 1)
        self.assertIsNotNone(first["next_before_id"])
        self.assertEqual(len(second["items"]), 1)
        self.assertIsNone(second["next_before_id"])
        self.assertLess(second["items"][0]["id"], first["items"][0]["id"])

    def test_events_can_be_filtered_by_action_in_sql(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = AuditRepository(Path(directory) / "audit.sqlite3")
            repository.initialize()
            for message_id, action, review in (
                (1, Action.PERMANENT_BAN, ReviewStatus.CONFIRMED),
                (2, Action.NEEDS_REVIEW, ReviewStatus.PENDING),
            ):
                incoming = MessageInput(chat_id=-1, message_id=message_id, user_id=message_id, text="低价出售")
                normalized = normalize_message(incoming.text)
                repository.record(incoming, normalized, detect_suspicion(normalized), None, action, review)
            settings = Settings(
                telegram_bot_token="123:token",
                admin_user_ids=frozenset({1}),
                allowed_group_ids=frozenset({-1}),
                audit_database_path=Path(repository.database_path),
            )
            client = TestClient(create_app(settings, repository))
            headers = {"X-Telegram-Init-Data": signed_init_data("123:token", 1)}
            response = client.get("/v1/audit/events?chat_id=-1&filter=banned", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["action"] for item in response.json()["items"]], ["permanent_ban"])

    def test_review_actions_apply_telegram_change_before_updating_audit_row(self) -> None:
        class FakeModerationGateway:
            def __init__(self) -> None:
                self.calls: list[tuple[str, int, int, int | None]] = []

            async def confirm_ban(self, chat_id: int, user_id: int, message_id: int) -> None:
                self.calls.append(("ban", chat_id, user_id, message_id))

            async def restore_member(self, chat_id: int, user_id: int) -> None:
                self.calls.append(("restore", chat_id, user_id, None))

        with tempfile.TemporaryDirectory() as directory:
            repository = AuditRepository(Path(directory) / "audit.sqlite3")
            repository.initialize()
            incoming = MessageInput(chat_id=-1, message_id=9, user_id=8, text="疑似推广")
            normalized = normalize_message(incoming.text)
            event_id = repository.record(
                incoming, normalized, detect_suspicion(normalized), None,
                Action.NEEDS_REVIEW, ReviewStatus.PENDING,
            )
            gateway = FakeModerationGateway()
            client = TestClient(create_app(self._settings(Path(repository.database_path)), repository, moderation_gateway=gateway))
            headers = {"X-Telegram-Init-Data": signed_init_data("123:token", 1)}
            response = client.post(
                f"/v1/audit/events/{event_id}/confirm-ban?chat_id=-1",
                headers=headers,
                json={},
            )
            stored = client.get(f"/v1/audit/events/{event_id}?chat_id=-1", headers=headers).json()
            is_blocked = repository.is_user_blocked(8)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(gateway.calls, [("ban", -1, 8, 9)])
        self.assertEqual(stored["action"], "permanent_ban")
        self.assertTrue(is_blocked)

    def test_review_action_does_not_update_audit_when_telegram_fails(self) -> None:
        class FailingModerationGateway:
            async def confirm_ban(self, chat_id: int, user_id: int, message_id: int) -> None:
                raise RuntimeError("telegram unavailable")

            async def restore_member(self, chat_id: int, user_id: int) -> None:
                raise RuntimeError("telegram unavailable")

        with tempfile.TemporaryDirectory() as directory:
            repository = AuditRepository(Path(directory) / "audit.sqlite3")
            repository.initialize()
            incoming = MessageInput(chat_id=-1, message_id=9, user_id=8, text="疑似推广")
            normalized = normalize_message(incoming.text)
            event_id = repository.record(
                incoming, normalized, detect_suspicion(normalized), None,
                Action.NEEDS_REVIEW, ReviewStatus.PENDING,
            )
            client = TestClient(create_app(
                self._settings(Path(repository.database_path)), repository,
                moderation_gateway=FailingModerationGateway(),
            ))
            headers = {"X-Telegram-Init-Data": signed_init_data("123:token", 1)}
            response = client.post(
                f"/v1/audit/events/{event_id}/confirm-ban?chat_id=-1",
                headers=headers,
                json={},
            )
            stored = client.get(f"/v1/audit/events/{event_id}?chat_id=-1", headers=headers).json()

        self.assertEqual(response.status_code, 502)
        self.assertEqual(stored["action"], "needs_review")


if __name__ == "__main__":
    unittest.main()
