from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx


class EmbyItemNotFoundError(RuntimeError):
    """The requested item no longer exists in Emby or is not visible to the API key."""


class EmbyClient:
    def __init__(self, base_url: str, api_key: str, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-Emby-Token": api_key}
        self._timeout = timeout_seconds

    async def list_users(self) -> list[Mapping[str, object]]:
        payload = await self._request("GET", "/Users")
        if not isinstance(payload, list):
            raise RuntimeError("Emby /Users returned an unexpected response")
        return [entry for entry in payload if isinstance(entry, Mapping)]

    async def list_sessions(self) -> list[Mapping[str, object]]:
        payload = await self._request("GET", "/Sessions")
        if not isinstance(payload, list):
            raise RuntimeError("Emby /Sessions returned an unexpected response")
        return [entry for entry in payload if isinstance(entry, Mapping)]

    async def list_series(self) -> list[Mapping[str, object]]:
        return await self._list_items(
            "Series", "ProviderIds,DateCreated,ParentId,ServerId,IsFolder,Type,ProductionYear"
        )

    async def list_episodes_for_series(self, series_ids: list[int]) -> list[Mapping[str, object]]:
        """Fetch episodes only for tracked Emby series, with bounded parallelism."""
        semaphore = asyncio.Semaphore(8)

        async def fetch(series_id: int) -> list[Mapping[str, object]]:
            async with semaphore:
                return await self._list_items(
                    "Episode", "SeriesId,SeriesName,ParentIndexNumber,IndexNumber,DateCreated,Path",
                    parent_id=series_id,
                )

        pages = await asyncio.gather(*(fetch(series_id) for series_id in series_ids))
        return [episode for page in pages for episode in page]

    async def get_series(self, series_id: int) -> Mapping[str, object]:
        payload = await self._request(
            "GET",
            "/Items",
            params={
                "Ids": str(series_id),
                "IncludeItemTypes": "Series",
                "Fields": "ProviderIds,DateCreated,ParentId,ServerId,IsFolder,Type,ProductionYear",
            },
        )
        if not isinstance(payload, Mapping) or not isinstance(payload.get("Items"), list):
            raise RuntimeError("Emby series lookup returned an unexpected response")
        series = next((item for item in payload["Items"] if isinstance(item, Mapping)), None)
        if series is None:
            raise EmbyItemNotFoundError(f"Emby 中未找到剧集 ID {series_id}")
        return series

    async def get_user(self, user_id: str) -> Mapping[str, object]:
        payload = await self._request("GET", f"/Users/{user_id}")
        if not isinstance(payload, Mapping):
            raise RuntimeError("Emby user lookup returned an unexpected response")
        return payload

    async def update_user_policy(
        self, user_id: str, policy: Mapping[str, object]
    ) -> Mapping[str, object]:
        await self._request("POST", f"/Users/{user_id}/Policy", policy, expect_json=False)
        return await self.get_user(user_id)

    async def _list_items(
        self, item_type: str, fields: str, *, parent_id: int | None = None
    ) -> list[Mapping[str, object]]:
        items: list[Mapping[str, object]] = []
        start_index = 0
        page_size = 500
        while True:
            params = {
                "Recursive": "true",
                "IncludeItemTypes": item_type,
                "Fields": fields,
                "StartIndex": str(start_index),
                "Limit": str(page_size),
            }
            if parent_id is not None:
                params["ParentId"] = str(parent_id)
            payload = await self._request(
                "GET",
                "/Items",
                params=params,
            )
            if not isinstance(payload, Mapping) or not isinstance(payload.get("Items"), list):
                raise RuntimeError(f"Emby /Items {item_type} query returned an unexpected response")
            page = [entry for entry in payload["Items"] if isinstance(entry, Mapping)]
            items.extend(page)
            total = _number(payload.get("TotalRecordCount"))
            if not page or len(items) >= total:
                return items
            start_index += len(page)

    async def _request(
        self, method: str, path: str, json_body: Mapping[str, object] | None = None,
        expect_json: bool = True, params: Mapping[str, str] | None = None,
    ) -> object | None:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.request(
                method, f"{self._base_url}{path}", headers=self._headers, json=json_body, params=params
            )
        response.raise_for_status()
        return response.json() if expect_json else None


@dataclass(frozen=True)
class WatchSession:
    session_id: str
    user_id: str
    username: str
    item_id: str
    item_name: str
    item_type: str
    play_start_time: str
    play_position: int
    total_duration: int
    play_progress: float
    ip_address: str | None
    device_name: str | None
    client_name: str | None


@dataclass(frozen=True)
class LoginSession:
    session_id: str
    user_id: str
    username: str
    login_time: str
    ip_address: str | None
    device_name: str | None
    client_name: str | None


def watch_session_from_payload(payload: Mapping[str, object]) -> WatchSession | None:
    item = payload.get("NowPlayingItem")
    if not isinstance(item, Mapping):
        return None
    session_id = _string(payload.get("Id"))
    user_id = _string(payload.get("UserId"))
    username = _string(payload.get("UserName"))
    item_id = _string(item.get("Id"))
    item_type = _string(item.get("Type"))
    if not all((session_id, user_id, username, item_id, item_type)):
        return None
    play_state = payload.get("PlayState")
    position_ticks = _number(play_state.get("PositionTicks") if isinstance(play_state, Mapping) else None)
    duration_ticks = _number(item.get("RunTimeTicks"))
    position = round(position_ticks / 10_000_000) if position_ticks else 0
    duration = round(duration_ticks / 10_000_000) if duration_ticks else 0
    return WatchSession(
        session_id=session_id,
        user_id=user_id,
        username=username,
        item_id=item_id,
        item_name=_display_name(item)[:500],
        item_type=item_type[:50],
        play_start_time=_play_start_time(payload, play_state, position),
        play_position=position,
        total_duration=duration,
        play_progress=min(1.0, position / duration) if duration else 0.0,
        ip_address=_remote_ip(_string(payload.get("RemoteEndPoint"))),
        device_name=_optional(payload.get("DeviceName"), 255),
        client_name=_optional(payload.get("Client"), 255),
    )


def login_session_from_payload(payload: Mapping[str, object]) -> LoginSession | None:
    session_id = _string(payload.get("Id"))
    user_id = _string(payload.get("UserId"))
    username = _string(payload.get("UserName"))
    if not all((session_id, user_id, username)):
        return None
    return LoginSession(
        session_id=session_id,
        user_id=user_id,
        username=username,
        login_time=_string(payload.get("LastActivityDate")) or datetime.now(timezone.utc).isoformat(),
        ip_address=_remote_ip(_string(payload.get("RemoteEndPoint"))),
        device_name=_optional(payload.get("DeviceName"), 255),
        client_name=_optional(payload.get("Client"), 255),
    )


def _display_name(item: Mapping[str, object]) -> str:
    name = _string(item.get("Name"))
    if _string(item.get("Type")) != "Episode":
        return name or "Unknown item"
    series = _string(item.get("SeriesName"))
    season = _string(item.get("SeasonName")) or (
        f"第{item['ParentIndexNumber']}季" if item.get("ParentIndexNumber") is not None else ""
    )
    episode = f"第{item['IndexNumber']}集" if item.get("IndexNumber") is not None else ""
    return " - ".join(part for part in (series, season, episode, name) if part and part != episode)


def _play_start_time(
    payload: Mapping[str, object], play_state: Mapping[str, object] | None, position: int
) -> str:
    """Prefer Emby's session timestamp; estimate from progress when it is unavailable.

    `NowPlayingItem.StartDate` describes the library item, not when a user began watching.
    Some Emby clients omit a playback-start field entirely, so using the sync time made
    every active session appear to start at the same moment.
    """
    for source in (play_state, payload):
        if not isinstance(source, Mapping):
            continue
        for key in ("PlayStartTime", "PlaybackStartTime", "StartTime"):
            timestamp = _string(source.get(key))
            if timestamp:
                return timestamp
    return (datetime.now(timezone.utc) - timedelta(seconds=position)).isoformat()


def _remote_ip(endpoint: str) -> str | None:
    value = endpoint[1:endpoint.index("]")] if endpoint.startswith("[") and "]" in endpoint else endpoint
    if value.count(":") == 1:
        value = value.rsplit(":", 1)[0]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _optional(value: object, length: int) -> str | None:
    return _string(value)[:length] or None


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0
