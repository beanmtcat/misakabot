from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def _positive_seconds(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a positive integer number of seconds") from error
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer number of seconds")
    return value


def _csv_values(value: str) -> frozenset[str]:
    return frozenset(item.strip().casefold() for item in value.split(",") if item.strip())


def _moviepilot_media_path_mappings(value: str) -> tuple[tuple[str, Path], ...]:
    mappings: list[tuple[str, Path]] = []
    for entry in value.split(","):
        moviepilot_path, separator, mounted_path = entry.strip().partition("=")
        moviepilot_path = moviepilot_path.rstrip("/") or "/"
        mounted_path = mounted_path.strip()
        if not entry.strip():
            continue
        if not separator or not moviepilot_path.startswith("/") or not mounted_path.startswith("/"):
            raise RuntimeError(
                "MOVIEPILOT_MEDIA_PATH_MAPPINGS entries must be /moviepilot/path=/mounted/path"
            )
        mappings.append((moviepilot_path, Path(mounted_path)))
    return tuple(sorted(mappings, key=lambda item: len(item[0]), reverse=True))


def _origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
        raise RuntimeError("EMBY_MANAGER_ORIGIN must be an http(s) origin without a path")
    return f"{parsed.scheme}://{parsed.netloc}".lower()


@dataclass(frozen=True)
class Settings:
    database_url: str
    emby_base_url: str
    emby_api_key: str
    session_secret: str
    emby_web_base_url: str = ""
    host: str = "127.0.0.1"
    port: int = 8084
    request_timeout_seconds: float = 10.0
    session_https_only: bool = False
    web_root: Path | None = None
    user_sync_interval_seconds: int = 1800
    movie_sync_interval_seconds: int = 1800
    series_sync_interval_seconds: int = 1800
    login_sync_interval_seconds: int = 60
    watch_sync_interval_seconds: int = 60
    tmdb_api_token: str = ""
    tracking_sync_interval_seconds: int = 21600
    moviepilot_base_url: str = ""
    moviepilot_api_token: str = ""
    moviepilot_database_url: str = ""
    moviepilot_media_path_mappings: tuple[tuple[str, Path], ...] = ()
    moviepilot_sync_interval_seconds: int = 1800
    path_map_api_secret: str = ""
    manager_admin_usernames: frozenset[str] = frozenset()
    manager_origin: str = ""
    login_rate_limit_attempts: int = 5
    login_rate_limit_window_seconds: int = 900

    @classmethod
    def from_environment(cls) -> "Settings":
        values = {
            "DATABASE_URL": os.environ.get("DATABASE_URL", "").strip(),
            "EMBY_BASE_URL": os.environ.get("EMBY_BASE_URL", "").rstrip("/"),
            "EMBY_API_KEY": os.environ.get("EMBY_API_KEY", "").strip(),
            "SESSION_SECRET": os.environ.get("SESSION_SECRET", "").strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")
        if not values["DATABASE_URL"].startswith(("postgres://", "postgresql://")):
            raise RuntimeError("DATABASE_URL must be a PostgreSQL URL")
        manager_admin_usernames = _csv_values(os.environ.get("EMBY_MANAGER_ADMIN_USERNAMES", ""))
        if not manager_admin_usernames:
            raise RuntimeError("EMBY_MANAGER_ADMIN_USERNAMES must contain at least one username")
        manager_origin = _origin(os.environ.get("EMBY_MANAGER_ORIGIN", ""))
        moviepilot_database_url = os.environ.get("MOVIEPILOT_DATABASE_URL", "").strip()
        if moviepilot_database_url and not moviepilot_database_url.startswith(("postgres://", "postgresql://")):
            raise RuntimeError("MOVIEPILOT_DATABASE_URL must be a PostgreSQL URL")
        return cls(
            database_url=values["DATABASE_URL"],
            emby_base_url=values["EMBY_BASE_URL"],
            emby_api_key=values["EMBY_API_KEY"],
            session_secret=values["SESSION_SECRET"],
            emby_web_base_url=os.environ.get("EMBY_WEB_BASE_URL", "").strip().rstrip("/"),
            host=os.environ.get("EMBY_API_HOST", "127.0.0.1"),
            port=int(os.environ.get("EMBY_API_PORT", "8084")),
            request_timeout_seconds=float(os.environ.get("EMBY_REQUEST_TIMEOUT_SECONDS", "10")),
            session_https_only=os.environ.get("SESSION_HTTPS_ONLY", "false").lower()
            in {"1", "true", "yes", "on"},
            web_root=(
                Path(configured_web_root).expanduser()
                if (configured_web_root := os.environ.get("EMBY_WEB_ROOT", "").strip())
                else None
            ),
            user_sync_interval_seconds=_positive_seconds("EMBY_USER_SYNC_INTERVAL_SECONDS", 1800),
            movie_sync_interval_seconds=_positive_seconds("EMBY_MOVIE_SYNC_INTERVAL_SECONDS", 1800),
            series_sync_interval_seconds=_positive_seconds("EMBY_SERIES_SYNC_INTERVAL_SECONDS", 1800),
            login_sync_interval_seconds=_positive_seconds("EMBY_LOGIN_SYNC_INTERVAL_SECONDS", 60),
            watch_sync_interval_seconds=_positive_seconds("EMBY_WATCH_SYNC_INTERVAL_SECONDS", 60),
            tmdb_api_token=os.environ.get("TMDB_API_TOKEN", "").strip(),
            tracking_sync_interval_seconds=_positive_seconds(
                "EMBY_TRACKING_SYNC_INTERVAL_SECONDS", 21600
            ),
            moviepilot_base_url=os.environ.get("MOVIEPILOT_BASE_URL", "").strip().rstrip("/"),
            moviepilot_api_token=os.environ.get("MOVIEPILOT_API_TOKEN", "").strip(),
            moviepilot_database_url=moviepilot_database_url,
            moviepilot_media_path_mappings=_moviepilot_media_path_mappings(
                os.environ.get("MOVIEPILOT_MEDIA_PATH_MAPPINGS", "")
            ),
            moviepilot_sync_interval_seconds=_positive_seconds(
                "MOVIEPILOT_SYNC_INTERVAL_SECONDS", 1800
            ),
            path_map_api_secret=os.environ.get("EMBY_PATH_MAP_API_SECRET", "").strip(),
            manager_admin_usernames=manager_admin_usernames,
            manager_origin=manager_origin,
            login_rate_limit_attempts=_positive_seconds("EMBY_LOGIN_RATE_LIMIT_ATTEMPTS", 5),
            login_rate_limit_window_seconds=_positive_seconds(
                "EMBY_LOGIN_RATE_LIMIT_WINDOW_SECONDS", 900
            ),
        )
