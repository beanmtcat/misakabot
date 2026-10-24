from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
    BotCommandScopeDefault,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

async def register_bot_commands(bot: Bot, allowed_group_ids: frozenset[int]) -> None:
    """Register contextual Telegram command menus on every application start."""
    _ = await bot.set_my_commands(
        [BotCommand(command="start", description="查看 Bot 使用说明")],
        scope=BotCommandScopeAllPrivateChats(),
    )
    # Deleting a chat menu reveals the next broader scope, including BotFather's
    # default menu. Clear the legacy Chinese/English variants as well.
    for language_code in ("", "zh", "en"):
        for scope in (
            BotCommandScopeDefault(), BotCommandScopeAllGroupChats(),
            BotCommandScopeAllChatAdministrators(),
        ):
            _ = await bot.delete_my_commands(scope=scope, language_code=language_code)
    for chat_id in allowed_group_ids:
        # Members get no menu, so tapping can't post admin-only commands into the group.
        for language_code in ("", "zh", "en"):
            _ = await bot.delete_my_commands(
                scope=BotCommandScopeChat(chat_id=chat_id), language_code=language_code
            )
            if language_code:
                _ = await bot.delete_my_commands(
                    scope=BotCommandScopeChatAdministrators(chat_id=chat_id),
                    language_code=language_code,
                )
        _ = await bot.set_my_commands(
            [
                BotCommand(command="audit", description="查看本群审计日志"),
                BotCommand(command="unban", description="回复用户消息解除封禁"),
                BotCommand(command="join_guard", description="查看或调整入群防护"),
                BotCommand(command="memory", description="管理本群 AI 长期记忆"),
            ],
            scope=BotCommandScopeChatAdministrators(chat_id=chat_id),
        )
    logger.info("bot.commands_registered")
