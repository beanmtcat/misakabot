from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from misakabot.workers import _run_background_operation


class BackgroundOperationTests(unittest.IsolatedAsyncioTestCase):
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
