from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock

import httpx

from emby_manager.emby_client import EmbyClient, EmbyItemNotFoundError
from emby_manager.service import EmbyManagementService


class EmbyClientSeriesTests(unittest.IsolatedAsyncioTestCase):
    async def test_removed_series_is_isolated_from_other_episode_results(self) -> None:
        client = EmbyClient("https://emby.example", "token", 5)

        async def list_items(
            item_type: str, fields: str, *, parent_id: int | None = None
        ) -> list[dict[str, object]]:
            del item_type, fields
            if parent_id == 35307:
                request = httpx.Request("GET", "https://emby.example/Items")
                response = httpx.Response(404, request=request)
                raise httpx.HTTPStatusError("missing", request=request, response=response)
            return [{"Id": "1", "SeriesId": str(parent_id), "Name": "第 1 集"}]

        client._list_items = list_items  # type: ignore[method-assign]
        client.get_series = AsyncMock(  # type: ignore[method-assign]
            side_effect=EmbyItemNotFoundError("missing")
        )

        episodes, missing = await client.list_episodes_for_series([35307, 42])

        self.assertEqual(missing, (35307,))
        self.assertEqual([episode["SeriesId"] for episode in episodes], ["42"])


    async def test_unconfirmed_404_is_not_treated_as_removed(self) -> None:
        client = EmbyClient("https://emby.example", "token", 5)
        request = httpx.Request("GET", "https://emby.example/Items")
        not_found = httpx.HTTPStatusError(
            "proxy 404", request=request, response=httpx.Response(404, request=request)
        )
        client._list_items = AsyncMock(side_effect=not_found)  # type: ignore[method-assign]
        client.get_series = AsyncMock(side_effect=not_found)  # type: ignore[method-assign]

        with self.assertRaises(httpx.HTTPStatusError):
            await client.list_episodes_for_series([35307])


class EmbySeriesSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_archives_missing_series_and_continues_batch(self) -> None:
        repository = Mock()
        repository.tracked_emby_series_ids.return_value = [35307, 42]
        repository.invalidate_missing_emby_series.return_value = {"series": 1, "episodes": 3}
        repository.upsert_emby_episodes.return_value = {
            "synced": 1,
            "skipped": 0,
            "invalidated": 0,
        }
        repository.episode_ids_missing_parent_metadata.return_value = []
        client = Mock()
        client.list_series = AsyncMock(return_value=[])
        client.list_episodes_for_series = AsyncMock(
            return_value=([{"Id": "1", "SeriesId": "42", "Name": "第 1 集"}], (35307,))
        )
        service = EmbyManagementService(repository, client)

        result = await service.sync_series()

        repository.invalidate_missing_emby_series.assert_called_once_with((35307,))
        repository.upsert_emby_episodes.assert_called_once_with(
            [{"Id": "1", "SeriesId": "42", "Name": "第 1 集"}],
            [42],
        )
        self.assertEqual(result["missing_series_invalidated"], 1)
        self.assertEqual(result["missing_series_episodes_invalidated"], 3)

    async def test_sync_refuses_to_archive_most_of_the_library(self) -> None:
        repository = Mock()
        repository.tracked_emby_series_ids.return_value = list(range(1, 9))
        client = Mock()
        client.list_series = AsyncMock(return_value=[])
        client.list_episodes_for_series = AsyncMock(return_value=([], tuple(range(1, 7))))
        service = EmbyManagementService(repository, client)

        with self.assertRaises(RuntimeError):
            await service.sync_series()

        repository.invalidate_missing_emby_series.assert_not_called()
        repository.upsert_emby_episodes.assert_not_called()
