from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery

from ..domain import Action, ReviewStatus
from ..service import ModerationService
from .admin import AdminActions

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def build_moderation_router(
    service: ModerationService,
    allowed_group_ids: frozenset[int],
    admin: AdminActions,
) -> Router:
    router = Router(name="moderation")

    @router.callback_query(F.data.startswith("moderation_review:"))
    async def resolve_moderation_review(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.message is None or not callback.data:
            return
        chat_id = callback.message.chat.id
        if chat_id not in allowed_group_ids or not await admin.is_authorized_admin(chat_id, callback.from_user.id):
            await callback.answer("仅本群管理员可处理。", show_alert=True)
            return
        try:
            _, decision, event_value = callback.data.split(":", 2)
            event_id = int(event_value)
        except (TypeError, ValueError):
            await callback.answer("审核操作无效。", show_alert=True)
            return
        if decision not in {"ban", "allow"}:
            await callback.answer("审核操作无效。", show_alert=True)
            return

        now = datetime.now(timezone.utc).isoformat()
        target = service.repository.claim_moderation_review(event_id, chat_id, now)
        if target is None:
            await callback.answer("该审核已处理或不再待审核。", show_alert=True)
            return

        if decision == "ban":
            try:
                await service.gateway.ban_member(chat_id, target.user_id)
            except Exception:
                service.repository.reopen_moderation_review(event_id, chat_id, now)
                logger.exception(
                    "moderation.review_ban_failed event_id=%s chat_id=%s target_user_id=%s",
                    event_id, chat_id, target.user_id,
                )
                await callback.answer("封禁失败，审核已恢复为待处理。", show_alert=True)
                return
            try:
                await service.gateway.delete_message(chat_id, target.message_id)
            except Exception:
                # The account is already banned; leave the audit decision final even if Telegram
                # no longer permits deletion of the original post.
                logger.exception(
                    "moderation.review_source_delete_failed event_id=%s chat_id=%s message_id=%s",
                    event_id, chat_id, target.message_id,
                )
            service.repository.block_user(target.user_id, "管理员确认广告", now)
            service.repository.complete_moderation_review(
                event_id, chat_id, callback.from_user.id,
                Action.PERMANENT_BAN, ReviewStatus.CONFIRMED, now,
            )
            replacement = "🚫 管理员已确认广告，账号已封禁。"
            callback_text = "已封禁并移除原消息。"
        else:
            service.repository.complete_moderation_review(
                event_id, chat_id, callback.from_user.id,
                Action.ALLOW, ReviewStatus.FALSE_POSITIVE, now,
            )
            replacement = "✅ 管理员已放行。"
            callback_text = "已放行，提示将在 1 分钟后删除。"

        try:
            await service.gateway.replace_moderation_review(chat_id, callback.message.message_id, replacement)
            if decision == "allow":
                await service.gateway.schedule_message_deletion(chat_id, callback.message.message_id, 60)
        except Exception:
            logger.exception(
                "moderation.review_card_replace_failed event_id=%s chat_id=%s review_message_id=%s",
                event_id, chat_id, callback.message.message_id,
            )
        await callback.answer(callback_text)

    return router
