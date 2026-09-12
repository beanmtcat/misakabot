from __future__ import annotations

import logging

from aiogram.types import Message

from ..repository import AuditRepository
from ..telegram_gateway import TelegramGateway

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class AdminActions:
    """Shared authorization and unban operations for commands and callbacks."""

    def __init__(
        self, gateway: TelegramGateway, repository: AuditRepository, admin_user_ids: frozenset[int]
    ) -> None:
        self.gateway = gateway
        self.repository = repository
        self.admin_user_ids = admin_user_ids

    async def is_authorized_admin(self, chat_id: int, user_id: int) -> bool:
        if user_id in self.admin_user_ids:
            return True
        return await self.gateway.is_group_administrator(chat_id, user_id)

    async def perform_admin_unban(
        self, message: Message, target_user_id: int, actor_user_id: int
    ) -> None:
        gateway = self.gateway
        await gateway.unban_member(message.chat.id, target_user_id)
        local_indicator_removed = self.repository.unblock_user(target_user_id)
        logger.warning(
            "admin_unban.completed chat_id=%s actor_user_id=%s target_user_id=%s local_indicator_removed=%s",
            message.chat.id,
            actor_user_id,
            target_user_id,
            local_indicator_removed,
        )
        await message.answer("已解除 Telegram 封禁和本地黑名单。请让该用户重新申请入群以接收验证。")
