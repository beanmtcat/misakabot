from __future__ import annotations

from collections.abc import Mapping

import httpx


class MoviePilotClient:
    """Read MoviePilot subscriptions through its REST API."""

    def __init__(self, base_url: str, api_token: str, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-API-KEY": api_token, "Accept": "application/json"}
        self._timeout = timeout_seconds

    async def list_subscriptions(self) -> list[Mapping[str, object]]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}/api/v1/subscribe/", headers=self._headers
            )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, Mapping):
            payload = payload.get("data", payload.get("items", payload))
        if not isinstance(payload, list):
            raise RuntimeError("MoviePilot subscriptions returned an unexpected response")
        return [item for item in payload if isinstance(item, Mapping)]
