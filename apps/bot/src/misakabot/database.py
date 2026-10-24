"""Typed DB-API boundary shared by SQLite backups and the PostgreSQL runtime.

Column values deliberately remain ``object``: PostgreSQL timestamps/JSON/booleans
must retain their native values. Repository methods validate scalar fields where
they consume them, rather than allowing driver ``Any`` types to escape.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import TYPE_CHECKING, LiteralString, Protocol, TypeGuard

if TYPE_CHECKING:
    from psycopg import Connection, Cursor
    from psycopg.rows import RowMaker


DatabaseRow = tuple[object, ...]
Parameters = tuple[object, ...] | list[object]


class DatabaseCursor(Protocol):
    @property
    def columns(self) -> tuple[str, ...]: ...

    @property
    def rowcount(self) -> int: ...

    def fetchone(self) -> DatabaseRow | None: ...
    def fetchall(self) -> list[DatabaseRow]: ...
    def __iter__(self) -> Iterator[DatabaseRow]: ...


class DatabaseConnection(Protocol):
    def execute(self, sql: LiteralString, parameters: Parameters = ()) -> DatabaseCursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


class _SqliteRows(Protocol):
    """sqlite3's untyped fetch result, before tuple-shape validation."""

    def fetchone(self) -> object: ...


def _is_row(value: object) -> TypeGuard[DatabaseRow]:
    return isinstance(value, tuple)


class _SqliteCursor:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor: sqlite3.Cursor = cursor
        self._rows: _SqliteRows = cursor

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(column[0] for column in self._cursor.description or ())

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def fetchone(self) -> DatabaseRow | None:
        row = self._rows.fetchone()
        if row is None:
            return None
        if not _is_row(row):
            raise TypeError("SQLite returned a non-tuple row")
        return row

    def fetchall(self) -> list[DatabaseRow]:
        return list(self)

    def __iter__(self) -> Iterator[DatabaseRow]:
        while (row := self.fetchone()) is not None:
            yield row


class SqliteConnection:
    """Accept PostgreSQL placeholders while retaining SQLite transaction semantics."""

    def __init__(self, database_path: str) -> None:
        self._connection: sqlite3.Connection = sqlite3.connect(database_path)

    def execute(self, sql: LiteralString, parameters: Parameters = ()) -> DatabaseCursor:
        return _SqliteCursor(self._connection.execute(sql.replace("%s", "?"), parameters))

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


def _postgres_rows(_cursor: Cursor[DatabaseRow]) -> RowMaker[DatabaseRow]:
    # Keep psycopg's optimized tuple factory without leaking its TupleRow[Any].
    return tuple


class _PostgresCursor:
    def __init__(self, cursor: Cursor[DatabaseRow]) -> None:
        self._cursor: Cursor[DatabaseRow] = cursor

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(column.name for column in self._cursor.description or ())

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def fetchone(self) -> DatabaseRow | None:
        return self._cursor.fetchone()

    def fetchall(self) -> list[DatabaseRow]:
        return self._cursor.fetchall()

    def __iter__(self) -> Iterator[DatabaseRow]:
        return iter(self._cursor)


class PostgresConnection:
    def __init__(self, database_url: str) -> None:
        try:
            import psycopg
        except ImportError as error:
            raise RuntimeError("PostgreSQL requires psycopg; install the project dependencies") from error
        self._connection: Connection[DatabaseRow] = psycopg.connect(database_url, row_factory=_postgres_rows)

    def execute(self, sql: LiteralString, parameters: Parameters = ()) -> DatabaseCursor:
        if sql.strip() == "BEGIN IMMEDIATE":
            # Preserve the one-winner guarantee for challenge consumption/expiry.
            _ = self._connection.execute("BEGIN")
            return _PostgresCursor(
                self._connection.execute("LOCK TABLE member_onboarding IN SHARE ROW EXCLUSIVE MODE")
            )
        return _PostgresCursor(self._connection.execute(sql.replace("datetime('now')", "NOW()"), parameters))

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()
