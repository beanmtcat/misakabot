from __future__ import annotations

from collections.abc import Mapping

import httpx


class TmdbClient:
    """Small, read-only client for TMDB's TV-series details endpoint."""

    def __init__(self, api_token: str, timeout_seconds: float) -> None:
        self._headers = {"Authorization": f"Bearer {api_token}", "Accept": "application/json"}
        self._timeout = timeout_seconds

    async def get_tv_details(self, tmdb_id: int) -> Mapping[str, object]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"https://api.themoviedb.org/3/tv/{tmdb_id}",
                headers=self._headers,
                params={"language": "zh-CN"},
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise RuntimeError("TMDB TV details returned an unexpected response")
        return payload

    async def get_tv_season_details(self, tmdb_id: int, season_number: int) -> Mapping[str, object]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}",
                headers=self._headers,
                params={"language": "zh-CN"},
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise RuntimeError("TMDB TV season details returned an unexpected response")
        return payload
