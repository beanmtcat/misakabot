from __future__ import annotations

import hashlib
import logging
import random
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from .domain import DirectJoinInput, JoinRequestInput, JoinVerificationOutcome, OnboardingState
from .repository import AuditRepository
from .telegram_gateway import TelegramGateway

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


@dataclass(frozen=True)
class PendingJoin:
    state: OnboardingState
    callback_data: str | None


@dataclass(frozen=True)
class VpsSafetyQuestion:
    prompt: str
    options: tuple[str, ...]
    correct_option: str


VPS_SAFETY_QUESTIONS: tuple[VpsSafetyQuestion, ...] = (
    VpsSafetyQuestion(
        "购买二手 VPS 时，卖家催你“现在转账，不然就卖给别人”。更稳妥的做法是？",
        (
            "先核验卖家和资产归属，再决定是否交易",
            "立刻转账，避免错过名额",
            "只要卖家发过截图就直接付款",
            "把验证码交给卖家处理",
        ),
        "先核验卖家和资产归属，再决定是否交易",
    ),
    VpsSafetyQuestion(
        "交易 VPS 或账号时，哪项最能降低“付款后失联”风险？",
        (
            "使用可核验的可信中介，并保留完整交易记录",
            "只在私聊里口头约定",
            "先把全款转给对方再说",
            "让对方提供一张余额截图",
        ),
        "使用可核验的可信中介，并保留完整交易记录",
    ),
    VpsSafetyQuestion(
        "卖家只有一张旧截图，且拒绝可信中介或担保流程。更稳妥的做法是？",
        (
            "暂不交易，等对方接受可信中介或担保流程",
            "截图看起来真实就直接付款",
            "把自己的账号密码发给卖家比对",
            "安装卖家发来的远程控制软件",
        ),
        "暂不交易，等对方接受可信中介或担保流程",
    ),
    VpsSafetyQuestion(
        "完成 VPS/账号交付后，第一时间应该做什么？",
        (
            "修改密码、绑定自己的二次验证并检查恢复方式",
            "继续沿用卖家给的全部登录方式",
            "把自己的验证码发给卖家备案",
            "关闭所有安全提醒以免打扰",
        ),
        "修改密码、绑定自己的二次验证并检查恢复方式",
    ),
    VpsSafetyQuestion(
        "对方指定一个你从未核验过的“中介”，并催你直接付款。应该？",
        (
            "停止操作，独立核验中介身份和担保规则",
            "按对方指定的中介流程立刻付款",
            "把支付验证码交给对方指定的中介",
            "邀请更多陌生人共同转账",
        ),
        "停止操作，独立核验中介身份和担保规则",
    ),
    VpsSafetyQuestion(
        "卖家声称机器“马上到期，低价秒出”，但不给出任何可验证资料。应该？",
        (
            "不因时间压力付款，先完成必要核验",
            "先付款锁定，再慢慢核验",
            "只要价格足够低就不必核验",
            "把银行卡和验证码发给对方",
        ),
        "不因时间压力付款，先完成必要核验",
    ),
    VpsSafetyQuestion(
        "VLESS Reality 为什么不宜直接拿 Cloudflare CDN 域名作 target？",
        (
            "会被滥用作 CF 端口转发，偷跑流量",
            "会自动获得该域名的 Cloudflare 账户权限",
            "会让自己的订阅链接立刻失效",
            "会把正常用户的代理流量全部改为经 Cloudflare 中转",
        ),
        "会被滥用作 CF 端口转发，偷跑流量",
    ),
)


