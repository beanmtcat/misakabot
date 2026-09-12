from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage

from misakabot.gateway import AiogramGateway
from misakabot.repository import AuditRepository


class NoticeDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.repository = AuditRepository(Path(self.directory.name) / "audit.sqlite3")
        self.repository.initialize()
        self.bot = AsyncMock(spec=Bot)
        self.gateway = AiogramGateway(self.bot, self.repository)
        self.now = datetime.now(timezone.utc)
        self.repository.schedule_message_deletion(
            -100, 42, (self.now - timedelta(seconds=1)).isoformat()
        )

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def test_timer_and_stale_scanner_only_delete_once(self) -> None:
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def delete(*args: object, **kwargs: object) -> bool:
            entered.set()
            await finish.wait()
            return True

        self.bot.delete_message.side_effect = delete
        timer = asyncio.create_task(self.gateway._delete_message_after(-100, 42, 0))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            # The scanner may have fetched this row before the timer claimed it.
            with patch.object(self.repository, "due_message_deletions", return_value=[(-100, 42)]):
                await self.gateway.process_scheduled_message_deletions()
            finish.set()
            await timer
            # Even a stale scan after the first deletion must not send a second request.
            self.assertFalse(await self.gateway._delete_scheduled_message(-100, 42))
            self.bot.delete_message.assert_awaited_once()
        finally:
            finish.set()
            await timer

    async def test_two_gateways_share_the_same_claim(self) -> None:
        other = AiogramGateway(self.bot, AuditRepository(self.repository.database_path))
        results = await asyncio.gather(
            self.gateway._delete_scheduled_message(-100, 42),
            other._delete_scheduled_message(-100, 42),
        )
        self.assertEqual(sum(results), 1)
        self.bot.delete_message.assert_awaited_once()

    async def test_missing_message_completes_without_traceback(self) -> None:
        self.bot.delete_message.side_effect = TelegramBadRequest(
            method=DeleteMessage(chat_id=-100, message_id=42),
            message="Bad Request: message to delete not found",
        )
        with self.assertLogs("misakabot.gateway", level="INFO") as logs:
            self.assertTrue(await self.gateway._delete_scheduled_message(-100, 42))
        self.assertTrue(any("already_absent" in record.message for record in logs.records))
        self.assertTrue(all(record.exc_info is None for record in logs.records))
        self.assertEqual(
            self.repository.due_message_deletions((self.now + timedelta(days=1)).isoformat()), []
        )

    async def test_network_failure_retains_job_for_retry_after_lease(self) -> None:
        self.bot.delete_message.side_effect = TimeoutError("temporary outage")
        self.assertFalse(await self.gateway._delete_scheduled_message(-100, 42))
        self.assertEqual(self.repository.due_message_deletions(self.now.isoformat()), [])
        later = self.now + timedelta(minutes=3)
        self.assertEqual(self.repository.due_message_deletions(later.isoformat()), [(-100, 42)])
        # A new worker can reclaim an abandoned lease after a restart.
        self.assertTrue(self.repository.claim_message_deletion(
            -100, 42, later.isoformat(), (later + timedelta(minutes=2)).isoformat()
        ))

    async def test_lease_expiry_and_rescheduling_preserve_new_owner(self) -> None:
        until = (self.now + timedelta(minutes=2)).isoformat()
        self.assertTrue(self.repository.claim_message_deletion(-100, 42, self.now.isoformat(), until))
        new_due = (self.now + timedelta(minutes=5)).isoformat()
        self.repository.schedule_message_deletion(-100, 42, new_due)
        self.repository.complete_message_deletion(-100, 42, until)
        self.assertEqual(self.repository.due_message_deletions(new_due), [(-100, 42)])
