from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiogram.types.update import UpdateTypeLookupError
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ValidationError

from .audit_api import create_app
from .bot_commands import register_bot_commands
from .config import Settings
from .gateway import AiogramGateway
from .join_verify import TurnstileVerifier, join_verify_page
from .onboarding import OnboardingService
from .repository import AuditRepository
from .telegram_init_data import InitDataError, verify_init_data
from .workers import verification_expiry_worker

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

class JoinVerifySubmission(BaseModel):
    session_token: str
    init_data: str
    turnstile_token: str

def build_webhook_app(
    settings: Settings,
    bot: Bot,
    dispatcher: Dispatcher,
    repository: AuditRepository,
    onboarding: OnboardingService | None = None,
    turnstile: TurnstileVerifier | None = None,
) -> FastAPI:
    app = create_app(settings, repository)
    verifier = turnstile or TurnstileVerifier(settings.turnstile_secret_key)
    expiry_task: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def webhook_lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal expiry_task
        await register_bot_commands(bot, settings.allowed_group_ids)
        await bot.set_webhook(
            settings.telegram_webhook_url,
            secret_token=settings.telegram_webhook_secret,
            allowed_updates=dispatcher.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        if onboarding is not None:
            # The gateway also drains durable, delayed notice deletions after a restart.
            gateway = onboarding.gateway if isinstance(onboarding.gateway, AiogramGateway) else None
            expiry_task = asyncio.create_task(verification_expiry_worker(onboarding, gateway))
        logger.info("webhook.registered url=%s", settings.telegram_webhook_url)
        try:
            yield
        finally:
            if expiry_task is not None:
                expiry_task.cancel()
                with suppress(asyncio.CancelledError):
                    await expiry_task
            await bot.session.close()

    # create_app also hosts the audit API routes. Supplying the lifespan here keeps
    # both services on the supported FastAPI lifecycle API without duplicating routes.
    app.router.lifespan_context = webhook_lifespan

    @app.post("/telegram/webhook", include_in_schema=False)
    async def receive_telegram_update(
        request: Request,
        secret_token: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    ) -> dict[str, bool]:
        if not hmac.compare_digest(secret_token or "", settings.telegram_webhook_secret):
            logger.warning("webhook.rejected invalid_secret")
            raise HTTPException(status_code=401, detail="invalid webhook secret")
        try:
            update = Update.model_validate(await request.json(), context={"bot": bot})
        except (ValueError, ValidationError) as error:
            logger.warning("webhook.rejected malformed_update error=%s", error)
            raise HTTPException(status_code=400, detail="invalid Telegram update") from error
        try:
            update_type = update.event_type
        except UpdateTypeLookupError:
            update_type = "unknown"
        logger.info("webhook.received update_id=%s type=%s", update.update_id, update_type)
        now = datetime.now(timezone.utc)
        claim = repository.claim_webhook_update(
            update.update_id,
            now.isoformat(),
            (now - timedelta(minutes=2)).isoformat(),
        )
        if claim == "processed":
            logger.info("webhook.duplicate_ignored update_id=%s", update.update_id)
            return {"ok": True}
        if claim == "busy":
            # A concurrent handler still owns this update. Tell Telegram to retry
            # instead of processing the same update twice in parallel.
            logger.warning("webhook.update_busy update_id=%s", update.update_id)
            raise HTTPException(status_code=503, detail="update is already processing")
        try:
            await dispatcher.feed_update(bot, update)
        except Exception:
            repository.release_webhook_update(update.update_id)
            logger.exception("webhook.update_failed update_id=%s type=%s", update.update_id, update_type)
            # Telegram retries non-2xx webhook responses. The persisted claim is released
            # first, so the retry can run instead of silently losing the update.
            raise HTTPException(status_code=500, detail="update handling failed")
        repository.complete_webhook_update(update.update_id, datetime.now(timezone.utc).isoformat())
        logger.info("webhook.processed update_id=%s", update.update_id)
        return {"ok": True}

    if onboarding is not None:
        @app.get("/join-verify/", include_in_schema=False)
        async def show_join_verify_page() -> HTMLResponse:
            if not settings.turnstile_site_key:
                raise HTTPException(status_code=503, detail="Turnstile is not configured")
            return HTMLResponse(join_verify_page(settings.turnstile_site_key))

        @app.post("/join-verify/api/complete", include_in_schema=False)
        async def complete_join_verify(submission: JoinVerifySubmission, request: Request) -> dict[str, object]:
            try:
                identity = verify_init_data(submission.init_data, settings.telegram_bot_token)
            except InitDataError as error:
                logger.warning("join_verify.invalid_init_data reason=%s", error)
                raise HTTPException(status_code=401, detail="Telegram 身份校验失败") from error
            remote_ip = request.client.host if request.client else None
            if not await verifier.verify(submission.turnstile_token, remote_ip):
                outcome = await onboarding.fail_initial_verification(
                    submission.session_token, identity.user_id
                )
                raise HTTPException(status_code=403, detail=outcome.reason)
            outcome = await onboarding.verify_token(submission.session_token, identity.user_id)
            if not outcome.accepted:
                raise HTTPException(status_code=409, detail=outcome.reason)
            logger.info("join_verify.completed user_id=%s", identity.user_id)
            return {"ok": True, "message": outcome.reason}

    return app
