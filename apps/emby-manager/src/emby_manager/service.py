from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import date, datetime
import json
import logging
from pathlib import Path, PurePosixPath
from time import monotonic
from zoneinfo import ZoneInfo

import httpx
import psycopg

from .auth import verify_password
from .emby_client import EmbyClient, WatchSession, login_session_from_payload, watch_session_from_payload
from .moviepilot_client import MoviePilotClient
from .repository import LegacyEmbyRepository
from .tmdb_client import TmdbClient

logger = logging.getLogger(__name__)

_PATH_MAP_CACHE_SECONDS = 60
_TRACKING_TIMEZONE = ZoneInfo("Asia/Shanghai")
_VIDEO_SUFFIXES = frozenset({
    ".3gp", ".asf", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".rmvb", ".ts", ".webm", ".wmv",
})


class EmbyManagementService:
    def __init__(
        self,
        repository: LegacyEmbyRepository,
        client: EmbyClient,
        tmdb_client: TmdbClient | None = None,
        moviepilot_client: MoviePilotClient | None = None,
        moviepilot_database_url: str = "",
        moviepilot_media_path_mappings: tuple[tuple[str, Path], ...] = (),
    ) -> None:
        self._repository = repository
        self._client = client
        self._tmdb_client = tmdb_client
        self._moviepilot_client = moviepilot_client
        self._moviepilot_database_url = moviepilot_database_url
        self._moviepilot_media_path_mappings = moviepilot_media_path_mappings
        self._series_path_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}

    def authenticate(self, username: str, password: str) -> bool:
        password_hash = self._repository.login_password_hash(username)
        return password_hash is not None and verify_password(password, password_hash)

    def is_login_enabled(self, username: str) -> bool:
        return self._repository.is_login_enabled(username)

    def sync_network_stats(self, interface_name: str, stats: list[tuple[date, int, int, float]]) -> int:
        return self._repository.upsert_network_stats(interface_name, stats)

    def list_users(self, page: int, size: int, query: str | None, disabled: bool | None) -> dict[str, object]:
        return self._repository.list_users(page, size, query, disabled)

    def list_watch_logs(self, page: int, size: int, query: str | None, item_type: str | None,
                        start_at: str | None, end_at: str | None, item_id: str | None = None) -> dict[str, object]:
        return self._repository.list_watch_logs(page, size, query, item_type, start_at, end_at, item_id)

    def list_login_logs(self, page: int, size: int, query: str | None) -> dict[str, object]:
        return self._repository.list_login_logs(page, size, query)

    def dashboard(self) -> dict[str, object]:
        return self._repository.dashboard()

    def list_movies(self, page: int, size: int, query: str | None) -> dict[str, object]:
        return self._repository.list_movies(page, size, query)

    def list_libraries(self) -> list[dict[str, object]]:
        return self._repository.list_libraries()

    async def series_path_entries_for_node(self, node_id: str) -> list[dict[str, str]]:
        """Build transfer mappings from MoviePilot and manually linked tracked series."""
        requested_node = self._repository.canonical_storage_node(node_id)
        if not requested_node:
            return []
        cached = self._series_path_cache.get(requested_node)
        if cached and monotonic() - cached[0] < _PATH_MAP_CACHE_SECONDS:
            return cached[1].copy()
        cloud_targets = self._repository.series_cloud_path_targets()
        if not cloud_targets and not self._moviepilot_database_url:
            self._series_path_cache[requested_node] = (monotonic(), [])
            return []
        library_nodes = self._repository.library_nodes()

        moviepilot_entries: list[dict[str, str]] = []
        if self._moviepilot_database_url and self._moviepilot_media_path_mappings:
            moviepilot_entries = await asyncio.to_thread(
                _moviepilot_transfer_path_entries, self._moviepilot_database_url,
            )
        elif self._moviepilot_database_url:
            logger.warning("moviepilot_transfer_history_unavailable")

        result: list[dict[str, str]] = []
        mapping_keys: set[tuple[str, str]] = set()
        mapped_sources: set[str] = set()
        for target in cloud_targets:
            target_library = _text(target.get("library_name"))
            target_node = _transfer_node(target_library, _text(target.get("node_name")))
            if target_node != requested_node:
                continue
            source_directory = _tracked_cloud_media_directory(target)
            if source_directory is None:
                logger.warning(
                    "tracked_cloud_transfer_path_unusable series_id=%s name=%r alias=%r",
                    target.get("id"), target.get("name"), target.get("alias"),
                )
                continue
            entry = {
                "library_name": target_library,
                "tmdb_id": _text(target.get("themoviedb")),
                "source_hint": f"{source_directory}/",
                "destination_path": f"/data/media/tv/{source_directory}/",
            }
            key = (entry["source_hint"], entry["destination_path"])
            if key not in mapping_keys:
                result.append(entry)
                mapping_keys.add(key)
                mapped_sources.add(entry["source_hint"])

        moviepilot_nodes = self._repository.series_storage_nodes(
            entry["item_id"] for entry in moviepilot_entries
        )
        for moviepilot_entry in moviepilot_entries:
            destination = moviepilot_entry["source_destination"]
            if not _moviepilot_destination_is_live(
                destination, self._moviepilot_media_path_mappings,
            ):
                continue
            source_location = _moviepilot_media_directory(destination, library_nodes)
            target_location = _moviepilot_media_directory(
                moviepilot_entry["target_path"], library_nodes, is_directory=True,
            )
            if source_location is None or target_location is None:
                continue
            _, source_directory = source_location
            target_library, target_directory = target_location
            target_node = _transfer_node(
                target_library, moviepilot_nodes.get(moviepilot_entry["item_id"], ""),
            )
            if target_node != requested_node:
                continue
            source_hint = f"{source_directory}/"
            if source_hint in mapped_sources:
                continue
            entry = {
                "library_name": target_library,
                "tmdb_id": moviepilot_entry["media_id"],
                "source_hint": source_hint,
                "destination_path": f"/data/media/tv/{target_directory}/",
            }
            result.append(entry)
            mapping_keys.add((entry["source_hint"], entry["destination_path"]))
            mapped_sources.add(source_hint)
        result.sort(key=lambda item: item["source_hint"])
        self._series_path_cache[requested_node] = (monotonic(), result)
        return result.copy()

    async def series_path_lines_for_node(self, node_id: str) -> list[str]:
        entries = await self.series_path_entries_for_node(node_id)
        lines: list[str] = []
        for entry in entries:
            lines.append(f"{entry['source_hint']}|{entry['destination_path']}")
        return lines

    def list_series(
        self, page: int, size: int | None, query: str | None, tracking: bool | None = None,
        state: str | None = None,
    ) -> dict[str, object]:
        return self._repository.list_series(page, size, query, tracking, state)

    def set_series_tracking(self, series_id: int, tracking: bool) -> bool:
        return self._repository.set_series_tracking(series_id, tracking)

    def series_detail(self, series_id: int) -> dict[str, object] | None:
        return self._repository.series_detail(series_id)

    def update_series_detail(self, series_id: int, detail: Mapping[str, object]) -> bool:
        return self._repository.update_series_detail(series_id, detail)

    def episode_comparison(self, series_id: int) -> dict[str, object] | None:
        return self._repository.episode_comparison(series_id)

    async def sync_series_tracking(self) -> dict[str, int]:
        if self._tmdb_client is None:
            raise RuntimeError("TMDB_API_TOKEN is not configured")
        return await self._sync_tracking_rows(self._repository.list_tracked_series())

    async def _sync_tracking_rows(self, rows: list[Mapping[str, object]]) -> dict[str, int]:
        if self._tmdb_client is None:
            raise RuntimeError("TMDB_API_TOKEN is not configured")
        updated = skipped = failed = episode_synced = episode_skipped = 0
        for row in rows:
            series_id = _positive_int(row.get("id"))
            tmdb_id = _positive_int(row.get("themoviedb"))
            if series_id is None or tmdb_id is None:
                skipped += 1
                continue
            try:
                details = await self._tmdb_client.get_tv_details(tmdb_id)
                snapshot = _tracking_snapshot(row, details)
                if snapshot is None:
                    skipped += 1
                    continue
                season_number = _positive_int(snapshot["season_number"])
                if season_number is not None:
                    try:
                        season_details = await self._tmdb_client.get_tv_season_details(tmdb_id, season_number)
                        snapshot = _tracking_snapshot(row, details, season_details) or snapshot
                        episode_result = self._repository.upsert_tmdb_episodes(series_id, season_details)
                        episode_synced += episode_result["synced"]
                        episode_skipped += episode_result["skipped"]
                    except (httpx.HTTPError, RuntimeError, ValueError):
                        logger.warning(
                            "series_tracking_season_lookup_failed series_id=%s tmdb_id=%s season=%s",
                            series_id, tmdb_id, season_number,
                            exc_info=True,
                        )
                self._repository.save_tracking_metadata(series_id, **snapshot)
                updated += 1
            except (httpx.HTTPError, RuntimeError, ValueError):
                logger.exception("series_tracking_sync_failed series_id=%s tmdb_id=%s", series_id, tmdb_id)
                failed += 1
        return {
            "updated": updated,
            "skipped": skipped,
            "failed": failed,
            "episodes": episode_synced,
            "episode_skipped": episode_skipped,
        }

    async def sync_movies(self) -> dict[str, int]:
        """Import the legacy movie index and media-source tables from Emby."""
        known_library_parents = self._repository.library_subfolder_ids()
        resolved_parents: dict[int, int | None] = {}
        movies: list[Mapping[str, object]] = []
        for payload in await self._client.list_movies():
            movie_id = _positive_int(payload.get("Id"))
            parent_id = _positive_int(payload.get("ParentId"))
            if movie_id is not None and parent_id is not None and parent_id not in known_library_parents:
                if parent_id not in resolved_parents:
                    resolved_parents[parent_id] = await self._client.movie_library_parent_id(movie_id)
                if (resolved_parent := resolved_parents[parent_id]) is not None:
                    updated_payload = dict(payload)
                    updated_payload["ParentId"] = resolved_parent
                    movies.append(updated_payload)
                    continue
            movies.append(payload)
        return self._repository.upsert_emby_movies(movies)

    async def sync_moviepilot_subscriptions(self) -> dict[str, int]:
        if self._moviepilot_client is None:
            raise RuntimeError("MoviePilot is not configured")
        subscriptions = await self._moviepilot_client.list_subscriptions()
        series = {
            tmdb_id: _moviepilot_title(item, tmdb_id)
            for item in subscriptions if _moviepilot_is_tv(item)
            if (tmdb_id := _moviepilot_tmdb_id(item)) is not None
        }
        result = self._repository.import_moviepilot_series(series)
        return {
            "subscriptions": len(series),
            "matched": result["matched"],
            "enabled": result["enabled"],
            "created": result["created"],
        }

    async def sync_series(self) -> dict[str, int]:
        created = updated = promoted = skipped = 0
        for payload in await self._client.list_series():
            action = self._repository.upsert_emby_series(payload)
            if action == "created":
                created += 1
            elif action == "updated":
                updated += 1
            elif action == "promoted":
                promoted += 1
            else:
                skipped += 1
        tracked_ids = self._repository.tracked_emby_series_ids()
        episodes = self._repository.upsert_emby_episodes(
            await self._client.list_episodes_for_series(tracked_ids)
        )
        return {
            "created": created, "updated": updated, "promoted": promoted, "skipped": skipped,
            "episodes": episodes["synced"], "episode_skipped": episodes["skipped"],
        }

    async def sync_one_series(self, series_id: int) -> dict[str, object]:
        if series_id <= 0:
            raise ValueError("该剧集尚未关联 Emby，无法单独同步")
        action = self._repository.upsert_emby_series(await self._client.get_series(series_id))
        episodes = self._repository.upsert_emby_episodes(
            await self._client.list_episodes_for_series([series_id])
        )
        result: dict[str, object] = {
            "action": action,
            "episodes": episodes["synced"],
            "episode_skipped": episodes["skipped"],
        }
        tracked = self._repository.tracked_series(series_id)
        if tracked is not None and self._tmdb_client is not None:
            result["tracking"] = await self._sync_tracking_rows([tracked])
        return result

    async def sync_users(self) -> dict[str, int]:
        synced = skipped = 0
        for user in await self._client.list_users():
            if self._repository.upsert_user(user):
                synced += 1
            else:
                skipped += 1
        return {"synced": synced, "skipped": skipped}

    async def sync_login_logs(self) -> dict[str, int]:
        created = skipped = 0
        for payload in await self._client.list_sessions():
            session = login_session_from_payload(payload)
            if session is None:
                skipped += 1
            elif self._repository.save_login_session(session):
                created += 1
            else:
                skipped += 1
        return {"created": created, "skipped": skipped}

    async def sync_watch_logs(self) -> dict[str, int]:
        created = updated = skipped = 0
        active: set[tuple[str, str]] = set()
        for payload in await self._client.list_sessions():
            session = watch_session_from_payload(payload)
            if session is None:
                skipped += 1
                continue
            active.add((session.session_id, session.item_id))
            if self._repository.save_watch_session(session):
                created += 1
            else:
                updated += 1
        return {
            "created": created,
            "updated": updated,
            "ended": self._repository.finish_absent_sessions(active),
            "skipped": skipped,
        }

    async def update_user_policy(
        self, user_id: str, is_disabled: bool | None, enable_remote_access: bool | None
    ) -> Mapping[str, object]:
        if is_disabled is None and enable_remote_access is None:
            raise ValueError("At least one user policy field is required")
        user = await self._client.get_user(user_id)
        policy = user.get("Policy")
        updated = dict(policy) if isinstance(policy, Mapping) else {}
        if is_disabled is not None:
            updated["IsDisabled"] = is_disabled
        if enable_remote_access is not None:
            updated["EnableRemoteAccess"] = enable_remote_access
        changed_user = await self._client.update_user_policy(user_id, updated)
        self._repository.upsert_user(changed_user)
        return changed_user


