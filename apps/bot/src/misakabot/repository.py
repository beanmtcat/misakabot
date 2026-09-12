from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

from .domain import (
    Action,
    JoinRequestInput,
    MessageInput,
    ModerationVerdict,
    NormalizedMessage,
    OnboardingState,
    ReviewStatus,
    SuspicionSignals,
)


def database_integer(value: object) -> int:
    """Validate integer database fields before passing them to Telegram APIs."""
    if isinstance(value, (int, str)):
        return int(value)
    raise TypeError(f"Expected an integer database field, got {type(value).__name__}")


@dataclass(frozen=True)
class GroupChallengeResolution:
    chat_id: int | None
    user_id: int | None
    message_id: int | None
    accepted: bool
    terminal: bool
    attempts_left: int


class _SqliteConnection:
    """Accept PostgreSQL-style placeholders while keeping the local backup backend testable."""

    def __init__(self, database_path: str) -> None:
        self._connection = sqlite3.connect(database_path)

    def execute(self, sql: str, parameters: tuple[object, ...] | list[object] = ()) -> sqlite3.Cursor:
        return self._connection.execute(sql.replace("%s", "?"), parameters)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


class AuditRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)

    @property
    def is_postgres(self) -> bool:
        return False

    def _connect(self) -> Any:
        return _SqliteConnection(self.database_path)

    def _json_value(self, value: str) -> object:
        """Return a database-native JSON parameter when the backend supports one."""
        return value

    def initialize(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS moderation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    raw_text TEXT NOT NULL,
                    normalized_text TEXT NOT NULL,
                    urls_json TEXT NOT NULL,
                    signals_json TEXT NOT NULL,
                    verdict_json TEXT,
                    action TEXT NOT NULL,
                    review_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_id, message_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS member_onboarding (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    user_chat_id INTEGER NOT NULL,
                    username TEXT,
                    invite_link TEXT,
                    verification_flow TEXT NOT NULL DEFAULT 'join_request',
                    state TEXT NOT NULL,
                    verification_token_hash TEXT,
                    verification_expires_at TEXT,
                    verification_attempts INTEGER NOT NULL DEFAULT 0,
                    verification_answer_index INTEGER,
                    verification_message_id INTEGER,
                    joined_at TEXT,
                    first_message_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, user_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_moderation_events_chat_action_id
                ON moderation_events(chat_id, action, id DESC)
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(member_onboarding)")}
            if "verification_flow" not in columns:
                connection.execute(
                    "ALTER TABLE member_onboarding ADD COLUMN verification_flow TEXT NOT NULL DEFAULT 'join_request'"
                )
            if "verification_answer_index" not in columns:
                connection.execute("ALTER TABLE member_onboarding ADD COLUMN verification_answer_index INTEGER")
            if "verification_message_id" not in columns:
                connection.execute("ALTER TABLE member_onboarding ADD COLUMN verification_message_id INTEGER")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS blocked_indicators (
                    indicator_type TEXT NOT NULL,
                    indicator_value TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(indicator_type, indicator_value)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS member_first_observations (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    first_message_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, user_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_sessions (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, user_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_assistant_messages_context
                ON assistant_messages(chat_id, user_id, id DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_memory_preferences (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    memory_enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, user_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_user_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_id, user_id, content)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_updates (
                    update_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,
                    leased_at TEXT NOT NULL,
                    processed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_message_deletions (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    due_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, message_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scheduled_message_deletions_due
                ON scheduled_message_deletions(due_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_webhook_updates_processed_at
                ON webhook_updates(status, processed_at)
                """
            )
            connection.commit()

    def record(
        self,
        incoming: MessageInput,
        normalized: NormalizedMessage,
        signals: SuspicionSignals,
        verdict: ModerationVerdict | None,
        action: Action,
        review_status: ReviewStatus,
    ) -> int:
        now = incoming.sent_at.isoformat()
        payload = None if verdict is None else json.dumps(
            {
                "is_ad": verdict.is_ad,
                "category": verdict.category,
                "confidence": verdict.confidence,
                "evidence": verdict.evidence,
                "reason": verdict.reason,
            }, ensure_ascii=False,
        )
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO moderation_events (
                    chat_id, message_id, user_id, username, raw_text, normalized_text,
                    urls_json, signals_json, verdict_json, action, review_status, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    verdict_json=excluded.verdict_json, action=excluded.action,
                    review_status=excluded.review_status, updated_at=excluded.updated_at
                """,
                (
                    incoming.chat_id, incoming.message_id, incoming.user_id, incoming.username,
                    normalized.raw_text, normalized.normalized_text,
                    self._json_value(json.dumps(normalized.urls, ensure_ascii=False)),
                    self._json_value(json.dumps({"score": signals.score, "reasons": signals.reasons}, ensure_ascii=False)),
                    self._json_value(payload) if payload is not None else None,
                    action.value, review_status.value, now, now,
                ),
            )
            connection.commit()
            return int(connection.execute(
                "SELECT id FROM moderation_events WHERE chat_id=%s AND message_id=%s",
                (incoming.chat_id, incoming.message_id),
            ).fetchone()[0])

    def query_all(self, sql: str, parameters: tuple[object, ...] | list[object] = ()) -> list[dict[str, object]]:
        """Execute a read query and return portable mapping rows for the audit API."""
        with closing(self._connect()) as connection:
            cursor = connection.execute(sql, parameters)
            columns = [column[0] for column in cursor.description or ()]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def query_one(
        self, sql: str, parameters: tuple[object, ...] | list[object] = ()
    ) -> dict[str, object] | None:
        with closing(self._connect()) as connection:
            cursor = connection.execute(sql, parameters)
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [column[0] for column in cursor.description or ()]
            return dict(zip(columns, row, strict=True))

    def execute_write(self, sql: str, parameters: tuple[object, ...] | list[object] = ()) -> int:
        with closing(self._connect()) as connection:
            cursor = connection.execute(sql, parameters)
            connection.commit()
            return int(cursor.rowcount)

    def assistant_memory_enabled(self, chat_id: int, user_id: int) -> bool:
        row = self.query_one(
            "SELECT memory_enabled FROM assistant_memory_preferences WHERE chat_id=%s AND user_id=%s",
            (chat_id, user_id),
        )
        return bool(row and row["memory_enabled"])

    def set_assistant_memory_enabled(self, chat_id: int, user_id: int, enabled: bool, now: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO assistant_memory_preferences(chat_id, user_id, memory_enabled, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    memory_enabled=excluded.memory_enabled, updated_at=excluded.updated_at
                """,
                (chat_id, user_id, enabled, now),
            )
            connection.commit()

    def remember_assistant_fact(self, chat_id: int, user_id: int, content: str, now: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO assistant_user_memories(chat_id, user_id, content, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(chat_id, user_id, content) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (chat_id, user_id, content, now, now),
            )
            connection.commit()

    def clear_assistant_memories(self, chat_id: int, user_id: int) -> int:
        return self.execute_write(
            "DELETE FROM assistant_user_memories WHERE chat_id=%s AND user_id=%s", (chat_id, user_id)
        )

    def assistant_memories(self, chat_id: int, user_id: int, limit: int = 20) -> list[str]:
        rows = self.query_all(
            """
            SELECT content FROM assistant_user_memories
            WHERE chat_id=%s AND user_id=%s
            ORDER BY updated_at DESC, id DESC LIMIT %s
            """,
            (chat_id, user_id, limit),
        )
        return [str(row["content"]) for row in rows]

    def assistant_context(self, chat_id: int, user_id: int, limit: int = 12) -> list[tuple[str, str]]:
        rows = self.query_all(
            """
            SELECT role, content FROM assistant_messages
            WHERE chat_id=%s AND user_id=%s
            ORDER BY id DESC LIMIT %s
            """,
            (chat_id, user_id, limit),
        )
        return [(str(row["role"]), str(row["content"])) for row in reversed(rows)]

    def record_assistant_message(
        self, chat_id: int, user_id: int, role: str, content: str, now: str, keep: int = 12
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO assistant_sessions(chat_id, user_id, created_at, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (chat_id, user_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO assistant_messages(chat_id, user_id, role, content, created_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (chat_id, user_id, role, content, now),
            )
            connection.execute(
                """
                DELETE FROM assistant_messages
                WHERE chat_id=%s AND user_id=%s AND id NOT IN (
                    SELECT id FROM assistant_messages
                    WHERE chat_id=%s AND user_id=%s ORDER BY id DESC LIMIT %s
                )
                """,
                (chat_id, user_id, chat_id, user_id, keep),
            )
            connection.commit()

    def claim_webhook_update(self, update_id: int, now: str, stale_before: str) -> str:
        """Claim one Telegram update; a stale lease may be safely retried."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, leased_at FROM webhook_updates WHERE update_id=%s", (update_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO webhook_updates(update_id, status, leased_at) VALUES (%s, 'processing', %s)",
                    (update_id, now),
                )
                result = "claimed"
            elif row[0] == "processed":
                result = "processed"
            elif str(row[1]) > stale_before:
                result = "busy"
            else:
                connection.execute(
                    "UPDATE webhook_updates SET status='processing', leased_at=%s, processed_at=NULL WHERE update_id=%s",
                    (now, update_id),
                )
                result = "claimed"
            connection.commit()
            return result

    def complete_webhook_update(self, update_id: int, now: str) -> None:
        self.execute_write(
            "UPDATE webhook_updates SET status='processed', processed_at=%s WHERE update_id=%s",
            (now, update_id),
        )

    def release_webhook_update(self, update_id: int) -> None:
        self.execute_write("DELETE FROM webhook_updates WHERE update_id=%s AND status='processing'", (update_id,))

    def prune_webhook_updates(self, processed_before: str, processing_before: str) -> int:
        """Bound completed records and abandoned processing leases.

        A processing lease older than the normal replay window cannot represent an
        active handler anymore. Removing it prevents a crash without a Telegram
        retry from leaving permanent rows behind.
        """
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                DELETE FROM webhook_updates
                WHERE (status='processed' AND processed_at < %s)
                   OR (status='processing' AND leased_at < %s)
                """,
                (processed_before, processing_before),
            )
            connection.commit()
            return int(cursor.rowcount)

    def schedule_message_deletion(self, chat_id: int, message_id: int, due_at: str) -> None:
        self.execute_write(
            """
            INSERT INTO scheduled_message_deletions(chat_id, message_id, due_at)
            VALUES (%s, %s, %s)
            ON CONFLICT(chat_id, message_id) DO UPDATE SET due_at=excluded.due_at
            """,
            (chat_id, message_id, due_at),
        )

    def due_message_deletions(self, now: str, limit: int = 100) -> list[tuple[int, int]]:
        rows = self.query_all(
            """
            SELECT chat_id, message_id FROM scheduled_message_deletions
            WHERE due_at <= %s ORDER BY due_at ASC LIMIT %s
            """,
            (now, limit),
        )
        return [
            (database_integer(row["chat_id"]), database_integer(row["message_id"]))
            for row in rows
        ]

    def claim_message_deletion(
        self, chat_id: int, message_id: int, now: str, claimed_until: str
    ) -> bool:
        """Atomically postpone eligibility while one worker performs the deletion.

        Both the timer and the recovery worker must claim before calling Telegram.
        The row remains durable; a crashed claimant becomes retryable after the lease.
        """
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE scheduled_message_deletions SET due_at=%s
                WHERE chat_id=%s AND message_id=%s AND due_at <= %s
                """,
                (claimed_until, chat_id, message_id, now),
            )
            claimed = cursor.rowcount == 1
            connection.commit()
            return claimed

    def complete_message_deletion(
        self, chat_id: int, message_id: int, claimed_until: str
    ) -> None:
        self.execute_write(
            """
            DELETE FROM scheduled_message_deletions
            WHERE chat_id=%s AND message_id=%s AND due_at=%s
            """,
            (chat_id, message_id, claimed_until),
        )

    def is_user_blocked(self, user_id: int) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM blocked_indicators WHERE indicator_type='telegram_user_id' AND indicator_value=%s",
                (str(user_id),),
            ).fetchone()
        return row is not None

    def block_user(self, user_id: int, reason: str, created_at: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO blocked_indicators(indicator_type, indicator_value, reason, created_at)
                VALUES ('telegram_user_id', %s, %s, %s)
                ON CONFLICT(indicator_type, indicator_value) DO UPDATE SET reason=excluded.reason
                """,
                (str(user_id), reason, created_at),
            )
            connection.commit()

    def unblock_user(self, user_id: int) -> bool:
        """Remove the local permanent-ban indicator after Telegram has unbanned the account."""
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM blocked_indicators WHERE indicator_type='telegram_user_id' AND indicator_value=%s",
                (str(user_id),),
            )
            connection.commit()
        return cursor.rowcount == 1

    def create_pending_verification(
        self,
        request: JoinRequestInput,
        token_hash: str,
        expires_at: str,
        verification_flow: str = "join_request",
    ) -> None:
        now = request.requested_at.isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO member_onboarding(
                    chat_id, user_id, user_chat_id, username, invite_link, verification_flow, state,
                    verification_token_hash, verification_expires_at, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    user_chat_id=excluded.user_chat_id, username=excluded.username,
                    invite_link=excluded.invite_link, verification_flow=excluded.verification_flow,
                    state=excluded.state,
                    verification_token_hash=excluded.verification_token_hash,
                    verification_expires_at=excluded.verification_expires_at,
                    verification_attempts=0, updated_at=excluded.updated_at
                """,
                (
                    request.chat_id, request.user_id, request.user_chat_id, request.username,
                    request.invite_link, verification_flow, OnboardingState.VERIFICATION_PENDING.value,
                    token_hash, expires_at, now, now,
                ),
            )
            connection.commit()

    def onboarding_state(self, chat_id: int, user_id: int) -> OnboardingState | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT state FROM member_onboarding WHERE chat_id=%s AND user_id=%s",
                (chat_id, user_id),
            ).fetchone()
        return OnboardingState(str(row[0])) if row else None

    def onboarding_username(self, chat_id: int, user_id: int) -> str | None:
        """Return the most recently observed public username for a member, if any."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT username FROM member_onboarding WHERE chat_id=%s AND user_id=%s",
                (chat_id, user_id),
            ).fetchone()
        return str(row[0]) if row and row[0] else None

    def mark_join_request_manually_approved(self, chat_id: int, user_id: int, now: str) -> bool:
        """Close the first-stage timer when a Telegram administrator approves a request.

        Telegram does not send a distinct "approved by another admin" update to the Bot.
        Seeing the member join while their join request is still pending is therefore the
        authoritative signal that the first stage was manually approved.
        """
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE member_onboarding
                SET state=%s, verification_token_hash=NULL, verification_expires_at=NULL,
                    joined_at=%s, updated_at=%s
                WHERE chat_id=%s AND user_id=%s AND state=%s AND verification_flow=%s
                """,
                (
                    OnboardingState.PENDING_FIRST_MESSAGE.value,
                    now,
                    now,
                    chat_id,
                    user_id,
                    OnboardingState.VERIFICATION_PENDING.value,
                    "join_request",
                ),
            )
            connection.commit()
        return cursor.rowcount == 1

    def pending_direct_join_verifications(self) -> list[tuple[int, int, str | None]]:
        """Return legacy click-to-pass direct joins so startup can replace their challenge."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT chat_id, user_id, username FROM member_onboarding
                WHERE verification_flow=%s AND state=%s
                """,
                ("direct_join", OnboardingState.VERIFICATION_PENDING.value),
            ).fetchall()
        return [(int(row[0]), int(row[1]), row[2]) for row in rows]

    def create_group_challenge(
        self,
        *,
        chat_id: int,
        user_id: int,
        username: str | None,
        token_hash: str,
        answer_index: int,
        expires_at: str,
        now: str,
        verification_flow: str,
    ) -> None:
        """Persist a one-time post-join challenge; correct option is never exposed in callback data."""
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO member_onboarding(
                    chat_id, user_id, user_chat_id, username, verification_flow, state,
                    verification_token_hash, verification_expires_at, verification_attempts,
                    verification_answer_index, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    username=excluded.username, verification_flow=excluded.verification_flow,
                    state=excluded.state, verification_token_hash=excluded.verification_token_hash,
                    verification_expires_at=excluded.verification_expires_at,
                    verification_attempts=0,
                    verification_answer_index=excluded.verification_answer_index,
                    verification_message_id=NULL,
                    updated_at=excluded.updated_at
                """,
                (
                    chat_id,
                    user_id,
                    chat_id,
                    username,
                    verification_flow,
                    OnboardingState.SECONDARY_VERIFICATION_PENDING.value,
                    token_hash,
                    expires_at,
                    answer_index,
                    now,
                    now,
                ),
            )
            connection.commit()

    def set_group_challenge_message_id(self, chat_id: int, user_id: int, message_id: int, now: str) -> None:
        """Store the group message so its challenge can later be replaced with a final status."""
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE member_onboarding
                SET verification_message_id=%s, updated_at=%s
                WHERE chat_id=%s AND user_id=%s AND state=%s
                """,
                (
                    message_id,
                    now,
                    chat_id,
                    user_id,
                    OnboardingState.SECONDARY_VERIFICATION_PENDING.value,
                ),
            )
            connection.commit()

    def resolve_group_challenge(
        self,
        token_hash: str,
        telegram_user_id: int,
        selected_index: int,
        now: str,
        max_attempts: int = 3,
    ) -> GroupChallengeResolution:
        """Atomically resolve a bound multiple-choice challenge and count wrong answers."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT chat_id, user_id, verification_answer_index, verification_attempts,
                       verification_message_id
                FROM member_onboarding
                WHERE verification_token_hash=%s AND user_id=%s AND state=%s
                  AND verification_expires_at > %s
                """,
                (
                    token_hash,
                    telegram_user_id,
                    OnboardingState.SECONDARY_VERIFICATION_PENDING.value,
                    now,
                ),
            ).fetchone()
            if row is None:
                connection.rollback()
                return GroupChallengeResolution(None, None, None, False, False, 0)
            chat_id, user_id, answer_index, attempts = map(int, row[:4])
            message_id = int(row[4]) if row[4] is not None else None
            if selected_index == answer_index:
                connection.execute(
                    """
                    UPDATE member_onboarding
                    SET state=%s, updated_at=%s
                    WHERE chat_id=%s AND user_id=%s
                    """,
                    (OnboardingState.PENDING_FIRST_MESSAGE.value, now, chat_id, user_id),
                )
                connection.commit()
                return GroupChallengeResolution(chat_id, user_id, message_id, True, False, max_attempts - attempts)

            used_attempts = attempts + 1
            terminal = used_attempts >= max_attempts
            if terminal:
                connection.execute(
                    """
                    UPDATE member_onboarding
                    SET state=%s, verification_attempts=%s, updated_at=%s
                    WHERE chat_id=%s AND user_id=%s
                    """,
                    (OnboardingState.DECLINED.value, used_attempts, now, chat_id, user_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE member_onboarding SET verification_attempts=%s, updated_at=%s
                    WHERE chat_id=%s AND user_id=%s
                    """,
                    (used_attempts, now, chat_id, user_id),
                )
            connection.commit()
        return GroupChallengeResolution(
            chat_id, user_id, message_id, False, terminal, max_attempts - used_attempts
        )

    def resolve_group_challenge_by_admin(
        self, token_hash: str, now: str, approved: bool
    ) -> GroupChallengeResolution:
        """Let a group administrator settle one active post-join challenge."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT chat_id, user_id, verification_attempts, verification_message_id
                FROM member_onboarding
                WHERE verification_token_hash=%s AND state=%s AND verification_expires_at > %s
                """,
                (token_hash, OnboardingState.SECONDARY_VERIFICATION_PENDING.value, now),
            ).fetchone()
            if row is None:
                connection.rollback()
                return GroupChallengeResolution(None, None, None, False, False, 0)
            chat_id, user_id, attempts = map(int, row[:3])
            message_id = int(row[3]) if row[3] is not None else None
            state = OnboardingState.PENDING_FIRST_MESSAGE if approved else OnboardingState.DECLINED
            connection.execute(
                """
                UPDATE member_onboarding
                SET state=%s, updated_at=%s
                WHERE chat_id=%s AND user_id=%s
                """,
                (state.value, now, chat_id, user_id),
            )
            connection.commit()
        return GroupChallengeResolution(chat_id, user_id, message_id, approved, not approved, 3 - attempts)

    def finalize_group_challenge(self, chat_id: int, user_id: int, now: str) -> None:
        """Clear replay material only after Telegram completed the member action."""
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE member_onboarding
                SET verification_token_hash=NULL, verification_expires_at=NULL,
                    verification_answer_index=NULL, updated_at=%s
                WHERE chat_id=%s AND user_id=%s AND state IN (%s, %s)
                """,
                (
                    now,
                    chat_id,
                    user_id,
                    OnboardingState.PENDING_FIRST_MESSAGE.value,
                    OnboardingState.DECLINED.value,
                ),
            )
            connection.commit()

    def reopen_group_challenge(self, chat_id: int, user_id: int, now: str) -> None:
        """Restore a challenge when the Telegram action failed after its provisional decision."""
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE member_onboarding SET state=%s, updated_at=%s
                WHERE chat_id=%s AND user_id=%s AND state IN (%s, %s)
                """,
                (
                    OnboardingState.SECONDARY_VERIFICATION_PENDING.value,
                    now,
                    chat_id,
                    user_id,
                    OnboardingState.PENDING_FIRST_MESSAGE.value,
                    OnboardingState.DECLINED.value,
                ),
            )
            connection.commit()

    def consume_verification(
        self,
        token_hash: str,
        telegram_user_id: int,
        now: str,
    ) -> tuple[int, int, str] | None:
        """Atomically consume a one-time button token and return its verified join flow."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT chat_id, user_id, verification_flow FROM member_onboarding
                WHERE verification_token_hash=%s AND user_id=%s
                  AND state=%s AND verification_expires_at > %s
                """,
                (token_hash, telegram_user_id, OnboardingState.VERIFICATION_PENDING.value, now),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            connection.execute(
                """
                UPDATE member_onboarding
                SET state=%s, verification_token_hash=NULL, verification_expires_at=NULL,
                    joined_at=%s, updated_at=%s
                WHERE chat_id=%s AND user_id=%s
                """,
                (OnboardingState.PENDING_FIRST_MESSAGE.value, now, now, row[0], row[1]),
            )
            connection.commit()
        return int(row[0]), int(row[1]), str(row[2])

    def pending_initial_verification(
        self, token_hash: str, telegram_user_id: int
    ) -> tuple[int, int] | None:
        """Find a still-pending first-stage request owned by the Telegram account."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT chat_id, user_id FROM member_onboarding
                WHERE verification_token_hash=%s AND user_id=%s AND state=%s AND verification_flow=%s
                """,
                (
                    token_hash,
                    telegram_user_id,
                    OnboardingState.VERIFICATION_PENDING.value,
                    "join_request",
                ),
            ).fetchone()
        return (int(row[0]), int(row[1])) if row else None

    def mark_initial_verification_failed(self, chat_id: int, user_id: int, now: str) -> bool:
        """Close a first-stage request after Telegram has rejected it."""
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE member_onboarding
                SET state=%s, verification_token_hash=NULL, verification_expires_at=NULL, updated_at=%s
                WHERE chat_id=%s AND user_id=%s AND state=%s AND verification_flow=%s
                """,
                (
                    OnboardingState.DECLINED.value,
                    now,
                    chat_id,
                    user_id,
                    OnboardingState.VERIFICATION_PENDING.value,
                    "join_request",
                ),
            )
            connection.commit()
        return cursor.rowcount == 1

    def mark_first_message(self, chat_id: int, user_id: int, timestamp: str) -> bool:
        """Returns true only for a member's first message after automatic verification."""
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE member_onboarding
                SET state=%s, first_message_at=%s, updated_at=%s
                WHERE chat_id=%s AND user_id=%s AND state=%s AND first_message_at IS NULL
                """,
                (
                    OnboardingState.OBSERVING.value, timestamp, timestamp, chat_id, user_id,
                    OnboardingState.PENDING_FIRST_MESSAGE.value,
                ),
            )
            connection.commit()
        return cursor.rowcount == 1

    def mark_first_observed_message(self, chat_id: int, user_id: int, timestamp: str) -> bool:
        """Return true exactly once for every sender observed in a specific group."""
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO member_first_observations(chat_id, user_id, first_message_at)
                VALUES (%s, %s, %s)
                ON CONFLICT(chat_id, user_id) DO NOTHING
                """,
                (chat_id, user_id, timestamp),
            )
            connection.commit()
        return cursor.rowcount == 1

    def expired_pending_verifications(self, now: str) -> list[tuple[int, int, str, OnboardingState, int | None]]:
        """List unverified members whose deadline passed; callers perform Telegram actions."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT chat_id, user_id, verification_flow, state, verification_message_id
                FROM member_onboarding
                WHERE state IN (%s, %s) AND verification_expires_at IS NOT NULL
                  AND verification_expires_at <= %s
                """,
                (
                    OnboardingState.VERIFICATION_PENDING.value,
                    OnboardingState.SECONDARY_VERIFICATION_PENDING.value,
                    now,
                ),
            ).fetchall()
        return [
            (
                int(row[0]),
                int(row[1]),
                str(row[2]),
                OnboardingState(str(row[3])),
                int(row[4]) if row[4] is not None else None,
            )
            for row in rows
        ]

    def claim_expired_verification(
        self, chat_id: int, user_id: int, state: OnboardingState, now: str
    ) -> bool:
        """Atomically reserve an expired verification before a Telegram-side action.

        The expiry worker can otherwise read an expired row, an administrator can approve
        it, and the worker can still ban that now-approved member using its stale read.
        Moving the row out of the pending state before the external action makes the
        administrator action and the timeout action mutually exclusive.
        """
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE member_onboarding
                SET state=%s, updated_at=%s
                WHERE chat_id=%s AND user_id=%s AND state=%s
                  AND verification_expires_at IS NOT NULL AND verification_expires_at <= %s
                """,
                (
                    OnboardingState.DECLINED.value,
                    now,
                    chat_id,
                    user_id,
                    state.value,
                    now,
                ),
            )
            connection.commit()
        return cursor.rowcount == 1

    def reopen_expired_verification(
        self, chat_id: int, user_id: int, state: OnboardingState, now: str
    ) -> None:
        """Restore a claimed timeout when Telegram could not complete its action."""
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE member_onboarding
                SET state=%s, updated_at=%s
                WHERE chat_id=%s AND user_id=%s AND state=%s
                  AND verification_expires_at IS NOT NULL AND verification_expires_at <= %s
                """,
                (
                    state.value,
                    now,
                    chat_id,
                    user_id,
                    OnboardingState.DECLINED.value,
                    now,
                ),
            )
            connection.commit()

    def finalize_expired_verification(self, chat_id: int, user_id: int, now: str) -> bool:
        """Clear sensitive verification fields after its Telegram-side timeout completed."""
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE member_onboarding
                SET verification_token_hash=NULL, verification_expires_at=NULL, updated_at=%s
                WHERE chat_id=%s AND user_id=%s AND state=%s
                """,
                (now, chat_id, user_id, OnboardingState.DECLINED.value),
            )
            connection.commit()
        return cursor.rowcount == 1

    def mark_verification_expired(self, chat_id: int, user_id: int, now: str) -> bool:
        """Consume a pending verification only after its Telegram-side rejection succeeded."""
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE member_onboarding
                SET state=%s, verification_token_hash=NULL, verification_expires_at=NULL, updated_at=%s
                WHERE chat_id=%s AND user_id=%s AND state IN (%s, %s) AND verification_expires_at <= %s
                """,
                (
                    OnboardingState.DECLINED.value,
                    now,
                    chat_id,
                    user_id,
                    OnboardingState.VERIFICATION_PENDING.value,
                    OnboardingState.SECONDARY_VERIFICATION_PENDING.value,
                    now,
                ),
            )
            connection.commit()
        return cursor.rowcount == 1

    def reset_verification_after_approval_failure(
        self,
        chat_id: int,
        user_id: int,
        token_hash: str,
        expires_at: str,
        now: str,
    ) -> None:
        """Restore the one-time token only when Telegram could not approve the request."""
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE member_onboarding
                SET state=%s, verification_token_hash=%s, verification_expires_at=%s, updated_at=%s
                WHERE chat_id=%s AND user_id=%s AND state=%s
                """,
                (
                    OnboardingState.VERIFICATION_PENDING.value, token_hash, expires_at, now,
                    chat_id, user_id, OnboardingState.PENDING_FIRST_MESSAGE.value,
                ),
            )
            connection.commit()


POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS moderation_events (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    username TEXT,
    raw_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    urls_json JSONB NOT NULL,
    signals_json JSONB NOT NULL,
    verdict_json JSONB,
    action TEXT NOT NULL,
    review_status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS member_onboarding (
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    user_chat_id BIGINT NOT NULL,
    username TEXT,
    invite_link TEXT,
    verification_flow TEXT NOT NULL DEFAULT 'join_request',
    state TEXT NOT NULL,
    verification_token_hash TEXT,
    verification_expires_at TIMESTAMPTZ,
    verification_attempts INTEGER NOT NULL DEFAULT 0,
    verification_answer_index INTEGER,
    verification_message_id BIGINT,
    joined_at TIMESTAMPTZ,
    first_message_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS blocked_indicators (
    indicator_type TEXT NOT NULL,
    indicator_value TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(indicator_type, indicator_value)
);
CREATE TABLE IF NOT EXISTS member_first_observations (
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    first_message_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_moderation_events_chat_action_id
    ON moderation_events(chat_id, action, id DESC);
CREATE TABLE IF NOT EXISTS assistant_sessions (
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS assistant_messages (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assistant_messages_context
    ON assistant_messages(chat_id, user_id, id DESC);
CREATE TABLE IF NOT EXISTS assistant_memory_preferences (
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    memory_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS assistant_user_memories (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(chat_id, user_id, content)
);
CREATE TABLE IF NOT EXISTS webhook_updates (
    update_id BIGINT PRIMARY KEY,
    status TEXT NOT NULL,
    leased_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS scheduled_message_deletions (
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_scheduled_message_deletions_due
    ON scheduled_message_deletions(due_at);
CREATE INDEX IF NOT EXISTS idx_webhook_updates_processed_at
    ON webhook_updates(status, processed_at);
"""


