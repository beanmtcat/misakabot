import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from misakabot.domain import DirectJoinInput, JoinRequestInput, OnboardingState, MAX_GROUP_CHALLENGE_ATTEMPTS
from misakabot.onboarding import OnboardingService
from misakabot.repository import AuditRepository
from test_onboarding import FakeGateway


class VerificationDepartureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repository = AuditRepository(Path(directory.name) / "departure.sqlite3")
        self.repository.initialize()
        self.gateway = FakeGateway()
        self.service = OnboardingService(self.repository, self.gateway, join_verify_url="https://example.test/verify")
        self.base = datetime(2026, 10, 24, 1, tzinfo=timezone.utc)
        patcher = patch("misakabot.onboarding.datetime", wraps=datetime)
        self.clock = patcher.start()
        self.addCleanup(patcher.stop)
        self.clock.now.return_value = self.base
        evaluate = self.service.join_guard.evaluate
        guard_patcher = patch.object(self.service.join_guard, "evaluate", side_effect=lambda request: evaluate(request, now=self.clock.now.return_value))
        guard_patcher.start()
        self.addCleanup(guard_patcher.stop)

    def snapshot(self):
        return self.repository.group_challenge_snapshot(-1, 42)

    def callback(self, correct=False):
        row = self.repository.query_one("SELECT verification_answer_index FROM member_onboarding WHERE chat_id=-1 AND user_id=42")
        index = row["verification_answer_index"]
        choices = self.gateway.group_challenges[-1][3]
        return choices[index if correct else (index + 1) % len(choices)][1]

    def request(self):
        return JoinRequestInput(chat_id=-1, user_id=42, user_chat_id=42, requested_at=self.clock.now.return_value)

    async def joined(self, join_request=False):
        if join_request:
            pending = await self.service.start(self.request())
            token = parse_qs(urlparse(pending.callback_data).query)["session"][0]
            self.assertTrue((await self.service.verify_token(token, 42)).accepted)
        else:
            await self.service.start_direct_join(DirectJoinInput(chat_id=-1, user_id=42, joined_at=self.base))
        for _ in range(MAX_GROUP_CHALLENGE_ATTEMPTS - 1):
            await self.service.verify_group_challenge(self.callback(), 42)
        self.assertEqual(self.snapshot().attempts, 2)

    async def leave(self, kicked=False):
        return await self.service.mark_member_departed(-1, 42, self.clock.now.return_value, kicked=kicked)

    async def test_wrong_twice_leave_and_rejoin_cannot_get_new_attempts(self) -> None:
        await self.joined()
        self.assertTrue(await self.leave())
        self.assertEqual(self.gateway.banned, [(-1, 42)])
        self.assertEqual(self.gateway.ban_durations, [1])
        self.assertEqual(self.snapshot().attempts, 2)
        self.assertEqual(self.snapshot().state, OnboardingState.VERIFICATION_DEPARTURE_BANNED)
        count = len(self.gateway.group_challenges)
        pending = await self.service.start_direct_join(DirectJoinInput(chat_id=-1, user_id=42, joined_at=self.base))
        self.assertIsNone(pending.callback_data)
        self.assertEqual(len(self.gateway.group_challenges), count)
        self.assertEqual(self.snapshot().attempts, 2)
        self.assertIn("未通过", self.gateway.replaced_group_verifications[-1][2])
        self.assertEqual(self.gateway.replaced_group_delete_after[-1], 60)

    async def test_join_request_cannot_reset_pending_challenge_even_without_departure_update(self) -> None:
        await self.joined(join_request=True)
        self.gateway.group_members.clear()
        old = self.snapshot()
        self.assertIsNone((await self.service.start(self.request())).callback_data)
        self.assertEqual(self.snapshot(), old)

    async def test_join_request_reentry_requires_cooldown_then_all_stages(self) -> None:
        await self.joined(join_request=True)
        await self.leave()
        self.gateway.group_members.clear()
        self.assertIsNone((await self.service.start(self.request())).callback_data)
        self.assertEqual(self.gateway.declined, [(-1, 42)])
        self.assertEqual(self.snapshot().attempts, 2)
        self.clock.now.return_value = self.base + timedelta(hours=1, seconds=1)
        pending = await self.service.start(self.request())
        self.assertEqual(pending.state, OnboardingState.VERIFICATION_PENDING)
        self.assertEqual(len(self.gateway.verifications), 2)
        token = parse_qs(urlparse(pending.callback_data).query)["session"][0]
        self.assertTrue((await self.service.verify_token(token, 42)).accepted)
        self.assertEqual(self.snapshot().state, OnboardingState.SECONDARY_VERIFICATION_PENDING)
        self.assertEqual(self.snapshot().attempts, 0)

    async def test_completed_and_observing_departures_keep_existing_behavior(self) -> None:
        for state in (OnboardingState.PENDING_FIRST_MESSAGE, OnboardingState.OBSERVING):
            with self.subTest(state=state):
                self.repository.execute_write("DELETE FROM member_onboarding")
                pending = await self.service.start_direct_join(DirectJoinInput(chat_id=-1, user_id=42, joined_at=self.base))
                await self.service.resolve_group_challenge_by_admin(pending.callback_data, True, chat_id=-1)
                if state is OnboardingState.OBSERVING:
                    self.service.mark_first_message(-1, 42, self.base)
                self.assertTrue(await self.leave())
                self.assertEqual(self.snapshot().state, OnboardingState.DECLINED)
                self.assertEqual(self.gateway.banned, [])

    async def test_timeout_wins_then_late_departure_does_not_repeat_ban(self) -> None:
        await self.joined()
        self.clock.now.return_value = self.base + timedelta(minutes=3)
        self.assertEqual(await self.service.expire_pending_verifications(), 1)
        self.assertFalse(await self.leave())
        self.assertEqual(self.gateway.banned, [(-1, 42)])

    async def test_failed_ban_is_durable_retry_and_cannot_reset_attempts(self) -> None:
        await self.joined()
        with patch.object(self.gateway, "ban_member", new=AsyncMock(side_effect=RuntimeError("network down"))):
            with self.assertLogs("misakabot.onboarding", level="ERROR"):
                await self.leave()
        self.assertEqual(self.snapshot().state, OnboardingState.VERIFICATION_DEPARTURE_PENDING)
        self.assertEqual(self.snapshot().attempts, 2)
        self.assertIsNone((await self.service.start(self.request())).callback_data)
        self.assertIsNone((await self.service.start_direct_join(DirectJoinInput(chat_id=-1, user_id=42))).callback_data)
        self.assertEqual(await self.service.expire_pending_verifications(), 0)
        self.clock.now.return_value = self.base + timedelta(minutes=2, seconds=1)
        self.assertEqual(await self.service.expire_pending_verifications(), 1)
        self.assertEqual(self.snapshot().state, OnboardingState.VERIFICATION_DEPARTURE_BANNED)
        self.assertEqual(self.snapshot().attempts, 2)
        self.assertEqual(self.gateway.banned, [(-1, 42)])
        self.assertEqual(await self.service.expire_pending_verifications(), 0)

    async def test_departure_claim_wins_over_answer_admin_timeout_and_duplicate_leave(self) -> None:
        await self.joined()
        correct = self.callback(correct=True)
        token = correct.split(":")[1]
        entered, finish = asyncio.Event(), asyncio.Event()
        original_ban = self.gateway.ban_member

        async def blocked_ban(*args, **kwargs):
            entered.set()
            await finish.wait()
            await original_ban(*args, **kwargs)

        with patch.object(self.gateway, "ban_member", side_effect=blocked_ban):
            departure = asyncio.create_task(self.leave())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertFalse((await self.service.verify_group_challenge(correct, 42)).accepted)
                self.assertFalse((await self.service.resolve_group_challenge_by_admin(token, True, chat_id=-1)).accepted)
                self.assertFalse(await self.leave())
                self.assertEqual(await self.service.expire_pending_verifications(self.base + timedelta(minutes=1)), 0)
                self.assertEqual(self.snapshot().attempts, 2)
            finally:
                finish.set()
                await departure
        self.assertEqual(self.gateway.banned, [(-1, 42)])

    async def test_admin_claim_before_late_departure_is_not_banned(self) -> None:
        await self.joined()
        token = self.callback(correct=True).split(":")[1]
        entered, finish = asyncio.Event(), asyncio.Event()

        async def blocked_release(*args):
            entered.set()
            await finish.wait()

        with patch.object(self.gateway, "release_member", side_effect=blocked_release):
            approval = asyncio.create_task(self.service.resolve_group_challenge_by_admin(token, True, chat_id=-1))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertFalse(await self.leave())
                self.assertEqual(self.gateway.banned, [])
            finally:
                finish.set()
                result = await approval
        self.assertTrue(result.accepted)
        self.assertEqual(self.snapshot().state, OnboardingState.PENDING_FIRST_MESSAGE)

    async def test_kicked_pending_member_only_retires_card_without_extra_ban(self) -> None:
        await self.joined()
        self.assertTrue(await self.leave(kicked=True))
        self.assertEqual(self.gateway.banned, [])
        self.assertEqual(self.snapshot().attempts, 2)
        self.assertIsNone(self.snapshot().token_hash)
        self.assertEqual(self.snapshot().state, OnboardingState.DECLINED)
        self.assertIn("验证已终止", self.gateway.replaced_group_verifications[-1][2])

    async def test_zero_duration_is_permanent_and_explicit_unban_is_scoped(self) -> None:
        self.service.secondary_verification_ban_hours = 0
        await self.joined()
        await self.leave()
        self.assertEqual(self.gateway.ban_durations, [0])
        self.clock.now.return_value = self.base + timedelta(days=365)
        self.assertIsNone((await self.service.start(self.request())).callback_data)
        self.assertFalse(self.repository.unblock_user(42, -2))
        self.assertTrue(self.repository.unblock_user(42, -1))
        self.assertIsNotNone((await self.service.start(self.request())).callback_data)

    async def test_configured_ban_hours_are_used_by_telegram_and_local_cooldown(self) -> None:
        self.service.secondary_verification_ban_hours = 2
        await self.joined()
        await self.leave()
        self.assertEqual(self.gateway.ban_durations, [2])
        self.assertTrue(self.repository.departure_blocks_join(-1, 42, (self.base + timedelta(hours=1)).isoformat()))
        self.assertFalse(self.repository.departure_blocks_join(-1, 42, (self.base + timedelta(hours=2)).isoformat()))

    async def test_paused_and_inflight_recovery_cards_are_retired(self) -> None:
        await self.service.start_direct_join(DirectJoinInput(chat_id=-1, user_id=42, joined_at=self.base))
        with patch.object(self.gateway, "update_group_vps_challenge", new=AsyncMock(side_effect=RuntimeError("edit failed"))):
            await self.service.verify_group_challenge(self.callback(), 42)
        self.assertIsNotNone(self.snapshot().remaining_seconds)
        self.clock.now.return_value = self.base + timedelta(minutes=5)
        send = self.gateway.send_group_vps_challenge

        async def leave_while_sending(*args, **kwargs):
            message_id = await send(*args, **kwargs)
            self.repository.schedule_message_deletion(-1, message_id, (self.base + timedelta(hours=1)).isoformat(), verification_user_id=42)
            await self.leave()
            return message_id

        with patch.object(self.gateway, "send_group_vps_challenge", side_effect=leave_while_sending):
            self.assertEqual(await self.service.resume_undelivered_group_challenges(frozenset({-1})), 0)
        due = set(self.repository.due_message_deletions((self.clock.now.return_value + timedelta(seconds=60)).isoformat()))
        self.assertIn((-1, 1001), due)
        self.assertIn((-1, 1002), due)
        self.assertEqual(self.gateway.banned, [(-1, 42)])
        self.assertEqual(self.snapshot().attempts, 1)
        self.assertEqual(await self.service.resume_undelivered_group_challenges(frozenset({-1})), 0)

    async def test_rejected_creation_cannot_overwrite_departure_claim(self) -> None:
        await self.joined()
        self.assertTrue(self.repository.claim_verification_departure(-1, 42, self.base.isoformat()))
        self.assertFalse(self.repository.create_group_challenge(
            chat_id=-1, user_id=42, username=None, token_hash="reset", answer_index=0,
            expires_at=(self.base + timedelta(minutes=2)).isoformat(), now=self.base.isoformat(), verification_flow="direct_join",
        ))
        self.assertFalse(self.repository.create_pending_verification(self.request(), "reset", self.base.isoformat(), restart_existing=True))
        self.assertEqual(self.snapshot().attempts, 2)

    async def test_departure_while_paused_without_recovery_does_not_restart_timer(self) -> None:
        await self.service.start_direct_join(DirectJoinInput(chat_id=-1, user_id=42, joined_at=self.base))
        with patch.object(self.gateway, "update_group_vps_challenge", new=AsyncMock(side_effect=RuntimeError("Bot edit failed"))):
            await self.service.verify_group_challenge(self.callback(), 42)
        self.assertEqual(self.snapshot().attempts, 1)
        self.assertIsNotNone(self.snapshot().remaining_seconds)
        await self.leave()
        self.assertEqual(self.snapshot().attempts, 1)
        self.assertEqual(self.gateway.banned, [(-1, 42)])
        self.assertIn((-1, 1001), self.repository.due_message_deletions((self.base + timedelta(seconds=60)).isoformat()))
        self.clock.now.return_value = self.base + timedelta(minutes=5)
        self.assertEqual(await self.service.resume_undelivered_group_challenges(frozenset({-1})), 0)

    async def test_correct_answer_claim_before_departure_is_not_banned(self) -> None:
        await self.joined()
        entered, finish = asyncio.Event(), asyncio.Event()

        async def blocked_release(*args):
            entered.set()
            await finish.wait()

        with patch.object(self.gateway, "release_member", side_effect=blocked_release):
            answer = asyncio.create_task(self.service.verify_group_challenge(self.callback(correct=True), 42))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertFalse(await self.leave())
                self.assertEqual(self.gateway.banned, [])
            finally:
                finish.set()
                result = await answer
        self.assertTrue(result.accepted)

    async def test_common_attempt_limit_matches_normal_admin_and_recovery_results(self) -> None:
        import inspect

        self.assertEqual(inspect.signature(self.repository.resolve_group_challenge).parameters["max_attempts"].default,
                         MAX_GROUP_CHALLENGE_ATTEMPTS)
        await self.service.start_direct_join(DirectJoinInput(chat_id=-1, user_id=42, joined_at=self.base))
        wrong = await self.service.verify_group_challenge(self.callback(), 42)
        self.assertIn(f"{MAX_GROUP_CHALLENGE_ATTEMPTS - 1} 次", wrong.reason)
        token = self.callback(correct=True).split(":")[1]
        result = self.repository.resolve_group_challenge_by_admin(self.service._token_hash(token), self.base.isoformat(), True, chat_id=-1)
        self.assertEqual(result.attempts_left, MAX_GROUP_CHALLENGE_ATTEMPTS - 1)
        self.repository.finalize_group_challenge(-1, 42, self.base.isoformat())
        await self.service.mark_member_departed(-1, 42, self.base)
        await self.service.start_direct_join(DirectJoinInput(chat_id=-1, user_id=42, joined_at=self.base))
        with patch.object(self.gateway, "update_group_vps_challenge", new=AsyncMock(side_effect=RuntimeError("edit failed"))):
            await self.service.verify_group_challenge(self.callback(), 42)
        self.clock.now.return_value = self.base + timedelta(minutes=5)
        self.assertEqual(await self.service.resume_undelivered_group_challenges(frozenset({-1})), 1)
        self.assertEqual(self.gateway.rotated_challenges[-1][2], MAX_GROUP_CHALLENGE_ATTEMPTS - 1)

