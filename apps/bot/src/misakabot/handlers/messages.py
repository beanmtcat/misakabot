from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import Message

from ..domain import Action, MessageInput, ModerationOutcome
from ..group_reply import GroupReplyService
from ..onboarding import OnboardingService
from ..service import ModerationService
from .commands import is_management_command

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def review_reason(outcome: ModerationOutcome) -> str:
    """Format a concise operator-facing reason without repeating the source post."""
    verdict = outcome.verdict
    if verdict is not None and verdict.reason:
        reason = " ".join(verdict.reason.split())[:180]
        confidence = round(verdict.confidence * 100)
        category = " ".join(verdict.category.split())[:40] or "广告"
        return f"AI 判定「{category}」{confidence}%：{reason}"
    reasons = outcome.signals.reasons
    detail = "；".join(" ".join(str(reason).split()) for reason in reasons if reason)[:180]
    return f"规则初筛：{detail or '需要管理员人工判断'}"


def build_message_router(
    service: ModerationService,
    onboarding: OnboardingService,
    allowed_group_ids: frozenset[int],
    replies: GroupReplyService,
) -> Router:
    router = Router(name="messages")

    async def moderate_incoming(message: Message) -> None:
        if (
            message.from_user is None
            or message.from_user.is_bot
            or message.chat.type not in {"group", "supergroup"}
            or message.chat.id not in allowed_group_ids
        ):
            return
        incoming = MessageInput(
            chat_id=message.chat.id,
            message_id=message.message_id,
            user_id=message.from_user.id,
            username=message.from_user.username,
            display_name=message.from_user.full_name,
            text=message.text or message.caption or "",
            media_type=message.content_type if message.content_type != "text" else None,
        )
        is_group_admin = False
        try:
            is_group_admin = await service.gateway.is_group_administrator(
                message.chat.id, message.from_user.id
            )
            if is_group_admin:
                event_id = service.record_administrator_message(incoming)
                logger.info(
                    "message.administrator_recorded chat_id=%s message_id=%s user_id=%s event_id=%s",
                    message.chat.id,
                    message.message_id,
                    message.from_user.id,
                    event_id,
                )
        except Exception as error:
            # Failing open is deliberate: an unknown administrator must never be
            # auto-moderated just because Telegram's membership lookup failed.
            logger.warning(
                "message.group_admin_lookup_deferred chat_id=%s user_id=%s reason=%s",
                message.chat.id,
                message.from_user.id,
                error,
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
                incoming,
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
                        message.chat.id, message.message_id, outcome.event_id, review_reason(outcome)
                    )
                    _ = service.repository.set_moderation_review_message(
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
            if not is_group_admin and (
                is_management_command(message, replies.bot_username)
                or replies.addressed_memory_action(message) is not None
            ):
                # Admin-only commands from members get no reply; removing them keeps
                # menu taps and probing from cluttering the group.
                try:
                    await service.gateway.delete_message(message.chat.id, message.message_id)
                except Exception as error:
                    logger.info(
                        "message.non_admin_command_delete_failed chat_id=%s message_id=%s reason=%s",
                        message.chat.id,
                        message.message_id,
                        error,
                    )
                return
            await replies.handle(message)

    _ = router.message.register(moderate_incoming)

    return router
