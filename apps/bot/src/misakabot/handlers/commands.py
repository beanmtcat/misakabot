from __future__ import annotations

import logging
from urllib.parse import urlencode

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ..group_reply import GroupReplyService
from ..onboarding import OnboardingService
from ..service import ModerationService
from .admin import AdminActions

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def build_command_router(
    service: ModerationService,
    onboarding: OnboardingService,
    allowed_group_ids: frozenset[int],
    admin: AdminActions,
    replies: GroupReplyService,
    audit_web_app_url: str,
) -> Router:
    router = Router(name="commands")
    is_authorized_admin = admin.is_authorized_admin
    perform_admin_unban = admin.perform_admin_unban
    handle_memory_request = replies.handle_memory_request

    @router.message(Command("start"))
    async def start_private_chat(message: Message) -> None:
        if message.chat.type != "private":
            return
        await message.answer("本 Bot 不提供私聊对话，请在已启用群组中 @我或回复我的消息。")

    @router.message(Command("unban"))
    async def unban_member_command(message: Message) -> None:
        if (
            message.from_user is None
            or message.from_user.is_bot
            or message.chat.type not in {"group", "supergroup"}
            or message.chat.id not in allowed_group_ids
        ):
            return
        if not await is_authorized_admin(message.chat.id, message.from_user.id):
            logger.warning(
                "admin_unban.denied chat_id=%s actor_user_id=%s",
                message.chat.id,
                message.from_user.id,
            )
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 2:
            try:
                target_user_id = int(parts[1].strip())
            except ValueError:
                await message.answer("用户 ID 必须是数字；或直接回复该用户消息后发送 /unban。")
                return
        elif message.reply_to_message and message.reply_to_message.from_user:
            target_user_id = message.reply_to_message.from_user.id
        else:
            await message.answer("直接回复目标用户的消息发送 /unban，或使用 /unban <Telegram 用户 ID>。")
            return
        await perform_admin_unban(message, target_user_id, message.from_user.id)

    @router.message(Command("audit"))
    async def open_audit_command(message: Message) -> None:
        if (
            message.from_user is None
            or message.chat.type not in {"group", "supergroup"}
            or message.chat.id not in allowed_group_ids
        ):
            return
        if not await is_authorized_admin(message.chat.id, message.from_user.id):
            logger.warning(
                "audit_open.denied chat_id=%s actor_user_id=%s",
                message.chat.id,
                message.from_user.id,
            )
            return
        if not audit_web_app_url:
            await message.answer("审计页面尚未配置 AUDIT_WEB_APP_URL。")
            return
        separator = "&" if "?" in audit_web_app_url else "?"
        audit_query = urlencode({
            'chat_id': message.chat.id,
            # Display-only context. The API still authorizes strictly by chat_id.
            'chat_title': message.chat.title or str(message.chat.id),
        })
        audit_url = f"{audit_web_app_url}{separator}{audit_query}"
        try:
            await service.gateway.send_private_audit_link(message.from_user.id, audit_url)
        except TelegramForbiddenError:
            await message.answer("请先私聊 Bot 并发送 /start，再回到此群发送 /audit。")
            return
        await service.gateway.delete_message(message.chat.id, message.message_id)
        logger.info(
            "audit_open.sent_privately chat_id=%s actor_user_id=%s",
            message.chat.id,
            message.from_user.id,
        )

    @router.message(Command("memory"))
    async def memory_command(message: Message) -> None:
        if (
            message.from_user is None
            or message.chat.type not in {"group", "supergroup"}
            or message.chat.id not in allowed_group_ids
        ):
            return
        argument = ((message.text or "").split(maxsplit=1)[1:] or [""])[0].strip().casefold()
        action = {"on": "enable", "off": "disable", "clear": "clear"}.get(argument)
        if action is None:
            enabled = onboarding.repository.assistant_memory_enabled(message.chat.id, message.from_user.id)
            state = "开启" if enabled else "关闭"
            await message.reply(
                f"本群长期记忆当前：{state}。用法：/memory on、/memory off、/memory clear；"
                "@我后发送“记住：内容”可保存一条。"
            )
            return
        await handle_memory_request(message, action)

    @router.callback_query(F.data.startswith("admin_unban:"))
    async def unban_member_button(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.message is None:
            return
        if not isinstance(callback.message, Message):
            # Telegram can deliver a callback for an inaccessible/expired message.
            # It has chat metadata but cannot be used for reply/answer operations.
            await callback.answer("原管理消息已不可访问，请在群内使用 /unban。", show_alert=True)
            return
        if callback.message.chat.id not in allowed_group_ids or not await is_authorized_admin(
            callback.message.chat.id, callback.from_user.id
        ):
            await callback.answer("无管理员权限。", show_alert=True)
            return
        try:
            target_user_id = int((callback.data or "").removeprefix("admin_unban:"))
        except ValueError:
            await callback.answer("解除封禁请求无效。", show_alert=True)
            return
        await perform_admin_unban(callback.message, target_user_id, callback.from_user.id)
        await callback.answer("已解除封禁。")

    return router
