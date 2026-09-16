from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import Message

from ..domain import Action, MessageInput
from ..group_reply import GroupReplyService
from ..onboarding import OnboardingService
from ..service import ModerationService

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def build_message_router(
    service: ModerationService,
    onboarding: OnboardingService,
    allowed_group_ids: frozenset[int],
    replies: GroupReplyService,
) -> Router:
    router = Router(name="messages")

    @router.message()
    async def moderate_incoming(message: Message) -> None:
        if (
            message.from_user is None
            or message.from_user.is_bot
            or message.chat.type not in {"group", "supergroup"}
            or message.chat.id not in allowed_group_ids
        ):
            return
        is_group_admin = False
        try:
            is_group_admin = await service.gateway.is_group_administrator(
                message.chat.id, message.from_user.id
            )
            if is_group_admin:
                logger.info(
                    "message.moderation_skipped_group_admin chat_id=%s message_id=%s user_id=%s",
                    message.chat.id,
                    message.message_id,
                    message.from_user.id,
                )
        except Exception:
            # Failing open is deliberate: an unknown administrator must never be
            # auto-moderated just because Telegram's membership lookup failed.
            logger.exception(
                "message.group_admin_lookup_failed chat_id=%s user_id=%s",
                message.chat.id,
                message.from_user.id,
            )
            return
        outcome = None
        if not is_group_admin:
            is_first_observed = onboarding.mark_first_observed_message(
                message.chat.id, message.from_user.id, message.date
            )
            logger.info(
                "message.received chat_id=%s message_id=%s user_id=%s first_observed=%s media_type=%s text_length=%s",
                message.chat.id,
                message.message_id,
                message.from_user.id,
                is_first_observed,
                message.content_type,
                len(message.text or message.caption or ""),
            )
            outcome = await service.moderate(
                MessageInput(
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    user_id=message.from_user.id,
                    username=message.from_user.username,
                    display_name=message.from_user.full_name,
                    text=message.text or message.caption or "",
                    media_type=message.content_type if message.content_type != "text" else None,
                ),
                force_llm_review=is_first_observed,
            )
            logger.info(
                "message.moderated chat_id=%s message_id=%s action=%s event_id=%s",
                message.chat.id,
                message.message_id,
                outcome.action,
                outcome.event_id,
            )
            if outcome.action is Action.NEEDS_REVIEW and outcome.event_id is not None:
                try:
                    review_message_id = await service.gateway.send_moderation_review(
                        message.chat.id, message.message_id, outcome.event_id
                    )
                    service.repository.set_moderation_review_message(
                        outcome.event_id, message.chat.id, review_message_id
                    )
                except Exception:
                    # The audit event remains pending even if Telegram cannot accept the
                    # card, so an administrator can still resolve it from the audit UI.
                    logger.exception(
                        "moderation.review_card_failed chat_id=%s message_id=%s event_id=%s",
                        message.chat.id, message.message_id, outcome.event_id,
                    )

        if outcome is None or outcome.action in {Action.ALLOW, Action.RELEASE}:
            await replies.handle(message)

    return router
