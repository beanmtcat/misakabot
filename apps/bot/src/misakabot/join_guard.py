from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from .domain import JoinRequestInput
from .repository import AuditRepository


# Flood accounts commonly use a single generated Latin name, including very
# short values such as ``hal`` and ``luc``. Length is deliberately not used as
# an escape hatch; this signal is only enforced after the group has entered
# high defense, so ordinary traffic does not get rejected on the name alone.
_SINGLE_ASCII_NAME = re.compile(r"[A-Za-z]{1,64}").fullmatch


class JoinGuardMode(StrEnum):
    NORMAL = "normal"
    HIGH = "high"
    LOCKDOWN = "lockdown"
    PERMANENT_HIGH = "permanent_high"
    PERMANENT_LOCKDOWN = "permanent_lockdown"


@dataclass(frozen=True)
class JoinGuardPolicy:
    burst_per_minute: int = 10
    burst_per_five_minutes: int = 25
    suspicious_name_minimum: int = 6
    suspicious_name_ratio: float = 0.70
    lockdown_per_five_minutes: int = 100
    quiet_minutes: int = 120
    repeat_cooldown_hours: int = 24
    stale_request_minutes: int = 5
    notice_interval_minutes: int = 30


@dataclass(frozen=True)
class JoinGuardDecision:
    mode: JoinGuardMode
    reject: bool
    reason: str
    one_minute_count: int
    five_minute_count: int
    suspicious_name_ratio: float
    notify_admins: bool


