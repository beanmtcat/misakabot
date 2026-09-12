from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _positive_seconds(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a positive integer number of seconds") from error
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer number of seconds")
    return value


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
    series_sync_interval_seconds: int = 1800
    login_sync_interval_seconds: int = 60
    watch_sync_interval_seconds: int = 60
    tmdb_api_token: str = ""
    tracking_sync_interval_seconds: int = 21600
    moviepilot_base_url: str = ""
    moviepilot_api_token: str = ""
    moviepilot_sqlite_path: Path | None = None
    moviepilot_sync_interval_seconds: int = 1800
    path_map_api_secret: str = ""

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
            series_sync_interval_seconds=_positive_seconds("EMBY_SERIES_SYNC_INTERVAL_SECONDS", 1800),
            login_sync_interval_seconds=_positive_seconds("EMBY_LOGIN_SYNC_INTERVAL_SECONDS", 60),
            watch_sync_interval_seconds=_positive_seconds("EMBY_WATCH_SYNC_INTERVAL_SECONDS", 60),
            tmdb_api_token=os.environ.get("TMDB_API_TOKEN", "").strip(),
            tracking_sync_interval_seconds=_positive_seconds(
                "EMBY_TRACKING_SYNC_INTERVAL_SECONDS", 21600
            ),
            moviepilot_base_url=os.environ.get("MOVIEPILOT_BASE_URL", "").strip().rstrip("/"),
            moviepilot_api_token=os.environ.get("MOVIEPILOT_API_TOKEN", "").strip(),
            moviepilot_sqlite_path=(
                Path(configured_moviepilot_sqlite_path)
                if (configured_moviepilot_sqlite_path := os.environ.get(
                    "MOVIEPILOT_SQLITE_PATH", ""
                ).strip())
                else None
            ),
            moviepilot_sync_interval_seconds=_positive_seconds(
                "MOVIEPILOT_SYNC_INTERVAL_SECONDS", 1800
            ),
            # EMBY_PATH_MAP_API_TOKEN is accepted during the HMAC migration so
            # existing deployments do not stop serving path mappings.
            path_map_api_secret=(
                os.environ.get("EMBY_PATH_MAP_API_SECRET", "").strip()
                or os.environ.get("EMBY_PATH_MAP_API_TOKEN", "").strip()
            ),
        )
