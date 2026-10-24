import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from misakabot.domain import OnboardingState, JoinRequestInput
from misakabot.onboarding import OnboardingService
from misakabot.repository import AuditRepository


class RecoveryRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "recovery.sqlite3"
        self.repository = AuditRepository(self.path)

    def test_expiry_methods_require_delivery_deadline_before_opening_sqlite(self) -> None:
        self.repository.initialize()
        now = datetime.now(timezone.utc).isoformat()
        # Deliberately omit the required argument: Python must reject the call
        # before either backend has an opportunity to compare timestamps.
        with patch.object(self.repository, "_connect") as connect:
            for method, args in (
                (self.repository.expired_pending_verifications, (now,)),
                (self.repository.claim_expired_verification,
                 (-1, 42, OnboardingState.SECONDARY_VERIFICATION_PENDING, now)),
            ):
                with self.subTest(method=method.__name__), self.assertRaisesRegex(TypeError, "delivery_deadline"):
                    method(*args)
            connect.assert_not_called()

    def test_undelivered_expiry_and_claim_preserve_thirty_minute_grace_boundary(self) -> None:
        self.repository.initialize()
        started = datetime(2026, 10, 24, 1, tzinfo=timezone.utc)
        expires = started + timedelta(minutes=2)
        for user_id in (1, 2):
            self.repository.create_group_challenge(
                chat_id=-1, user_id=user_id, username=None, token_hash=f"token-{user_id}",
                answer_index=0, expires_at=expires.isoformat(), now=started.isoformat(),
                verification_flow="direct_join",
            )
        self.repository.set_group_challenge_message_id(-1, 1, 41, started.isoformat(), expires.isoformat(), "token-1")
        self.repository.create_pending_verification(
            JoinRequestInput(chat_id=-1, user_id=3, user_chat_id=3, requested_at=started),
            "initial-token", expires.isoformat(),
        )
        grace = timedelta(minutes=OnboardingService.UNDELIVERED_CHALLENGE_GRACE_MINUTES)
        before = expires + grace - timedelta(seconds=1)
        cutoff = (before - grace).isoformat()
        self.assertEqual(
            {record[1] for record in self.repository.expired_pending_verifications(before.isoformat(), cutoff)}, {1, 3},
        )
        self.assertFalse(self.repository.claim_expired_verification(
            -1, 2, OnboardingState.SECONDARY_VERIFICATION_PENDING, before.isoformat(), cutoff,
        ))
        boundary = expires + grace
        cutoff = (boundary - grace).isoformat()
        self.assertEqual(
            {record[1] for record in self.repository.expired_pending_verifications(boundary.isoformat(), cutoff)}, {1, 2, 3},
        )
        self.assertTrue(self.repository.claim_expired_verification(
            -1, 2, OnboardingState.SECONDARY_VERIFICATION_PENDING, boundary.isoformat(), cutoff,
        ))

    def test_cancel_deletion_existing_missing_and_group_isolation(self) -> None:
        self.repository.initialize()
        due = "2026-10-24T01:00:00+00:00"
        for chat_id, message_id in ((-1, 42), (-2, 42), (-1, 43)):
            self.repository.schedule_message_deletion(chat_id, message_id, due)
        self.assertFalse(self.repository.cancel_message_deletion(-1, 999))
        self.assertTrue(self.repository.cancel_message_deletion(-1, 42))
        self.assertFalse(self.repository.cancel_message_deletion(-1, 42))
        self.assertEqual(set(self.repository.due_message_deletions(due)), {(-2, 42), (-1, 43)})

    def test_sqlite_existing_schema_migrates_idempotently_without_resetting_attempts(self) -> None:
        # Model the previous release's table, genuinely missing the new column.
        with sqlite3.connect(self.path) as connection:
            connection.execute("""CREATE TABLE member_onboarding (
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, user_chat_id INTEGER NOT NULL,
                username TEXT, invite_link TEXT, verification_flow TEXT NOT NULL DEFAULT 'join_request',
                state TEXT NOT NULL, verification_token_hash TEXT, verification_expires_at TEXT,
                verification_attempts INTEGER NOT NULL DEFAULT 0, verification_answer_index INTEGER,
                verification_message_id INTEGER, joined_at TEXT, first_message_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(chat_id, user_id)
            )""")
            connection.execute("""INSERT INTO member_onboarding (
                chat_id, user_id, user_chat_id, state, verification_token_hash,
                verification_expires_at, verification_attempts, created_at, updated_at
            ) VALUES (-1, 42, 42, ?, 'old-token', '2026-10-24T01:02:00+00:00', 2,
                      '2026-10-24T01:00:00+00:00', '2026-10-24T01:00:40+00:00')""",
                (OnboardingState.SECONDARY_VERIFICATION_PENDING.value,))
        self.repository.initialize()
        snapshot = self.repository.group_challenge_snapshot(-1, 42)
        self.assertEqual(snapshot.attempts, 2)
        self.assertEqual(snapshot.token_hash, "old-token")
        self.assertEqual(snapshot.expires_at, "2026-10-24T01:02:00+00:00")
        self.assertIsNone(snapshot.remaining_seconds)
        self.assertTrue(self.repository.retry_failed_group_challenge_edit(
            -1, 42, "old-token", 99, "2026-10-24T01:00:40+00:00",
        ))
        self.repository.initialize()
        migrated = self.repository.group_challenge_snapshot(-1, 42)
        self.assertEqual(migrated.attempts, 2)
        self.assertEqual(migrated.remaining_seconds, 80)
        self.assertEqual(migrated.expires_at, snapshot.expires_at)

    def test_departure_columns_migrate_without_removing_existing_deletion_jobs(self) -> None:
        # Start at the second-round schema, then exercise real ALTER statements.
        self.repository.initialize()
        with sqlite3.connect(self.path) as connection:
            connection.execute("ALTER TABLE member_onboarding DROP COLUMN verification_ban_until")
            connection.execute("ALTER TABLE scheduled_message_deletions DROP COLUMN verification_user_id")
            connection.execute("INSERT INTO scheduled_message_deletions(chat_id,message_id,due_at) VALUES(-1,42,'2026-10-24T01:00:00+00:00')")
        self.repository.initialize()
        self.repository.initialize()
        self.assertTrue(self.repository.cancel_message_deletion(-1, 42))
        self.repository.schedule_message_deletion(-1, 42, "2026-10-24T01:01:00+00:00", verification_user_id=7)
        row = self.repository.query_one("SELECT verification_user_id FROM scheduled_message_deletions WHERE chat_id=-1 AND message_id=42")
        self.assertEqual(row["verification_user_id"], 7)
