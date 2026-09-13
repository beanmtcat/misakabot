from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field, ValidationError

from .auth import LoginRateLimiter
from .config import Settings
from .emby_client import EmbyClient, EmbyItemNotFoundError
from .moviepilot_client import MoviePilotClient
from .repository import LegacyEmbyRepository
from .service import EmbyManagementService
from .tmdb_client import TmdbClient

logger = logging.getLogger(__name__)
APP_PREFIX = "/emby-manager"
PATH_MAP_SIGNATURE_MAX_AGE_SECONDS = 300
PATH_MAP_NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}")
NETWORK_SYNC_MAX_BODY_BYTES = 16 * 1024


def _path_map_signature_payload(
    method: str, raw_path: str, query: str, timestamp: str, nonce: str
) -> bytes:
    return "\n".join((method.upper(), raw_path, query, timestamp, nonce)).encode("utf-8")


def _path_map_signature(
    secret: str, method: str, raw_path: str, query: str, timestamp: str, nonce: str
) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        _path_map_signature_payload(method, raw_path, query, timestamp, nonce),
        hashlib.sha256,
    ).hexdigest()


def _network_sync_signature(
    secret: str, method: str, raw_path: str, query: str, timestamp: str, nonce: str, body: bytes
) -> str:
    body_digest = hashlib.sha256(body).hexdigest()
    payload = "\n".join(
        (method.upper(), raw_path, query, timestamp, nonce, body_digest)
    ).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


class SessionRegistry:
    """In-process revocation for signed browser sessions.

    Starlette already validates the cookie signature and its 24-hour age.  The
    registry adds immediate revocation while this process is running, but a
    deployment must not force every administrator to authenticate again.
    """

    def __init__(self, max_age_seconds: int) -> None:
        self._max_age_seconds = max_age_seconds
        self._sessions: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def issue(self, username: str) -> str:
        session_id = secrets.token_urlsafe(32)
        async with self._lock:
            self._purge()
            self._sessions[session_id] = (username, time.monotonic() + self._max_age_seconds)
        return session_id

    async def is_active(self, session_id: str, username: str) -> bool:
        async with self._lock:
            self._purge()
            value = self._sessions.get(session_id)
            if value is None:
                # A signed, unexpired cookie can only have been created by this
                # service. Re-register it after a process restart; the caller
                # still verifies the account's current management permission.
                self._sessions[session_id] = (username, time.monotonic() + self._max_age_seconds)
                return True
            return value[0] == username

    async def revoke(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)

    async def revoke_user(self, username: str) -> None:
        async with self._lock:
            self._purge()
            for session_id, (known_username, _) in list(self._sessions.items()):
                if known_username == username:
                    del self._sessions[session_id]

    def _purge(self) -> None:
        now = time.monotonic()
        for session_id, (_, expires_at) in list(self._sessions.items()):
            if expires_at <= now:
                del self._sessions[session_id]


class PolicyChange(BaseModel):
    is_disabled: bool | None = None
    enable_remote_access: bool | None = None


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=256)


class NetworkStat(BaseModel):
    date: date
    rx: int = Field(ge=0)
    tx: int = Field(ge=0)
    avg_rate: float = Field(default=0, ge=0, le=10_000_000)


