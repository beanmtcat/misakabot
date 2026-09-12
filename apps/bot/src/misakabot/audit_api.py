from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Mapping, Protocol

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from .config import Settings
from .repository import AuditRepository, database_integer
from .telegram_init_data import InitDataError, TelegramIdentity, verify_init_data

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class ReviewAction(BaseModel):
    note: str = ""


GroupAdminChecker = Callable[[int, int], Awaitable[bool]]


class AuditModerationGateway(Protocol):
    async def confirm_ban(self, chat_id: int, user_id: int, message_id: int) -> None: ...

    async def restore_member(self, chat_id: int, user_id: int) -> None: ...


class TelegramAuditModerationGateway:
    """Telegram actions executed by the separately deployed audit-api service."""

    def __init__(self, bot_token: str) -> None:
        self.bot_token = bot_token

    async def _call(self, method: str, payload: dict[str, object]) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.bot_token}/{method}", json=payload
            )
            response.raise_for_status()
        result = response.json()
        if result.get("ok") is not True:
            raise RuntimeError(f"Telegram {method} rejected: {result.get('description', 'unknown error')}")

    async def confirm_ban(self, chat_id: int, user_id: int, message_id: int) -> None:
        # Ban first: a failed delete leaves the user contained and the event pending for retry.
        await self._call("banChatMember", {"chat_id": chat_id, "user_id": user_id})
        await self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    async def restore_member(self, chat_id: int, user_id: int) -> None:
        await self._call("unbanChatMember", {"chat_id": chat_id, "user_id": user_id, "only_if_banned": True})
        await self._call(
            "restrictChatMember",
            {
                "chat_id": chat_id,
                "user_id": user_id,
                "permissions": {
                    "can_send_messages": True,
                    "can_send_audios": True,
                    "can_send_documents": True,
                    "can_send_photos": True,
                    "can_send_videos": True,
                    "can_send_video_notes": True,
                    "can_send_voice_notes": True,
                    "can_send_polls": True,
                    "can_send_other_messages": True,
                    "can_add_web_page_previews": True,
                    "can_change_info": False,
                    "can_invite_users": True,
                    "can_pin_messages": False,
                    "can_manage_topics": False,
                },
            },
        )


