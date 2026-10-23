from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from misakabot.workers import _run_background_operation, verification_expiry_worker


class BackgroundOperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_recovery_runs_before_expiry_and_housekeeping(self) -> None:
        from unittest.mock import Mock

        onboarding = Mock()
        gateway = Mock()
        calls: list[str] = []

        async def recover(groups: frozenset[int]) -> int:
            self.assertEqual(groups, frozenset({-100}))
            calls.append("recover")
            return 0

        async def expire() -> int:
            calls.append("expire")
            return 0

        async def housekeeping() -> None:
            calls.append("housekeeping")

        onboarding.resume_undelivered_group_challenges = recover
        onboarding.expire_pending_verifications = expire
        gateway.process_scheduled_message_deletions = housekeeping
        with patch("misakabot.workers.asyncio.sleep", new=AsyncMock(
            side_effect=asyncio.CancelledError
        )):
            with self.assertRaises(asyncio.CancelledError):
                await verification_expiry_worker(
                    onboarding, gateway, allowed_group_ids=frozenset({-100})
                )
        self.assertEqual(calls, ["recover", "expire", "housekeeping"])

    async def test_transient_failure_is_retried(self) -> None:
        operation = AsyncMock(side_effect=[RuntimeError("database restart"), None])

        with patch("misakabot.workers.asyncio.sleep", new=AsyncMock()) as sleep:
            completed = await _run_background_operation(
                "test", operation, retry_delays=(0.0,)
            )

        self.assertTrue(completed)
        self.assertEqual(operation.await_count, 2)
        sleep.assert_awaited_once_with(0.0)

    async def test_persistent_failure_is_deferred_without_raising(self) -> None:
        operation = AsyncMock(side_effect=RuntimeError("database unavailable"))

        with patch("misakabot.workers.asyncio.sleep", new=AsyncMock()):
            completed = await _run_background_operation(
                "test", operation, retry_delays=(0.0, 0.0)
            )

        self.assertFalse(completed)
        self.assertEqual(operation.await_count, 3)
