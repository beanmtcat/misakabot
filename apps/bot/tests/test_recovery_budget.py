import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from misakabot.domain import DirectJoinInput, OnboardingState
from misakabot.onboarding import OnboardingService
from misakabot.repository import AuditRepository
from test_onboarding import FakeGateway


class RecoveryBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repository = AuditRepository(Path(self.directory.name) / "audit.sqlite3")
        self.repository.initialize()
        self.gateway = FakeGateway()
        self.service = OnboardingService(self.repository, self.gateway)
        self.base = datetime(2026, 10, 24, 1, 0, tzinfo=timezone.utc)

    def wrong_callback(self):
        row = self.repository.query_one(
            "SELECT verification_answer_index FROM member_onboarding WHERE chat_id=%s AND user_id=%s", (-1, 42)
        )
        choices = self.gateway.group_challenges[-1][3]
        return choices[(row["verification_answer_index"] + 1) % len(choices)][1]

    def fail_rotation(self, remaining=80):
        with patch("misakabot.onboarding.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.base
            asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-1, user_id=42)))
            failed_at = self.base + timedelta(seconds=120 - remaining)
            clock.now.return_value = failed_at
            wrong = self.wrong_callback()
            with patch.object(self.gateway, "update_group_vps_challenge", new=AsyncMock(side_effect=RuntimeError("Bot edit failed"))):
                outcome = asyncio.run(self.service.verify_group_challenge(wrong, 42))
        self.assertIn("正在重新发送", outcome.reason)
        snapshot = self.repository.group_challenge_snapshot(-1, 42)
        self.assertEqual(snapshot.remaining_seconds, remaining)
        self.assertEqual(snapshot.attempts, 1)
        self.assertEqual(datetime.fromisoformat(snapshot.expires_at), self.base + timedelta(seconds=120))
        return failed_at, wrong

    def resume(self, now, send_delay=0):
        send = self.gateway.send_group_vps_challenge
        with patch("misakabot.onboarding.datetime", wraps=datetime) as clock:
            clock.now.return_value = now

            async def slow_send(*args, **kwargs):
                message_id = await send(*args, **kwargs)
                clock.now.return_value = now + timedelta(seconds=send_delay)
                return message_id

            with patch.object(self.gateway, "send_group_vps_challenge", side_effect=slow_send):
                self.assertEqual(asyncio.run(self.service.resume_undelivered_group_challenges(frozenset({-1}))), 1)
        return self.repository.group_challenge_snapshot(-1, 42)

    def test_eighty_seconds_survive_a_five_minute_bot_outage(self) -> None:
        failed_at, _ = self.fail_rotation(80)
        delivered = failed_at + timedelta(minutes=5)
        snapshot = self.resume(delivered)
        self.assertEqual(datetime.fromisoformat(snapshot.expires_at), delivered + timedelta(seconds=80))
        self.assertEqual(snapshot.attempts, 1)
        self.assertIsNone(snapshot.remaining_seconds)

    def test_five_seconds_receive_the_sixty_second_minimum(self) -> None:
        failed_at, _ = self.fail_rotation(5)
        delivered = failed_at + timedelta(minutes=5)
        snapshot = self.resume(delivered)
        self.assertEqual(datetime.fromisoformat(snapshot.expires_at), delivered + timedelta(seconds=60))
        self.assertEqual(snapshot.attempts, 1)

    def test_slow_send_uses_one_visible_and_registered_deadline(self) -> None:
        failed_at, _ = self.fail_rotation(80)
        started = failed_at + timedelta(minutes=5)
        snapshot = self.resume(started, send_delay=25)
        self.assertTrue(self.gateway.sent_group_preparing[-1])
        self.assertIsNone(self.gateway.sent_group_deadlines[-1])
        self.assertEqual(self.gateway.displayed_group_deadlines[-1], snapshot.expires_at)
        self.assertEqual(datetime.fromisoformat(snapshot.expires_at), started + timedelta(seconds=105))

    def test_member_actions_do_not_increase_paused_budget_or_active_deadline(self) -> None:
        failed_at, old_callback = self.fail_rotation(80)
        with patch("misakabot.onboarding.datetime", wraps=datetime) as clock:
            clock.now.return_value = failed_at + timedelta(seconds=20)
            self.assertFalse(asyncio.run(self.service.verify_group_challenge(old_callback, 42)).accepted)
            asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-1, user_id=42)))
        self.assertEqual(self.repository.group_challenge_snapshot(-1, 42).remaining_seconds, 80)

        delivered = failed_at + timedelta(minutes=5)
        recovered = self.resume(delivered)
        with patch("misakabot.onboarding.datetime", wraps=datetime) as clock:
            clock.now.return_value = delivered + timedelta(seconds=10)
            wrong = self.wrong_callback()
            asyncio.run(self.service.verify_group_challenge(wrong, 42))
            self.assertFalse(asyncio.run(self.service.verify_group_challenge(wrong, 42)).accepted)
            asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-1, user_id=42)))
            self.assertEqual(asyncio.run(self.service.resume_undelivered_group_challenges(frozenset({-1}))), 0)
        after = self.repository.group_challenge_snapshot(-1, 42)
        self.assertEqual(after.expires_at, recovered.expires_at)
        self.assertEqual(after.attempts, 2)
        self.assertIsNone(after.remaining_seconds)

    def test_fractional_delivery_rounds_up_once_to_the_visible_second(self) -> None:
        failed_at, _ = self.fail_rotation(5)
        delivered = failed_at + timedelta(minutes=5, microseconds=750000)
        snapshot = self.resume(delivered)
        deadline = datetime.fromisoformat(snapshot.expires_at)
        self.assertEqual(deadline.microsecond, 0)
        self.assertGreaterEqual((deadline - delivered).total_seconds(), 60)
        self.assertLess((deadline - delivered).total_seconds(), 61)
        self.assertEqual(self.gateway.displayed_group_deadlines[-1], snapshot.expires_at)

    def test_repeated_bot_delivery_failures_preserve_original_frozen_seconds(self) -> None:
        failed_at, _ = self.fail_rotation(80)
        future = failed_at + timedelta(minutes=5)
        with patch("misakabot.onboarding.datetime", wraps=datetime) as clock, patch.object(
            self.gateway, "update_group_vps_challenge", new=AsyncMock(side_effect=RuntimeError("Bot final edit failed"))
        ):
            clock.now.return_value = future
            self.assertEqual(asyncio.run(self.service.resume_undelivered_group_challenges(frozenset({-1}))), 0)
        snapshot = self.repository.group_challenge_snapshot(-1, 42)
        self.assertEqual(snapshot.remaining_seconds, 80)
        self.assertEqual(snapshot.attempts, 1)
        self.assertIsNone(snapshot.message_id)
        recovered = self.resume(future + timedelta(minutes=3))
        self.assertEqual(datetime.fromisoformat(recovered.expires_at), future + timedelta(minutes=3, seconds=80))
        self.assertEqual(recovered.attempts, 1)

    def test_admin_decision_during_preparation_is_not_reopened(self) -> None:
        for approved in (True, False):
            with self.subTest(approved=approved):
                self.service.secondary_verification_ttl_minutes = 10
                with patch("misakabot.onboarding.datetime", wraps=datetime) as clock:
                    clock.now.return_value = self.base
                    asyncio.run(self.service._start_group_challenge(
                        chat_id=-1, user_id=42, username=None, verification_flow="direct_join", started_at=self.base,
                    ))
                    clock.now.return_value = self.base + timedelta(seconds=40)
                    with patch.object(self.gateway, "update_group_vps_challenge", new=AsyncMock(side_effect=RuntimeError("Bot edit failed"))):
                        asyncio.run(self.service.verify_group_challenge(self.wrong_callback(), 42))
                original_send = self.gateway.send_group_vps_challenge

                async def decided_during_send(*args, **kwargs):
                    message_id = await original_send(*args, **kwargs)
                    token = args[4][0][1].split(":")[1]
                    result = await self.service.resolve_group_challenge_by_admin(token, approved, chat_id=-1)
                    self.assertEqual(result.accepted, approved)
                    return message_id

                with patch("misakabot.onboarding.datetime", wraps=datetime) as clock, patch.object(
                    self.gateway, "send_group_vps_challenge", side_effect=decided_during_send
                ):
                    clock.now.return_value = self.base + timedelta(minutes=3)
                    with self.assertLogs("misakabot.onboarding", level="WARNING"):
                        self.assertEqual(asyncio.run(self.service.resume_undelivered_group_challenges(frozenset({-1}))), 0)
                    self.assertEqual(asyncio.run(self.service.resume_undelivered_group_challenges(frozenset({-1}))), 0)
                snapshot = self.repository.group_challenge_snapshot(-1, 42)
                self.assertEqual(snapshot.state, OnboardingState.PENDING_FIRST_MESSAGE if approved else OnboardingState.DECLINED)
                self.assertIsNone(snapshot.remaining_seconds)
                self.assertIn("欢迎加入群组" if approved else "二次验证未通过", self.gateway.replaced_group_verifications[-1][2])

    def test_crashed_rotation_recovers_budget_from_persisted_edit_timestamp(self) -> None:
        with patch("misakabot.onboarding.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.base
            asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-1, user_id=42)))
        wrong = self.wrong_callback()
        _, token, selected = wrong.split(":")
        failed_at = self.base + timedelta(seconds=40)
        self.repository.resolve_group_challenge(
            self.service._token_hash(token), 42, int(selected), failed_at.isoformat(),
            next_token_hash="crashed-token", next_answer_index=0,
        )
        self.assertIsNone(self.repository.group_challenge_snapshot(-1, 42).remaining_seconds)
        snapshot = self.resume(failed_at + timedelta(minutes=5))
        self.assertEqual(datetime.fromisoformat(snapshot.expires_at), failed_at + timedelta(minutes=5, seconds=80))
        self.assertEqual(snapshot.attempts, 1)

    def test_stale_delivery_ack_does_not_consume_paused_budget(self) -> None:
        failed_at, _ = self.fail_rotation(80)
        self.assertFalse(self.repository.set_group_challenge_message_id(
            -1, 42, 9999, failed_at.isoformat(), (failed_at + timedelta(days=1)).isoformat(), "stale-token",
        ))
        snapshot = self.repository.group_challenge_snapshot(-1, 42)
        self.assertEqual(snapshot.remaining_seconds, 80)
        self.assertEqual(snapshot.attempts, 1)
        self.assertEqual(datetime.fromisoformat(snapshot.expires_at), self.base + timedelta(seconds=120))
