from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.types import Message

from .knowledge import dmit_knowledge_for_query
from .llm import GroupReplyClient
from .repository import AuditRepository

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

BOT_WAKE_WORDS = ("猫猫",)

def _message_text(message: Message) -> str:
    """Return the user-visible text that is safe to send as short LLM context."""
    return message.text or message.caption or ""


def _is_explicit_bot_reply_trigger(
    message: Message,
    *,
    bot_user_id: int | None,
    bot_username: str | None,
) -> bool:
    """Only spend an LLM call when a member intentionally addresses this bot."""
    text = _message_text(message).strip()
    if not text or text.startswith("/"):
        return False
    if bot_username and f"@{bot_username.casefold()}" in text.casefold():
        return True
    if any(text.startswith(wake_word) for wake_word in BOT_WAKE_WORDS):
        return True
    replied = message.reply_to_message
    return bool(
        replied
        and replied.from_user
        and bot_user_id is not None
        and replied.from_user.id == bot_user_id
    )


def _reply_prompt_text(text: str, bot_username: str | None) -> str:
    """Remove an explicit Bot address from the model input; preserve the question."""
    if bot_username:
        text = re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.IGNORECASE)
    for wake_word in BOT_WAKE_WORDS:
        text = re.sub(rf"^\s*{re.escape(wake_word)}\s*[，,：:！!?？]?\s*", "", text)
    return text.strip()


def _memory_request(text: str) -> tuple[str, str | None] | None:
    """Parse explicit, opt-in memory controls after the Bot mention was removed."""
    normalized = text.strip()
    command = normalized.casefold()
    if command == "/memory on":
        return "enable", None
    if command == "/memory off":
        return "disable", None
    if command == "/memory clear":
        return "clear", None
    match = re.fullmatch(r"(?:记住|记下)\s*[：:]\s*(.+)", normalized, flags=re.DOTALL)
    return ("remember", match.group(1).strip()[:400]) if match else None


def _is_sensitive_memory(content: str) -> bool:
    """Never persist obvious credentials even if a member asks the Bot to remember them."""
    return bool(
        re.search(
            r"(?i)(?:password|passwd|token|api[ _-]?key|private[ _-]?key|secret|"
            r"密码|口令|私钥|密钥|订阅链接|分享链接)",
            content,
        )
    )

class GroupReplyService:
    """Explicitly addressed group conversations and opt-in memory controls."""

    def __init__(
        self,
        bot: Bot,
        repository: AuditRepository,
        client: GroupReplyClient | None,
        bot_user_id: int | None,
        bot_username: str | None,
        knowledge_group_ids: frozenset[int],
    ) -> None:
        self.bot = bot
        self.repository = repository
        self.client = client
        self.bot_user_id = bot_user_id
        self.bot_username = bot_username
        self.knowledge_group_ids = knowledge_group_ids

    async def handle_memory_request(
        self, message: Message, action: str, content: str | None = None
    ) -> None:
        """Handle explicit, per-group memory controls without invoking the model."""
        if message.from_user is None:
            return
        chat_id = message.chat.id
        user_id = message.from_user.id
        repository = self.repository
        now = datetime.now(timezone.utc).isoformat()
        if action == "enable":
            repository.set_assistant_memory_enabled(chat_id, user_id, True, now)
            await message.reply("已开启长期记忆。发送“记住：内容”可保存偏好；/memory off 可关闭。")
        elif action == "disable":
            repository.set_assistant_memory_enabled(chat_id, user_id, False, now)
            await message.reply("已关闭长期记忆；已有记忆不会用于后续回答。/memory clear 可删除。")
        elif action == "clear":
            deleted = repository.clear_assistant_memories(chat_id, user_id)
            await message.reply(f"已删除 {deleted} 条长期记忆。")
        elif action == "remember" and content:
            if not repository.assistant_memory_enabled(chat_id, user_id):
                await message.reply("长期记忆当前关闭。请先发送 /memory on。")
            elif _is_sensitive_memory(content):
                await message.reply("为保护安全，我不会保存密码、密钥、Token 或订阅链接。")
            else:
                repository.remember_assistant_fact(chat_id, user_id, content, now)
                await message.reply("记住了，仅用于本群后续与我的对话。")

    async def keep_typing(self, message: Message) -> None:
        """Keep Telegram's transient typing indicator alive while Kimi is processing."""
        while True:
            try:
                _ = await self.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
            except Exception:
                logger.debug("group_reply.typing_indicator_failed", exc_info=True)
            await asyncio.sleep(4)

    async def handle(self, message: Message) -> None:
        group_reply_client = self.client
        bot_username = self.bot_username
        dmit_knowledge_group_ids = self.knowledge_group_ids
        if message.from_user is None or group_reply_client is None:
            return
        if not _is_explicit_bot_reply_trigger(
            message, bot_user_id=self.bot_user_id, bot_username=bot_username
        ):
            return
        user_message = _reply_prompt_text(_message_text(message), bot_username)
        if not user_message:
            await message.reply("想问什么？请在 @Bot 后补充具体问题。")
            return
        memory_request = _memory_request(user_message)
        if memory_request is not None:
            await self.handle_memory_request(message, *memory_request)
            return
        replied_text = _message_text(message.reply_to_message) if message.reply_to_message else None
        repository = self.repository
        conversation = tuple(repository.assistant_context(message.chat.id, message.from_user.id))
        memories = (
            tuple(repository.assistant_memories(message.chat.id, message.from_user.id))
            if repository.assistant_memory_enabled(message.chat.id, message.from_user.id)
            else ()
        )
        knowledge = dmit_knowledge_for_query(user_message) if message.chat.id in dmit_knowledge_group_ids else ()
        thinking_task = asyncio.create_task(self.keep_typing(message))
        try:
            answer = await group_reply_client.reply(
                user_message,
                replied_text,
                conversation=conversation,
                memories=memories,
                knowledge=knowledge,
            )
        except Exception:
            logger.exception(
                "group_reply.failed chat_id=%s message_id=%s user_id=%s",
                message.chat.id,
                message.message_id,
                message.from_user.id,
            )
            await message.reply("抱歉，暂时无法回复，请稍后再试。")
            return
        finally:
            thinking_task.cancel()
            with suppress(asyncio.CancelledError):
                await thinking_task
        await message.reply(answer)
        try:
            now = datetime.now(timezone.utc).isoformat()
            repository.record_assistant_message(message.chat.id, message.from_user.id, "user", user_message, now)
            repository.record_assistant_message(message.chat.id, message.from_user.id, "assistant", answer, now)
        except Exception:
            # The user already received the reply. A history-write outage must not
            # turn this completed Telegram action into a webhook retry and duplicate it.
            logger.exception(
                "group_reply.history_record_failed chat_id=%s message_id=%s user_id=%s",
                message.chat.id,
                message.message_id,
                message.from_user.id,
            )
        logger.info(
            "group_reply.sent chat_id=%s message_id=%s user_id=%s trigger=%s",
            message.chat.id,
            message.message_id,
            message.from_user.id,
            "mention"
            if bot_username and f"@{bot_username.casefold()}" in _message_text(message).casefold()
            else "wake_word"
            if any(_message_text(message).strip().startswith(word) for word in BOT_WAKE_WORDS)
            else "reply",
        )
