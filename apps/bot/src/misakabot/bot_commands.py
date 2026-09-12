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
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

async def register_bot_commands(bot: Bot, allowed_group_ids: frozenset[int]) -> None:
    """Register contextual Telegram command menus on every application start."""
    await bot.set_my_commands(
        [BotCommand(command="start", description="查看 Bot 使用说明")],
        scope=BotCommandScopeAllPrivateChats(),
    )
    # Remove menus from older deployments, which exposed commands in every group.
    await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
    await bot.delete_my_commands(scope=BotCommandScopeAllChatAdministrators())
    for chat_id in allowed_group_ids:
        await bot.set_my_commands(
            [BotCommand(command="memory", description="管理本群 AI 长期记忆")],
            scope=BotCommandScopeChat(chat_id=chat_id),
        )
        await bot.set_my_commands(
            [
                BotCommand(command="audit", description="查看本群审计日志"),
                BotCommand(command="unban", description="回复用户消息解除封禁"),
                BotCommand(command="memory", description="管理本群 AI 长期记忆"),
            ],
            scope=BotCommandScopeChatAdministrators(chat_id=chat_id),
        )
    logger.info("bot.commands_registered")
