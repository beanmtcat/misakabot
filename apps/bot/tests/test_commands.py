from __future__ import annotations

import unittest
from datetime import datetime, timezone

from misakabot.handlers.commands import format_join_guard_expiry


class CommandFormattingTests(unittest.TestCase):
    def test_join_guard_expiry_uses_singapore_time_in_utc_container(self) -> None:
        expiry = datetime(2026, 10, 8, 17, 6, 47, tzinfo=timezone.utc)

        self.assertEqual(
            format_join_guard_expiry(expiry),
            "\n预计结束（新加坡时间）：2026-10-09 01:06:47",
        )

    def test_join_guard_without_expiry_has_no_suffix(self) -> None:
        self.assertEqual(format_join_guard_expiry(None), "")


if __name__ == "__main__":
    unittest.main()