def _tracking_snapshot(
    row: Mapping[str, object], details: Mapping[str, object], season_details: Mapping[str, object] | None = None,
) -> dict[str, object] | None:
    today = datetime.now(_TRACKING_TIMEZONE).date()
    seasons = [item for item in details.get("seasons", []) if isinstance(item, Mapping)]
    non_special = [item for item in seasons if _positive_int(item.get("season_number")) not in (None, 0)]
    last = details.get("last_episode_to_air")
    next_episode = details.get("next_episode_to_air")
    last_episode = last if isinstance(last, Mapping) else {}
    upcoming_episode = next_episode if isinstance(next_episode, Mapping) else {}
    season_number = (
        _positive_int(row.get("lock_season"))
        or _positive_int(upcoming_episode.get("season_number"))
        or _positive_int(last_episode.get("season_number"))
    )
    if season_number is None and non_special:
        season_number = max(_positive_int(item.get("season_number")) or 0 for item in non_special) or None
    if season_number is None:
        return None
    season = next(
        (item for item in non_special if _positive_int(item.get("season_number")) == season_number), {}
    )
    season_name = _text(season.get("name"))
    total = _positive_int(season.get("episode_count"))
    official_latest = (
        _positive_int(last_episode.get("episode_number"))
        if _positive_int(last_episode.get("season_number")) == season_number
        else None
    )
    next_air_date = _date_value(upcoming_episode.get("air_date"))
    next_update = (
        next_air_date.isoformat()
        if _positive_int(upcoming_episode.get("season_number")) == season_number
        and next_air_date is not None and next_air_date >= today
        else None
    )
    if isinstance(season_details, Mapping):
        aired: list[tuple[int, date]] = []
        scheduled: list[date] = []
        for episode in season_details.get("episodes", []):
            if not isinstance(episode, Mapping):
                continue
            episode_number = _positive_int(episode.get("episode_number"))
            air_date = _date_value(episode.get("air_date"))
            if episode_number is None or air_date is None:
                continue
            if air_date <= today:
                aired.append((episode_number, air_date))
            if air_date >= today:
                scheduled.append(air_date)
        if aired:
            official_latest = len(aired)
        # A same-day episode is both already aired for progress purposes and the
        # nearest scheduled update for the list.  If none remains, clear any
        # stale series-level next_episode_to_air value.
        next_update = min(scheduled).isoformat() if scheduled else None
    return {
        "season_name": season_name or None,
        "season_number": season_number,
        "official_latest": official_latest,
        "next_update": next_update or None,
        "total": total,
    }


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _date_value(value: object) -> date | None:
    try:
        return date.fromisoformat(_text(value))
    except ValueError:
        return None


