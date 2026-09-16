from __future__ import annotations

from typing import Protocol


class TelegramGateway(Protocol):
    async def delete_message(self, chat_id: int, message_id: int) -> None: ...

    async def restrict_member(self, chat_id: int, user_id: int, minutes: int) -> None: ...

    async def ban_member(self, chat_id: int, user_id: int) -> None: ...

    async def unban_member(self, chat_id: int, user_id: int) -> None: ...

    async def is_group_administrator(self, chat_id: int, user_id: int) -> bool: ...

    async def is_group_member(self, chat_id: int, user_id: int) -> bool: ...

    async def get_member_info(self, chat_id: int, user_id: int) -> str: ...

    async def send_private_audit_link(self, user_id: int, audit_url: str) -> None: ...

    async def send_moderation_notice(self, chat_id: int, user_id: int) -> None: ...

    async def send_moderation_review(
        self, chat_id: int, reply_to_message_id: int, event_id: int
    ) -> int: ...

    async def replace_moderation_review(
        self, chat_id: int, review_message_id: int, text: str
    ) -> None: ...

    async def schedule_message_deletion(self, chat_id: int, message_id: int, seconds: int) -> None: ...

    async def release_member(self, chat_id: int, user_id: int) -> None: ...

    async def approve_join_request(self, chat_id: int, user_id: int) -> None: ...

    async def decline_join_request(self, chat_id: int, user_id: int) -> None: ...

    async def send_join_verification(
        self, user_chat_id: int, verification_url: str, group_title: str | None
    ) -> None: ...

    async def send_initial_verification_result(
        self,
        chat_id: int,
        user_id: int,
        username: str | None,
        text: str,
        delete_after_seconds: int | None = None,
    ) -> None: ...

    async def send_manual_join_welcome(self, chat_id: int, user_id: int) -> None: ...

    async def send_group_vps_challenge(
        self,
        chat_id: int,
        user_id: int,
        username: str | None,
        prompt: str,
        choices: tuple[tuple[str, str], ...],
        ttl_minutes: int,
    ) -> int: ...

    async def replace_group_verification_message(
        self,
        chat_id: int,
        message_id: int,
        user_id: int,
        username: str | None,
        text: str,
        delete_after_seconds: int | None = None,
        welcome_channel_url: str | None = None,
    ) -> None: ...
