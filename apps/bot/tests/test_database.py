import unittest

from misakabot.database import SqliteConnection


class SqliteAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = SqliteConnection(":memory:")
        self.connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, content TEXT)")
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()

    def test_columns_and_values_preserve_dbapi_types(self) -> None:
        cursor = self.connection.execute(
            "SELECT %s AS identifier, %s AS content, NULL AS absent, %s AS blob",
            (42, "猫猫", b"bytes"),
        )
        self.assertEqual(cursor.columns, ("identifier", "content", "absent", "blob"))
        self.assertEqual(cursor.fetchone(), (42, "猫猫", None, b"bytes"))
        self.assertIsNone(cursor.fetchone())

    def test_cursor_fetchall_and_iteration_share_position(self) -> None:
        cursor = self.connection.execute("SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3")
        self.assertEqual(cursor.fetchone(), (1,))
        self.assertEqual(list(cursor), [(2,), (3,)])
        self.assertEqual(cursor.fetchall(), [])
        self.assertIsNone(self.connection.execute("SELECT 1 WHERE 0").fetchone())

    def test_write_rowcount_and_rollback(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        inserted = self.connection.execute("INSERT INTO sample VALUES (%s, %s)", (1, "value"))
        self.assertEqual(inserted.columns, ())
        self.assertEqual(inserted.rowcount, 1)
        self.connection.rollback()
        self.assertEqual(self.connection.execute("SELECT * FROM sample").fetchall(), [])

        self.connection.execute("INSERT INTO sample VALUES (%s, %s)", (1, "value"))
        self.connection.commit()
        self.assertEqual(self.connection.execute("DELETE FROM sample WHERE id=%s", (2,)).rowcount, 0)
        self.assertEqual(self.connection.execute("DELETE FROM sample WHERE id=%s", (1,)).rowcount, 1)