def _moviepilot_is_tv(payload: Mapping[str, object]) -> bool:
    value = _text(payload.get("type")).lower()
    return value in {"tv", "电视剧"}


def _moviepilot_tmdb_id(payload: Mapping[str, object]) -> str | None:
    for key in ("tmdbid", "tmdb_id"):
        value = _positive_int(payload.get(key))
        if value is not None:
            return str(value)
    media_id = _text(payload.get("mediaid") or payload.get("media_id"))
    media_source = _moviepilot_source_key(_text(payload.get("media_source")))
    if media_source == "tmdb" and (value := _positive_int(media_id)) is not None:
        return str(value)
    prefix, separator, value = media_id.partition(":")
    if prefix.lower() == "tmdb" and separator and _positive_int(value) is not None:
        return value
    return None


def _moviepilot_title(payload: Mapping[str, object], tmdb_id: str) -> str:
    for key in ("name", "title", "media_name", "media_title"):
        value = _text(payload.get(key))
        if value:
            return value[:200]
    return f"TMDB {tmdb_id}"


def _moviepilot_media_directory(
    destination: str, library_nodes: Mapping[str, str], *, is_directory: bool = False,
) -> tuple[str, str] | None:
    """Extract ``library/series directory`` from a MoviePilot transfer destination.

    A transfer destination is an absolute file path inside MoviePilot's storage, e.g.
    ``/mnt/share/media1/media/综艺/姐姐快醒醒 (2026)/S01E06.mkv``. The configured
    library segment is taken from the real MoviePilot destination; this intentionally
    avoids guessing from the transfer title or from Emby's scraped title/category.
    ``mediaserveritem.path`` is already a series-directory path, so it must
    retain its last component instead of being handled like a video file.
    """
    parts = PurePosixPath(destination).parts
    indexes = [index for index, value in enumerate(parts) if value in library_nodes]
    if not indexes or len(parts) < 3:
        return None
    for index in reversed(indexes):
        directory_parts = parts[index:] if is_directory else parts[index:-1]
        if len(directory_parts) < 2:
            continue
        if any(part in {"", ".", "..", "/"} for part in directory_parts):
            continue
        library_name = directory_parts[0]
        return library_name, "/".join(directory_parts)
    return None


