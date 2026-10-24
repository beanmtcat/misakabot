from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

from emby_manager.api import APP_PREFIX, create_app
from emby_manager.config import Settings


class FullTrackingSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        web_root = Path(self.directory.name)
        (web_root / "assets").mkdir()
        self.manager = Mock()
        self.manager.authenticate.return_value = True
        self.manager.is_login_enabled.return_value = True
        self.manager.sync_series = AsyncMock(return_value={"synced": 2})
        self.manager.sync_moviepilot_subscriptions = AsyncMock(return_value={"synced": 3})
        self.manager.sync_series_tracking = AsyncMock(return_value={"updated": 4})
        self.settings = Settings(
            database_url="postgresql://unused/test", emby_base_url="https://emby.example",
            emby_api_key="fake", session_secret="test-secret", web_root=web_root,
            manager_admin_usernames=frozenset({"admin"}), manager_origin="http://testserver",
            moviepilot_base_url="https://moviepilot.example", moviepilot_api_token="fake",
            tmdb_api_token="fake",
        )
        # Do not start lifespan/background jobs or touch a real Emby/PG server.
        self.client = TestClient(create_app(self.settings, service=self.manager))
        self.addCleanup(self.client.close)
        response = self.client.post(f"{APP_PREFIX}/auth/login", json={
            "username": "admin", "password": "test-password",
        }, headers={"Origin": "http://testserver"})
        self.assertEqual(response.status_code, 200)
        self.headers = {"Origin": "http://testserver", "X-CSRF-Token": response.json()["csrf_token"]}

    def sync(self):
        return self.client.post(f"{APP_PREFIX}/v1/emby/series/tracking/sync", headers=self.headers)

    def status(self):
        response = self.client.get(f"{APP_PREFIX}/v1/emby/sync-status")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_archive_guard_failure_does_not_skip_moviepilot(self) -> None:
        self.manager.sync_series.side_effect = RuntimeError("archive guard rejected missing series")
        with self.assertLogs("emby_manager.api", level="ERROR"):
            response = self.sync()
        self.assertEqual(response.status_code, 502)
        self.manager.sync_moviepilot_subscriptions.assert_awaited_once()
        self.manager.sync_series_tracking.assert_not_awaited()
        status = self.status()
        self.assertIn("archive guard", status["series"]["last_error"])
        self.assertIsNone(status["series"]["last_success_at"])
        self.assertIsNotNone(status["series"]["last_attempt_at"])
        self.assertEqual(status["moviepilot_subscriptions"]["last_result"], {"synced": 3})
        self.assertIsNotNone(status["moviepilot_subscriptions"]["last_success_at"])
        self.assertIn("跳过", status["series_tracking"]["last_error"])

    def test_success_after_failure_clears_errors_and_releases_sync_lock(self) -> None:
        self.assertEqual(self.sync().status_code, 200)
        previous_success = self.status()["series"]["last_success_at"]
        self.manager.sync_series.side_effect = RuntimeError("archive guard")
        with self.assertLogs("emby_manager.api", level="ERROR"):
            self.assertEqual(self.sync().status_code, 502)
        status = self.status()
        self.assertEqual(status["series"]["last_success_at"], previous_success)
        self.assertEqual(status["series"]["last_result"], {"synced": 2})
        self.manager.sync_series.side_effect = None
        response = self.sync()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "emby_series": {"synced": 2}, "moviepilot": {"synced": 3}, "tracking": {"updated": 4},
        })
        for stage in ("series", "moviepilot_subscriptions", "series_tracking"):
            self.assertIsNone(self.status()[stage]["last_error"])

    def test_both_failures_are_recorded_without_reporting_success(self) -> None:
        self.manager.sync_series.side_effect = RuntimeError("archive guard")
        self.manager.sync_moviepilot_subscriptions.side_effect = RuntimeError("MoviePilot unavailable")
        with self.assertLogs("emby_manager.api", level="ERROR"):
            self.assertEqual(self.sync().status_code, 502)
        status = self.status()
        self.assertEqual(status["series"]["last_error"], "archive guard")
        self.assertEqual(status["moviepilot_subscriptions"]["last_error"], "MoviePilot unavailable")
        self.assertIsNone(status["moviepilot_subscriptions"]["last_success_at"])

    def test_moviepilot_failure_does_not_skip_independent_tracking_after_series_success(self) -> None:
        self.manager.sync_moviepilot_subscriptions.side_effect = RuntimeError("MoviePilot unavailable")
        with self.assertLogs("emby_manager.api", level="ERROR"):
            self.assertEqual(self.sync().status_code, 502)
        self.manager.sync_series_tracking.assert_awaited_once()
        self.assertEqual(self.status()["series_tracking"]["last_result"], {"updated": 4})

    def test_cancellation_is_not_swallowed_or_continued(self) -> None:
        self.manager.sync_series.side_effect = asyncio.CancelledError()
        with self.assertRaises(CancelledError):
            self.sync()
        self.manager.sync_moviepilot_subscriptions.assert_not_awaited()
