from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, ChatJoinRequest, ChatMemberUpdated, Message

from ..domain import DirectJoinInput, JoinRequestInput
from ..gateway import AiogramGateway
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

    @router.chat_join_request()
    async def receive_join_request(request: ChatJoinRequest) -> None:
        if request.chat.type not in {"group", "supergroup"}:
            logger.warning("join_request.ignored non_group chat_id=%s", request.chat.id)
            return
        if request.chat.id not in allowed_group_ids:
            logger.warning("join_request.ignored chat_not_allowed chat_id=%s", request.chat.id)
            return
        logger.info("join_request.received chat_id=%s user_id=%s", request.chat.id, request.from_user.id)
        await onboarding.start(
            JoinRequestInput(
                chat_id=request.chat.id,
                user_id=request.from_user.id,
                user_chat_id=request.user_chat_id,
                group_title=request.chat.title,
                username=request.from_user.username,
                invite_link=request.invite_link.invite_link if request.invite_link else None,
            )
        )

    @router.chat_member()
    async def invalidate_member_role_cache(event: ChatMemberUpdated) -> None:
        """Apply Telegram administrator promotions/revocations without cache delay."""
        if event.chat.id not in allowed_group_ids:
            return
        # Cache invalidation is an Aiogram implementation detail, not an onboarding
        # gateway requirement. Other gateways remain usable without this optimisation.
        if isinstance(service.gateway, AiogramGateway):
            service.gateway.invalidate_group_administrator_cache(
                event.chat.id, event.new_chat_member.user.id
            )
        logger.info(
            "group_member_role_changed cache_invalidated chat_id=%s user_id=%s",
            event.chat.id,
            event.new_chat_member.user.id,
        )

    @router.message(F.new_chat_members)
    async def receive_direct_join(message: Message) -> None:
        if message.chat.type not in {"group", "supergroup"} or message.chat.id not in allowed_group_ids:
            return
        for member in message.new_chat_members or ():
            if member.is_bot:
                continue
            logger.info("member_join.received chat_id=%s user_id=%s", message.chat.id, member.id)
            await onboarding.start_direct_join(
                DirectJoinInput(
                    chat_id=message.chat.id,
                    user_id=member.id,
                    username=member.username,
                    joined_at=message.date,
                )
            )

    @router.callback_query(F.data.startswith("vps_verify:"))
    async def receive_group_vps_challenge(callback: CallbackQuery) -> None:
        if callback.from_user is None or not callback.data:
            return
        outcome = await onboarding.verify_group_challenge(callback.data, callback.from_user.id)
        logger.info(
            "group_challenge.completed user_id=%s accepted=%s state=%s",
            callback.from_user.id,
            outcome.accepted,
            outcome.state,
        )
        await callback.answer(outcome.reason, show_alert=not outcome.accepted)

    @router.callback_query(F.data.startswith("user_info:"))
    async def show_member_info(callback: CallbackQuery) -> None:
        """Show profile metadata without relying on tg:// deep-link support in clients."""
        if callback.from_user is None or callback.message is None or not callback.data:
            return
        chat_id = callback.message.chat.id
        if chat_id not in allowed_group_ids or not await is_authorized_admin(chat_id, callback.from_user.id):
            await callback.answer("仅本群管理员可查看用户信息。", show_alert=True)
            return
        try:
            user_id = int(callback.data.removeprefix("user_info:"))
        except ValueError:
            await callback.answer("用户信息请求无效。", show_alert=True)
            return
        try:
            details = await service.gateway.get_member_info(chat_id, user_id)
        except Exception:
            logger.exception("member_info.lookup_failed chat_id=%s user_id=%s", chat_id, user_id)
            details = f"用户 ID：{user_id}\n暂时无法读取其他 Telegram 资料。"
        await callback.answer(details[:190], show_alert=True)

    @router.callback_query(F.data.startswith("vps_admin_"))
    async def resolve_group_challenge_as_admin(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.message is None or not callback.data:
            return
        chat_id = callback.message.chat.id
        if chat_id not in allowed_group_ids or not await is_authorized_admin(chat_id, callback.from_user.id):
            await callback.answer("无管理员权限。", show_alert=True)
            return
        approved: bool
        if callback.data.startswith(OnboardingService.GROUP_CHALLENGE_ADMIN_APPROVE_PREFIX):
            approved = True
            token = callback.data.removeprefix(OnboardingService.GROUP_CHALLENGE_ADMIN_APPROVE_PREFIX)
        elif callback.data.startswith(OnboardingService.GROUP_CHALLENGE_ADMIN_REJECT_PREFIX):
            approved = False
            token = callback.data.removeprefix(OnboardingService.GROUP_CHALLENGE_ADMIN_REJECT_PREFIX)
        else:
            await callback.answer("管理员操作无效。", show_alert=True)
            return
        if not token:
            await callback.answer("管理员操作无效。", show_alert=True)
            return
        outcome = await onboarding.resolve_group_challenge_by_admin(token, approved)
        logger.info(
            "group_challenge.admin_resolved chat_id=%s actor_user_id=%s accepted=%s state=%s",
            chat_id,
            callback.from_user.id,
            outcome.accepted,
            outcome.state,
        )
        await callback.answer(outcome.reason, show_alert=not outcome.accepted)

    @router.callback_query(F.data.startswith("join_verify:"))
    async def reject_legacy_group_verification(callback: CallbackQuery) -> None:
        # Old deployments had a click-to-pass callback. Never let it bypass the new challenge.
        await callback.answer("旧验证按钮已失效，请等待新的 VPS 安全验证题目。", show_alert=True)

    return router
