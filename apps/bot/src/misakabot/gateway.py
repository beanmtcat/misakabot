from __future__ import annotations

import asyncio
import html
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from time import monotonic

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import ChatMemberUnion, InlineKeyboardButton, InlineKeyboardMarkup

from .onboarding import OnboardingService
from .repository import AuditRepository

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_DISPLAY_NAME_LIMIT = 20
_LINK_LIKE_PATTERN = re.compile(
    (
        r"(?i)(?:https?://|www\.|t\.me/|telegram\.(?:me|dog)/)\S*"
        r"|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|me|io|cc|top|xyz|vip|cn|co|app|link|site)\b\S*"
    )
)


def sanitize_display_name(name: str) -> str:
    """Strip control characters, links and mentions so a name only identifies a member."""
    visible = "".join(char for char in name if not unicodedata.category(char).startswith("C"))
    visible = _LINK_LIKE_PATTERN.sub(" ", visible).replace("@", " ")
    return " ".join(visible.split())[:_DISPLAY_NAME_LIMIT]


def is_active_chat_member(member: ChatMemberUnion) -> bool:
    """Return actual membership, including Telegram's restricted/is_member split.

    Telegram keeps the status value as ``restricted`` after some restricted users
    leave. In that state ``is_member`` is false, so status alone is insufficient.
    """
    status = getattr(member.status, "value", member.status)
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return status in {"creator", "owner", "administrator", "member"}