class _PostgresConnection:
    """Minimal SQLite-API compatibility layer for the repository's parameterized SQL."""

    def __init__(self, database_url: str) -> None:
        try:
            self._psycopg = import_module("psycopg")
        except ImportError as error:  # pragma: no cover - exercised in deployment configuration
            raise RuntimeError("PostgreSQL requires psycopg; install the project dependencies") from error
        self._connection = self._psycopg.connect(database_url)

    def execute(self, sql: str, parameters: tuple[object, ...] | list[object] = ()) -> Any:
        if sql.strip() == "BEGIN IMMEDIATE":
            # SQLite's BEGIN IMMEDIATE serialises subsequent writers.  Take a
            # transaction-scoped table lock so challenge consumption/expiry keeps
            # the same one-winner guarantee after moving to PostgreSQL.
            self._connection.execute("BEGIN")
            return self._connection.execute("LOCK TABLE member_onboarding IN SHARE ROW EXCLUSIVE MODE")
        return self._connection.execute(sql.replace("datetime('now')", "NOW()"), parameters)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


class PostgresAuditRepository(AuditRepository):
    """PostgreSQL implementation of the Bot and audit repository.

    The public repository API intentionally stays identical to ``AuditRepository`` so
    moderation and onboarding use the same transactional behaviour after cutover.
    """

    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgres://", "postgresql://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        self.database_url = database_url
        # Kept only for compatibility with code that logs the repository location.
        super().__init__("postgresql")

    @property
    def is_postgres(self) -> bool:
        return True

    def _connect(self) -> _PostgresConnection:
        return _PostgresConnection(self.database_url)

    def _json_value(self, value: str) -> object:
        try:
            json_types = import_module("psycopg.types.json")
        except ImportError as error:  # pragma: no cover - covered by _connect in deployment
            raise RuntimeError("PostgreSQL requires psycopg; install the project dependencies") from error
        return json_types.Jsonb(json.loads(value))

    def initialize(self) -> None:
        with closing(self._connect()) as connection:
            # Bot and audit-api start independently and can both initialise a
            # fresh database.  PostgreSQL's CREATE TABLE IF NOT EXISTS is not
            # race-free in that case: concurrent table creation can collide on
            # the internal composite type.  Keep all schema DDL behind one
            # transaction-scoped advisory lock.
            connection.execute("SELECT pg_advisory_xact_lock(619420431)")
            # psycopg accepts multiple statements on a simple query, but execute each
            # one separately so this remains safe with prepared-statement settings.
            for statement in POSTGRES_SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.commit()