class JoinGuard:
    """Persistent, per-group admission flood protection without rotating invite links."""

    def __init__(self, repository: AuditRepository, policy: JoinGuardPolicy) -> None:
        self.repository = repository
        self.policy = policy

    def evaluate(
        self, request: JoinRequestInput, now: datetime | None = None
    ) -> JoinGuardDecision:
        current = now or datetime.now(timezone.utc)
        requested_at = _as_utc(request.requested_at)
        current_iso = current.isoformat()
        requested_iso = requested_at.isoformat()
        self.repository.record_join_request_event(
            chat_id=request.chat_id,
            user_id=request.user_id,
            requested_at=requested_iso,
            display_name=request.display_name,
            username=request.username,
            invite_link=request.invite_link,
            now=current_iso,
        )

        minute_names = self.repository.recent_join_request_names(
            request.chat_id, (current - timedelta(minutes=1)).isoformat(), current_iso
        )
        five_minute_names = self.repository.recent_join_request_names(
            request.chat_id, (current - timedelta(minutes=5)).isoformat(), current_iso
        )
        one_minute_count = len(minute_names)
        five_minute_count = len(five_minute_names)
        suspicious_count = sum(_is_single_ascii_name(name) for name in minute_names)
        suspicious_ratio = suspicious_count / one_minute_count if one_minute_count else 0.0

        raw_mode, raw_until, raw_last_attack, _ = self.repository.join_guard_state(request.chat_id)
        mode = _mode(raw_mode)
        defense_until = _timestamp(raw_until)
        last_attack_at = _timestamp(raw_last_attack)
        if mode in {JoinGuardMode.HIGH, JoinGuardMode.LOCKDOWN} and (
            defense_until is None or defense_until <= current
        ):
            mode = JoinGuardMode.NORMAL
            defense_until = None

        extreme = five_minute_count >= self.policy.lockdown_per_five_minutes
        burst = (
            one_minute_count >= self.policy.burst_per_minute
            or five_minute_count >= self.policy.burst_per_five_minutes
            or (
                one_minute_count >= self.policy.suspicious_name_minimum
                and suspicious_ratio >= self.policy.suspicious_name_ratio
            )
        )
        automatic_mode = mode not in {
            JoinGuardMode.PERMANENT_HIGH,
            JoinGuardMode.PERMANENT_LOCKDOWN,
        }
        if automatic_mode and (extreme or burst):
            mode = JoinGuardMode.LOCKDOWN if extreme else JoinGuardMode.HIGH
            defense_until = current + timedelta(minutes=self.policy.quiet_minutes)
            last_attack_at = current

        recent_rejection = self.repository.has_recent_rejected_join_request(
            request.chat_id,
            request.user_id,
            (current - timedelta(hours=self.policy.repeat_cooldown_hours)).isoformat(),
        )
        stale = requested_at <= current - timedelta(minutes=self.policy.stale_request_minutes)
        suspicious_name = _is_single_ascii_name(request.display_name or "")
        # Automatic protection expires only after the attack has actually gone
        # quiet. A low-rate continuation may no longer satisfy the original burst
        # threshold, so every matching request extends the quiet window while high
        # protection is active. Lockdown treats every request as attack traffic.
        continuing_attack = (
            mode is JoinGuardMode.LOCKDOWN
            or (mode is JoinGuardMode.HIGH and suspicious_name)
        )
        if automatic_mode and continuing_attack and not stale:
            defense_until = current + timedelta(minutes=self.policy.quiet_minutes)
            last_attack_at = current
        if stale:
            reject, reason = True, "stale_request"
        elif recent_rejection:
            reject, reason = True, "repeat_cooldown"
        elif mode in {JoinGuardMode.LOCKDOWN, JoinGuardMode.PERMANENT_LOCKDOWN}:
            reject, reason = True, "lockdown"
        elif mode in {JoinGuardMode.HIGH, JoinGuardMode.PERMANENT_HIGH} and suspicious_name:
            reject, reason = True, "high_risk_name_cohort"
        else:
            reject, reason = False, "verification"

        self.repository.save_join_guard_state(
            request.chat_id,
            mode.value,
            defense_until.isoformat() if defense_until else None,
            last_attack_at.isoformat() if last_attack_at else None,
            current_iso,
        )
        self.repository.mark_join_request_event(
            request.chat_id,
            request.user_id,
            requested_iso,
            "rejected" if reject else "verification",
            reason,
            current_iso,
        )
        notify = mode is not JoinGuardMode.NORMAL and self.repository.claim_join_guard_notice(
            request.chat_id,
            current_iso,
            (current - timedelta(minutes=self.policy.notice_interval_minutes)).isoformat(),
        )
        return JoinGuardDecision(
            mode,
            reject,
            reason,
            one_minute_count,
            five_minute_count,
            suspicious_ratio,
            notify,
        )

    def set_mode(self, chat_id: int, mode: JoinGuardMode, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        if mode not in {
            JoinGuardMode.NORMAL,
            JoinGuardMode.PERMANENT_HIGH,
            JoinGuardMode.PERMANENT_LOCKDOWN,
        }:
            raise ValueError("manual join guard mode must be normal, permanent_high or permanent_lockdown")
        self.repository.save_join_guard_state(
            chat_id,
            mode.value,
            None,
            current.isoformat() if mode is not JoinGuardMode.NORMAL else None,
            current.isoformat(),
        )

    def status(self, chat_id: int, now: datetime | None = None) -> tuple[JoinGuardMode, datetime | None]:
        current = now or datetime.now(timezone.utc)
        raw_mode, raw_until, _, _ = self.repository.join_guard_state(chat_id)
        mode = _mode(raw_mode)
        defense_until = _timestamp(raw_until)
        if mode in {JoinGuardMode.HIGH, JoinGuardMode.LOCKDOWN} and (
            defense_until is None or defense_until <= current
        ):
            return JoinGuardMode.NORMAL, None
        return mode, defense_until


def _is_single_ascii_name(value: str) -> bool:
    return _SINGLE_ASCII_NAME(value.strip()) is not None


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str) and value:
        return _as_utc(datetime.fromisoformat(value))
    return None


def _mode(value: str) -> JoinGuardMode:
    try:
        return JoinGuardMode(value)
    except ValueError:
        return JoinGuardMode.NORMAL
