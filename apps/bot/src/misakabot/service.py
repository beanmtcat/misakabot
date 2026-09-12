from __future__ import annotations

from dataclasses import replace
import logging

from .domain import Action, MessageInput, ModerationOutcome, ReviewStatus
from .llm import ModerationClient
from .normalizer import normalize_message
from .repository import AuditRepository
from .signals import ALLOWED_VPS_TRADE_REASON, FORCED_FIRST_OBSERVED_REASON, detect_suspicion
from .telegram_gateway import TelegramGateway

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class ModerationService:
    def __init__(
        self,
        repository: AuditRepository,
        gateway: TelegramGateway,
        llm: ModerationClient,
        auto_ban_threshold: float = 0.90,
        review_threshold: float = 0.60,
    ) -> None:
        self.repository = repository
        self.gateway = gateway
        self.llm = llm
        self.auto_ban_threshold = auto_ban_threshold
        self.review_threshold = review_threshold

    async def moderate(
        self,
        incoming: MessageInput,
        *,
        force_llm_review: bool = False,
    ) -> ModerationOutcome:
        sender_identity = " ".join(
            part for part in (incoming.display_name, incoming.username) if part
        )
        normalized = normalize_message(incoming.text, sender_name=sender_identity)
        signals = detect_suspicion(normalized, incoming.media_type)
        has_concrete_suspicion = signals.is_suspicious
        if force_llm_review and not signals.is_suspicious:
            signals = replace(
                signals,
                is_suspicious=True,
                score=max(signals.score, 1),
                reasons=(*signals.reasons, FORCED_FIRST_OBSERVED_REASON),
            )
        allowed_vps_trade = ALLOWED_VPS_TRADE_REASON in signals.reasons
        if allowed_vps_trade:
            # Keep the promised first-message model audit, but never hide or punish a category
            # that this group explicitly permits. Later VPS listings do not need an LLM call.
            verdict = None
            if force_llm_review:
                try:
                    verdict = await self.llm.judge(normalized, signals)
                except Exception:
                    logger.exception(
                        "moderation.allowed_vps_trade_llm_failed chat_id=%s message_id=%s user_id=%s",
                        incoming.chat_id,
                        incoming.message_id,
                        incoming.user_id,
                    )
            event_id = self.repository.record(
                incoming, normalized, signals, verdict, Action.ALLOW, ReviewStatus.NOT_REQUIRED
            )
            logger.info(
                "moderation.allowed_vps_trade chat_id=%s message_id=%s user_id=%s event_id=%s",
                incoming.chat_id,
                incoming.message_id,
                incoming.user_id,
                event_id,
            )
            return ModerationOutcome(Action.ALLOW, verdict, signals, event_id)
        if force_llm_review and not has_concrete_suspicion:
            # First-observed messages (notably those from members who predate this Bot) are
            # audited by Kimi without the destructive quarantine path. A model-only verdict is
            # not enough evidence to delete a normal conversation or temporarily mute a member.
            try:
                verdict = await self.llm.judge(normalized, signals)
            except Exception:
                logger.exception(
                    "moderation.first_observed_llm_failed chat_id=%s message_id=%s user_id=%s",
                    incoming.chat_id,
                    incoming.message_id,
                    incoming.user_id,
                )
                verdict = None
            if verdict is not None and verdict.is_ad:
                action, review = Action.NEEDS_REVIEW, ReviewStatus.PENDING
            else:
                action, review = Action.ALLOW, ReviewStatus.NOT_REQUIRED
            event_id = self.repository.record(incoming, normalized, signals, verdict, action, review)
            logger.info(
                "moderation.first_observed_audited chat_id=%s message_id=%s user_id=%s action=%s event_id=%s",
                incoming.chat_id,
                incoming.message_id,
                incoming.user_id,
                action,
                event_id,
            )
            return ModerationOutcome(action, verdict, signals, event_id)
        if not signals.is_suspicious:
            event_id = self.repository.record(
                incoming, normalized, signals, None, Action.ALLOW, ReviewStatus.NOT_REQUIRED
            )
            logger.info(
                "moderation.allowed chat_id=%s message_id=%s user_id=%s signal_score=%s",
                incoming.chat_id, incoming.message_id, incoming.user_id, signals.score,
            )
            return ModerationOutcome(Action.ALLOW, None, signals, event_id)

        # Rules are intentionally only a routing signal. Do not delete or mute before Kimi has
        # confirmed the content: Telegram cannot restore a false-positive message afterwards.
        logger.info(
            "moderation.llm_review_started chat_id=%s message_id=%s user_id=%s signal_score=%s reasons=%s",
            incoming.chat_id,
            incoming.message_id,
            incoming.user_id,
            signals.score,
            ",".join(signals.reasons),
        )
        try:
            verdict = await self.llm.judge(normalized, signals)
        except Exception:
            logger.exception(
                "moderation.llm_failed chat_id=%s message_id=%s user_id=%s",
                incoming.chat_id, incoming.message_id, incoming.user_id,
            )
            event_id = self.repository.record(
                incoming, normalized, signals, None, Action.NEEDS_REVIEW, ReviewStatus.PENDING
            )
            return ModerationOutcome(Action.NEEDS_REVIEW, None, signals, event_id)

        if verdict.is_ad and verdict.confidence >= self.auto_ban_threshold:
            try:
                await self.gateway.delete_message(incoming.chat_id, incoming.message_id)
                await self.gateway.ban_member(incoming.chat_id, incoming.user_id)
            except Exception:
                logger.exception(
                    "moderation.confirmed_ad_action_failed chat_id=%s message_id=%s user_id=%s",
                    incoming.chat_id,
                    incoming.message_id,
                    incoming.user_id,
                )
                action, review = Action.NEEDS_REVIEW, ReviewStatus.PENDING
            else:
                self.repository.block_user(incoming.user_id, verdict.reason, incoming.sent_at.isoformat())
                try:
                    await self.gateway.send_moderation_notice(incoming.chat_id, incoming.user_id)
                except Exception:
                    logger.exception(
                        "moderation.notice_action_failed chat_id=%s message_id=%s user_id=%s",
                        incoming.chat_id,
                        incoming.message_id,
                        incoming.user_id,
                    )
                action, review = Action.PERMANENT_BAN, ReviewStatus.CONFIRMED
        elif verdict.is_ad and verdict.confidence >= self.review_threshold:
            action, review = Action.NEEDS_REVIEW, ReviewStatus.PENDING
        else:
            action, review = Action.ALLOW, ReviewStatus.NOT_REQUIRED
        event_id = self.repository.record(incoming, normalized, signals, verdict, action, review)
        logger.info(
            "moderation.resolved chat_id=%s message_id=%s user_id=%s action=%s confidence=%.2f category=%s event_id=%s",
            incoming.chat_id, incoming.message_id, incoming.user_id, action,
            verdict.confidence, verdict.category, event_id,
        )
        return ModerationOutcome(action, verdict, signals, event_id)