class NetworkStatsSyncRequest(BaseModel):
    interface: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9_.:-]+$")
    data: list[NetworkStat] = Field(min_length=1, max_length=31)


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
        settings.moviepilot_database_url,
        settings.moviepilot_media_path_mappings,
    )
    sync_lock = asyncio.Lock()
    session_registry = SessionRegistry(86400)
    login_rate_limiter = LoginRateLimiter(
        settings.login_rate_limit_attempts, settings.login_rate_limit_window_seconds
    )
    path_map_nonce_lock = asyncio.Lock()
    path_map_seen_nonces: dict[str, int] = {}
    network_sync_nonce_lock = asyncio.Lock()
    network_sync_seen_nonces: dict[str, int] = {}

    def audit_address(request: Request) -> str:
        return request.headers.get("X-Real-IP", "").strip() or (
            request.client.host if request.client else "unknown"
        )

    def require_same_origin(request: Request) -> None:
        origin = request.headers.get("Origin", "").rstrip("/").lower()
        if origin != settings.manager_origin:
            logger.warning("security.origin_rejected remote_addr=%s origin=%r", audit_address(request), origin)
            raise HTTPException(status_code=403, detail="请求来源不被允许")

    async def require_management_admin(request: Request) -> str:
        username = request.session.get("username")
        session_id = request.session.get("session_id")
        if not isinstance(username, str) or not isinstance(session_id, str):
            raise HTTPException(status_code=401, detail="请先登录")
        if (
            username.casefold() not in settings.manager_admin_usernames
            or not await session_registry.is_active(session_id, username)
            or not manager.is_login_enabled(username)
        ):
            await session_registry.revoke(session_id)
            request.session.clear()
            logger.warning("security.session_revoked username=%s remote_addr=%s", username, audit_address(request))
            raise HTTPException(status_code=401, detail="登录会话已失效")
        return username

    async def require_write_authorization(request: Request) -> str:
        username = await require_management_admin(request)
        require_same_origin(request)
        expected_csrf_token = request.session.get("csrf_token")
        supplied_csrf_token = request.headers.get("X-CSRF-Token", "")
        if not isinstance(expected_csrf_token, str) or not secrets.compare_digest(
            supplied_csrf_token, expected_csrf_token
        ):
            logger.warning("security.csrf_rejected username=%s remote_addr=%s", username, audit_address(request))
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
        return username
    sync_status: dict[str, dict[str, object | None]] = {
        "users": {"last_success_at": None, "last_result": None},
        "movies": {"last_success_at": None, "last_result": None},
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

    async def run_movie_sync() -> dict[str, int]:
        async with sync_lock:
            result = await manager.sync_movies()
            sync_status["movies"] = {
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
                    "movies",
                    settings.movie_sync_interval_seconds,
                    run_movie_sync,
                    stop_event,
                    run_immediately=False,
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
    async def login(payload: LoginRequest, request: Request) -> dict[str, str]:
        username = payload.username.strip()
        require_same_origin(request)
        rate_limit_key = f"{audit_address(request)}:{username.casefold()}"
        retry_after = login_rate_limiter.retry_after(rate_limit_key)
        if retry_after:
            logger.warning(
                "security.login_rate_limited username=%s remote_addr=%s retry_after=%s",
                username,
                audit_address(request),
                retry_after,
            )
            raise HTTPException(
                status_code=429,
                detail="登录尝试过于频繁，请稍后再试",
                headers={"Retry-After": str(retry_after)},
            )
        if (
            not username
            or username.casefold() not in settings.manager_admin_usernames
            or not payload.password
            or not manager.authenticate(username, payload.password)
        ):
            login_rate_limiter.record_failure(rate_limit_key)
            logger.warning("security.login_failed username=%s remote_addr=%s", username, audit_address(request))
            raise HTTPException(status_code=401, detail="用户名或密码不正确")
        request.session.clear()
        await session_registry.revoke_user(username)
        request.session["session_id"] = await session_registry.issue(username)
        request.session["username"] = username
        csrf_token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = csrf_token
        login_rate_limiter.reset(rate_limit_key)
        logger.info("security.login_succeeded username=%s remote_addr=%s", username, audit_address(request))
        return {"username": username, "csrf_token": csrf_token}

    @app.get(f"{APP_PREFIX}/auth/session")
    async def session(request: Request, username: str = Depends(require_management_admin)) -> dict[str, str]:
        csrf_token = request.session.get("csrf_token")
        if not isinstance(csrf_token, str):
            raise HTTPException(status_code=401, detail="登录会话已失效")
        return {"username": username, "csrf_token": csrf_token}

    @app.post(f"{APP_PREFIX}/auth/logout")
    async def logout(request: Request, _: str = Depends(require_write_authorization)) -> Response:
        session_id = request.session.get("session_id")
        if isinstance(session_id, str):
            await session_registry.revoke(session_id)
        request.session.clear()
        return Response(status_code=204)

    @app.get(f"{APP_PREFIX}/v1/emby/users")
    def list_users(
        page: int = Query(1, ge=1), size: int = Query(30, ge=1, le=100),
        query: str | None = Query(None, max_length=100), is_disabled: bool | None = None,
        _: str = Depends(require_management_admin),
    ) -> dict[str, object]:
        return manager.list_users(page, size, query, is_disabled)

    @app.post(f"{APP_PREFIX}/v1/emby/users/sync")
    async def sync_users(_: str = Depends(require_write_authorization)) -> dict[str, int]:
        return await _remote_operation(run_user_sync())

    @app.get(f"{APP_PREFIX}/v1/emby/login-logs")
    def list_login_logs(
        page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
        query: str | None = Query(None, max_length=100), _: str = Depends(require_management_admin),
    ) -> dict[str, object]:
        return manager.list_login_logs(page, size, query)

    @app.post(f"{APP_PREFIX}/v1/emby/login-logs/sync")
    async def sync_login_logs(_: str = Depends(require_write_authorization)) -> dict[str, int]:
        return await _remote_operation(run_login_sync())

    @app.get(f"{APP_PREFIX}/v1/emby/sync-status")
    def get_sync_status(_: str = Depends(require_management_admin)) -> dict[str, dict[str, object | None]]:
        return sync_status

    @app.get(f"{APP_PREFIX}/v1/emby/dashboard")
    def dashboard(_: str = Depends(require_management_admin)) -> dict[str, object]:
        return manager.dashboard()

    @app.get(f"{APP_PREFIX}/v1/emby/movies")
    def list_movies(
        page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
        query: str | None = Query(None, max_length=100), _: str = Depends(require_management_admin),
    ) -> dict[str, object]:
        return manager.list_movies(page, size, query)

    @app.post(f"{APP_PREFIX}/v1/emby/movies/sync")
    async def sync_movies(_: str = Depends(require_write_authorization)) -> dict[str, int]:
        return await _remote_operation(run_movie_sync())

    @app.get(f"{APP_PREFIX}/v1/emby/items/{{item_id}}/open", include_in_schema=False)
    def open_emby_item(
        item_id: str,
        server_id: str | None = Query(None, max_length=100),
        _: str = Depends(require_management_admin),
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
        _: str = Depends(require_management_admin),
    ) -> dict[str, object]:
        return manager.list_series(page, None if all_items else size, query, tracking, state)

    @app.get(f"{APP_PREFIX}/v1/emby/libraries")
    def list_libraries(_: str = Depends(require_management_admin)) -> dict[str, object]:
        return {"items": manager.list_libraries()}

    async def require_path_map_request_signature(request: Request) -> None:
        """Authenticate the remote path-map pull without transmitting the secret."""
        signing_secret = settings.path_map_api_secret
        if not signing_secret:
            raise HTTPException(status_code=503, detail="未配置路径映射 API 签名密钥")

        timestamp = request.headers.get("X-Path-Map-Timestamp", "")
        nonce = request.headers.get("X-Path-Map-Nonce", "")
        supplied_signature = request.headers.get("X-Path-Map-Signature", "")
        try:
            timestamp_value = int(timestamp)
        except ValueError:
            raise HTTPException(status_code=401, detail="路径映射 API 请求签名无效") from None
        now = int(time.time())
        if (
            abs(now - timestamp_value) > PATH_MAP_SIGNATURE_MAX_AGE_SECONDS
            or not PATH_MAP_NONCE_PATTERN.fullmatch(nonce)
            or not re.fullmatch(r"[0-9a-f]{64}", supplied_signature)
        ):
            raise HTTPException(status_code=401, detail="路径映射 API 请求签名无效")

        raw_path = request.scope.get("raw_path", request.url.path.encode("utf-8")).decode(
            "ascii", "surrogateescape"
        )
        expected_signature = _path_map_signature(
            signing_secret,
            request.method,
            raw_path,
            request.url.query,
            timestamp,
            nonce,
        )
        if not secrets.compare_digest(supplied_signature, expected_signature):
            raise HTTPException(status_code=401, detail="路径映射 API 请求签名无效")

        async with path_map_nonce_lock:
            expired_nonces = [
                known_nonce
                for known_nonce, expires_at in path_map_seen_nonces.items()
                if expires_at <= now
            ]
            for known_nonce in expired_nonces:
                del path_map_seen_nonces[known_nonce]
            if nonce in path_map_seen_nonces:
                raise HTTPException(status_code=401, detail="路径映射 API 请求签名无效")
            path_map_seen_nonces[nonce] = now + PATH_MAP_SIGNATURE_MAX_AGE_SECONDS

    async def require_network_sync_signature(request: Request) -> None:
        signing_secret = settings.path_map_api_secret
        if not signing_secret:
            raise HTTPException(status_code=503, detail="未配置网络流量同步签名密钥")
        content_length = request.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > NETWORK_SYNC_MAX_BODY_BYTES:
                    raise HTTPException(status_code=413, detail="网络流量同步请求过大")
            except ValueError:
                raise HTTPException(status_code=400, detail="网络流量同步请求长度无效") from None
        timestamp = request.headers.get("X-Emby-Timestamp", "")
        nonce = request.headers.get("X-Emby-Nonce", "")
        supplied_signature = request.headers.get("X-Emby-Signature", "")
        try:
            timestamp_value = int(timestamp)
        except ValueError:
            raise HTTPException(status_code=401, detail="网络流量同步请求签名无效") from None
        now = int(time.time())
        if (
            abs(now - timestamp_value) > PATH_MAP_SIGNATURE_MAX_AGE_SECONDS
            or not PATH_MAP_NONCE_PATTERN.fullmatch(nonce)
            or not re.fullmatch(r"[0-9a-f]{64}", supplied_signature)
        ):
            raise HTTPException(status_code=401, detail="网络流量同步请求签名无效")
        raw_path = request.scope.get("raw_path", request.url.path.encode("utf-8")).decode(
            "ascii", "surrogateescape"
        )
        body = await request.body()
        if len(body) > NETWORK_SYNC_MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="网络流量同步请求过大")
        expected_signature = _network_sync_signature(
            signing_secret,
            request.method,
            raw_path,
            request.url.query,
            timestamp,
            nonce,
            body,
        )
        if not secrets.compare_digest(supplied_signature, expected_signature):
            raise HTTPException(status_code=401, detail="网络流量同步请求签名无效")
        async with network_sync_nonce_lock:
            expired_nonces = [
                known_nonce
                for known_nonce, expires_at in network_sync_seen_nonces.items()
                if expires_at <= now
            ]
            for known_nonce in expired_nonces:
                del network_sync_seen_nonces[known_nonce]
            if nonce in network_sync_seen_nonces:
                raise HTTPException(status_code=401, detail="网络流量同步请求签名无效")
            network_sync_seen_nonces[nonce] = now + PATH_MAP_SIGNATURE_MAX_AGE_SECONDS

    @app.post(f"{APP_PREFIX}/v1/emby/network-stats/sync")
    async def sync_network_stats(
        request: Request,
        _: None = Depends(require_network_sync_signature),
    ) -> dict[str, object]:
        try:
            payload = NetworkStatsSyncRequest.model_validate_json(await request.body())
        except ValidationError as error:
            raise HTTPException(status_code=422, detail=json.loads(error.json())) from None
        upserted = manager.sync_network_stats(
            payload.interface,
            [(item.date, item.rx, item.tx, item.avg_rate) for item in payload.data],
        )
        return {"interface": payload.interface, "upserted": upserted}

    @app.get(
        f"{APP_PREFIX}/v1/emby/nodes/{{node_id}}/series-paths",
        response_model=None,
    )
    async def series_paths_for_node(
        node_id: str,
        format: str = Query("text", pattern="^(text|json)$"),
        _: None = Depends(require_path_map_request_signature),
    ) -> PlainTextResponse | JSONResponse:
        if format == "json":
            return JSONResponse({"items": await manager.series_path_entries_for_node(node_id)})
        return PlainTextResponse("\n".join(await manager.series_path_lines_for_node(node_id)))

    @app.get(f"{APP_PREFIX}/v1/emby/series/{{series_id}}")
    def series_detail(series_id: int, _: str = Depends(require_management_admin)) -> dict[str, object]:
        detail = manager.series_detail(series_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="电视剧不存在或已失效")
        return detail

    @app.put(f"{APP_PREFIX}/v1/emby/series/{{series_id}}")
    def update_series_detail(
        series_id: int, change: SeriesDetailChange, _: str = Depends(require_write_authorization)
    ) -> dict[str, object]:
        if not manager.update_series_detail(series_id, change.model_dump()):
            raise HTTPException(status_code=404, detail="电视剧不存在或已失效")
        return {"id": series_id, "updated": True}

    @app.get(f"{APP_PREFIX}/v1/emby/series/{{series_id}}/episode-comparison")
    def episode_comparison(series_id: int, _: str = Depends(require_management_admin)) -> dict[str, object]:
        result = manager.episode_comparison(series_id)
        if result is None:
            raise HTTPException(status_code=404, detail="电视剧不存在或已失效")
        return result

    @app.post(f"{APP_PREFIX}/v1/emby/series/sync")
    async def sync_series(_: str = Depends(require_write_authorization)) -> dict[str, int]:
        return await _remote_operation(run_series_sync())

    @app.patch(f"{APP_PREFIX}/v1/emby/series/{{series_id}}/tracking")
    def set_series_tracking(
        series_id: int, change: TrackingChange, _: str = Depends(require_write_authorization)
    ) -> dict[str, object]:
        if not manager.set_series_tracking(series_id, change.tracking):
            raise HTTPException(status_code=404, detail="电视剧不存在或已失效")
        return {"id": series_id, "tracking": change.tracking}

    @app.post(f"{APP_PREFIX}/v1/emby/series/tracking/sync")
    async def sync_series_tracking(_: str = Depends(require_write_authorization)) -> dict[str, object]:
        if not settings.tmdb_api_token and not (
            settings.moviepilot_base_url and settings.moviepilot_api_token
        ):
            raise HTTPException(status_code=409, detail="未配置 TMDB 或 MoviePilot，无法同步追更数据")
        return await _remote_operation(run_full_tracking_sync())

    @app.post(f"{APP_PREFIX}/v1/emby/series/{{series_id}}/sync")
    async def sync_one_series(series_id: int, _: str = Depends(require_write_authorization)) -> dict[str, object]:
        return await _remote_operation(run_one_series_sync(series_id))

    @app.patch(f"{APP_PREFIX}/v1/emby/users/{{user_id}}/policy")
    async def update_policy(
        user_id: str, change: PolicyChange, _: str = Depends(require_write_authorization)
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
        item_id: str | None = Query(None, max_length=255), _: str = Depends(require_management_admin),
    ) -> dict[str, object]:
        return manager.list_watch_logs(page, size, query, item_type, start_at, end_at, item_id)

    @app.post(f"{APP_PREFIX}/v1/emby/watch-logs/sync")
    async def sync_watch_logs(_: str = Depends(require_write_authorization)) -> dict[str, int]:
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