def _tracked_cloud_media_directory(target: Mapping[str, object]) -> str | None:
    """Build ``category/title (year)`` for a manually linked tracking row.

    The title and scraped year are operator-maintained in the tracking table;
    unlike the MoviePilot branch, no inferred title or category is involved.
    """
    library_name = _text(target.get("library_name"))
    name = _text(target.get("name"))
    year = _text(target.get("alias")).strip("() ")
    if not library_name or not name or not _is_year(year):
        return None
    if any("/" in value or "|" in value for value in (library_name, name, year)):
        return None
    return f"{library_name}/{_title_with_year(name, year)}"


def _transfer_node(library_name: str, configured_node: str) -> str:
    """Use the exact Emby node; fall back only when no node is known."""
    return configured_node or ("emby2" if library_name.startswith("动漫集") else "emby0")


def _moviepilot_transfer_path_entries(database_url: str) -> list[dict[str, str]]:
    """Map an organized MoviePilot directory to its current Emby item directory.

    ``transferhistory.dest`` is the stable left-hand directory for auto-transfer.
    MoviePilot's media-server index is the authority for the current scraped
    right-hand directory.  This deliberately does not use Dragonli's copied
    TMDB field: a rescrape can replace that identifier while the media-server
    item still retains the real directory relationship.
    """
    transfer_query = '''SELECT DISTINCT ON (category,title,year,media_source,media_id)
            dest,media_source,media_id
        FROM transferhistory
        WHERE status IS TRUE AND dest IS NOT NULL
          AND type='电视剧'
        ORDER BY category,title,year,media_source,media_id,id DESC'''
    item_query = '''SELECT server,library,item_id,title,media_source,media_id,path,seasoninfo
        FROM mediaserveritem
        WHERE path IS NOT NULL AND btrim(path) <> ''
          AND lower(server)='emby'
        ORDER BY lst_mod_date DESC NULLS LAST,id DESC'''
    try:
        with psycopg.connect(database_url, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute(transfer_query)
                transfers = cursor.fetchall()
                cursor.execute(item_query)
                items = cursor.fetchall()
    except psycopg.Error as error:
        raise RuntimeError(f"MoviePilot PostgreSQL query failed: {error}") from error

    indexed_items: list[dict[str, str]] = []
    by_identity: dict[tuple[str, str], dict[str, str]] = {}
    for server, library, item_id, title, source, media_id, path, seasoninfo in items:
        entry = {
            "server": _text(server),
            "library": _text(library),
            "item_id": _text(item_id),
            "title": _text(title),
            "media_source": _moviepilot_source_key(_text(source)),
            "media_id": _text(media_id),
            "path": _text(path),
            # psycopg decodes JSON/JSONB columns into a mapping.  Re-encode it
            # so the season parser handles PostgreSQL and text-backed schemas
            # identically.
            "seasoninfo": json.dumps(seasoninfo) if isinstance(seasoninfo, Mapping) else _text(seasoninfo),
        }
        if not entry["library"] or not entry["title"] or not entry["path"]:
            continue
        indexed_items.append(entry)
        identity = (entry["media_source"], entry["media_id"])
        by_identity.setdefault(identity, entry)

    result: list[dict[str, str]] = []
    for destination, source, media_id in transfers:
        normalized_source = _moviepilot_source_key(_text(source))
        normalized_media_id = _text(media_id)
        source_item = by_identity.get((normalized_source, normalized_media_id))
        if source_item is None:
            continue
        target_item = _moviepilot_current_item(source_item, indexed_items)
        if target_item is None:
            continue
        organized_destination = _text(destination)
        if organized_destination:
            result.append({
                "source_destination": organized_destination,
                "target_path": target_item["path"],
                "media_id": normalized_media_id,
                "item_id": target_item["item_id"],
            })
    return result


def _moviepilot_source_key(value: str) -> str:
    normalized = value.casefold()
    return "tmdb" if normalized in {"tmdb", "themoviedb"} else normalized


def _moviepilot_current_item(
    source_item: Mapping[str, str], items: list[dict[str, str]],
) -> dict[str, str] | None:
    """Resolve a verified replacement item when MoviePilot has split a season.

    The direct media identity remains the default.  A different item is used
    only when it is in the same Emby library, its same-season episode set is a
    strict superset, and the titles share a meaningful leading identifier.  It
    covers cases such as a season being rescraped as a standalone title without
    turning unrelated shows into directory aliases.
    """
    source_seasons = _moviepilot_seasoninfo(source_item.get("seasoninfo", ""))
    best_item: dict[str, str] | None = None
    best_coverage = 0
    for candidate in items:
        if candidate["library"] != source_item.get("library"):
            continue
        if candidate["server"] != source_item.get("server"):
            continue
        if candidate["media_id"] == source_item.get("media_id") and candidate["media_source"] == source_item.get("media_source"):
            continue
        if not _moviepilot_titles_share_identity(source_item.get("title", ""), candidate["title"]):
            continue
        candidate_seasons = _moviepilot_seasoninfo(candidate["seasoninfo"])
        if not _moviepilot_has_strict_season_superset(source_seasons, candidate_seasons):
            continue
        coverage = sum(len(episodes) for episodes in candidate_seasons.values())
        if coverage > best_coverage:
            best_item = candidate
            best_coverage = coverage
    return best_item or dict(source_item)


def _moviepilot_seasoninfo(value: str) -> dict[int, set[int]]:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[int, set[int]] = {}
    for raw_season, raw_episodes in payload.items():
        season = _positive_int(raw_season)
        if season is None or not isinstance(raw_episodes, list):
            continue
        episodes = {episode for raw in raw_episodes if (episode := _positive_int(raw)) is not None}
        if episodes:
            result[season] = episodes
    return result


def _moviepilot_has_strict_season_superset(
    source: Mapping[int, set[int]], candidate: Mapping[int, set[int]],
) -> bool:
    return any(
        source_episodes < candidate.get(season, set())
        for season, source_episodes in source.items()
    )


def _moviepilot_titles_share_identity(left: str, right: str) -> bool:
    normalized_left = "".join(char for char in left.casefold() if char.isalnum())
    normalized_right = "".join(char for char in right.casefold() if char.isalnum())
    if not normalized_left or not normalized_right:
        return False
    # A rescrape often inserts a qualifier into an established Chinese title,
    # e.g. “中国说唱巅峰对决” and “中国新说唱”.  A common prefix would miss
    # that relationship; an ordered common identifier remains specific enough
    # when combined with the same-library strict season-episode coverage check.
    previous = [0] * (len(normalized_right) + 1)
    for left_char in normalized_left:
        current = [0]
        for index, right_char in enumerate(normalized_right, start=1):
            current.append(
                previous[index - 1] + 1
                if left_char == right_char
                else max(previous[index], current[-1])
            )
        previous = current
    common_length = previous[-1]
    # Four scattered common characters alone are not specific enough: e.g.
    # “我剩下的恋爱” and “我的AI伴侣 - 奇异恋爱” share four characters but
    # are unrelated shows.  The common identifier must also cover at least
    # 80% of the shorter normalized title.
    return common_length >= 4 and common_length * 5 >= min(
        len(normalized_left), len(normalized_right)
    ) * 4


def _moviepilot_destination_is_live(
    destination: str, path_mappings: tuple[tuple[str, Path], ...],
) -> bool:
    """Check the exact organized video through a read-only local mount.

    MoviePilot keeps transfer-history rows even after an operator deletes the
    organized source.  Only a present video file represents a pending handoff.
    """
    remote_path = PurePosixPath(destination)
    for moviepilot_root, mounted_root in path_mappings:
        try:
            relative_path = remote_path.relative_to(PurePosixPath(moviepilot_root))
        except ValueError:
            continue
        if not relative_path.parts or ".." in relative_path.parts:
            return False
        candidate = mounted_root.joinpath(*relative_path.parts)
        return candidate.is_file() and candidate.suffix.casefold() in _VIDEO_SUFFIXES
    return False


def _title_with_year(name: str, year: str) -> str:
    if _is_year(year) and f"({year})" not in name:
        return f"{name} ({year})"
    return name


def _is_year(value: str) -> bool:
    return len(value) == 4 and value.isdecimal() and 1800 <= int(value) <= 3000