class AiogramGateway:
    _DELETION_LEASE_SECONDS: int = 120
    _DELETION_REQUEST_TIMEOUT_SECONDS: int = 30
    _ADMIN_CACHE_TTL_SECONDS: float = 30.0
    _WEBHOOK_UPDATE_RETENTION_DAYS: int = 7
    _ABANDONED_WEBHOOK_LEASE_RETENTION_DAYS: int = 1
    _MEMBER_LOOKUP_RETRY_DELAYS_SECONDS: tuple[float, ...] = (0.25, 0.75, 1.5)

    def __init__(
        self,
        bot: Bot,
        repository: AuditRepository,
        *,
        show_secondary_verification_username: bool = True,
    ) -> None:
        self.bot: Bot = bot
        self.repository: AuditRepository = repository
        self.show_secondary_verification_username: bool = show_secondary_verification_username
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        _ = await self.bot.delete_message(chat_id, message_id)

    async def restrict_member(self, chat_id: int, user_id: int, minutes: int) -> None:
        from datetime import datetime, timedelta, timezone
        from aiogram.types import ChatPermissions

        _ = await self.bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=datetime.now(timezone.utc) + timedelta(minutes=minutes),
        )

    async def ban_member(self, chat_id: int, user_id: int, *, duration_hours: int = 0) -> None:
        if not 0 <= duration_hours <= 8784:
            raise ValueError("duration_hours must be 0..8784 (0 = permanent)")
        if duration_hours:
            until_date = datetime.now(timezone.utc) + timedelta(hours=duration_hours)
            _ = await self.bot.ban_chat_member(chat_id, user_id, until_date=until_date)
        else:
            _ = await self.bot.ban_chat_member(chat_id, user_id)

    async def unban_member(self, chat_id: int, user_id: int) -> None:
        _ = await self.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)

    async def is_group_administrator(self, chat_id: int, user_id: int) -> bool:
        key = (chat_id, user_id)
        cached = self._admin_cache.get(key)
        if cached and cached[1] > monotonic():
            return cached[0]
        member = await self._get_chat_member(chat_id, user_id)
        status = getattr(member.status, "value", member.status)
        is_admin = status in {"creator", "owner", "administrator"}
        self._admin_cache[key] = (is_admin, monotonic() + self._ADMIN_CACHE_TTL_SECONDS)
        return is_admin

    def invalidate_group_administrator_cache(self, chat_id: int, user_id: int) -> None:
        """Make group-role changes effective on the next authorization check."""
        _ = self._admin_cache.pop((chat_id, user_id), None)

    async def is_group_member(self, chat_id: int, user_id: int) -> bool:
        member = await self._get_chat_member(chat_id, user_id)
        return is_active_chat_member(member)

    async def get_member_info(self, chat_id: int, user_id: int) -> str:
        """Return a short, callback-alert-safe member summary for group administrators."""
        member = await self._get_chat_member(chat_id, user_id)
        user = member.user
        username = f"@{user.username}" if user.username else "未设置"
        display_name = (user.full_name or "未设置").replace("\n", " ")[:80]
        return f"用户 ID：{user.id}\n用户名：{username}\n显示名称：{display_name}"

    async def _get_chat_member(self, chat_id: int, user_id: int) -> ChatMemberUnion:
        """Retry the read-only Telegram lookup on transport and server failures.

        This call is safe to repeat and is used by both authorization checks and
        onboarding expiry.  Bad requests and permission failures are deliberately
        not retried because another attempt cannot change their outcome.
        """
        delays = self._MEMBER_LOOKUP_RETRY_DELAYS_SECONDS
        for attempt in range(len(delays) + 1):
            try:
                return await self.bot.get_chat_member(chat_id, user_id, request_timeout=10)
            except TelegramRetryAfter as error:
                if attempt >= len(delays):
                    raise
                delay = min(max(float(error.retry_after), delays[attempt]), 5.0)
                reason = str(error)
            except (TelegramNetworkError, TelegramServerError) as error:
                if attempt >= len(delays):
                    raise
                delay = delays[attempt]
                reason = str(error)
            logger.warning(
                "telegram.member_lookup_retry chat_id=%s user_id=%s attempt=%s delay=%.2f reason=%s",
                chat_id,
                user_id,
                attempt + 1,
                delay,
                reason,
            )
            await asyncio.sleep(delay)
        raise RuntimeError("unreachable member lookup retry state")

    @staticmethod
    def _member_info_keyboard(user_id: int, username: str | None = None) -> InlineKeyboardMarkup:
        """Use Telegram's native profile deep link, with an admin-only fallback.

        `tg://user?id=...` is the only Bot API URL that asks Telegram to open a
        user's native profile. Telegram rejects this URL for users who disable
        profile links, and rejects the *entire message* with
        ``BUTTON_USER_PRIVACY_RESTRICTED``. Use the existing admin-only callback
        in that case so onboarding messages can still be delivered.
        """
        normalized_username = (username or "").removeprefix("@").strip()
        if re.fullmatch(r"[A-Za-z0-9_]{5,}", normalized_username):
            return InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(
                        text="查看用户资料",
                        url=f"https://t.me/{normalized_username}?profile",
                    )
                ]]
            )
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="查看用户信息", callback_data=f"user_info:{user_id}")],
            ]
        )

    async def _delete_scheduled_message(self, chat_id: int, message_id: int) -> bool:
        try:
            now = datetime.now(timezone.utc)
            claimed_until = (now + timedelta(seconds=self._DELETION_LEASE_SECONDS)).isoformat()
            if not self.repository.claim_message_deletion(
                chat_id, message_id, now.isoformat(), claimed_until
            ):
                # Covers an in-flight claim and stale scan results after completion.
                return False
            completed = True
            try:
                _ = await self.bot.delete_message(
                    chat_id, message_id, request_timeout=self._DELETION_REQUEST_TIMEOUT_SECONDS
                )
            except TelegramBadRequest as error:
                if "message to delete not found" in error.message.casefold():
                    logger.info(
                        "temporary_notice_already_absent chat_id=%s message_id=%s",
                        chat_id, message_id,
                    )
                else:
                    completed = False
                    logger.warning(
                        "temporary_notice_deletion_abandoned chat_id=%s message_id=%s reason=%s",
                        chat_id, message_id, error.message,
                    )
            except TelegramForbiddenError as error:
                completed = False
                logger.warning(
                    "temporary_notice_deletion_abandoned chat_id=%s message_id=%s reason=%s",
                    chat_id, message_id, error.message,
                )
            else:
                logger.info("temporary_notice_deleted chat_id=%s message_id=%s", chat_id, message_id)
            self.repository.complete_message_deletion(chat_id, message_id, claimed_until)
            return completed
        except Exception:
            # Keep the durable lease: network/DB errors and crashes become retryable
            # after it expires. A new claimant cannot race this request's timeout.
            logger.warning(
                "temporary_notice_delete_failed chat_id=%s message_id=%s",
                chat_id,
                message_id,
                exc_info=True,
            )
            return False

    async def _delete_message_after(self, chat_id: int, message_id: int, seconds: int) -> None:
        await asyncio.sleep(seconds)
        _ = await self._delete_scheduled_message(chat_id, message_id)

    def _schedule_message_deletion(self, chat_id: int, message_id: int, seconds: int) -> None:
        """Persist a deletion before scheduling its in-process fast path."""
        due_at = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
        self.repository.schedule_message_deletion(chat_id, message_id, due_at)
        task = asyncio.create_task(self._delete_message_after(chat_id, message_id, seconds))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        logger.info(
            "temporary_notice_delete_scheduled chat_id=%s message_id=%s delay_seconds=%s",
            chat_id,
            message_id,
            seconds,
        )

    async def process_scheduled_message_deletions(self) -> None:
        due = self.repository.due_message_deletions(datetime.now(timezone.utc).isoformat())
        for chat_id, message_id in due:
            _ = await self._delete_scheduled_message(chat_id, message_id)
        now = datetime.now(timezone.utc)
        removed = self.repository.prune_webhook_updates(
            (now - timedelta(days=self._WEBHOOK_UPDATE_RETENTION_DAYS)).isoformat(),
            (now - timedelta(days=self._ABANDONED_WEBHOOK_LEASE_RETENTION_DAYS)).isoformat(),
        )
        if removed:
            logger.info("webhook_updates.pruned count=%s", removed)

    async def send_private_audit_link(self, user_id: int, audit_url: str) -> None:
        from aiogram.types import WebAppInfo

        _ = await self.bot.send_message(
            user_id,
            "点击下方按钮查看该群的审计日志：",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(
                        text="打开审计日志",
                        web_app=WebAppInfo(url=audit_url),
                    )
                ]]
            ),
        )

    async def send_moderation_notice(self, chat_id: int, user_id: int) -> None:
        # Never include a display name, @username, message body, or model reason here.
        # Those fields may themselves be advertising or hostile text.
        _ = await self.bot.send_message(
            chat_id,
            "已移除一名违规账号（广告推广）。请勿发送商品、服务或私聊引流信息。",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(
                        text="管理员解除封禁",
                        callback_data=f"admin_unban:{user_id}",
                    )
                ]]
            ),
        )

    async def send_moderation_review(
        self, chat_id: int, reply_to_message_id: int, event_id: int, reason: str
    ) -> int:
        """Post an admin-only decision card while keeping the source message visible."""
        message = await self.bot.send_message(
            chat_id,
            f"⚠️ 疑似广告，等待管理员处理。\n原因：{reason}",
            reply_to_message_id=reply_to_message_id,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🚫 管理员确认",
                        callback_data=f"moderation_review:ban:{event_id}",
                    ),
                    InlineKeyboardButton(
                        text="✅ 管理员放行",
                        callback_data=f"moderation_review:allow:{event_id}",
                    ),
                ]]
            ),
        )
        return message.message_id

    async def replace_moderation_review(
        self, chat_id: int, review_message_id: int, text: str, user_id: int | None = None
    ) -> None:
        markup = None
        if user_id is not None:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="查看用户信息", callback_data=f"user_info:{user_id}")
                ]]
            )
        _ = await self.bot.edit_message_text(
            chat_id=chat_id, message_id=review_message_id, text=text, reply_markup=markup
        )

    async def schedule_message_deletion(self, chat_id: int, message_id: int, seconds: int) -> None:
        self._schedule_message_deletion(chat_id, message_id, seconds)

    async def release_member(self, chat_id: int, user_id: int) -> None:
        from aiogram.types import ChatPermissions

        _ = await self.bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_change_info=False,
                can_invite_users=True,
                can_pin_messages=False,
                can_manage_topics=False,
            ),
        )

    async def approve_join_request(self, chat_id: int, user_id: int) -> None:
        _ = await self.bot.approve_chat_join_request(chat_id, user_id)

    async def decline_join_request(self, chat_id: int, user_id: int) -> None:
        _ = await self.bot.decline_chat_join_request(chat_id, user_id)

    async def send_join_verification(
        self, user_chat_id: int, verification_url: str, group_title: str | None
    ) -> None:
        from aiogram.types import WebAppInfo

        display_title = (group_title or "目标群组").strip()[:80] or "目标群组"
        _ = await self.bot.send_message(
            user_chat_id,
            (
                f"你正在申请加入「{display_title}」。\n\n"
                "请点击下方按钮完成安全验证。验证通过后将自动批准入群；"
                "首次发言仍会进行强化广告审核。"
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="开始安全验证", web_app=WebAppInfo(url=verification_url))]]
            ),
        )

    async def send_private_join_guard_alert(
        self, admin_user_ids: frozenset[int], text: str
    ) -> None:
        for admin_user_id in admin_user_ids:
            try:
                _ = await self.bot.send_message(admin_user_id, text)
            except (TelegramBadRequest, TelegramForbiddenError):
                logger.warning(
                    "join_guard.admin_notice_unavailable admin_user_id=%s",
                    admin_user_id,
                )

    async def send_initial_verification_result(
        self,
        chat_id: int,
        user_id: int,
        username: str | None,
        text: str,
        delete_after_seconds: int | None = None,
    ) -> None:
        message = await self.bot.send_message(
            chat_id,
            text,
            reply_markup=self._member_info_keyboard(user_id, username),
        )
        if delete_after_seconds is not None:
            self._schedule_message_deletion(chat_id, message.message_id, delete_after_seconds)

    async def send_manual_join_welcome(self, chat_id: int, user_id: int) -> None:
        # Do not include the member's name: it can itself contain advertising content.
        _ = user_id
        _ = await self.bot.send_message(chat_id, OnboardingService.MANUAL_JOIN_WELCOME_TEXT)

    async def _group_challenge_member_label(
        self, chat_id: int, user_id: int, username: str | None, display_name: str | None
    ) -> str:
        # Identity is wrapped in <code> so usernames and names that look like links or
        # mentions are never clickable: the Bot must not advertise for spam accounts.
        if not self.show_secondary_verification_username:
            return f"<code>{user_id}</code>"
        if username:
            return f"<code>@{html.escape(username)}</code>（ID：{user_id}）"
        if display_name is None:
            try:
                # One short attempt only: this runs during join floods.
                member = await self.bot.get_chat_member(chat_id, user_id, request_timeout=3)
                display_name = member.user.full_name
            except Exception:
                # Identity decoration must never prevent the actual challenge.
                logger.info(
                    "onboarding.challenge_name_unavailable chat_id=%s user_id=%s",
                    chat_id, user_id,
                )
        safe_name = sanitize_display_name(display_name or "")
        if safe_name:
            return f"<code>{html.escape(safe_name)}</code>（ID：{user_id}）"
        return f"<code>{user_id}</code>"

    def _group_challenge_keyboard(
        self, user_id: int, username: str | None, choices: tuple[tuple[str, str], ...]
    ) -> InlineKeyboardMarkup:
        token = choices[0][1].split(":", 2)[1]
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=text, callback_data=callback_data)]
                for text, callback_data in choices
            ] + [
                [
                    InlineKeyboardButton(
                        text="✅ 管理员：通过", callback_data=f"vps_admin_approve:{token}",
                    ),
                    InlineKeyboardButton(
                        text="❌ 管理员：拒绝", callback_data=f"vps_admin_reject:{token}",
                    ),
                ],
                *self._member_info_keyboard(user_id, username).inline_keyboard,
            ]
        )

    @staticmethod
    def _group_challenge_text(
        member_label: str, prompt: str, ttl_minutes: int, expires_at: str | None = None
    ) -> str:
        deadline = (
            "截止时间："
            + datetime.fromisoformat(expires_at).astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M:%S")
            + "（UTC+8，答错不重置）。"
            if expires_at else f"{ttl_minutes} 分钟内未完成将移出群组。"
        )
        return (
            f"新成员 {member_label} 请完成入群二次验证：\n"
            f"{html.escape(prompt)}\n\n"
            f"请选择正确答案。仅该成员可作答；三次答错将移出群组。{deadline}"
        )

    async def send_group_vps_challenge(
        self,
        chat_id: int,
        user_id: int,
        username: str | None,
        prompt: str,
        choices: tuple[tuple[str, str], ...],
        ttl_minutes: int,
        *,
        display_name: str | None = None,
        expires_at: str | None = None,
        preparing: bool = False,
    ) -> int:
        member_label = await self._group_challenge_member_label(
            chat_id, user_id, username, display_name
        )
        message = await self.bot.send_message(
            chat_id,
            (
                f"新成员 {member_label} 正在恢复入群验证，请稍候…"
                if preparing else self._group_challenge_text(member_label, prompt, ttl_minutes, expires_at)
            ),
            parse_mode="HTML",
            reply_markup=(
                self._member_info_keyboard(user_id, username) if preparing
                else self._group_challenge_keyboard(user_id, username, choices)
            ),
        )
        # If the process dies before onboarding stores this ID, the card must not
        # remain forever. No in-process timer: this durable orphan guard is cancelled
        # atomically by set_group_challenge_message_id on successful registration.
        self.repository.schedule_message_deletion(
            chat_id, message.message_id,
            (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes + 30)).isoformat(),
            verification_user_id=user_id,
        )
        return message.message_id

    async def update_group_vps_challenge(
        self,
        chat_id: int,
        message_id: int,
        user_id: int,
        username: str | None,
        prompt: str,
        choices: tuple[tuple[str, str], ...],
        ttl_minutes: int,
        attempts_left: int,
        *,
        display_name: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        """Replace the prompt after a wrong answer; the old buttons are already invalid."""
        member_label = await self._group_challenge_member_label(
            chat_id, user_id, username, display_name
        )
        try:
            _ = await self.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"回答错误，已换一道新题，还可尝试 {attempts_left} 次。\n\n"
                    + self._group_challenge_text(member_label, prompt, ttl_minutes, expires_at)
                ),
                parse_mode="HTML",
                reply_markup=self._group_challenge_keyboard(user_id, username, choices),
                request_timeout=30,
            )
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error).casefold():
                raise

    async def replace_group_verification_message(
        self,
        chat_id: int,
        message_id: int,
        user_id: int,
        username: str | None,
        text: str,
        delete_after_seconds: int | None = None,
        welcome_channel_url: str | None = None,
    ) -> None:
        keyboard_rows = self._member_info_keyboard(user_id, username).inline_keyboard
        if welcome_channel_url:
            keyboard_rows = [
                [InlineKeyboardButton(text="关注本群频道", url=welcome_channel_url)],
                *keyboard_rows,
            ]
        _ = await self.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        )
        if delete_after_seconds is not None:
            self._schedule_message_deletion(chat_id, message_id, delete_after_seconds)
