from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import uvicorn
from aiogram import Bot

from .audit_api import create_app
from .bot_commands import register_bot_commands
from .config import Settings
from .gateway import AiogramGateway
from .handlers import build_dispatcher
from .llm import (
    GroupReplyClient,
    KimiCodingGroupReplyClient,
    KimiCodingModerationClient,
    ModerationClient,
    OpenAICompatibleModerationClient,
    RuleBasedModerationClient,
)
from .onboarding import OnboardingService
from .repository import PostgresAuditRepository
from .service import ModerationService
from .webhook import build_webhook_app
from .workers import verification_expiry_worker

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

def build_runtime_repository(settings: Settings) -> PostgresAuditRepository:
    """Production services use PostgreSQL; SQLite is retained only for backup/migration tooling."""
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required; runtime SQLite has been retired")
    return PostgresAuditRepository(settings.database_url)

def configure_logging(log_level: str) -> None:
    level = getattr(logging, log_level, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    logging.getLogger("aiogram").setLevel(level)

def build_moderation_client(settings: Settings) -> ModerationClient:
    if settings.llm_mode == "rule_based":
        return RuleBasedModerationClient()
    if settings.llm_mode == "openai_compatible":
        if not (settings.llm_api_base_url and settings.llm_api_key and settings.llm_model):
            raise RuntimeError("LLM_API_BASE_URL, LLM_API_KEY and LLM_MODEL are required")
        return OpenAICompatibleModerationClient(
            settings.llm_api_base_url, settings.llm_api_key, settings.llm_model
        )
    if settings.llm_mode == "kimi_coding":
        if not (settings.kimi_api_key and settings.kimi_model):
            raise RuntimeError("KIMI_API_KEY and KIMI_MODEL are required for LLM_MODE=kimi_coding")
        return KimiCodingModerationClient(
            api_key=settings.kimi_api_key,
            model=settings.kimi_model,
            base_url=settings.kimi_api_base_url,
        )
    raise RuntimeError(f"Unsupported LLM_MODE: {settings.llm_mode}")


def build_group_reply_client(settings: Settings) -> GroupReplyClient | None:
    """Build the optional conversational client without enabling general chat listening."""
    if not settings.bot_reply_enabled:
        return None
    if settings.llm_mode != "kimi_coding":
        logger.info("group_reply.disabled reason=llm_mode mode=%s", settings.llm_mode)
        return None
    if not (settings.kimi_api_key and settings.kimi_model):
        # build_moderation_client provides the startup error for this invalid Kimi setup.
        return None
    return KimiCodingGroupReplyClient(
        api_key=settings.kimi_api_key,
        model=settings.kimi_model,
        base_url=settings.kimi_api_base_url,
    )

async def run_bot(settings: Settings) -> None:
    configure_logging(settings.log_level)
    repository = build_runtime_repository(settings)
    repository.initialize()
    bot = Bot(settings.telegram_bot_token)
    bot_identity = await bot.get_me()
    gateway = AiogramGateway(bot, repository)
    service = ModerationService(
        repository=repository,
        gateway=gateway,
        llm=build_moderation_client(settings),
        auto_ban_threshold=settings.auto_ban_threshold,
        review_threshold=settings.review_threshold,
    )
    onboarding = OnboardingService(
        repository,
        gateway,
        settings.verification_ttl_minutes,
        settings.join_verify_url,
        settings.secondary_verification_ttl_minutes,
        {
            chat_id: settings.dmit_news_channel_url
            for chat_id in settings.dmit_channel_group_ids
            if settings.dmit_news_channel_url
        },
    )
    await onboarding.resume_pending_direct_join_challenges(settings.allowed_group_ids)
    dispatcher = build_dispatcher(
        bot,
        service,
        onboarding,
        settings.allowed_group_ids,
        settings.admin_user_ids,
        settings.audit_web_app_url,
        settings.dmit_knowledge_group_ids,
        group_reply_client=build_group_reply_client(settings),
        bot_user_id=bot_identity.id,
        bot_username=bot_identity.username,
    )
    logger.info(
        "bot.starting allowed_group_count=%s llm_mode=%s transport=%s group_reply_enabled=%s",
        len(settings.allowed_group_ids),
        settings.llm_mode,
        settings.telegram_transport,
        settings.bot_reply_enabled and settings.llm_mode == "kimi_coding",
    )
    if settings.telegram_transport == "webhook":
        server = uvicorn.Server(
            uvicorn.Config(
                build_webhook_app(settings, bot, dispatcher, repository, onboarding),
                host=settings.bot_api_host,
                port=settings.bot_api_port,
                access_log=True,
            )
        )
        await server.serve()
        return
    await register_bot_commands(bot, settings.allowed_group_ids)
    expiry_task = asyncio.create_task(verification_expiry_worker(onboarding, gateway))
    try:
        await dispatcher.start_polling(bot)
    finally:
        expiry_task.cancel()
        with suppress(asyncio.CancelledError):
            await expiry_task


def main() -> None:
    settings = Settings.from_environment()
    asyncio.run(run_bot(settings))


def run_audit_api() -> None:
    settings = Settings.from_environment()
    configure_logging(settings.log_level)
    repository = build_runtime_repository(settings)
    repository.initialize()
    uvicorn.run(
        create_app(settings, repository),
        host=settings.audit_api_host,
        port=settings.audit_api_port,
        access_log=True,
    )

