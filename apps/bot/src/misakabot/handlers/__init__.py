from __future__ import annotations

from aiogram import Bot, Dispatcher

from ..group_reply import GroupReplyService
from ..llm import GroupReplyClient
from ..onboarding import OnboardingService
from ..service import ModerationService
from .admin import AdminActions
from .commands import build_command_router
from .messages import build_message_router
from .onboarding import build_onboarding_router

def build_dispatcher(
    bot: Bot,
    service: ModerationService,
    onboarding: OnboardingService,
    allowed_group_ids: frozenset[int],
    admin_user_ids: frozenset[int],
    audit_web_app_url: str = "",
    dmit_knowledge_group_ids: frozenset[int] = frozenset(),
    group_reply_client: GroupReplyClient | None = None,
    bot_user_id: int | None = None,
    bot_username: str | None = None,
) -> Dispatcher:
    admin = AdminActions(service.gateway, onboarding.repository, admin_user_ids)
    replies = GroupReplyService(
        bot, onboarding.repository, group_reply_client,
        bot_user_id, bot_username, dmit_knowledge_group_ids,
    )
    dispatcher = Dispatcher()
    # Commands and service messages must precede the catch-all moderation handler.
    dispatcher.include_routers(
        build_command_router(service, onboarding, allowed_group_ids, admin, replies, audit_web_app_url),
        build_onboarding_router(service, onboarding, allowed_group_ids, admin),
        build_message_router(service, onboarding, allowed_group_ids, replies),
    )
    return dispatcher
