"""Optional real-driver checks; all writes are to connection-local temporary tables.

Run with MISAKABOT_TEST_DATABASE_URL set to a PostgreSQL DSN. No repository
initialization, production DDL or Telegram calls are performed by these tests.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from misakabot.audit_api import create_app
from misakabot.config import Settings
from misakabot.database import DatabaseConnection, PostgresConnection
from misakabot.domain import Action, MessageInput, ModerationVerdict, ReviewStatus, OnboardingState, JoinRequestInput
from misakabot.normalizer import normalize_message
from misakabot.onboarding import OnboardingService
from misakabot.repository import POSTGRES_SCHEMA, PostgresAuditRepository
from misakabot.signals import detect_suspicion
from test_audit_api import signed_init_data


class _BorrowedConnection:
    def __init__(self, connection: DatabaseConnection) -> None:
        self._connection = connection

    def execute(self, sql, parameters=()):
        return self._connection.execute(sql, parameters)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        # Repository methods borrow this session so its temporary tables survive.
        pass


class _TemporaryRepository(PostgresAuditRepository):
    def __init__(self, url: str, connection: DatabaseConnection) -> None:
        super().__init__(url)
        self._temporary = _BorrowedConnection(connection)

    def _connect(self) -> DatabaseConnection:
        return self._temporary


@unittest.skipUnless(os.environ.get("MISAKABOT_TEST_DATABASE_URL"), "PostgreSQL integration DSN not configured")
class PostgresIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        url = os.environ["MISAKABOT_TEST_DATABASE_URL"]
        self.connection = PostgresConnection(url)
        self.addCleanup(self.connection.close)
        # Only create TEMP tables. They shadow identically named production
        # tables for this session and are dropped automatically on disconnect.
        for statement in POSTGRES_SCHEMA.split(";"):
            statement = statement.strip()
            if statement.startswith("CREATE TABLE IF NOT EXISTS "):
                self.connection.execute(statement.replace("CREATE TABLE IF NOT EXISTS ", "CREATE TEMP TABLE ", 1))
        self.connection.commit()
        self.repository = _TemporaryRepository(url, self.connection)
        self.now = datetime.now(timezone.utc)

    def test_native_driver_values_and_cursor_iteration(self) -> None:
        from psycopg.types.json import Jsonb

        cursor = self.connection.execute(
            "SELECT %s::bigint AS identifier, %s::timestamptz AS timestamp, %s::jsonb AS payload, %s::boolean AS enabled",
            (7389605164, self.now, Jsonb({"中文": [True, 42]}), True),
        )
        self.assertEqual(cursor.columns, ("identifier", "timestamp", "payload", "enabled"))
        row = cursor.fetchone()
        self.assertEqual(row, (7389605164, self.now, {"中文": [True, 42]}, True))
        self.assertIsInstance(row[1], datetime)
        self.assertIs(row[3], True)
        self.assertIsNone(cursor.fetchone())
        cursor = self.connection.execute("SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3")
        self.assertEqual(cursor.fetchone(), (1,))
        self.assertEqual(list(cursor), [(2,), (3,)])
        self.assertEqual(cursor.fetchall(), [])

    def test_memory_and_guard_timestamp_roundtrip(self) -> None:
        repo = self.repository
        now = self.now.isoformat()
        repo.set_assistant_memory_enabled(-1, 2, True, now)
        self.assertTrue(repo.assistant_memory_enabled(-1, 2))
        repo.remember_assistant_fact(-1, 2, "使用 DMIT", now)
        self.assertEqual(repo.assistant_memories(-1, 2), ["使用 DMIT"])
        repo.record_assistant_message(-1, 2, "user", "你好", now)
        repo.record_assistant_message(-1, 2, "assistant", "你好呀", now)
        self.assertEqual(repo.assistant_context(-1, 2), [("user", "你好"), ("assistant", "你好呀")])
        until = (self.now + timedelta(hours=1)).isoformat()
        repo.save_join_guard_state(-1, "high", until, now, now)
        self.assertEqual(repo.join_guard_state(-1), ("high", until, now, None))
        self.assertEqual(repo.clear_assistant_memories(-1, 2), 1)

    def test_paused_recovery_budget_native_timestamps_and_deletion_cancel(self) -> None:
        repo = self.repository
        now = self.now.isoformat()
        expires = (self.now + timedelta(seconds=120)).isoformat()
        repo.create_group_challenge(
            chat_id=-1, user_id=2, username=None, token_hash="first", answer_index=1,
            expires_at=expires, now=now, verification_flow="join_request",
        )
        self.assertTrue(repo.set_group_challenge_message_id(-1, 2, 10, now, expires, "first"))
        failed_at = self.now + timedelta(seconds=40)
        repo.resolve_group_challenge(
            "first", 2, 0, failed_at.isoformat(), next_token_hash="second", next_answer_index=2,
        )
        self.assertTrue(repo.retry_failed_group_challenge_edit(-1, 2, "second", 10, failed_at.isoformat()))
        paused = repo.group_challenge_snapshot(-1, 2)
        self.assertEqual(paused.remaining_seconds, 80)
        self.assertEqual(paused.attempts, 1)
        self.assertTrue(repo.cancel_message_deletion(-1, 10))
        self.assertFalse(repo.cancel_message_deletion(-1, 10))
        delivered = failed_at + timedelta(minutes=5)
        self.assertTrue(repo.create_group_challenge(
            chat_id=-1, user_id=2, username=None, token_hash="third", answer_index=2,
            expires_at=(delivered + timedelta(minutes=2)).isoformat(), now=delivered.isoformat(),
            verification_flow="join_request", only_if_undelivered=True,
            retry_before=(delivered - timedelta(minutes=2)).isoformat(),
        ))
        recovered = repo.group_challenge_snapshot(-1, 2)
        self.assertEqual(recovered.remaining_seconds, 80)
        self.assertEqual(recovered.attempts, 1)
        deadline = (delivered + timedelta(seconds=80)).isoformat()
        self.assertFalse(repo.set_group_challenge_message_id(-1, 2, 11, delivered.isoformat(), deadline, "second"))
        self.assertEqual(repo.group_challenge_snapshot(-1, 2).remaining_seconds, 80)
        self.assertTrue(repo.set_group_challenge_message_id(-1, 2, 11, delivered.isoformat(), deadline, "third"))
        final = repo.group_challenge_snapshot(-1, 2)
        self.assertIsNone(final.remaining_seconds)
        self.assertEqual(final.expires_at, deadline)
        self.assertEqual(final.attempts, 1)

    def test_departure_claim_retry_cooldown_and_join_request_guards(self) -> None:
        repo = self.repository
        now = self.now.isoformat()
        expires = (self.now + timedelta(minutes=2)).isoformat()
        repo.create_group_challenge(chat_id=-1, user_id=2, username=None, token_hash="first", answer_index=1,
                                    expires_at=expires, now=now, verification_flow="join_request")
        repo.set_group_challenge_message_id(-1, 2, 10, now, expires, "first")
        repo.resolve_group_challenge("first", 2, 0, now, next_token_hash="second", next_answer_index=1)
        self.assertTrue(repo.claim_verification_departure(-1, 2, now))
        self.assertFalse(repo.claim_verification_departure(-1, 2, now))
        self.assertIsNone(repo.resolve_group_challenge_by_admin("second", now, True, chat_id=-1).chat_id)
        self.assertFalse(repo.claim_expired_verification(
            -1, 2, OnboardingState.SECONDARY_VERIFICATION_PENDING, expires,
            (datetime.fromisoformat(expires) - timedelta(minutes=OnboardingService.UNDELIVERED_CHALLENGE_GRACE_MINUTES)).isoformat(),
        ))
        request = JoinRequestInput(chat_id=-1, user_id=2, user_chat_id=2, requested_at=self.now)
        self.assertFalse(repo.create_pending_verification(request, "reset", expires, restart_existing=True))
        self.assertFalse(repo.create_group_challenge(chat_id=-1, user_id=2, username=None, token_hash="reset", answer_index=0,
                                                    expires_at=expires, now=now, verification_flow="direct_join"))
        retry = self.now + timedelta(minutes=3)
        self.assertTrue(repo.claim_verification_departure(-1, 2, retry.isoformat(), retry_before=expires))
        self.assertEqual(repo.group_challenge_snapshot(-1, 2).attempts, 1)
        until = retry + timedelta(hours=1)
        self.assertTrue(repo.finish_verification_departure(-1, 2, retry.isoformat(), retry.isoformat(), until.isoformat()))
        self.assertTrue(repo.departure_blocks_join(-1, 2, retry.isoformat()))
        self.assertFalse(repo.departure_blocks_join(-1, 2, until.isoformat()))
        self.assertIn((-1, 10), repo.due_message_deletions(until.isoformat()))
        self.assertTrue(repo.unblock_user(2, -1))

    def test_challenge_rotation_ack_replay_and_admin_scope(self) -> None:
        repo = self.repository
        now = self.now.isoformat()
        expires = (self.now + timedelta(minutes=2)).isoformat()
        repo.create_group_challenge(
            chat_id=-1, user_id=2, username=None, token_hash="first", answer_index=1,
            expires_at=expires, now=now, verification_flow="join_request",
        )
        repo.schedule_message_deletion(-1, 10, expires)
        self.assertTrue(repo.set_group_challenge_message_id(-1, 2, 10, now, expires, "first"))
        self.assertEqual(repo.query_all("SELECT * FROM scheduled_message_deletions"), [])
        result = repo.resolve_group_challenge("first", 2, 0, now, next_token_hash="second", next_answer_index=2)
        self.assertFalse(result.accepted)
        self.assertEqual(result.attempts_left, 2)
        self.assertEqual(datetime.fromisoformat(result.expires_at), datetime.fromisoformat(expires))
        self.assertIsNone(repo.resolve_group_challenge("first", 2, 1, now).chat_id)
        self.assertTrue(repo.set_group_challenge_message_id(-1, 2, 10, now, result.expires_at, "second"))
        self.assertIsNone(repo.resolve_group_challenge_by_admin("second", now, True, chat_id=-2).chat_id)
        self.assertTrue(repo.resolve_group_challenge("second", 2, 2, now).accepted)
        self.assertFalse(repo.resolve_group_challenge("second", 2, 2, now).accepted)

    def test_audit_jsonb_mapping_filter_pagination_and_summary(self) -> None:
        repo = self.repository
        for message_id, action, review in (
            (1, Action.ALLOW, ReviewStatus.NOT_REQUIRED),
            (2, Action.PERMANENT_BAN, ReviewStatus.CONFIRMED),
            (3, Action.NEEDS_REVIEW, ReviewStatus.PENDING),
        ):
            incoming = MessageInput(chat_id=-1, message_id=message_id, user_id=2, text="测试消息")
            normalized = normalize_message(incoming.text)
            repo.record(incoming, normalized, detect_suspicion(normalized), ModerationVerdict(
                is_ad=action != Action.ALLOW, category="test", confidence=0.95, evidence=("测试",), reason="测试",
            ), action, review)
        client = TestClient(create_app(Settings(
            telegram_bot_token="123:token", admin_user_ids=frozenset({1}), allowed_group_ids=frozenset({-1}),
        ), repo))
        headers = {"X-Telegram-Init-Data": signed_init_data("123:token", 1)}
        with client:
            first_response = client.get("/v1/audit/events?chat_id=-1&limit=1", headers=headers)
            self.assertEqual(first_response.status_code, 200)
            first = first_response.json()
            self.assertEqual(first["items"][0]["verdict"]["evidence"], ["测试"])
            next_page = client.get(
                f"/v1/audit/events?chat_id=-1&limit=1&before_id={first['next_before_id']}", headers=headers,
            ).json()
            self.assertLess(next_page["items"][0]["id"], first["items"][0]["id"])
            bans = client.get("/v1/audit/events?chat_id=-1&filter=banned", headers=headers).json()
            self.assertEqual([row["action"] for row in bans["items"]], ["permanent_ban"])
            summary = client.get("/v1/audit/summary?chat_id=-1", headers=headers).json()
            self.assertEqual(summary["total"], 3)
            self.assertEqual(summary["permanent_bans"], 1)
            self.assertEqual(summary["pending_review"], 1)

    def test_admin_adopts_inflight_message_with_typed_snapshot(self) -> None:
        repo = self.repository
        now = self.now.isoformat()
        expires = (self.now + timedelta(minutes=2)).isoformat()
        repo.create_group_challenge(
            chat_id=-1, user_id=2, username=None, token_hash="first", answer_index=1,
            expires_at=expires, now=now, verification_flow="join_request",
        )
        self.assertTrue(repo.set_group_challenge_message_id(-1, 2, 10, now, expires, "first"))
        repo.resolve_group_challenge("first", 2, 0, now, next_token_hash="second", next_answer_index=2)
        snapshot = repo.group_challenge_snapshot(-1, 2)
        self.assertEqual(snapshot.attempts, 1)
        self.assertEqual(snapshot.expires_at, expires)
        self.assertIsNone(snapshot.message_id)
        self.assertIsNone(repo.resolve_group_challenge_by_admin(
            "second", now, True, chat_id=-2, callback_message_id=999,
        ).chat_id)
        accepted = repo.resolve_group_challenge_by_admin("second", now, True, chat_id=-1, callback_message_id=10)
        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.message_id, 10)
        self.assertEqual(repo.group_challenge_snapshot(-1, 2).message_id, 10)
