from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from .domain import DirectJoinInput, EMPTY_IDS, JoinRequestInput, JoinVerificationOutcome, OnboardingState
from .join_guard import JoinGuard, JoinGuardDecision, JoinGuardMode, JoinGuardPolicy
from .join_questions import GLOSSARY_QUESTIONS
from .repository import AuditRepository
from .telegram_gateway import TelegramGateway

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


@dataclass(frozen=True)
class PendingJoin:
    state: OnboardingState
    callback_data: str | None


class OnboardingService:
    """Automatic join verification plus per-group first-observed-message scrutiny."""

    GROUP_CHALLENGE_PREFIX: str = "vps_verify:"
    GROUP_CHALLENGE_ADMIN_APPROVE_PREFIX: str = "vps_admin_approve:"
    GROUP_CHALLENGE_ADMIN_REJECT_PREFIX: str = "vps_admin_reject:"
    GROUP_CHALLENGE_WELCOME_TEXT: str = (
        "✅ 入群二次验证通过\n\n"
        "欢迎加入群组。请遵守群规；首次发言仍会进行 AI 内容审核。"
    )
    GROUP_CHALLENGE_FAILURE_TEXT: str = "❌ 入群二次验证未通过\n\n该用户已被移出群组。"
    INITIAL_VERIFICATION_TIMEOUT_TEXT: str = "⌛ 入群申请已超时\n\n该用户未在规定时间内完成安全验证，申请已自动拒绝。"
    INITIAL_VERIFICATION_FAILURE_TEXT: str = "❌ 入群申请验证失败\n\n该用户未通过安全验证，申请已自动拒绝。"
    STALE_INITIAL_VERIFICATION_TEXT: str = "此验证会话已失效，请在 Telegram 私聊中点击最新的“开始安全验证”按钮。"
    # Retry window for second-stage prompts Telegram never accepted (bot lost
    # permissions, member already left, ...). Afterwards the normal timeout applies.
    UNDELIVERED_CHALLENGE_GRACE_MINUTES: int = 30
    MANUAL_JOIN_WELCOME_TEXT: str = (
        "✅ 管理员已批准入群申请\n\n"
        "欢迎加入群组。请完成下方二次验证；完成前暂不能发言。"
    )

    @staticmethod
    def _is_terminal_expiry_error(error: Exception) -> bool:
        """Telegram may have already removed a join request before our expiry worker sees it."""
        detail = str(error).casefold()
        return any(
            marker in detail
            for marker in (
                "user is deactivated",
                "join request not found",
                "request not found",
                "user not found",
                "hide_requester_missing",
            )
        )

    @staticmethod
    def _is_terminal_private_message_error(error: Exception) -> bool:
        """Return whether Telegram has definitively refused the verification DM."""
        detail = str(error).casefold()
        return any(
            marker in detail
            for marker in (
                "bot can't initiate conversation with a user",
                "bot was blocked by the user",
                "user is deactivated",
                "chat not found",
            )
        )

    @staticmethod
    def _is_absent_member_lookup_error(error: Exception) -> bool:
        """Telegram cannot return membership for a requester who never joined.

        A timed-out join request commonly produces ``member not found`` from
        getChatMember. That means "not already admitted", so the request should
        proceed to the normal decline path instead of being retried forever.
        """
        detail = str(error).casefold()
        return any(marker in detail for marker in (
            "member not found", "user not found", "user is deactivated",
        ))

    @staticmethod
    def _is_already_group_member_error(error: Exception) -> bool:
        """Telegram reports this when an administrator won the approval race."""
        detail = str(error).casefold()
        return "user_already_participant" in detail or "user already participant" in detail

    def __init__(
        self,
        repository: AuditRepository,
        gateway: TelegramGateway,
        verification_ttl_minutes: int = 5,
        join_verify_url: str = "",
        secondary_verification_ttl_minutes: int = 2,
        welcome_channel_url_by_group: Mapping[int, str] | None = None,
        join_guard_policy: JoinGuardPolicy | None = None,
        admin_user_ids: frozenset[int] = EMPTY_IDS,
        secondary_verification_ban_hours: int = 1,
    ) -> None:
        self.repository: AuditRepository = repository
        self.gateway: TelegramGateway = gateway
        self.verification_ttl_minutes: int = verification_ttl_minutes
        self.secondary_verification_ttl_minutes: int = secondary_verification_ttl_minutes
        self.secondary_verification_ban_hours: int = secondary_verification_ban_hours
        self.join_verify_url: str = join_verify_url.rstrip("/") + "/" if join_verify_url else ""
        self.welcome_channel_url_by_group: dict[int, str] = dict(welcome_channel_url_by_group or {})
        self.join_guard: JoinGuard = JoinGuard(repository, join_guard_policy or JoinGuardPolicy())
        self.admin_user_ids: frozenset[int] = admin_user_ids

    @property
    def verification_ban_summary(self) -> str:
        if self.secondary_verification_ban_hours == 0:
            return "永久封禁。"
        return f"封禁 {self.secondary_verification_ban_hours} 小时，到期后可重新申请入群。"

    @property
    def group_challenge_failure_text(self) -> str:
        return f"{self.GROUP_CHALLENGE_FAILURE_TEXT}\n{self.verification_ban_summary}"

    async def _ban_failed_verification(self, chat_id: int, user_id: int) -> None:
        await self.gateway.ban_member(
            chat_id, user_id, duration_hours=self.secondary_verification_ban_hours
        )
        logger.info(
            "onboarding.verification_failed_ban chat_id=%s user_id=%s duration_hours=%s",
            chat_id, user_id, self.secondary_verification_ban_hours,
        )

    async def start(self, request: JoinRequestInput) -> PendingJoin:
        guard = self.join_guard.evaluate(request)
        if guard.notify_admins:
            await self._notify_join_guard(request, guard)
        if guard.reject:
            try:
                await self.gateway.decline_join_request(request.chat_id, request.user_id)
            except Exception as error:
                # Replayed Telegram updates frequently refer to a requester that is
                # already gone. Treat those terminal responses as successfully
                # closed so the webhook is acknowledged instead of retried forever.
                if not self._is_terminal_expiry_error(error):
                    raise
                logger.info(
                    "join_guard.request_already_closed chat_id=%s user_id=%s reason=%s",
                    request.chat_id,
                    request.user_id,
                    error,
                )
            logger.warning(
                (
                    "join_guard.request_rejected chat_id=%s user_id=%s mode=%s reason=%s "
                    "one_minute=%s five_minutes=%s"
                ),
                request.chat_id,
                request.user_id,
                guard.mode,
                guard.reason,
                guard.one_minute_count,
                guard.five_minute_count,
            )
            return PendingJoin(OnboardingState.DECLINED, None)
        if self.repository.is_user_blocked(request.user_id):
            await self.gateway.decline_join_request(request.chat_id, request.user_id)
            logger.warning("onboarding.blocked_user_declined chat_id=%s user_id=%s", request.chat_id, request.user_id)
            return PendingJoin(OnboardingState.BANNED, None)
        if not self.join_verify_url:
            raise RuntimeError("JOIN_VERIFY_URL is required for join-request verification")

        token = secrets.token_urlsafe(24)
        expires_at = request.requested_at + timedelta(minutes=self.verification_ttl_minutes)
        verification_created = self.repository.create_pending_verification(
            request, self._token_hash(token), expires_at.isoformat()
        )
        if not verification_created:
            current_state = self.repository.onboarding_state(request.chat_id, request.user_id)
            # A user who left and applies again can still have a trusted state from
            # their previous membership. Telegram's current member status separates
            # that new application from a delayed retry of the old webhook update.
            try:
                still_in_group = await self.gateway.is_group_member(request.chat_id, request.user_id)
            except Exception as error:
                logger.warning(
                    "onboarding.verification_retry_member_lookup_failed chat_id=%s user_id=%s state=%s reason=%s",
                    request.chat_id,
                    request.user_id,
                    current_state,
                    error,
                )
                still_in_group = True
            if not still_in_group:
                verification_created = self.repository.create_pending_verification(
                    request,
                    self._token_hash(token),
                    expires_at.isoformat(),
                    restart_existing=True,
                )
                if verification_created:
                    logger.info(
                        "onboarding.rejoin_verification_restarted chat_id=%s user_id=%s old_state=%s",
                        request.chat_id,
                        request.user_id,
                        current_state,
                    )
            # A retry of an old join-request update arrived after this member had
            # already advanced. Acknowledge it without sending a stale first-stage
            # link or regressing the persisted state.
        if not verification_created:
            current_state = self.repository.onboarding_state(request.chat_id, request.user_id)
            logger.info(
                "onboarding.verification_retry_ignored chat_id=%s user_id=%s state=%s",
                request.chat_id,
                request.user_id,
                current_state,
            )
            return PendingJoin(current_state or OnboardingState.VERIFICATION_PENDING, None)
        verification_url = f"{self.join_verify_url}?{urlencode({'session': token})}"
        try:
            await self.gateway.send_join_verification(
                request.user_chat_id, verification_url, request.group_title
            )
        except Exception as error:
            if not self._is_terminal_private_message_error(error):
                raise
            # The pending row deliberately remains active: an administrator may still
            # approve the request, after which the chat-member update starts stage two.
            logger.warning(
                "onboarding.verification_dm_unavailable chat_id=%s user_id=%s reason=%s",
                request.chat_id,
                request.user_id,
                error,
            )
            return PendingJoin(OnboardingState.VERIFICATION_PENDING, None)
        logger.info(
            "onboarding.verification_sent chat_id=%s user_id=%s ttl_minutes=%s",
            request.chat_id, request.user_id, self.verification_ttl_minutes,
        )
        return PendingJoin(OnboardingState.VERIFICATION_PENDING, verification_url)

    async def _notify_join_guard(
        self, request: JoinRequestInput, decision: JoinGuardDecision
    ) -> None:
        if not self.admin_user_ids:
            return
        mode = {
            JoinGuardMode.HIGH: "高防",
            JoinGuardMode.LOCKDOWN: "完全封锁",
            JoinGuardMode.PERMANENT_HIGH: "永久高防",
            JoinGuardMode.PERMANENT_LOCKDOWN: "永久完全封锁",
        }.get(decision.mode, "正常")
        try:
            await self.gateway.send_private_join_guard_alert(
                self.admin_user_ids,
                (
                    f"🛡 入群防护：{mode}\n"
                    f"群组：{request.group_title or request.chat_id}\n"
                    f"最近 1 分钟：{decision.one_minute_count}\n"
                    f"最近 5 分钟：{decision.five_minute_count}\n"
                    f"纯英文单词昵称占比：{decision.suspicious_name_ratio:.0%}\n\n"
                    "邀请链接保持不变；可在群内使用 /join_guard 查看或调整。"
                ),
            )
        except Exception:
            logger.exception("join_guard.admin_notice_failed chat_id=%s", request.chat_id)

    def set_join_guard_mode(self, chat_id: int, mode: JoinGuardMode) -> None:
        self.join_guard.set_mode(chat_id, mode)

    def join_guard_status(self, chat_id: int) -> tuple[JoinGuardMode, datetime | None]:
        return self.join_guard.status(chat_id)

    def mark_member_departed(self, chat_id: int, user_id: int, departed_at: datetime) -> bool:
        """Forget in-group verification state when a member voluntarily leaves."""
        changed = self.repository.mark_member_departed(chat_id, user_id, departed_at.isoformat())
        if changed:
            logger.info("onboarding.member_departed chat_id=%s user_id=%s", chat_id, user_id)
        return changed

    async def start_direct_join(self, joined: DirectJoinInput) -> PendingJoin:
        """Handle ordinary joins where Telegram did not create a join-request update."""
        existing_state = self.repository.onboarding_state(joined.chat_id, joined.user_id)
        manually_approved = False
        if existing_state is OnboardingState.VERIFICATION_PENDING:
            # An administrator can approve the Telegram join request without ever opening
            # our Mini App. The new-member update is the signal to retire that first-stage
            # deadline, then begin the required in-group second stage with a fresh deadline.
            if self.repository.mark_join_request_manually_approved(
                joined.chat_id, joined.user_id, joined.joined_at.isoformat()
            ):
                logger.info(
                    "onboarding.join_request_manually_approved chat_id=%s user_id=%s",
                    joined.chat_id,
                    joined.user_id,
                )
                existing_state = OnboardingState.PENDING_FIRST_MESSAGE
                manually_approved = True
                await self._send_manual_join_welcome(joined.chat_id, joined.user_id)
        if existing_state is not None and existing_state in {
            OnboardingState.SECONDARY_VERIFICATION_PENDING,
            OnboardingState.PENDING_FIRST_MESSAGE,
            OnboardingState.OBSERVING,
        } and not manually_approved:
            logger.info(
                "onboarding.direct_join_known_member chat_id=%s user_id=%s state=%s",
                joined.chat_id,
                joined.user_id,
                existing_state,
            )
            return PendingJoin(existing_state, None)
        if self.repository.is_user_blocked(joined.user_id):
            await self.gateway.ban_member(joined.chat_id, joined.user_id)
            logger.warning("onboarding.blocked_direct_join_banned chat_id=%s user_id=%s", joined.chat_id, joined.user_id)
            return PendingJoin(OnboardingState.BANNED, None)

        return await self._start_group_challenge(
            chat_id=joined.chat_id,
            user_id=joined.user_id,
            username=joined.username,
            verification_flow="direct_join",
            started_at=joined.joined_at,
            display_name=joined.display_name,
        )

    async def resume_pending_direct_join_challenges(self, allowed_group_ids: frozenset[int]) -> int:
        """Replace old click-to-pass buttons after a deployment without stranding members."""
        resumed = 0
        for chat_id, user_id, username in self.repository.pending_direct_join_verifications():
            if chat_id not in allowed_group_ids:
                continue
            try:
                _ = await self._start_group_challenge(
                    chat_id=chat_id,
                    user_id=user_id,
                    username=username,
                    verification_flow="direct_join",
                    started_at=datetime.now(timezone.utc),
                )
            except Exception:
                logger.exception(
                    "onboarding.group_challenge_resume_failed chat_id=%s user_id=%s",
                    chat_id,
                    user_id,
                )
                continue
            resumed += 1
        if resumed:
            logger.info("onboarding.group_challenge_resumed count=%s", resumed)
        return resumed

    def _undelivered_challenge_deadline(self, now: datetime) -> str:
        """Challenges still undelivered this long after their deadline stop retrying."""
        return (now - timedelta(minutes=self.UNDELIVERED_CHALLENGE_GRACE_MINUTES)).isoformat()

    async def resume_undelivered_group_challenges(self, allowed_group_ids: frozenset[int]) -> int:
        """Retry active second-stage checks that have no Telegram prompt message."""
        resumed = 0
        now = datetime.now(timezone.utc)
        retry_before = (now - timedelta(minutes=2)).isoformat()
        for chat_id, user_id, username, verification_flow in self.repository.undelivered_group_challenges(
            retry_before, self._undelivered_challenge_deadline(now)
        ):
            if chat_id not in allowed_group_ids:
                continue
            try:
                outcome = await self._start_group_challenge(
                    chat_id=chat_id,
                    user_id=user_id,
                    username=username,
                    verification_flow=verification_flow,
                    started_at=now,
                    only_if_undelivered=True,
                    retry_before=retry_before,
                )
            except Exception:
                logger.exception(
                    "onboarding.undelivered_group_challenge_resume_failed chat_id=%s user_id=%s",
                    chat_id,
                    user_id,
                )
                continue
            if outcome.callback_data is not None:
                resumed += 1
        if resumed:
            logger.info("onboarding.undelivered_group_challenges_resumed count=%s", resumed)
        return resumed

    async def _start_group_challenge(
        self,
        *,
        chat_id: int,
        user_id: int,
        username: str | None,
        verification_flow: str,
        started_at: datetime,
        only_if_undelivered: bool = False,
        retry_before: str | None = None,
        display_name: str | None = None,
    ) -> PendingJoin:
        prompt, options, answer_index, token = self._new_group_question()
        expires_at = started_at + timedelta(minutes=self.secondary_verification_ttl_minutes)
        created = self.repository.create_group_challenge(
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            token_hash=self._token_hash(token),
            answer_index=answer_index,
            expires_at=expires_at.isoformat(),
            now=started_at.isoformat(),
            verification_flow=verification_flow,
            only_if_undelivered=only_if_undelivered,
            retry_before=retry_before,
        )
        if not created:
            state = self.repository.onboarding_state(chat_id, user_id) or OnboardingState.DECLINED
            return PendingJoin(state, None)
        choices = self._group_choices(options, token)
        await self.gateway.restrict_member(chat_id, user_id, minutes=60 * 24 * 365)
        if display_name is None and not username:
            # Join requests already recorded the applicant's name; avoid an extra API call.
            display_name = self.repository.latest_join_request_display_name(chat_id, user_id)
        message_id = await self.gateway.send_group_vps_challenge(
            chat_id, user_id, username, prompt, choices,
            self.secondary_verification_ttl_minutes, display_name=display_name,
        )
        delivered_at = datetime.now(timezone.utc)
        _ = self.repository.set_group_challenge_message_id(
            chat_id,
            user_id,
            message_id,
            delivered_at.isoformat(),
            (delivered_at + timedelta(minutes=self.secondary_verification_ttl_minutes)).isoformat(),
            token_hash=self._token_hash(token),
        )
        logger.info(
            "onboarding.group_challenge_sent chat_id=%s user_id=%s flow=%s ttl_minutes=%s",
            chat_id,
            user_id,
            verification_flow,
            self.secondary_verification_ttl_minutes,
        )
        return PendingJoin(OnboardingState.SECONDARY_VERIFICATION_PENDING, token)

    async def verify_token(self, token: str, telegram_user_id: int) -> JoinVerificationOutcome:
        now = datetime.now(timezone.utc).isoformat()
        token_hash = self._token_hash(token)
        joined = self.repository.consume_verification(token_hash, telegram_user_id, now)
        if joined is None:
            logger.warning("onboarding.verification_rejected user_id=%s", telegram_user_id)
            return JoinVerificationOutcome(False, OnboardingState.DECLINED, self.STALE_INITIAL_VERIFICATION_TEXT)
        chat_id, user_id, verification_flow = joined
        try:
            if verification_flow == "join_request":
                await self.gateway.approve_join_request(chat_id, user_id)
            elif verification_flow == "direct_join":
                # Legacy callbacks cannot complete the post-join challenge.
                return JoinVerificationOutcome(
                    False, OnboardingState.SECONDARY_VERIFICATION_PENDING, "请完成群内二次验证"
                )
            else:
                raise ValueError(f"unknown verification flow: {verification_flow}")
        except Exception as error:
            if self._is_already_group_member_error(error):
                # An administrator approved the same request between Turnstile
                # completion and this API call.  Approval is idempotently complete;
                # continue with stage two instead of rolling the member backwards.
                logger.info(
                    "onboarding.approval_already_completed chat_id=%s user_id=%s",
                    chat_id,
                    user_id,
                )
            else:
                logger.exception("onboarding.approval_failed chat_id=%s user_id=%s", chat_id, user_id)
                retry_expiry = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
                self.repository.reset_verification_after_approval_failure(
                    chat_id, user_id, token_hash, retry_expiry, now
                )
                return JoinVerificationOutcome(
                    False,
                    OnboardingState.VERIFICATION_PENDING,
                    "自动放行失败，请重新申请入群",
                )
        try:
            _ = await self._start_group_challenge(
                chat_id=chat_id,
                user_id=user_id,
                username=self.repository.onboarding_username(chat_id, user_id),
                verification_flow=verification_flow,
                started_at=datetime.now(timezone.utc),
            )
        except Exception:
            logger.exception("onboarding.group_challenge_start_failed chat_id=%s user_id=%s", chat_id, user_id)
            return JoinVerificationOutcome(
                False,
                OnboardingState.SECONDARY_VERIFICATION_PENDING,
                "已完成第一阶段验证，请在群内等待二次验证题目。",
            )
        return JoinVerificationOutcome(
            True,
            OnboardingState.SECONDARY_VERIFICATION_PENDING,
            "第一阶段验证通过。请在群内完成二次验证。",
        )

    def has_current_initial_verification(self, token: str, telegram_user_id: int) -> bool:
        return self.repository.has_current_initial_verification(
            self._token_hash(token), telegram_user_id, datetime.now(timezone.utc).isoformat(),
        )

    async def fail_initial_verification(
        self, token: str, telegram_user_id: int
    ) -> JoinVerificationOutcome:
        """Reject a join request after its human-verification attempt fails."""
        now = datetime.now(timezone.utc).isoformat()
        pending = self.repository.pending_initial_verification(self._token_hash(token), telegram_user_id)
        if pending is None:
            return JoinVerificationOutcome(False, OnboardingState.DECLINED, "验证已过期或不属于当前账号")
        chat_id, user_id = pending
        try:
            await self.gateway.decline_join_request(chat_id, user_id)
        except Exception as error:
            if self._is_terminal_expiry_error(error):
                _ = self.repository.mark_initial_verification_failed(chat_id, user_id, now)
                await self._send_initial_verification_result(
                    chat_id, user_id, self.INITIAL_VERIFICATION_FAILURE_TEXT
                )
                return JoinVerificationOutcome(False, OnboardingState.DECLINED, "验证失败，申请已关闭")
            logger.exception("onboarding.initial_verification_decline_failed chat_id=%s user_id=%s", chat_id, user_id)
            return JoinVerificationOutcome(False, OnboardingState.VERIFICATION_PENDING, "验证失败，暂时无法处理申请")
        if self.repository.mark_initial_verification_failed(chat_id, user_id, now):
            await self._send_initial_verification_result(
                chat_id, user_id, self.INITIAL_VERIFICATION_FAILURE_TEXT
            )
        return JoinVerificationOutcome(False, OnboardingState.DECLINED, "人机验证失败，入群申请已拒绝")

    async def _send_initial_verification_result(
        self, chat_id: int, user_id: int, text: str, delete_after_seconds: int | None = None
    ) -> None:
        try:
            await self.gateway.send_initial_verification_result(
                chat_id,
                user_id,
                self.repository.onboarding_username(chat_id, user_id),
                text,
                delete_after_seconds,
            )
        except Exception:
            logger.exception(
                "onboarding.initial_verification_notice_failed chat_id=%s user_id=%s",
                chat_id,
                user_id,
            )

    async def _send_manual_join_welcome(self, chat_id: int, user_id: int) -> None:
        try:
            await self.gateway.send_manual_join_welcome(chat_id, user_id)
        except Exception:
            # A welcome notice must never stop the actual second-stage protection.
            logger.exception("onboarding.manual_join_welcome_failed chat_id=%s user_id=%s", chat_id, user_id)

    @staticmethod
    def _new_group_question() -> tuple[str, list[str], int, str]:
        """Pick a random question with shuffled options and a fresh one-time token."""
        question = secrets.choice(GLOSSARY_QUESTIONS)
        options = list(question.options)
        random.SystemRandom().shuffle(options)
        return (
            question.prompt,
            options,
            options.index(question.correct_option),
            secrets.token_urlsafe(12),
        )

    def _group_choices(self, options: list[str], token: str) -> tuple[tuple[str, str], ...]:
        return tuple(
            (option, f"{self.GROUP_CHALLENGE_PREFIX}{token}:{index}")
            for index, option in enumerate(options)
        )

    async def verify_group_challenge(
        self,
        callback_data: str,
        telegram_user_id: int,
        *,
        username: str | None = None,
        display_name: str | None = None,
    ) -> JoinVerificationOutcome:
        try:
            prefix, token, index_text = callback_data.split(":", maxsplit=2)
            if prefix != self.GROUP_CHALLENGE_PREFIX.removesuffix(":"):
                raise ValueError
            selected_index = int(index_text)
        except ValueError:
            return JoinVerificationOutcome(False, OnboardingState.DECLINED, "验证请求无效")
        # A wrong answer switches to a new question, so eliminating options across
        # attempts does not help: random guessing passes 1 - (3/4)^3 ≈ 58%, not 75%.
        next_prompt, next_options, next_answer_index, next_token = self._new_group_question()
        result = self.repository.resolve_group_challenge(
            self._token_hash(token),
            telegram_user_id,
            selected_index,
            datetime.now(timezone.utc).isoformat(),
            next_token_hash=self._token_hash(next_token),
            next_answer_index=next_answer_index,
        )
        if result.chat_id is None:
            return JoinVerificationOutcome(False, OnboardingState.DECLINED, "验证已过期或不属于当前账号")
        if result.accepted:
            try:
                await self.gateway.release_member(result.chat_id, result.user_id or telegram_user_id)
            except Exception:
                logger.exception(
                    "onboarding.group_challenge_release_failed chat_id=%s user_id=%s",
                    result.chat_id,
                    result.user_id,
                )
                self.repository.reopen_group_challenge(
                    result.chat_id, result.user_id or telegram_user_id, datetime.now(timezone.utc).isoformat()
                )
                return JoinVerificationOutcome(
                    False, OnboardingState.SECONDARY_VERIFICATION_PENDING, "验证通过，但暂时无法解除禁言"
                )
            self.repository.finalize_group_challenge(
                result.chat_id, result.user_id or telegram_user_id, datetime.now(timezone.utc).isoformat()
            )
            await self._replace_group_challenge_message(
                result.chat_id, result.message_id, result.user_id or telegram_user_id,
                self.GROUP_CHALLENGE_WELCOME_TEXT,
            )
            return JoinVerificationOutcome(
                True, OnboardingState.PENDING_FIRST_MESSAGE, "验证通过，已解除禁言。首次发言仍会经 Kimi 审核。"
            )
        if result.terminal:
            try:
                await self._ban_failed_verification(result.chat_id, result.user_id or telegram_user_id)
            except Exception:
                logger.exception(
                    "onboarding.group_challenge_ban_failed chat_id=%s user_id=%s",
                    result.chat_id,
                    result.user_id,
                )
                self.repository.reopen_group_challenge(
                    result.chat_id, result.user_id or telegram_user_id, datetime.now(timezone.utc).isoformat()
                )
                return JoinVerificationOutcome(False, OnboardingState.DECLINED, "验证失败，移出群组时发生错误")
            self.repository.finalize_group_challenge(
                result.chat_id, result.user_id or telegram_user_id, datetime.now(timezone.utc).isoformat()
            )
            await self._replace_group_challenge_message(
                result.chat_id, result.message_id, result.user_id or telegram_user_id,
                self.group_challenge_failure_text,
            )
            return JoinVerificationOutcome(
                False, OnboardingState.DECLINED, f"三次回答错误，已{self.verification_ban_summary}"
            )
        if result.message_id is not None:
            try:
                await self.gateway.update_group_vps_challenge(
                    result.chat_id,
                    result.message_id,
                    result.user_id or telegram_user_id,
                    username,
                    next_prompt,
                    self._group_choices(next_options, next_token),
                    self.secondary_verification_ttl_minutes,
                    result.attempts_left,
                    display_name=display_name,
                    expires_at=result.expires_at,
                )
                if result.expires_at is None:
                    raise RuntimeError("rotated challenge is missing its deadline")
                _ = self.repository.set_group_challenge_message_id(
                    result.chat_id, result.user_id or telegram_user_id, result.message_id,
                    datetime.now(timezone.utc).isoformat(), result.expires_at,
                    token_hash=self._token_hash(next_token),
                )
            except Exception:
                # The member did not receive a usable new question. Recover through
                # the durable delivery queue; don't expire them on the old answer timer.
                recovered = self.repository.retry_failed_group_challenge_edit(
                    result.chat_id, result.user_id or telegram_user_id,
                    self._token_hash(next_token), result.message_id,
                    datetime.now(timezone.utc).isoformat(),
                )
                logger.exception(
                    "onboarding.group_challenge_rotate_failed chat_id=%s user_id=%s",
                    result.chat_id,
                    result.user_id,
                )
                if recovered:
                    return JoinVerificationOutcome(
                        False, OnboardingState.SECONDARY_VERIFICATION_PENDING,
                        "题目更新失败，正在重新发送，请稍候。剩余答题次数保留。",
                    )
        return JoinVerificationOutcome(
            False,
            OnboardingState.SECONDARY_VERIFICATION_PENDING,
            f"回答错误，已换一道新题，还可尝试 {result.attempts_left} 次。",
        )

    async def resolve_group_challenge_by_admin(
        self, token: str, approved: bool, *, chat_id: int | None = None
    ) -> JoinVerificationOutcome:
        """A group administrator may settle an active second-stage verification."""
        result = self.repository.resolve_group_challenge_by_admin(
            self._token_hash(token), datetime.now(timezone.utc).isoformat(), approved, chat_id
        )
        if result.chat_id is None or result.user_id is None:
            return JoinVerificationOutcome(False, OnboardingState.DECLINED, "验证已过期或已处理")
        try:
            if approved:
                await self.gateway.release_member(result.chat_id, result.user_id)
                text = self.GROUP_CHALLENGE_WELCOME_TEXT
                reason = "管理员已通过验证并解除禁言。"
            else:
                await self._ban_failed_verification(result.chat_id, result.user_id)
                text = self.group_challenge_failure_text
                reason = f"管理员已拒绝验证，已{self.verification_ban_summary}"
        except Exception:
            logger.exception(
                "onboarding.group_challenge_admin_action_failed chat_id=%s user_id=%s approved=%s",
                result.chat_id,
                result.user_id,
                approved,
            )
            self.repository.reopen_group_challenge(
                result.chat_id, result.user_id, datetime.now(timezone.utc).isoformat()
            )
            return JoinVerificationOutcome(
                False,
                OnboardingState.SECONDARY_VERIFICATION_PENDING,
                "Telegram 操作失败，验证已恢复为待处理状态，请重试或手动处理该成员。",
            )
        self.repository.finalize_group_challenge(
            result.chat_id, result.user_id, datetime.now(timezone.utc).isoformat()
        )
        await self._replace_group_challenge_message(result.chat_id, result.message_id, result.user_id, text)
        return JoinVerificationOutcome(
            approved,
            OnboardingState.PENDING_FIRST_MESSAGE if approved else OnboardingState.DECLINED,
            reason,
        )

    async def _replace_group_challenge_message(
        self,
        chat_id: int,
        message_id: int | None,
        user_id: int,
        text: str,
        delete_after_seconds: int | None = None,
    ) -> None:
        if message_id is None:
            return
        try:
            await self.gateway.replace_group_verification_message(
                chat_id,
                message_id,
                user_id,
                self.repository.onboarding_username(chat_id, user_id),
                text,
                delete_after_seconds,
                self.welcome_channel_url_by_group.get(chat_id)
                if text == self.GROUP_CHALLENGE_WELCOME_TEXT
                else None,
            )
        except Exception as error:
            # The Telegram action must not be considered failed only because an old challenge
            # message was removed by another moderator.
            if "message to edit not found" in str(error).lower():
                logger.info(
                    (
                        "onboarding.group_challenge_message_already_removed "
                        "chat_id=%s message_id=%s"
                    ),
                    chat_id,
                    message_id,
                )
                return
            logger.exception(
                "onboarding.group_challenge_message_replace_failed chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )

    def mark_first_message(self, chat_id: int, user_id: int, timestamp: datetime) -> bool:
        """Advance the onboarding state for verified newcomers only."""
        is_first_message = self.repository.mark_first_message(chat_id, user_id, timestamp.isoformat())
        if is_first_message:
            logger.info("onboarding.first_message_started chat_id=%s user_id=%s", chat_id, user_id)
        return is_first_message

    def mark_first_observed_message(self, chat_id: int, user_id: int, timestamp: datetime) -> bool:
        """Return true for the first message this Bot sees from a member in this group.

        This deliberately works for existing members as well as members that joined after
        the Bot. The onboarding state update remains best-effort bookkeeping only.
        """
        _ = self.mark_first_message(chat_id, user_id, timestamp)
        first_observed = self.repository.mark_first_observed_message(
            chat_id, user_id, timestamp.isoformat()
        )
        if first_observed:
            logger.info("message.first_observed chat_id=%s user_id=%s", chat_id, user_id)
        return first_observed

    async def expire_pending_verifications(self, now: datetime | None = None) -> int:
        """Reject/ban unverified joins whose deadline passed, retrying Telegram failures later."""
        current = now or datetime.now(timezone.utc)
        timestamp = current.isoformat()
        expired = self.repository.expired_pending_verifications(
            timestamp, self._undelivered_challenge_deadline(current)
        )
        # A join-request flood can leave thousands of expired rows. Eight workers
        # reduce recovery from roughly an hour to minutes while staying below
        # Telegram's normal Bot API request budget.
        semaphore = asyncio.Semaphore(8)

        async def process(
            record: tuple[int, int, str, OnboardingState, int | None]
        ) -> int:
            async with semaphore:
                return await self._expire_pending_verification(record, current, timestamp)

        return sum(await asyncio.gather(*(process(record) for record in expired)))

    async def _expire_pending_verification(
        self,
        record: tuple[int, int, str, OnboardingState, int | None],
        current: datetime,
        timestamp: str,
    ) -> int:
        chat_id, user_id, verification_flow, state, message_id = record
        if state is OnboardingState.VERIFICATION_PENDING and verification_flow == "join_request":
            try:
                already_joined = await self.gateway.is_group_member(chat_id, user_id)
            except Exception as error:
                if self._is_absent_member_lookup_error(error):
                    already_joined = False
                    logger.info(
                        "onboarding.expiry_requester_not_member chat_id=%s user_id=%s",
                        chat_id,
                        user_id,
                    )
                else:
                    # Membership is genuinely unknown. Never turn a failed read into a
                    # destructive decline: leave the row pending for the next cycle.
                    logger.warning(
                        (
                            "onboarding.expiry_member_status_lookup_deferred "
                            "chat_id=%s user_id=%s reason=%s"
                        ),
                        chat_id,
                        user_id,
                        error,
                    )
                    return 0
            if already_joined:
                # The new-member event can be delayed or missed during a deployment.
                # Do not reject an already approved member; convert them to the second
                # stage instead, which gets its own fresh timeout.
                if self.repository.mark_join_request_manually_approved(chat_id, user_id, timestamp):
                    await self._send_manual_join_welcome(chat_id, user_id)
                    try:
                        _ = await self._start_group_challenge(
                            chat_id=chat_id,
                            user_id=user_id,
                            username=None,
                            verification_flow="join_request",
                            started_at=current,
                        )
                    except Exception:
                        logger.exception(
                            "onboarding.manual_join_group_challenge_start_failed chat_id=%s user_id=%s",
                            chat_id,
                            user_id,
                        )
                return 0
        # Claim the row before calling Telegram. This prevents a delayed timeout worker
        # from removing a member an administrator has just approved.
        if not self.repository.claim_expired_verification(
            chat_id, user_id, state, timestamp, self._undelivered_challenge_deadline(current)
        ):
            logger.info(
                "onboarding.expiry_skipped_already_resolved chat_id=%s user_id=%s flow=%s",
                chat_id,
                user_id,
                verification_flow,
            )
            return 0
        try:
            if state is OnboardingState.VERIFICATION_PENDING and verification_flow == "join_request":
                await self.gateway.decline_join_request(chat_id, user_id)
            else:
                # The member is already in the group during the post-join challenge.
                await self._ban_failed_verification(chat_id, user_id)
        except Exception as error:
            if self._is_terminal_expiry_error(error):
                finalized = self._finalize_expired_record(
                    chat_id, user_id, verification_flow, state, timestamp
                )
                if finalized and state is OnboardingState.SECONDARY_VERIFICATION_PENDING:
                    await self._replace_group_challenge_message(
                        chat_id,
                        message_id,
                        user_id,
                        self.group_challenge_failure_text,
                        delete_after_seconds=60,
                    )
                logger.info(
                    "onboarding.expiry_already_closed chat_id=%s user_id=%s flow=%s reason=%s",
                    chat_id,
                    user_id,
                    verification_flow,
                    error,
                )
                return int(finalized)
            self.repository.reopen_expired_verification(chat_id, user_id, state, timestamp)
            logger.exception(
                "onboarding.expiry_action_failed chat_id=%s user_id=%s flow=%s",
                chat_id,
                user_id,
                verification_flow,
            )
            return 0
        finalized = self._finalize_expired_record(
            chat_id, user_id, verification_flow, state, timestamp
        )
        if finalized:
            if state is OnboardingState.SECONDARY_VERIFICATION_PENDING:
                await self._replace_group_challenge_message(
                    chat_id,
                    message_id,
                    user_id,
                    self.group_challenge_failure_text,
                    delete_after_seconds=60,
                )
            logger.info(
                "onboarding.expired chat_id=%s user_id=%s flow=%s",
                chat_id,
                user_id,
                verification_flow,
            )
        return int(finalized)

    def _finalize_expired_record(
        self,
        chat_id: int,
        user_id: int,
        verification_flow: str,
        state: OnboardingState,
        timestamp: str,
    ) -> bool:
        if state is OnboardingState.VERIFICATION_PENDING and verification_flow == "join_request":
            return self.repository.delete_onboarding_record(chat_id, user_id)
        return self.repository.finalize_expired_verification(chat_id, user_id, timestamp)

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