class OnboardingService:
    """Automatic join verification plus per-group first-observed-message scrutiny."""

    GROUP_CHALLENGE_PREFIX = "vps_verify:"
    GROUP_CHALLENGE_ADMIN_APPROVE_PREFIX = "vps_admin_approve:"
    GROUP_CHALLENGE_ADMIN_REJECT_PREFIX = "vps_admin_reject:"
    GROUP_CHALLENGE_WELCOME_TEXT = (
        "✅ 入群二次验证通过\n\n"
        "欢迎加入群组。请遵守群规；首次发言仍会进行 AI 内容审核。"
    )
    GROUP_CHALLENGE_FAILURE_TEXT = "❌ 入群二次验证未通过\n\n该用户已被移出群组。"
    INITIAL_VERIFICATION_TIMEOUT_TEXT = "⌛ 入群申请已超时\n\n该用户未在规定时间内完成安全验证，申请已自动拒绝。"
    INITIAL_VERIFICATION_FAILURE_TEXT = "❌ 入群申请验证失败\n\n该用户未通过安全验证，申请已自动拒绝。"
    STALE_INITIAL_VERIFICATION_TEXT = "此验证会话已失效，请在 Telegram 私聊中点击最新的“开始安全验证”按钮。"
    MANUAL_JOIN_WELCOME_TEXT = (
        "✅ 管理员已批准入群申请\n\n"
        "欢迎加入群组。请完成下方 VPS 交易安全二次验证；完成前暂不能发言。"
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

    def __init__(
        self,
        repository: AuditRepository,
        gateway: TelegramGateway,
        verification_ttl_minutes: int = 5,
        join_verify_url: str = "",
        secondary_verification_ttl_minutes: int = 2,
        welcome_channel_url_by_group: Mapping[int, str] | None = None,
    ) -> None:
        self.repository = repository
        self.gateway = gateway
        self.verification_ttl_minutes = verification_ttl_minutes
        self.secondary_verification_ttl_minutes = secondary_verification_ttl_minutes
        self.join_verify_url = join_verify_url.rstrip("/") + "/" if join_verify_url else ""
        self.welcome_channel_url_by_group = dict(welcome_channel_url_by_group or {})

    async def start(self, request: JoinRequestInput) -> PendingJoin:
        if self.repository.is_user_blocked(request.user_id):
            await self.gateway.decline_join_request(request.chat_id, request.user_id)
            logger.warning("onboarding.blocked_user_declined chat_id=%s user_id=%s", request.chat_id, request.user_id)
            return PendingJoin(OnboardingState.BANNED, None)
        if not self.join_verify_url:
            raise RuntimeError("JOIN_VERIFY_URL is required for join-request verification")

        token = secrets.token_urlsafe(24)
        expires_at = request.requested_at + timedelta(minutes=self.verification_ttl_minutes)
        self.repository.create_pending_verification(
            request, self._token_hash(token), expires_at.isoformat()
        )
        verification_url = f"{self.join_verify_url}?{urlencode({'session': token})}"
        await self.gateway.send_join_verification(
            request.user_chat_id, verification_url, request.group_title
        )
        logger.info(
            "onboarding.verification_sent chat_id=%s user_id=%s ttl_minutes=%s",
            request.chat_id, request.user_id, self.verification_ttl_minutes,
        )
        return PendingJoin(OnboardingState.VERIFICATION_PENDING, verification_url)

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
        )

    async def resume_pending_direct_join_challenges(self, allowed_group_ids: frozenset[int]) -> int:
        """Replace old click-to-pass buttons after a deployment without stranding members."""
        resumed = 0
        for chat_id, user_id, username in self.repository.pending_direct_join_verifications():
            if chat_id not in allowed_group_ids:
                continue
            try:
                await self._start_group_challenge(
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

    async def resume_undelivered_group_challenges(self, allowed_group_ids: frozenset[int]) -> int:
        """Retry active second-stage checks that have no Telegram prompt message."""
        resumed = 0
        now = datetime.now(timezone.utc)
        for chat_id, user_id, username, verification_flow in self.repository.undelivered_group_challenges(
            now.isoformat()
        ):
            if chat_id not in allowed_group_ids:
                continue
            try:
                await self._start_group_challenge(
                    chat_id=chat_id,
                    user_id=user_id,
                    username=username,
                    verification_flow=verification_flow,
                    started_at=now,
                )
            except Exception:
                logger.exception(
                    "onboarding.undelivered_group_challenge_resume_failed chat_id=%s user_id=%s",
                    chat_id,
                    user_id,
                )
                continue
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
    ) -> PendingJoin:
        question = secrets.choice(VPS_SAFETY_QUESTIONS)
        options = list(question.options)
        random.SystemRandom().shuffle(options)
        answer_index = options.index(question.correct_option)
        token = secrets.token_urlsafe(12)
        expires_at = started_at + timedelta(minutes=self.secondary_verification_ttl_minutes)
        self.repository.create_group_challenge(
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            token_hash=self._token_hash(token),
            answer_index=answer_index,
            expires_at=expires_at.isoformat(),
            now=started_at.isoformat(),
            verification_flow=verification_flow,
        )
        choices = tuple(
            (option, f"{self.GROUP_CHALLENGE_PREFIX}{token}:{index}")
            for index, option in enumerate(options)
        )
        await self.gateway.restrict_member(chat_id, user_id, minutes=60 * 24 * 365)
        message_id = await self.gateway.send_group_vps_challenge(
            chat_id, user_id, username, question.prompt, choices, self.secondary_verification_ttl_minutes
        )
        self.repository.set_group_challenge_message_id(
            chat_id, user_id, message_id, datetime.now(timezone.utc).isoformat()
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
                    False, OnboardingState.SECONDARY_VERIFICATION_PENDING, "请完成群内 VPS 安全验证"
                )
            else:
                raise ValueError(f"unknown verification flow: {verification_flow}")
        except Exception:
            logger.exception("onboarding.approval_failed chat_id=%s user_id=%s", chat_id, user_id)
            retry_expiry = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
            self.repository.reset_verification_after_approval_failure(
                chat_id, user_id, token_hash, retry_expiry, now
            )
            return JoinVerificationOutcome(False, OnboardingState.VERIFICATION_PENDING, "自动放行失败，请重新申请入群")
        try:
            await self._start_group_challenge(
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
            "第一阶段验证通过。请在群内完成 VPS 交易安全验证。",
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
                self.repository.mark_initial_verification_failed(chat_id, user_id, now)
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

    async def verify_group_challenge(
        self, callback_data: str, telegram_user_id: int
    ) -> JoinVerificationOutcome:
        try:
            prefix, token, index_text = callback_data.split(":", maxsplit=2)
            if prefix != self.GROUP_CHALLENGE_PREFIX.removesuffix(":"):
                raise ValueError
            selected_index = int(index_text)
        except ValueError:
            return JoinVerificationOutcome(False, OnboardingState.DECLINED, "验证请求无效")
        result = self.repository.resolve_group_challenge(
            self._token_hash(token), telegram_user_id, selected_index, datetime.now(timezone.utc).isoformat()
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
                await self.gateway.ban_member(result.chat_id, result.user_id or telegram_user_id)
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
                self.GROUP_CHALLENGE_FAILURE_TEXT,
            )
            return JoinVerificationOutcome(False, OnboardingState.DECLINED, "三次回答错误，已移出群组")
        return JoinVerificationOutcome(
            False,
            OnboardingState.SECONDARY_VERIFICATION_PENDING,
            f"回答错误，还可尝试 {result.attempts_left} 次。",
        )

    async def resolve_group_challenge_by_admin(
        self, token: str, approved: bool
    ) -> JoinVerificationOutcome:
        """A group administrator may settle an active second-stage verification."""
        result = self.repository.resolve_group_challenge_by_admin(
            self._token_hash(token), datetime.now(timezone.utc).isoformat(), approved
        )
        if result.chat_id is None or result.user_id is None:
            return JoinVerificationOutcome(False, OnboardingState.DECLINED, "验证已过期或已处理")
        try:
            if approved:
                await self.gateway.release_member(result.chat_id, result.user_id)
                text = self.GROUP_CHALLENGE_WELCOME_TEXT
                reason = "管理员已通过验证并解除禁言。"
            else:
                await self.gateway.ban_member(result.chat_id, result.user_id)
                text = self.GROUP_CHALLENGE_FAILURE_TEXT
                reason = "管理员已拒绝验证并移出成员。"
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
        except Exception:
            # The Telegram action must not be considered failed only because an old challenge
            # message was removed by another moderator.
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
        self.mark_first_message(chat_id, user_id, timestamp)
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
        expired = self.repository.expired_pending_verifications(timestamp)
        completed = 0
        for chat_id, user_id, verification_flow, state, message_id in expired:
            if state is OnboardingState.VERIFICATION_PENDING and verification_flow == "join_request":
                try:
                    already_joined = await self.gateway.is_group_member(chat_id, user_id)
                except Exception:
                    logger.exception(
                        "onboarding.expiry_member_status_lookup_failed chat_id=%s user_id=%s",
                        chat_id,
                        user_id,
                    )
                    already_joined = False
                if already_joined:
                    # The new-member event can be delayed or missed during a deployment.
                    # Do not reject an already approved member; convert them to the second
                    # stage instead, which gets its own fresh timeout.
                    if self.repository.mark_join_request_manually_approved(chat_id, user_id, timestamp):
                        await self._send_manual_join_welcome(chat_id, user_id)
                        try:
                            await self._start_group_challenge(
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
                    continue
            # Claim the row before calling Telegram. This prevents a delayed timeout worker
            # from removing a member an administrator has just approved.
            if not self.repository.claim_expired_verification(chat_id, user_id, state, timestamp):
                logger.info(
                    "onboarding.expiry_skipped_already_resolved chat_id=%s user_id=%s flow=%s",
                    chat_id,
                    user_id,
                    verification_flow,
                )
                continue
            try:
                if state is OnboardingState.VERIFICATION_PENDING and verification_flow == "join_request":
                    await self.gateway.decline_join_request(chat_id, user_id)
                else:
                    # The member is already in the group during the post-join challenge.
                    await self.gateway.ban_member(chat_id, user_id)
            except Exception as error:
                if self._is_terminal_expiry_error(error):
                    if self.repository.finalize_expired_verification(chat_id, user_id, timestamp):
                        completed += 1
                        if state is OnboardingState.VERIFICATION_PENDING and verification_flow == "join_request":
                            await self._send_initial_verification_result(
                                chat_id, user_id, self.INITIAL_VERIFICATION_TIMEOUT_TEXT, delete_after_seconds=60
                            )
                        elif state is OnboardingState.SECONDARY_VERIFICATION_PENDING:
                            await self._replace_group_challenge_message(
                                chat_id,
                                message_id,
                                user_id,
                                self.GROUP_CHALLENGE_FAILURE_TEXT,
                                delete_after_seconds=60,
                            )
                    logger.info(
                        "onboarding.expiry_already_closed chat_id=%s user_id=%s flow=%s reason=%s",
                        chat_id,
                        user_id,
                        verification_flow,
                        error,
                    )
                    continue
                self.repository.reopen_expired_verification(chat_id, user_id, state, timestamp)
                logger.exception(
                    "onboarding.expiry_action_failed chat_id=%s user_id=%s flow=%s",
                    chat_id,
                    user_id,
                    verification_flow,
                )
                continue
            if self.repository.finalize_expired_verification(chat_id, user_id, timestamp):
                completed += 1
                if state is OnboardingState.VERIFICATION_PENDING and verification_flow == "join_request":
                    await self._send_initial_verification_result(
                        chat_id, user_id, self.INITIAL_VERIFICATION_TIMEOUT_TEXT, delete_after_seconds=60
                    )
                elif state is OnboardingState.SECONDARY_VERIFICATION_PENDING:
                    await self._replace_group_challenge_message(
                        chat_id,
                        message_id,
                        user_id,
                        self.GROUP_CHALLENGE_FAILURE_TEXT,
                        delete_after_seconds=60,
                    )
                logger.info(
                    "onboarding.expired chat_id=%s user_id=%s flow=%s",
                    chat_id,
                    user_id,
                    verification_flow,
                )
        return completed

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
