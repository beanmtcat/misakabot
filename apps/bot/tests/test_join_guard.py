from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from misakabot.domain import JoinRequestInput
from misakabot.join_guard import JoinGuard, JoinGuardMode, JoinGuardPolicy
from misakabot.repository import AuditRepository


class JoinGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.repository = AuditRepository(Path(self.directory.name) / "join-guard.sqlite3")
        self.repository.initialize()
        self.policy = JoinGuardPolicy(
            burst_per_minute=4,
            burst_per_five_minutes=6,
            suspicious_name_minimum=3,
            suspicious_name_ratio=0.70,
            lockdown_per_five_minutes=8,
            quiet_minutes=120,
            repeat_cooldown_hours=24,
            stale_request_minutes=5,
            notice_interval_minutes=30,
        )
        self.guard = JoinGuard(self.repository, self.policy)
        self.now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def request(self, user_id: int, name: str, at: datetime | None = None) -> JoinRequestInput:
        timestamp = at or self.now
        return JoinRequestInput(
            chat_id=-100123,
            user_id=user_id,
            user_chat_id=user_id * 10,
            group_title="测试群",
            display_name=name,
            requested_at=timestamp,
        )

    def test_ascii_name_cohort_activates_high_defense(self) -> None:
        first = self.guard.evaluate(self.request(1, "durham"), self.now)
        second = self.guard.evaluate(self.request(2, "metcalf"), self.now)
        third = self.guard.evaluate(self.request(3, "simpson"), self.now)

        self.assertFalse(first.reject)
        self.assertFalse(second.reject)
        self.assertEqual(third.mode, JoinGuardMode.HIGH)
        self.assertTrue(third.reject)
        self.assertEqual(third.reason, "high_risk_name_cohort")
        self.assertTrue(third.notify_admins)

    def test_slow_continuation_extends_high_defense_until_attack_is_quiet(self) -> None:
        for user_id, name in enumerate(("durham", "metcalf", "simpson"), start=1):
            self.guard.evaluate(self.request(user_id, name), self.now)

        continued_at = self.now + timedelta(minutes=90)
        decision = self.guard.evaluate(self.request(4, "krishna", continued_at), continued_at)
        mode, defense_until = self.guard.status(-100123, continued_at)

        self.assertEqual(decision.mode, JoinGuardMode.HIGH)
        self.assertTrue(decision.reject)
        self.assertEqual(mode, JoinGuardMode.HIGH)
        self.assertEqual(defense_until, continued_at + timedelta(minutes=120))

    def test_high_defense_does_not_reject_unrelated_name(self) -> None:
        for user_id, name in enumerate(("durham", "metcalf", "simpson"), start=1):
            self.guard.evaluate(self.request(user_id, name), self.now)

        decision = self.guard.evaluate(self.request(4, "小明 123", self.now), self.now)

        self.assertEqual(decision.mode, JoinGuardMode.HIGH)
        self.assertFalse(decision.reject)
        self.assertEqual(decision.reason, "verification")

    def test_extreme_flood_enters_lockdown(self) -> None:
        decision = None
        for user_id in range(1, 9):
            decision = self.guard.evaluate(self.request(user_id, f"User{user_id}"), self.now)

        assert decision is not None
        self.assertEqual(decision.mode, JoinGuardMode.LOCKDOWN)
        self.assertTrue(decision.reject)
        self.assertEqual(decision.reason, "lockdown")

    def test_stale_replayed_update_is_rejected_silently_from_current_window(self) -> None:
        stale_at = self.now - timedelta(hours=1)
        decision = self.guard.evaluate(self.request(1, "durham", stale_at), self.now)

        self.assertTrue(decision.reject)
        self.assertEqual(decision.reason, "stale_request")
        self.assertEqual(decision.one_minute_count, 0)
        self.assertEqual(decision.mode, JoinGuardMode.NORMAL)

    def test_rejected_user_has_24_hour_cooldown(self) -> None:
        for user_id, name in enumerate(("durham", "metcalf", "simpson"), start=1):
            self.guard.evaluate(self.request(user_id, name), self.now)
        later = self.now + timedelta(hours=3)

        decision = self.guard.evaluate(self.request(3, "张三", later), later)

        self.assertTrue(decision.reject)
        self.assertEqual(decision.reason, "repeat_cooldown")

    def test_manual_lockdown_persists(self) -> None:
        self.guard.set_mode(-100123, JoinGuardMode.PERMANENT_LOCKDOWN, self.now)

        decision = self.guard.evaluate(
            self.request(1, "正常用户", self.now + timedelta(days=1)),
            self.now + timedelta(days=1),
        )

        self.assertEqual(decision.mode, JoinGuardMode.PERMANENT_LOCKDOWN)
        self.assertTrue(decision.reject)


if __name__ == "__main__":
    unittest.main()
