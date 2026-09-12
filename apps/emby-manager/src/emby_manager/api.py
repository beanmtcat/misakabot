from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

from .auth import require_admin
from .config import Settings
from .emby_client import EmbyClient, EmbyItemNotFoundError
from .moviepilot_client import MoviePilotClient
from .repository import LegacyEmbyRepository
from .service import EmbyManagementService
from .tmdb_client import TmdbClient

logger = logging.getLogger(__name__)
APP_PREFIX = "/emby-manager"


class PolicyChange(BaseModel):
    is_disabled: bool | None = None
    enable_remote_access: bool | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class TrackingChange(BaseModel):
    tracking: bool


class SeriesDetailChange(BaseModel):
    tracking: bool
    library_name: str | None = None
    themoviedb: str | None = None
    quark: str | None = None
    alipan: str | None = None
    alias: str | None = None
    lock_season: int | None = None
    index_name: str | None = None


def create_app(
    settings: Settings,
    service: EmbyManagementService | None = None,
) -> FastAPI:
    manager = service or EmbyManagementService(
        LegacyEmbyRepository(settings.database_url),
        EmbyClient(settings.emby_base_url, settings.emby_api_key, settings.request_timeout_seconds),
        TmdbClient(settings.tmdb_api_token, settings.request_timeout_seconds)
        if settings.tmdb_api_token else None,
        MoviePilotClient(
            settings.moviepilot_base_url,
            settings.moviepilot_api_token,
            settings.request_timeout_seconds,
        ) if settings.moviepilot_base_url and settings.moviepilot_api_token else None,
        settings.moviepilot_sqlite_path,
    )
    sync_lock = asyncio.Lock()
    sync_status: dict[str, dict[str, object | None]] = {
        "users": {"last_success_at": None, "last_result": None},
        "series": {"last_success_at": None, "last_result": None},
        "login_logs": {"last_success_at": None, "last_result": None},
        "watch_logs": {"last_success_at": None, "last_result": None},
        "series_tracking": {
            "last_success_at": None,
            "last_result": None,
            "enabled": bool(settings.tmdb_api_token),
        },
        "moviepilot_subscriptions": {
            "last_success_at": None,
            "last_result": None,
            "enabled": bool(settings.moviepilot_base_url and settings.moviepilot_api_token),
        },
    }

    async def run_user_sync() -> dict[str, int]:
        async with sync_lock:
            result = await manager.sync_users()
            sync_status["users"] = {
                "last_success_at": datetime.now(timezone.utc).isoformat(),
                "last_result": result,
            }
            return result

    async def run_watch_sync() -> dict[str, int]:
        async with sync_lock:
            result = await manager.sync_watch_logs()
            sync_status["watch_logs"] = {
                "last_success_at": datetime.now(timezone.utc).isoformat(),
                "last_result": result,
            }
            return result

    async def run_login_sync() -> dict[str, int]:
        async with sync_lock:
            result = await manager.sync_login_logs()
            sync_status["login_logs"] = {
                "last_success_at": datetime.now(timezone.utc).isoformat(),
                "last_result": result,
            }
            return result

    async def run_series_sync() -> dict[str, int]:
        async with sync_lock:
            result = await manager.sync_series()
            sync_status["series"] = {
                "last_success_at": datetime.now(timezone.utc).isoformat(),
                "last_result": result,
            }
            return result

    async def run_one_series_sync(series_id: int) -> dict[str, object]:
        async with sync_lock:
            result = await manager.sync_one_series(series_id)
            sync_status["series"] = {
                "last_success_at": datetime.now(timezone.utc).isoformat(),
                "last_result": result,
            }
            return result

    async def run_tracking_sync() -> dict[str, int]:
        async with sync_lock:
            result = await manager.sync_series_tracking()
            sync_status["series_tracking"] = {
                "last_success_at": datetime.now(timezone.utc).isoformat(),
                "last_result": result,
                "enabled": True,
            }
            return result

    async def run_moviepilot_sync() -> dict[str, int]:
        async with sync_lock:
            result = await manager.sync_moviepilot_subscriptions()
            sync_status["moviepilot_subscriptions"] = {
                "last_success_at": datetime.now(timezone.utc).isoformat(),
                "last_result": result,
                "enabled": True,
            }
            return result

    async def run_full_tracking_sync() -> dict[str, object]:
        async with sync_lock:
            result: dict[str, object] = {}
            emby_series = await manager.sync_series()
            sync_status["series"] = {
                "last_success_at": datetime.now(timezone.utc).isoformat(),
                "last_result": emby_series,
            }
            result["emby_series"] = emby_series
            if settings.moviepilot_base_url and settings.moviepilot_api_token:
                moviepilot_result = await manager.sync_moviepilot_subscriptions()
                sync_status["moviepilot_subscriptions"] = {
                    "last_success_at": datetime.now(timezone.utc).isoformat(),
                    "last_result": moviepilot_result,
                    "enabled": True,
                }
                result["moviepilot"] = moviepilot_result
            if settings.tmdb_api_token:
                tracking_result = await manager.sync_series_tracking()
                sync_status["series_tracking"] = {
                    "last_success_at": datetime.now(timezone.utc).isoformat(),
                    "last_result": tracking_result,
                    "enabled": True,
                }
                result["tracking"] = tracking_result
            return result

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop_event = asyncio.Event()
        tasks = [
            asyncio.create_task(
                _periodic_sync(
                    "login_logs", settings.login_sync_interval_seconds, run_login_sync, stop_event
                )
            ),
            asyncio.create_task(
                _periodic_sync(
                    "users", settings.user_sync_interval_seconds, run_user_sync, stop_event
                )
            ),
            asyncio.create_task(
                _periodic_sync(
                    "series",
                    settings.series_sync_interval_seconds,
                    run_series_sync,
                    stop_event,
                    run_immediately=False,
                )
            ),
            asyncio.create_task(
                _periodic_sync(
                    "watch_logs", settings.watch_sync_interval_seconds, run_watch_sync, stop_event
                )
            ),
        ]
        if settings.tmdb_api_token:
            tasks.append(
                asyncio.create_task(
                    _periodic_sync(
                        "series_tracking",
                        settings.tracking_sync_interval_seconds,
                        run_tracking_sync,
                        stop_event,
                        run_immediately=False,
                    )
                )
            )
        if settings.moviepilot_base_url and settings.moviepilot_api_token:
            tasks.append(
                asyncio.create_task(
                    _periodic_sync(
                        "moviepilot_subscriptions",
                        settings.moviepilot_sync_interval_seconds,
                        run_moviepilot_sync,
                        stop_event,
                    )
                )
            )
        try:
            yield
        finally:
            stop_event.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(
        title="Emby Manager API",
        version="0.1.0",
        docs_url=f"{APP_PREFIX}/docs",
        openapi_url=f"{APP_PREFIX}/openapi.json",
        lifespan=lifespan,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="emby_manager_session",
        path=APP_PREFIX,
        same_site="strict",
        https_only=settings.session_https_only,
        max_age=86400,
    )

    @app.get("/healthz")
    def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    web_root = settings.web_root or Path(__file__).resolve().parents[2] / "web" / "dist"
    if not web_root.is_dir():
        raise RuntimeError(
            f"Frontend build directory '{web_root}' does not exist. "
            "Run npm run build or set EMBY_WEB_ROOT to the built web/dist directory."
        )
    app.mount(f"{APP_PREFIX}/assets", StaticFiles(directory=web_root), name="assets")

    @app.get(f"{APP_PREFIX}/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(web_root / "index.html")

    @app.post(f"{APP_PREFIX}/auth/login")
    def login(payload: LoginRequest, request: Request) -> dict[str, str]:
        username = payload.username.strip()
        if not username or not payload.password or not manager.authenticate(username, payload.password):
            raise HTTPException(status_code=401, detail="用户名或密码不正确")
        request.session.clear()
        request.session["username"] = username
        return {"username": username}

    @app.get(f"{APP_PREFIX}/auth/session")
    async def session(username: str = Depends(require_admin)) -> dict[str, str]:
        return {"username": username}

    @app.post(f"{APP_PREFIX}/auth/logout")
    def logout(request: Request) -> Response:
        request.session.clear()
        return Response(status_code=204)

    @app.get(f"{APP_PREFIX}/v1/emby/users")
    def list_users(
        page: int = Query(1, ge=1), size: int = Query(30, ge=1, le=100),
        query: str | None = Query(None, max_length=100), is_disabled: bool | None = None,
        _: str = Depends(require_admin),
    ) -> dict[str, object]:
        return manager.list_users(page, size, query, is_disabled)

    @app.post(f"{APP_PREFIX}/v1/emby/users/sync")
    async def sync_users(_: str = Depends(require_admin)) -> dict[str, int]:
        return await _remote_operation(run_user_sync())

    @app.get(f"{APP_PREFIX}/v1/emby/login-logs")
    def list_login_logs(
        page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
        query: str | None = Query(None, max_length=100), _: str = Depends(require_admin),
    ) -> dict[str, object]:
        return manager.list_login_logs(page, size, query)

    @app.post(f"{APP_PREFIX}/v1/emby/login-logs/sync")
    async def sync_login_logs(_: str = Depends(require_admin)) -> dict[str, int]:
        return await _remote_operation(run_login_sync())

    @app.get(f"{APP_PREFIX}/v1/emby/sync-status")
    def get_sync_status(_: str = Depends(require_admin)) -> dict[str, dict[str, object | None]]:
        return sync_status

    @app.get(f"{APP_PREFIX}/v1/emby/dashboard")
    def dashboard(_: str = Depends(require_admin)) -> dict[str, object]:
        return manager.dashboard()

    @app.get(f"{APP_PREFIX}/v1/emby/movies")
    def list_movies(
        page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
        query: str | None = Query(None, max_length=100), _: str = Depends(require_admin),
    ) -> dict[str, object]:
        return manager.list_movies(page, size, query)

    @app.get(f"{APP_PREFIX}/v1/emby/items/{{item_id}}/open", include_in_schema=False)
    def open_emby_item(
        item_id: str,
        server_id: str | None = Query(None, max_length=100),
        _: str = Depends(require_admin),
    ) -> RedirectResponse:
        fragment = f"!/item?id={quote(item_id, safe='')}"
        if server_id:
            fragment += f"&serverId={quote(server_id, safe='')}"
        web_base_url = settings.emby_web_base_url or settings.emby_base_url
        return RedirectResponse(f"{web_base_url}/web/index.html#{fragment}", status_code=307)

    @app.get(f"{APP_PREFIX}/v1/emby/series")
    def list_series(
        page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
        query: str | None = Query(None, max_length=100), tracking: bool | None = None,
        state: str | None = Query(None, pattern="^(today|exception)$"),
        all_items: bool = False,
        _: str = Depends(require_admin),
    ) -> dict[str, object]:
        return manager.list_series(page, None if all_items else size, query, tracking, state)

    @app.get(f"{APP_PREFIX}/v1/emby/libraries")
    def list_libraries(_: str = Depends(require_admin)) -> dict[str, object]:
        return {"items": manager.list_libraries()}

    async def require_path_map_api_token(request: Request) -> None:
        expected_token = settings.path_map_api_token
        if not expected_token:
            raise HTTPException(status_code=503, detail="未配置路径映射 API Token")
        scheme, _, supplied_token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(supplied_token, expected_token):
            raise HTTPException(status_code=401, detail="路径映射 API 鉴权失败")

    @app.get(
        f"{APP_PREFIX}/v1/emby/nodes/{{node_id}}/series-paths",
        response_model=None,
    )
    async def series_paths_for_node(
        node_id: str,
        format: str = Query("text", pattern="^(text|json)$"),
        _: None = Depends(require_path_map_api_token),
    ) -> PlainTextResponse | JSONResponse:
        if format == "json":
            return JSONResponse({"items": await manager.series_path_entries_for_node(node_id)})
        return PlainTextResponse("\n".join(await manager.series_path_lines_for_node(node_id)))

    @app.get(f"{APP_PREFIX}/v1/emby/series/{{series_id}}")
    def series_detail(series_id: int, _: str = Depends(require_admin)) -> dict[str, object]:
        detail = manager.series_detail(series_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="电视剧不存在或已失效")
        return detail

    @app.put(f"{APP_PREFIX}/v1/emby/series/{{series_id}}")
    def update_series_detail(
        series_id: int, change: SeriesDetailChange, _: str = Depends(require_admin)
    ) -> dict[str, object]:
        if not manager.update_series_detail(series_id, change.model_dump()):
            raise HTTPException(status_code=404, detail="电视剧不存在或已失效")
        return {"id": series_id, "updated": True}

    @app.get(f"{APP_PREFIX}/v1/emby/series/{{series_id}}/episode-comparison")
    def episode_comparison(series_id: int, _: str = Depends(require_admin)) -> dict[str, object]:
        result = manager.episode_comparison(series_id)
        if result is None:
            raise HTTPException(status_code=404, detail="电视剧不存在或已失效")
        return result

    @app.post(f"{APP_PREFIX}/v1/emby/series/sync")
    async def sync_series(_: str = Depends(require_admin)) -> dict[str, int]:
        return await _remote_operation(run_series_sync())

    @app.patch(f"{APP_PREFIX}/v1/emby/series/{{series_id}}/tracking")
    def set_series_tracking(
        series_id: int, change: TrackingChange, _: str = Depends(require_admin)
    ) -> dict[str, object]:
        if not manager.set_series_tracking(series_id, change.tracking):
            raise HTTPException(status_code=404, detail="电视剧不存在或已失效")
        return {"id": series_id, "tracking": change.tracking}

    @app.post(f"{APP_PREFIX}/v1/emby/series/tracking/sync")
    async def sync_series_tracking(_: str = Depends(require_admin)) -> dict[str, object]:
        if not settings.tmdb_api_token and not (
            settings.moviepilot_base_url and settings.moviepilot_api_token
        ):
            raise HTTPException(status_code=409, detail="未配置 TMDB 或 MoviePilot，无法同步追更数据")
        return await _remote_operation(run_full_tracking_sync())

    @app.post(f"{APP_PREFIX}/v1/emby/series/{{series_id}}/sync")
    async def sync_one_series(series_id: int, _: str = Depends(require_admin)) -> dict[str, object]:
        return await _remote_operation(run_one_series_sync(series_id))

    @app.patch(f"{APP_PREFIX}/v1/emby/users/{{user_id}}/policy")
    async def update_policy(
        user_id: str, change: PolicyChange, _: str = Depends(require_admin)
    ) -> dict[str, object]:
        try:
            user = await manager.update_user_policy(
                user_id, change.is_disabled, change.enable_remote_access
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (httpx.HTTPError, RuntimeError, ValueError) as error:
            logger.exception("emby_policy_update_failed user_id=%s", user_id)
            raise HTTPException(status_code=502, detail="Emby 用户策略更新失败") from error
        return {"id": user.get("Id", user_id), "policy": user.get("Policy", {})}

    @app.get(f"{APP_PREFIX}/v1/emby/watch-logs")
    def list_watch_logs(
        page: int = Query(1, ge=1), size: int = Query(30, ge=1, le=100),
        query: str | None = Query(None, max_length=100), item_type: str | None = Query(None, max_length=50),
        start_at: str | None = None, end_at: str | None = None,
        item_id: str | None = Query(None, max_length=255), _: str = Depends(require_admin),
    ) -> dict[str, object]:
        return manager.list_watch_logs(page, size, query, item_type, start_at, end_at, item_id)

    @app.post(f"{APP_PREFIX}/v1/emby/watch-logs/sync")
    async def sync_watch_logs(_: str = Depends(require_admin)) -> dict[str, int]:
        return await _remote_operation(run_watch_sync())

    return app


async def _remote_operation(operation) -> dict[str, object]:
    try:
        return await operation
    except EmbyItemNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (httpx.HTTPError, RuntimeError, ValueError) as error:
        logger.exception("emby_remote_operation_failed")
        raise HTTPException(status_code=502, detail="Emby API 请求失败") from error


async def _periodic_sync(
    name: str,
    interval_seconds: int,
    operation,
    stop_event: asyncio.Event,
    *,
    run_immediately: bool = True,
) -> None:
    """Run on the configured interval, optionally starting with an immediate run."""
    if not run_immediately:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
    while not stop_event.is_set():
        try:
            result = await operation()
            logger.info("emby_periodic_sync_complete target=%s result=%s", name, result)
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, RuntimeError, ValueError):
            logger.exception("emby_periodic_sync_failed target=%s", name)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
