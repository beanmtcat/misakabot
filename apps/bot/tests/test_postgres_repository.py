from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from misakabot.domain import Action, MessageInput, ReviewStatus
from misakabot.normalizer import normalize_message
from misakabot.repository import PostgresAuditRepository
from misakabot.signals import detect_suspicion


class _Jsonb:
    def __init__(self, value: object) -> None:
        self.value = value


class _Cursor:
    description = [("id",)]

    def __init__(self, row: tuple[object, ...] | None = None) -> None:
        self._row = row
        self.rowcount = 1

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


class _Connection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []
        self.committed = False

    def execute(self, sql: str, parameters: object = ()) -> _Cursor:
        self.executed.append((sql, parameters))
        return _Cursor((42,)) if "SELECT id FROM moderation_events" in sql else _Cursor()

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class PostgresRepositoryTests(unittest.TestCase):
    def test_uses_native_postgres_placeholders_and_jsonb(self) -> None:
        connections: list[_Connection] = []

        def connect(_: str) -> _Connection:
            connection = _Connection()
            connections.append(connection)
            return connection

        modules = {
            "psycopg": SimpleNamespace(connect=connect),
            "psycopg.types.json": SimpleNamespace(Jsonb=_Jsonb),
        }
        with patch("misakabot.repository.import_module", side_effect=lambda name: modules[name]):
            repository = PostgresAuditRepository("postgresql://user:pass@postgres:5432/misakabot")
            repository.initialize()
            incoming = MessageInput(
                chat_id=-100,
                message_id=1,
                user_id=2,
                text="测试消息",
                sent_at=datetime.now(timezone.utc),
            )
            normalized = normalize_message(incoming.text)
            event_id = repository.record(
                incoming,
                normalized,
                detect_suspicion(normalized),
                None,
                Action.ALLOW,
                ReviewStatus.NOT_REQUIRED,
            )

        self.assertEqual(event_id, 42)
        self.assertTrue(
            any(
                "pg_advisory_xact_lock(619420431)" in sql
                for connection in connections
                for sql, _ in connection.executed
            )
        )
        record_sql, record_parameters = next(
            item for connection in connections for item in connection.executed
            if "INSERT INTO moderation_events" in item[0]
        )
        self.assertIn("%s", record_sql)
        self.assertNotIn("?", record_sql)
        self.assertIsInstance(record_parameters[6], _Jsonb)
        self.assertIsInstance(record_parameters[7], _Jsonb)
