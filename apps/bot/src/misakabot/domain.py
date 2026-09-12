from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


class Action(StrEnum):
    ALLOW = "allow"
    QUARANTINE = "quarantine"
    PERMANENT_BAN = "permanent_ban"
    NEEDS_REVIEW = "needs_review"
    RELEASE = "release"


class ReviewStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"


class OnboardingState(StrEnum):
    """A member is never trusted merely because time has passed after joining."""

    VERIFICATION_PENDING = "verification_pending"
    SECONDARY_VERIFICATION_PENDING = "secondary_verification_pending"
    PENDING_FIRST_MESSAGE = "pending_first_message"
    OBSERVING = "observing"
    DECLINED = "declined"
    BANNED = "banned"


@dataclass(frozen=True)
class MessageInput:
    chat_id: int
    message_id: int
    user_id: int
    text: str = ""
    username: str | None = None
    display_name: str | None = None
    media_type: str | None = None
    sent_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class JoinRequestInput:
    chat_id: int
    user_id: int
    user_chat_id: int
    group_title: str | None = None
    username: str | None = None
    invite_link: str | None = None
    requested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class DirectJoinInput:
    chat_id: int
    user_id: int
    username: str | None = None
    joined_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class JoinVerificationOutcome:
    accepted: bool
    state: OnboardingState
    reason: str


@dataclass(frozen=True)
class NormalizedMessage:
    raw_text: str
    normalized_text: str
    urls: tuple[str, ...]
    mentions: tuple[str, ...]
    had_invisible_chars: bool
    sender_name_raw: str = ""
    normalized_sender_name: str = ""


@dataclass(frozen=True)
class SuspicionSignals:
    is_suspicious: bool
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ModerationVerdict:
    is_ad: bool
    category: str
    confidence: float
    evidence: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ModerationOutcome:
    action: Action
    verdict: ModerationVerdict | None
    signals: SuspicionSignals
    event_id: int | None = None
