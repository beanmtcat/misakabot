from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, ChatJoinRequest, ChatMemberUpdated, Message

from ..domain import DirectJoinInput, JoinRequestInput
from ..gateway import AiogramGateway, is_active_chat_member
from ..onboarding import OnboardingService
from ..service import ModerationService
from .admin import AdminActions

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def build_onboarding_router(
    service: ModerationService,
    onboarding: OnboardingService,
    allowed_group_ids: frozenset[int],
    admin: AdminActions,
) -> Router:
    router = Router(name="onboarding")
    is_authorized_admin = admin.is_authorized_admin

    async def receive_join_request(request: ChatJoinRequest) -> None:
        if request.chat.type not in {"group", "supergroup"}:
            logger.warning("join_request.ignored non_group chat_id=%s", request.chat.id)
            return
        if request.chat.id not in allowed_group_ids:
            logger.warning("join_request.ignored chat_not_allowed chat_id=%s", request.chat.id)
            return
        logger.info("join_request.received chat_id=%s user_id=%s", request.chat.id, request.from_user.id)
        _ = await onboarding.start(
            JoinRequestInput(
                chat_id=request.chat.id,
                user_id=request.from_user.id,
                user_chat_id=request.user_chat_id,
                group_title=request.chat.title,
                username=request.from_user.username,
                display_name=request.from_user.full_name,
                invite_link=request.invite_link.invite_link if request.invite_link else None,
                requested_at=request.date,
            )
        )

    _ = router.chat_join_request.register(receive_join_request)

    async def receive_member_status_change(event: ChatMemberUpdated) -> None:
        """Apply role changes and use membership transitions as the join signal.

        Telegram does not consistently emit a ``new_chat_members`` service message
        when an administrator approves a join request.  The ``chat_member`` transition
        from left/kicked to an active status is the reliable Bot API signal.
        """
        if event.chat.id not in allowed_group_ids:
            return
        # Cache invalidation is an Aiogram implementation detail, not an onboarding
        # gateway requirement. Other gateways remain usable without this optimisation.
        if isinstance(service.gateway, AiogramGateway):
            service.gateway.invalidate_group_administrator_cache(
                event.chat.id, event.new_chat_member.user.id
            )
        old_status = getattr(event.old_chat_member.status, "value", event.old_chat_member.status)
        new_status = getattr(event.new_chat_member.status, "value", event.new_chat_member.status)
        was_member = is_active_chat_member(event.old_chat_member)
        is_member = is_active_chat_member(event.new_chat_member)
        member = event.new_chat_member.user
        if (
            not was_member
            and is_member
            and not member.is_bot
        ):
            logger.info(
                "member_join_status.received chat_id=%s user_id=%s via_join_request=%s",
                event.chat.id,
                member.id,
                event.via_join_request,
            )
            _ = await onboarding.start_direct_join(
                DirectJoinInput(
                    chat_id=event.chat.id,
                    user_id=member.id,
                    username=member.username,
                    display_name=member.full_name,
                    joined_at=event.date,
                )
            )
        elif (
            was_member
            and not is_member
            and new_status != "kicked"
            and not member.is_bot
        ):
            _ = onboarding.mark_member_departed(event.chat.id, member.id, event.date)
        logger.info(
            (
                "group_member_role_changed cache_invalidated chat_id=%s user_id=%s "
                "old_status=%s new_status=%s was_member=%s is_member=%s"
            ),
            event.chat.id,
            event.new_chat_member.user.id,
            old_status,
            new_status,
            was_member,
            is_member,
        )

    _ = router.chat_member.register(receive_member_status_change)

    async def receive_direct_join(message: Message) -> None:
        if message.chat.type not in {"group", "supergroup"} or message.chat.id not in allowed_group_ids:
            return
        for member in message.new_chat_members or ():
            if member.is_bot:
                continue
            logger.info("member_join.received chat_id=%s user_id=%s", message.chat.id, member.id)
            _ = await onboarding.start_direct_join(
                DirectJoinInput(
                    chat_id=message.chat.id,
                    user_id=member.id,
                    username=member.username,
                    display_name=member.full_name,
                    joined_at=message.date,
                )
            )

    _ = router.message.register(receive_direct_join, F.new_chat_members)

    async def receive_group_vps_challenge(callback: CallbackQuery) -> None:
        if not callback.data:
            return
        # Only the bound member can answer, so the clicker's identity labels the next question.
        outcome = await onboarding.verify_group_challenge(
            callback.data,
            callback.from_user.id,
            username=callback.from_user.username,
            display_name=callback.from_user.full_name,
        )
        logger.info(
            "group_challenge.completed user_id=%s accepted=%s state=%s",
            callback.from_user.id,
            outcome.accepted,
            outcome.state,
        )
        _ = await callback.answer(outcome.reason, show_alert=not outcome.accepted)

    _ = router.callback_query.register(receive_group_vps_challenge, F.data.startswith("vps_verify:"))

    async def show_member_info(callback: CallbackQuery) -> None:
        """Show profile metadata without relying on tg:// deep-link support in clients."""
        if callback.message is None or not callback.data:
            return
        chat_id = callback.message.chat.id
        if chat_id not in allowed_group_ids or not await is_authorized_admin(chat_id, callback.from_user.id):
            _ = await callback.answer("仅本群管理员可查看用户信息。", show_alert=True)
            return
        try:
            user_id = int(callback.data.removeprefix("user_info:"))
        except ValueError:
            _ = await callback.answer("用户信息请求无效。", show_alert=True)
            return
        try:
            details = await service.gateway.get_member_info(chat_id, user_id)
        except Exception:
            logger.exception("member_info.lookup_failed chat_id=%s user_id=%s", chat_id, user_id)
            details = f"用户 ID：{user_id}\n暂时无法读取其他 Telegram 资料。"
        _ = await callback.answer(details[:190], show_alert=True)

    _ = router.callback_query.register(show_member_info, F.data.startswith("user_info:"))

    async def resolve_group_challenge_as_admin(callback: CallbackQuery) -> None:
        if callback.message is None or not callback.data:
            return
        chat_id = callback.message.chat.id
        if chat_id not in allowed_group_ids or not await is_authorized_admin(chat_id, callback.from_user.id):
            _ = await callback.answer("无管理员权限。", show_alert=True)
            return
        approved: bool
        if callback.data.startswith(OnboardingService.GROUP_CHALLENGE_ADMIN_APPROVE_PREFIX):
            approved = True
            token = callback.data.removeprefix(OnboardingService.GROUP_CHALLENGE_ADMIN_APPROVE_PREFIX)
        elif callback.data.startswith(OnboardingService.GROUP_CHALLENGE_ADMIN_REJECT_PREFIX):
            approved = False
            token = callback.data.removeprefix(OnboardingService.GROUP_CHALLENGE_ADMIN_REJECT_PREFIX)
        else:
            _ = await callback.answer("管理员操作无效。", show_alert=True)
            return
        if not token:
            _ = await callback.answer("管理员操作无效。", show_alert=True)
            return
        outcome = await onboarding.resolve_group_challenge_by_admin(token, approved, chat_id=chat_id)
        logger.info(
            "group_challenge.admin_resolved chat_id=%s actor_user_id=%s accepted=%s state=%s",
            chat_id,
            callback.from_user.id,
            outcome.accepted,
            outcome.state,
        )
        _ = await callback.answer(outcome.reason, show_alert=not outcome.accepted)

    _ = router.callback_query.register(resolve_group_challenge_as_admin, F.data.startswith("vps_admin_"))

    async def reject_legacy_group_verification(callback: CallbackQuery) -> None:
        # Old deployments had a click-to-pass callback. Never let it bypass the new challenge.
        _ = await callback.answer("旧验证按钮已失效，请等待新的入群验证题目。", show_alert=True)

    _ = router.callback_query.register(reject_legacy_group_verification, F.data.startswith("join_verify:"))

    return router