def create_app(
    settings: Settings,
    repository: AuditRepository,
    group_admin_checker: GroupAdminChecker | None = None,
    moderation_gateway: AuditModerationGateway | None = None,
) -> FastAPI:
    app = FastAPI(title="Misaka Guard Audit API", version="0.1.0")
    checker = group_admin_checker or _build_group_admin_checker(settings)
    gateway = moderation_gateway or TelegramAuditModerationGateway(settings.telegram_bot_token)

    async def require_scoped_admin(
        chat_id: int = Query(...),
        init_data: str = Header(alias="X-Telegram-Init-Data"),
    ) -> TelegramIdentity:
        try:
            identity = verify_init_data(init_data, settings.telegram_bot_token)
        except InitDataError as error:
            logger.warning("audit_api.invalid_init_data reason=%s", error)
            raise HTTPException(status_code=401, detail=str(error)) from error
        if chat_id not in settings.allowed_group_ids:
            logger.warning("audit_api.scope_denied user_id=%s chat_id=%s", identity.user_id, chat_id)
            raise HTTPException(status_code=403, detail="Chat is not enabled for audit")
        is_allowed = identity.user_id in settings.admin_user_ids or await checker(identity.user_id, chat_id)
        if not is_allowed:
            logger.warning("audit_api.access_denied user_id=%s chat_id=%s", identity.user_id, chat_id)
            raise HTTPException(status_code=403, detail="Telegram user is not an administrator")
        return identity

    @app.get("/healthz")
    def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/audit/summary")
    def summary(
        chat_id: int = Query(...),
        _: TelegramIdentity = Depends(require_scoped_admin),
    ) -> dict[str, int]:
        rows = repository.query_all(
            "SELECT action, COUNT(*) AS count FROM moderation_events WHERE chat_id = %s GROUP BY action",
            (chat_id,),
        )
        counts = {str(row["action"]): database_integer(row["count"]) for row in rows}
        return {
            "permanent_bans": counts.get("permanent_ban", 0),
            "pending_review": counts.get("needs_review", 0),
            "quarantined": counts.get("quarantine", 0),
            "total": sum(counts.values()),
        }

    @app.get("/v1/audit/events")
    def list_events(
        chat_id: int = Query(...),
        status: str | None = None,
        action_filter: str | None = Query(default=None, alias="filter"),
        query: str | None = Query(default=None, max_length=100),
        before_id: int | None = Query(default=None, gt=0),
        limit: int = Query(default=30, ge=1, le=100),
        _: TelegramIdentity = Depends(require_scoped_admin),
    ) -> dict[str, object]:
        clauses = ["chat_id = %s"]
        parameters: list[object] = [chat_id]
        if before_id is not None:
            clauses.append("id < %s")
            parameters.append(before_id)
        if status:
            clauses.append("review_status = %s")
            parameters.append(status)
        if action_filter == "banned":
            clauses.append("action = %s")
            parameters.append("permanent_ban")
        elif action_filter == "review":
            clauses.append("(action = %s OR action = %s)")
            parameters.extend(["needs_review", "quarantine"])
        elif action_filter == "restored":
            clauses.append("(action = %s OR review_status = %s)")
            parameters.extend(["release", "false_positive"])
        elif action_filter == "allowed":
            clauses.append("action = %s")
            parameters.append("allow")
        elif action_filter:
            raise HTTPException(status_code=422, detail="Unknown audit filter")
        if query:
            clauses.append("(raw_text LIKE %s OR username LIKE %s)")
            parameters.extend([f"%{query}%", f"%{query}%"])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT id, chat_id, message_id, user_id, username, raw_text, normalized_text,
                   urls_json, signals_json, verdict_json, action, review_status, created_at, updated_at
            FROM moderation_events {where}
            ORDER BY id DESC LIMIT %s
        """
        # One extra row tells the client whether another cursor page exists.
        parameters.append(limit + 1)
        rows = repository.query_all(sql, parameters)
        has_more = len(rows) > limit
        page = rows[:limit]
        return {
            "items": [_serialize_event(row) for row in page],
            "next_before_id": page[-1]["id"] if has_more and page else None,
        }

    @app.get("/v1/audit/events/{event_id}")
    def get_event(
        event_id: int,
        chat_id: int = Query(...),
        _: TelegramIdentity = Depends(require_scoped_admin),
    ) -> dict[str, object]:
        row = repository.query_one(
            "SELECT * FROM moderation_events WHERE id = %s AND chat_id = %s",
            (event_id, chat_id),
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Audit event not found")
        return _serialize_event(row)

    @app.post("/v1/audit/events/{event_id}/confirm-ban")
    async def confirm_ban(
        event_id: int,
        _: ReviewAction,
        chat_id: int = Query(...),
        admin: TelegramIdentity = Depends(require_scoped_admin),
    ) -> dict[str, object]:
        event = _get_event_target(repository, event_id, chat_id)
        try:
            await gateway.confirm_ban(chat_id, event["user_id"], event["message_id"])
        except Exception as error:
            logger.exception("audit_api.confirm_ban_telegram_failed event_id=%s", event_id)
            raise HTTPException(status_code=502, detail="Telegram 封禁或删消息失败，审计记录未更新") from error
        repository.block_user(
            event["user_id"], "管理员在审计页确认封禁", datetime.now(timezone.utc).isoformat()
        )
        return _update_review(repository, event_id, chat_id, "confirmed", "permanent_ban", admin)

    @app.post("/v1/audit/events/{event_id}/restore")
    async def restore(
        event_id: int,
        _: ReviewAction,
        chat_id: int = Query(...),
        admin: TelegramIdentity = Depends(require_scoped_admin),
    ) -> dict[str, object]:
        event = _get_event_target(repository, event_id, chat_id)
        try:
            await gateway.restore_member(chat_id, event["user_id"])
        except Exception as error:
            logger.exception("audit_api.restore_telegram_failed event_id=%s", event_id)
            raise HTTPException(status_code=502, detail="Telegram 恢复权限失败，审计记录未更新") from error
        repository.unblock_user(event["user_id"])
        return _update_review(repository, event_id, chat_id, "false_positive", "release", admin)

    return app


def _build_group_admin_checker(settings: Settings) -> GroupAdminChecker:
    async def is_group_administrator(user_id: int, chat_id: int) -> bool:
        if chat_id not in settings.allowed_group_ids:
            return False
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                response = await client.get(
                    f"https://api.telegram.org/bot{settings.telegram_bot_token}/getChatMember",
                    params={"chat_id": chat_id, "user_id": user_id},
                )
                response.raise_for_status()
                result = response.json()
                status = result.get("result", {}).get("status")
                return result.get("ok") is True and status in {"creator", "owner", "administrator"}
            except (httpx.HTTPError, ValueError, TypeError):
                logger.warning(
                    "audit_api.group_admin_lookup_failed chat_id=%s user_id=%s", chat_id, user_id
                )
        return False

    return is_group_administrator


def _serialize_event(row: Mapping[str, object]) -> dict[str, object]:
    event = dict(row)
    for key in ("urls_json", "signals_json", "verdict_json"):
        if event[key]:
            value = event.pop(key)
            event[key.removesuffix("_json")] = json.loads(value) if isinstance(value, str) else value
        else:
            event[key.removesuffix("_json")] = None
            event.pop(key)
    return event


def _get_event_target(repository: AuditRepository, event_id: int, chat_id: int) -> dict[str, int]:
    row = repository.query_one(
        "SELECT user_id, message_id FROM moderation_events WHERE id=%s AND chat_id=%s",
        (event_id, chat_id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Audit event not found")
    return {
        "user_id": database_integer(row["user_id"]),
        "message_id": database_integer(row["message_id"]),
    }


def _update_review(
    repository: AuditRepository,
    event_id: int,
    chat_id: int,
    review_status: str,
    action: str,
    admin: TelegramIdentity,
) -> dict[str, object]:
    updated = repository.execute_write(
        "UPDATE moderation_events SET review_status=%s, action=%s, updated_at=%s WHERE id=%s AND chat_id=%s",
        (review_status, action, datetime.now(timezone.utc).isoformat(), event_id, chat_id),
    )
    if updated != 1:
        raise HTTPException(status_code=404, detail="Audit event not found")
    logger.info(
        "audit_api.review_updated event_id=%s action=%s review_status=%s operator=%s",
        event_id, action, review_status, admin.user_id,
    )
    return {"id": event_id, "review_status": review_status, "action": action, "operator": admin.user_id}
