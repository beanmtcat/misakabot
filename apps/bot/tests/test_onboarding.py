from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from misakabot.domain import DirectJoinInput, JoinRequestInput, OnboardingState
from misakabot.onboarding import OnboardingService
from misakabot.join_questions import GLOSSARY_QUESTIONS
from misakabot.repository import AuditRepository


class FakeGateway:
    def __init__(self) -> None:
        self.approved: list[tuple[int, int]] = []
        self.declined: list[tuple[int, int]] = []
        self.verifications: list[tuple[int, str, str | None]] = []
        self.group_challenges: list[tuple[int, int, str, tuple[tuple[str, str], ...], int]] = []
        self.group_challenge_usernames: list[str | None] = []
        self.sent_group_deadlines: list[str | None] = []
        self.displayed_group_deadlines: list[str | None] = []
        self.sent_group_preparing: list[bool] = []
        self.deleted_messages: list[tuple[int, int]] = []
        self.rotated_challenges: list[tuple[int, str, int]] = []
        self.restricted: list[tuple[int, int, int]] = []
        self.released: list[tuple[int, int]] = []
        self.banned: list[tuple[int, int]] = []
        self.ban_durations: list[int] = []
        self.replaced_group_verifications: list[tuple[int, int, str]] = []
        self.replaced_group_delete_after: list[int | None] = []
        self.replaced_group_channel_urls: list[str | None] = []
        self.initial_verification_results: list[tuple[int, int, str]] = []
        self.initial_result_delete_after: list[int | None] = []
        self.manual_join_welcomes: list[tuple[int, int]] = []
        self.join_guard_alerts: list[tuple[frozenset[int], str]] = []
        self.group_members: set[tuple[int, int]] = set()

    async def approve_join_request(self, chat_id: int, user_id: int) -> None:
        self.approved.append((chat_id, user_id))
        self.group_members.add((chat_id, user_id))

    async def decline_join_request(self, chat_id: int, user_id: int) -> None:
        self.declined.append((chat_id, user_id))

    async def send_join_verification(
        self, user_chat_id: int, callback_data: str, group_title: str | None
    ) -> None:
        self.verifications.append((user_chat_id, callback_data, group_title))

    async def send_private_join_guard_alert(
        self, admin_user_ids: frozenset[int], text: str
    ) -> None:
        self.join_guard_alerts.append((admin_user_ids, text))

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
        self.group_challenges.append((chat_id, user_id, prompt, choices, ttl_minutes))
        self.group_challenge_usernames.append(username)
        self.sent_group_deadlines.append(expires_at)
        self.sent_group_preparing.append(preparing)
        if not preparing:
            self.displayed_group_deadlines.append(expires_at)
        return 1000 + len(self.group_challenges)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted_messages.append((chat_id, message_id))

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
        # Keep group_challenges[-1] as the question currently shown to the member.
        self.group_challenges.append((chat_id, user_id, prompt, choices, ttl_minutes))
        self.rotated_challenges.append((message_id, prompt, attempts_left))
        self.displayed_group_deadlines.append(expires_at)

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
        self.replaced_group_verifications.append((chat_id, message_id, text))
        self.replaced_group_delete_after.append(delete_after_seconds)
        self.replaced_group_channel_urls.append(welcome_channel_url)

    async def send_initial_verification_result(
        self,
        chat_id: int,
        user_id: int,
        username: str | None,
        text: str,
        delete_after_seconds: int | None = None,
    ) -> None:
        self.initial_verification_results.append((chat_id, user_id, text))
        self.initial_result_delete_after.append(delete_after_seconds)

    async def send_manual_join_welcome(self, chat_id: int, user_id: int) -> None:
        self.manual_join_welcomes.append((chat_id, user_id))

    async def restrict_member(self, chat_id: int, user_id: int, minutes: int) -> None:
        self.restricted.append((chat_id, user_id, minutes))

    async def release_member(self, chat_id: int, user_id: int) -> None:
        self.released.append((chat_id, user_id))

    async def ban_member(self, chat_id: int, user_id: int, *, duration_hours: int = 0) -> None:
        self.banned.append((chat_id, user_id))
        self.ban_durations.append(duration_hours)

    async def get_member_info(self, chat_id: int, user_id: int) -> str:
        return f"用户 ID：{user_id}"

    async def is_group_member(self, chat_id: int, user_id: int) -> bool:
        return (chat_id, user_id) in self.group_members


class DeactivatedUserGateway(FakeGateway):
    async def decline_join_request(self, chat_id: int, user_id: int) -> None:
        raise RuntimeError("Telegram server says - Forbidden: user is deactivated")


class DeactivatedMemberLookupGateway(DeactivatedUserGateway):
    async def is_group_member(self, chat_id: int, user_id: int) -> bool:
        raise RuntimeError("Telegram server says - Forbidden: user is deactivated")


class MissingHiddenRequesterGateway(FakeGateway):
    async def decline_join_request(self, chat_id: int, user_id: int) -> None:
        raise RuntimeError("Telegram server says - Bad Request: HIDE_REQUESTER_MISSING")


class ReleaseFailingGateway(FakeGateway):
    async def release_member(self, chat_id: int, user_id: int) -> None:
        raise RuntimeError("temporary Telegram failure")


class PrivateMessageUnavailableGateway(FakeGateway):
    async def send_join_verification(
        self, user_chat_id: int, callback_data: str, group_title: str | None
    ) -> None:
        raise RuntimeError("Telegram server says - Forbidden: bot can't initiate conversation with a user")


class AlreadyParticipantApprovalGateway(FakeGateway):
    async def approve_join_request(self, chat_id: int, user_id: int) -> None:
        raise RuntimeError("Telegram server says - Bad Request: USER_ALREADY_PARTICIPANT")


class MemberLookupFailingGateway(FakeGateway):
    async def is_group_member(self, chat_id: int, user_id: int) -> bool:
        raise RuntimeError("temporary Telegram member lookup failure")


class MissingMemberLookupGateway(FakeGateway):
    async def is_group_member(self, chat_id: int, user_id: int) -> bool:
        raise RuntimeError("Telegram server says - Bad Request: member not found")


class OnboardingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repository = AuditRepository(Path(self.tempdir.name) / "audit.sqlite3")
        self.repository.initialize()
        self.gateway = FakeGateway()
        self.service = OnboardingService(
            self.repository, self.gateway, join_verify_url="https://dashboard.example/join-verify"
        )
        self.request = JoinRequestInput(
            chat_id=-100123,
            user_id=42,
            user_chat_id=4200,
            group_title="测试 VPS 交流群",
            username="new_member",
            requested_at=datetime.now(timezone.utc),
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _correct_group_callback(self) -> str:
        _, _, prompt, choices, _ = self.gateway.group_challenges[-1]
        correct_option = next(question.correct_option for question in GLOSSARY_QUESTIONS if question.prompt == prompt)
        return next(callback_data for text, callback_data in choices if text == correct_option)

    def _wrong_group_callback(self) -> str:
        _, _, _, choices, _ = self.gateway.group_challenges[-1]
        correct = self._correct_group_callback()
        return next(data for _, data in choices if data != correct)

    def test_wrong_answer_rotates_to_a_new_question_and_invalidates_old_buttons(self) -> None:
        asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=44)))
        old_wrong = self._wrong_group_callback()
        old_correct = self._correct_group_callback()

        outcome = asyncio.run(self.service.verify_group_challenge(old_wrong, 44))

        self.assertIn("已换一道新题", outcome.reason)
        self.assertEqual(len(self.gateway.rotated_challenges), 1)
        message_id, _, attempts_left = self.gateway.rotated_challenges[0]
        self.assertEqual((message_id, attempts_left), (1001, 2))
        # The previous question's buttons, including its correct answer, are dead.
        stale = asyncio.run(self.service.verify_group_challenge(old_correct, 44))
        self.assertFalse(stale.accepted)
        self.assertEqual(
            self.repository.onboarding_state(-100123, 44),
            OnboardingState.SECONDARY_VERIFICATION_PENDING,
        )
        accepted = asyncio.run(self.service.verify_group_challenge(self._correct_group_callback(), 44))
        self.assertTrue(accepted.accepted)

    def test_verification_auto_approves_but_first_message_remains_special(self) -> None:
        pending = asyncio.run(self.service.start(self.request))
        self.assertEqual(pending.state, OnboardingState.VERIFICATION_PENDING)
        self.assertIsNotNone(pending.callback_data)

        token = parse_qs(urlparse(pending.callback_data or "").query)["session"][0]
        outcome = asyncio.run(self.service.verify_token(token, 42))
        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.state, OnboardingState.SECONDARY_VERIFICATION_PENDING)
        self.assertEqual(self.gateway.approved, [(-100123, 42)])
        self.assertEqual(len(self.gateway.group_challenges), 1)
        self.assertEqual(self.gateway.group_challenge_usernames, ["new_member"])
        self.assertEqual(self.gateway.group_challenges[-1][-1], 2)
        self.assertEqual(self.gateway.verifications[-1][2], "测试 VPS 交流群")

        challenge = asyncio.run(self.service.verify_group_challenge(self._correct_group_callback(), 42))
        self.assertTrue(challenge.accepted)
        self.assertEqual(challenge.state, OnboardingState.PENDING_FIRST_MESSAGE)
        self.assertEqual(self.gateway.released, [(-100123, 42)])
        self.assertIn("欢迎加入群组", self.gateway.replaced_group_verifications[-1][2])

        self.assertTrue(self.service.mark_first_message(-100123, 42, datetime.now(timezone.utc)))
        self.assertFalse(self.service.mark_first_message(-100123, 42, datetime.now(timezone.utc)))

    def test_first_stage_continues_when_admin_already_approved_request(self) -> None:
        self.gateway = AlreadyParticipantApprovalGateway()
        self.service = OnboardingService(
            self.repository, self.gateway, join_verify_url="https://dashboard.example/join-verify"
        )
        pending = asyncio.run(self.service.start(self.request))
        token = parse_qs(urlparse(pending.callback_data or "").query)["session"][0]

        outcome = asyncio.run(self.service.verify_token(token, self.request.user_id))

        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.state, OnboardingState.SECONDARY_VERIFICATION_PENDING)
        self.assertEqual(len(self.gateway.group_challenges), 1)
        self.assertEqual(
            self.repository.onboarding_state(self.request.chat_id, self.request.user_id),
            OnboardingState.SECONDARY_VERIFICATION_PENDING,
        )

    def test_only_the_requesting_account_can_use_its_token(self) -> None:
        pending = asyncio.run(self.service.start(self.request))
        token = parse_qs(urlparse(pending.callback_data or "").query)["session"][0]
        outcome = asyncio.run(self.service.verify_token(token, 999))
        self.assertFalse(outcome.accepted)
        self.assertEqual(self.gateway.approved, [])

    def test_previous_first_stage_session_is_reported_before_turnstile(self) -> None:
        old_pending = asyncio.run(self.service.start(self.request))
        old_token = parse_qs(urlparse(old_pending.callback_data or "").query)["session"][0]
        latest_pending = asyncio.run(self.service.start(self.request))
        latest_token = parse_qs(urlparse(latest_pending.callback_data or "").query)["session"][0]

        self.assertFalse(self.service.has_current_initial_verification(old_token, 42))
        self.assertTrue(self.service.has_current_initial_verification(latest_token, 42))
        outcome = asyncio.run(self.service.verify_token(old_token, 42))
        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, OnboardingService.STALE_INITIAL_VERIFICATION_TEXT)

    def test_failed_first_stage_rejects_request_and_posts_a_group_notice(self) -> None:
        pending = asyncio.run(self.service.start(self.request))
        token = parse_qs(urlparse(pending.callback_data or "").query)["session"][0]

        outcome = asyncio.run(self.service.fail_initial_verification(token, 42))

        self.assertFalse(outcome.accepted)
        self.assertEqual(self.gateway.declined, [(-100123, 42)])
        self.assertIn("验证失败", self.gateway.initial_verification_results[-1][2])

    def test_previously_banned_user_is_declined_without_a_verification_prompt(self) -> None:
        self.repository.block_user(42, "confirmed advertisement", datetime.now(timezone.utc).isoformat())
        outcome = asyncio.run(self.service.start(self.request))
        self.assertEqual(outcome.state, OnboardingState.BANNED)
        self.assertEqual(self.gateway.declined, [(-100123, 42)])
        self.assertEqual(self.gateway.verifications, [])

    def test_unblocking_a_user_allows_a_new_join_verification(self) -> None:
        self.repository.block_user(42, "confirmed advertisement", datetime.now(timezone.utc).isoformat())
        self.assertTrue(self.repository.unblock_user(42))
        outcome = asyncio.run(self.service.start(self.request))
        self.assertEqual(outcome.state, OnboardingState.VERIFICATION_PENDING)
        self.assertEqual(len(self.gateway.verifications), 1)

    def test_unavailable_private_message_keeps_request_for_manual_approval(self) -> None:
        self.gateway = PrivateMessageUnavailableGateway()
        self.service = OnboardingService(
            self.repository, self.gateway, join_verify_url="https://dashboard.example/join-verify"
        )

        outcome = asyncio.run(self.service.start(self.request))

        self.assertEqual(outcome.state, OnboardingState.VERIFICATION_PENDING)
        self.assertIsNone(outcome.callback_data)
        self.assertEqual(
            self.repository.onboarding_state(self.request.chat_id, self.request.user_id),
            OnboardingState.VERIFICATION_PENDING,
        )

    def test_retried_join_request_does_not_regress_second_stage(self) -> None:
        pending = asyncio.run(self.service.start(self.request))
        token = parse_qs(urlparse(pending.callback_data or "").query)["session"][0]
        verified = asyncio.run(self.service.verify_token(token, self.request.user_id))
        self.assertEqual(verified.state, OnboardingState.SECONDARY_VERIFICATION_PENDING)

        retried = asyncio.run(self.service.start(self.request))

        self.assertEqual(retried.state, OnboardingState.SECONDARY_VERIFICATION_PENDING)
        self.assertIsNone(retried.callback_data)
        self.assertEqual(len(self.gateway.verifications), 1)
        self.assertEqual(len(self.gateway.group_challenges), 1)

    def test_rejoin_restarts_first_stage_when_old_trusted_state_is_stale(self) -> None:
        joined_at = datetime.now(timezone.utc)
        pending = asyncio.run(
            self.service.start_direct_join(
                DirectJoinInput(chat_id=-100123, user_id=42, joined_at=joined_at)
            )
        )
        challenge = asyncio.run(
            self.service.resolve_group_challenge_by_admin(pending.callback_data or "", approved=True)
        )
        self.assertEqual(challenge.state, OnboardingState.PENDING_FIRST_MESSAGE)

        reapplied = asyncio.run(self.service.start(self.request))

        self.assertEqual(reapplied.state, OnboardingState.VERIFICATION_PENDING)
        self.assertIsNotNone(reapplied.callback_data)
        self.assertEqual(len(self.gateway.verifications), 1)

    def test_departure_retires_old_trusted_state_before_rejoin(self) -> None:
        joined_at = datetime.now(timezone.utc)
        pending = asyncio.run(
            self.service.start_direct_join(
                DirectJoinInput(chat_id=-100123, user_id=42, joined_at=joined_at)
            )
        )
        asyncio.run(
            self.service.resolve_group_challenge_by_admin(pending.callback_data or "", approved=True)
        )

        self.assertTrue(asyncio.run(self.service.mark_member_departed(-100123, 42, datetime.now(timezone.utc))))
        reapplied = asyncio.run(self.service.start(self.request))

        self.assertEqual(reapplied.state, OnboardingState.VERIFICATION_PENDING)
        self.assertEqual(len(self.gateway.verifications), 1)

    def test_direct_join_is_restricted_until_its_own_button_is_verified(self) -> None:
        pending = asyncio.run(
            self.service.start_direct_join(
                DirectJoinInput(
                    chat_id=-100123,
                    user_id=43,
                    username="direct_member",
                    joined_at=datetime.now(timezone.utc),
                )
            )
        )
        self.assertEqual(self.gateway.restricted[0][:2], (-100123, 43))
        self.assertEqual(pending.state, OnboardingState.SECONDARY_VERIFICATION_PENDING)
        outcome = asyncio.run(self.service.verify_group_challenge(self._correct_group_callback(), 43))
        self.assertTrue(outcome.accepted)
        self.assertEqual(self.gateway.released, [(-100123, 43)])

    def test_secondary_challenge_accepts_only_its_own_member(self) -> None:
        asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))
        outcome = asyncio.run(self.service.verify_group_challenge(self._correct_group_callback(), 99))
        self.assertFalse(outcome.accepted)
        self.assertEqual(self.gateway.released, [])

    def test_every_glossary_question_can_be_answered_after_option_shuffle(self) -> None:
        for index, question in enumerate(GLOSSARY_QUESTIONS):
            with self.subTest(prompt=question.prompt), patch(
                "misakabot.onboarding.secrets.choice", return_value=question
            ):
                user_id = 10000 + index
                asyncio.run(self.service.start_direct_join(DirectJoinInput(
                    chat_id=-100123, user_id=user_id,
                )))
                _, _, prompt, choices, _ = self.gateway.group_challenges[-1]
                self.assertEqual(prompt, question.prompt)
                self.assertEqual({text for text, _ in choices}, set(question.options))
                correct = next(data for text, data in choices if text == question.correct_option)
                result = asyncio.run(self.service.verify_group_challenge(correct, user_id))
                self.assertTrue(result.accepted)
                self.assertEqual(self.gateway.released[-1], (-100123, user_id))

    def test_successful_dmit_welcome_includes_the_news_channel_url(self) -> None:
        self.service = OnboardingService(
            self.repository,
            self.gateway,
            join_verify_url="https://dashboard.example/join-verify",
            welcome_channel_url_by_group={-100123: "https://t.me/dmitnews"},
        )
        asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))

        outcome = asyncio.run(self.service.verify_group_challenge(self._correct_group_callback(), 43))

        self.assertTrue(outcome.accepted)
        self.assertEqual(self.gateway.replaced_group_channel_urls[-1], "https://t.me/dmitnews")

    def test_second_stage_reopens_when_telegram_cannot_release_member(self) -> None:
        self.gateway = ReleaseFailingGateway()
        self.service = OnboardingService(
            self.repository, self.gateway, join_verify_url="https://dashboard.example/join-verify"
        )
        asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))

        outcome = asyncio.run(self.service.verify_group_challenge(self._correct_group_callback(), 43))

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.state, OnboardingState.SECONDARY_VERIFICATION_PENDING)
        self.assertEqual(
            self.repository.onboarding_state(-100123, 43), OnboardingState.SECONDARY_VERIFICATION_PENDING
        )

    def test_secondary_challenge_bans_after_three_wrong_answers(self) -> None:
        asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))
        for _ in range(2):
            outcome = asyncio.run(self.service.verify_group_challenge(self._wrong_group_callback(), 43))
            self.assertFalse(outcome.accepted)
            self.assertEqual(outcome.state, OnboardingState.SECONDARY_VERIFICATION_PENDING)
        outcome = asyncio.run(self.service.verify_group_challenge(self._wrong_group_callback(), 43))

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.state, OnboardingState.DECLINED)
        self.assertEqual(self.gateway.banned, [(-100123, 43)])
        self.assertEqual(self.gateway.ban_durations, [1])
        self.assertIn("二次验证未通过", self.gateway.replaced_group_verifications[-1][2])
        self.assertIn("封禁 1 小时", self.gateway.replaced_group_verifications[-1][2])

    def test_group_admin_can_approve_the_second_stage_verification(self) -> None:
        pending = asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))
        outcome = asyncio.run(self.service.resolve_group_challenge_by_admin(pending.callback_data or "", approved=True))

        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.state, OnboardingState.PENDING_FIRST_MESSAGE)
        self.assertEqual(self.gateway.released, [(-100123, 43)])
        self.assertIn("欢迎加入群组", self.gateway.replaced_group_verifications[-1][2])

    def test_failed_rotation_recovers_without_resetting_attempts(self) -> None:
        asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))
        wrong = self._wrong_group_callback()
        original_correct = self._correct_group_callback()
        with patch.object(self.gateway, "update_group_vps_challenge", new=AsyncMock(
            side_effect=RuntimeError("Telegram edit failed")
        )):
            result = asyncio.run(self.service.verify_group_challenge(wrong, 43))
        self.assertIn("正在重新发送", result.reason)
        remaining_seconds = self.repository.group_challenge_snapshot(-100123, 43).remaining_seconds
        self.assertFalse(asyncio.run(self.service.verify_group_challenge(original_correct, 43)).accepted)
        row = self.repository.query_one(
            "SELECT verification_attempts, verification_message_id FROM member_onboarding WHERE user_id=%s", (43,)
        )
        self.assertEqual(row["verification_attempts"], 1)
        self.assertIsNone(row["verification_message_id"])
        self.assertEqual(asyncio.run(self.service.expire_pending_verifications(
            datetime.now(timezone.utc) + timedelta(minutes=3)
        )), 0)
        future = (datetime.now(timezone.utc) + timedelta(minutes=3)).replace(microsecond=0)
        with patch("misakabot.onboarding.datetime", wraps=datetime) as clock:
            clock.now.return_value = future
            self.assertEqual(asyncio.run(self.service.resume_undelivered_group_challenges(
                frozenset({-100123})
            )), 1)
        row = self.repository.query_one(
            "SELECT verification_attempts, verification_expires_at FROM member_onboarding WHERE user_id=%s", (43,)
        )
        self.assertEqual(row["verification_attempts"], 1)
        self.assertEqual(
            datetime.fromisoformat(row["verification_expires_at"]),
            future + timedelta(seconds=max(remaining_seconds, 60)),
        )
        self.assertEqual(self.gateway.displayed_group_deadlines[-1], row["verification_expires_at"])
        self.assertIsNone(self.repository.group_challenge_snapshot(-100123, 43).remaining_seconds)
        self.assertEqual(self.repository.due_message_deletions(future.isoformat()), [(-100123, 1001)])
        for _ in range(2):
            result = asyncio.run(self.service.verify_group_challenge(self._wrong_group_callback(), 43))
        self.assertEqual(result.state, OnboardingState.DECLINED)
        self.assertEqual(self.gateway.ban_durations, [1])

    def test_recovered_rotation_restores_saved_budget_before_original_deadline(self) -> None:
        self.service.secondary_verification_ttl_minutes = 10
        asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))
        original = self.repository.group_challenge_snapshot(-100123, 43).expires_at
        with patch.object(self.gateway, "update_group_vps_challenge", new=AsyncMock(side_effect=RuntimeError("edit failed"))):
            asyncio.run(self.service.verify_group_challenge(self._wrong_group_callback(), 43))
        remaining_seconds = self.repository.group_challenge_snapshot(-100123, 43).remaining_seconds
        future = (datetime.now(timezone.utc) + timedelta(minutes=3)).replace(microsecond=0)
        with patch("misakabot.onboarding.datetime", wraps=datetime) as clock:
            clock.now.return_value = future
            self.assertEqual(asyncio.run(self.service.resume_undelivered_group_challenges(frozenset({-100123}))), 1)
        recovered = self.repository.group_challenge_snapshot(-100123, 43)
        self.assertEqual(datetime.fromisoformat(recovered.expires_at), future + timedelta(seconds=remaining_seconds))
        self.assertNotEqual(recovered.expires_at, original)
        self.assertEqual(recovered.attempts, 1)
        self.assertEqual(self.gateway.displayed_group_deadlines[-1], recovered.expires_at)

    def test_first_delivery_still_gets_full_time_after_send_succeeds(self) -> None:
        now = datetime.now(timezone.utc)
        original_send = self.gateway.send_group_vps_challenge
        with patch("misakabot.onboarding.datetime", wraps=datetime) as clock:
            clock.now.return_value = now

            async def delayed_send(*args, **kwargs):
                clock.now.return_value = now + timedelta(seconds=45)
                return await original_send(*args, **kwargs)

            with patch.object(self.gateway, "send_group_vps_challenge", side_effect=delayed_send):
                asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))
        snapshot = self.repository.group_challenge_snapshot(-100123, 43)
        self.assertEqual(datetime.fromisoformat(snapshot.expires_at), now + timedelta(seconds=45, minutes=2))
        self.assertIsNone(self.gateway.sent_group_deadlines[-1])

    def test_recovery_near_original_deadline_only_grants_minimum_window(self) -> None:
        self.service.secondary_verification_ttl_minutes = 5
        asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))
        original = datetime.fromisoformat(self.repository.group_challenge_snapshot(-100123, 43).expires_at)
        failure_at = original - timedelta(seconds=20)
        with patch("misakabot.onboarding.datetime", wraps=datetime) as clock, patch.object(
            self.gateway, "update_group_vps_challenge", new=AsyncMock(side_effect=RuntimeError("edit failed"))
        ):
            clock.now.return_value = failure_at
            asyncio.run(self.service.verify_group_challenge(self._wrong_group_callback(), 43))
        future = (failure_at + timedelta(minutes=3)).replace(microsecond=0)
        with patch("misakabot.onboarding.datetime", wraps=datetime) as clock:
            clock.now.return_value = future
            self.assertEqual(asyncio.run(self.service.resume_undelivered_group_challenges(frozenset({-100123}))), 1)
        recovered = self.repository.group_challenge_snapshot(-100123, 43)
        self.assertEqual(datetime.fromisoformat(recovered.expires_at), future + timedelta(seconds=60))

    def test_admin_decision_during_rotation_updates_the_clicked_card(self) -> None:
        for approved in (True, False):
            user_id = 440 if approved else 441
            asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=user_id)))
            message_id = self.repository.group_challenge_snapshot(-100123, user_id).message_id
            wrong = self._wrong_group_callback()
            original_edit = self.gateway.update_group_vps_challenge
            events = []
            original_replace = self.gateway.replace_group_verification_message

            async def record_replace(*args, **kwargs):
                events.append("final")
                await original_replace(*args, **kwargs)

            async def interleaved_edit(*args, **kwargs):
                token = args[5][0][1].split(":")[1]
                result = await self.service.resolve_group_challenge_by_admin(
                    token, approved, chat_id=-100123, callback_message_id=message_id,
                )
                self.assertEqual(result.accepted, approved)
                events.append("late_question")
                await original_edit(*args, **kwargs)

            with self.subTest(approved=approved), patch.object(
                self.gateway, "update_group_vps_challenge", side_effect=interleaved_edit
            ), patch.object(self.gateway, "replace_group_verification_message", side_effect=record_replace):
                with self.assertLogs("misakabot.onboarding", level="WARNING"):
                    asyncio.run(self.service.verify_group_challenge(wrong, user_id))
                self.assertEqual(events, ["final", "late_question", "final"])
                text = self.gateway.replaced_group_verifications[-1][2]
                self.assertIn("欢迎加入群组" if approved else "二次验证未通过", text)
                self.assertEqual(self.gateway.replaced_group_verifications[-1][:2], (-100123, message_id))
                self.assertEqual(self.repository.query_all("SELECT * FROM scheduled_message_deletions"), [])

    def test_admin_callback_fallback_cannot_override_registered_message(self) -> None:
        pending = asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))
        asyncio.run(self.service.resolve_group_challenge_by_admin(
            pending.callback_data, True, chat_id=-100123, callback_message_id=9999,
        ))
        self.assertEqual(self.gateway.replaced_group_verifications[-1][:2], (-100123, 1001))

    def test_rotation_does_not_delete_a_card_adopted_by_an_inflight_admin(self) -> None:
        async def scenario():
            await self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43))
            wrong = self._wrong_group_callback()
            started = asyncio.Event()
            finish = asyncio.Event()
            tasks = []

            async def slow_release(chat_id, user_id):
                started.set()
                await finish.wait()
                self.gateway.released.append((chat_id, user_id))

            async def interleaved_edit(*args, **kwargs):
                token = args[5][0][1].split(":")[1]
                tasks.append(asyncio.create_task(self.service.resolve_group_challenge_by_admin(
                    token, True, chat_id=-100123, callback_message_id=1001,
                )))
                await started.wait()

            with patch.object(self.gateway, "release_member", side_effect=slow_release), patch.object(
                self.gateway, "update_group_vps_challenge", side_effect=interleaved_edit
            ):
                await self.service.verify_group_challenge(wrong, 43)
                self.assertEqual(self.gateway.deleted_messages, [])
                finish.set()
                await asyncio.gather(*tasks)
            self.assertIn("欢迎加入群组", self.gateway.replaced_group_verifications[-1][2])
        with self.assertLogs("misakabot.onboarding", level="WARNING"):
            asyncio.run(scenario())

    def test_admin_final_card_survives_rotation_api_error(self) -> None:
        asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))

        async def failed_edit(*args, **kwargs):
            token = args[5][0][1].split(":")[1]
            await self.service.resolve_group_challenge_by_admin(
                token, True, chat_id=-100123, callback_message_id=1001,
            )
            raise RuntimeError("Telegram accepted the late edit but its response was lost")

        with patch.object(self.gateway, "update_group_vps_challenge", side_effect=failed_edit):
            with self.assertLogs("misakabot.onboarding", level="WARNING"):
                asyncio.run(self.service.verify_group_challenge(self._wrong_group_callback(), 43))
        self.assertIn("欢迎加入群组", self.gateway.replaced_group_verifications[-1][2])
        self.assertEqual(len(self.gateway.replaced_group_verifications), 2)
        self.assertEqual(self.repository.onboarding_state(-100123, 43), OnboardingState.PENDING_FIRST_MESSAGE)

    def test_failed_registration_after_admin_approval_replaces_newly_sent_card(self) -> None:
        original_send = self.gateway.send_group_vps_challenge

        async def approved_during_send(*args, **kwargs):
            message_id = await original_send(*args, **kwargs)
            token = args[4][0][1].split(":")[1]
            await self.service.resolve_group_challenge_by_admin(token, True, chat_id=-100123)
            return message_id

        with patch.object(self.gateway, "send_group_vps_challenge", side_effect=approved_during_send):
            with self.assertLogs("misakabot.onboarding", level="WARNING"):
                result = asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))
        self.assertIsNone(result.callback_data)
        self.assertEqual(result.state, OnboardingState.PENDING_FIRST_MESSAGE)
        self.assertEqual(self.gateway.replaced_group_verifications[-1][:2], (-100123, 1001))
        self.assertIn("欢迎加入群组", self.gateway.replaced_group_verifications[-1][2])

    def test_recovery_with_rejected_registration_is_not_counted_as_sent(self) -> None:
        now = datetime.now(timezone.utc) - timedelta(minutes=3)
        self.repository.create_group_challenge(
            chat_id=-100123, user_id=43, username=None, token_hash="unsent", answer_index=0,
            expires_at=(now + timedelta(minutes=2)).isoformat(), now=now.isoformat(), verification_flow="join_request",
        )
        with patch.object(self.repository, "set_group_challenge_message_id", return_value=False):
            with self.assertLogs("misakabot.onboarding", level="WARNING"):
                resumed = asyncio.run(self.service.resume_undelivered_group_challenges(frozenset({-100123})))
        self.assertEqual(resumed, 0)
        self.assertEqual(self.gateway.deleted_messages, [(-100123, 1001)])

    def test_failed_delivery_registration_deletes_unregistered_card_and_returns_no_token(self) -> None:
        with patch.object(self.repository, "set_group_challenge_message_id", return_value=False):
            with self.assertLogs("misakabot.onboarding", level="WARNING") as logs:
                result = asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))
        self.assertIsNone(result.callback_data)
        self.assertEqual(self.gateway.deleted_messages, [(-100123, 1001)])
        self.assertTrue(any("registration_rejected" in entry for entry in logs.output))

    def test_failed_registration_cleanup_error_only_logs_and_keeps_durable_guard(self) -> None:
        original_send = self.gateway.send_group_vps_challenge

        async def guarded_send(*args, **kwargs):
            message_id = await original_send(*args, **kwargs)
            self.repository.schedule_message_deletion(-100123, message_id, datetime.now(timezone.utc).isoformat())
            return message_id

        with patch.object(self.gateway, "send_group_vps_challenge", side_effect=guarded_send), patch.object(
            self.repository, "set_group_challenge_message_id", return_value=False
        ), patch.object(self.gateway, "delete_message", new=AsyncMock(side_effect=RuntimeError("delete failed"))):
            with self.assertLogs("misakabot.onboarding", level="WARNING") as logs:
                result = asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))
        self.assertIsNone(result.callback_data)
        self.assertTrue(any("cleanup_failed" in entry for entry in logs.output))
        self.assertEqual(self.repository.due_message_deletions(
            (datetime.now(timezone.utc) + timedelta(seconds=61)).isoformat()
        ), [(-100123, 1001)])

    def test_failed_final_replacement_after_late_edit_rearms_cleanup(self) -> None:
        pending = asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))
        asyncio.run(self.service.resolve_group_challenge_by_admin(pending.callback_data, True, chat_id=-100123))
        self.assertEqual(self.repository.query_all("SELECT * FROM scheduled_message_deletions"), [])
        with patch.object(self.gateway, "replace_group_verification_message", new=AsyncMock(side_effect=RuntimeError("edit failed"))):
            with self.assertLogs("misakabot.onboarding", level="WARNING"):
                asyncio.run(self.service._handle_unregistered_group_challenge(-100123, 43, 1001))
        self.assertEqual(self.repository.due_message_deletions(
            (datetime.now(timezone.utc) + timedelta(seconds=61)).isoformat()
        ), [(-100123, 1001)])

    def test_expiry_cannot_ban_member_during_card_edit(self) -> None:
        asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))

        async def slow_edit(*args: object, **kwargs: object) -> None:
            self.assertEqual(await self.service.expire_pending_verifications(
                datetime.now(timezone.utc) + timedelta(minutes=3)
            ), 0)
            raise RuntimeError("edit failed after waiting")

        with patch.object(self.gateway, "update_group_vps_challenge", new=AsyncMock(side_effect=slow_edit)):
            asyncio.run(self.service.verify_group_challenge(self._wrong_group_callback(), 43))
        self.assertEqual(self.gateway.banned, [])

    def test_rotation_keeps_original_deadline_and_displays_it(self) -> None:
        asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))
        deadline = self.repository.query_one(
            "SELECT verification_expires_at FROM member_onboarding WHERE user_id=%s", (43,)
        )["verification_expires_at"]
        edit = AsyncMock()
        with patch.object(self.gateway, "update_group_vps_challenge", new=edit):
            asyncio.run(self.service.verify_group_challenge(self._wrong_group_callback(), 43))
        self.assertEqual(edit.await_args.kwargs["expires_at"], deadline)
        self.assertEqual(self.repository.query_one(
            "SELECT verification_expires_at FROM member_onboarding WHERE user_id=%s", (43,)
        )["verification_expires_at"], deadline)

    def test_delivery_ack_cannot_overwrite_a_newer_token(self) -> None:
        asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))
        now = datetime.now(timezone.utc)
        self.assertFalse(self.repository.set_group_challenge_message_id(
            -100123, 43, 9999, now.isoformat(), (now + timedelta(minutes=2)).isoformat(),
            token_hash="obsolete-token",
        ))
        self.assertEqual(self.repository.query_one(
            "SELECT verification_message_id FROM member_onboarding WHERE user_id=%s", (43,)
        )["verification_message_id"], 1001)

    def test_group_admin_can_reject_the_second_stage_verification(self) -> None:
        pending = asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))
        outcome = asyncio.run(self.service.resolve_group_challenge_by_admin(pending.callback_data or "", approved=False))

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.state, OnboardingState.DECLINED)
        self.assertEqual(self.gateway.banned, [(-100123, 43)])
        self.assertEqual(self.gateway.ban_durations, [1])
        self.assertIn("二次验证未通过", self.gateway.replaced_group_verifications[-1][2])

    def test_admin_callback_cannot_resolve_a_token_from_another_group(self) -> None:
        pending = asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))
        for approved in (True, False):
            result = asyncio.run(self.service.resolve_group_challenge_by_admin(
                pending.callback_data or "", approved, chat_id=-100999
            ))
            self.assertFalse(result.accepted)
        self.assertEqual(self.gateway.released, [])
        self.assertEqual(self.gateway.banned, [])
        self.assertTrue(asyncio.run(self.service.resolve_group_challenge_by_admin(
            pending.callback_data or "", True, chat_id=-100123
        )).accepted)

    def test_configured_ban_duration_applies_to_all_second_stage_failures(self) -> None:
        for hours in (0, 3):
            for index, failure in enumerate(("timeout", "wrong_answers", "admin_reject")):
                with self.subTest(hours=hours, failure=failure):
                    user_id = 200 + hours * 10 + index
                    now = datetime.now(timezone.utc)
                    gateway = FakeGateway()
                    service = OnboardingService(
                        self.repository, gateway, secondary_verification_ban_hours=hours
                    )
                    pending = asyncio.run(service.start_direct_join(DirectJoinInput(
                        chat_id=-100123, user_id=user_id, joined_at=now,
                    )))
                    if failure == "timeout":
                        asyncio.run(service.expire_pending_verifications(now + timedelta(minutes=3)))
                    elif failure == "admin_reject":
                        asyncio.run(service.resolve_group_challenge_by_admin(
                            pending.callback_data or "", approved=False
                        ))
                    else:
                        for _ in range(3):
                            _, _, prompt, choices, _ = gateway.group_challenges[-1]
                            question = next(q for q in GLOSSARY_QUESTIONS if q.prompt == prompt)
                            wrong = next(
                                data for text, data in choices if text != question.correct_option
                            )
                            asyncio.run(service.verify_group_challenge(wrong, user_id))
                    self.assertEqual(gateway.ban_durations, [hours])
                    self.assertFalse(self.repository.is_user_blocked(user_id))
                    expected = "永久封禁" if hours == 0 else "封禁 3 小时"
                    self.assertIn(expected, gateway.replaced_group_verifications[-1][2])

    def test_verified_join_does_not_receive_a_duplicate_direct_join_challenge(self) -> None:
        pending = asyncio.run(self.service.start(self.request))
        token = parse_qs(urlparse(pending.callback_data or "").query)["session"][0]
        asyncio.run(self.service.verify_token(token, 42))
        self.assertEqual(len(self.gateway.group_challenges), 1)

        repeated = asyncio.run(
            self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=42))
        )

        self.assertEqual(repeated.state, OnboardingState.SECONDARY_VERIFICATION_PENDING)
        self.assertEqual(len(self.gateway.group_challenges), 1)

    def test_startup_replaces_legacy_direct_join_button_with_vps_challenge(self) -> None:
        now = datetime.now(timezone.utc)
        legacy = JoinRequestInput(
            chat_id=-100123,
            user_id=66,
            user_chat_id=-100123,
            requested_at=now,
        )
        self.repository.create_pending_verification(
            legacy,
            token_hash="old-callback-token",
            expires_at=(now + timedelta(minutes=10)).isoformat(),
            verification_flow="direct_join",
        )

        resumed = asyncio.run(self.service.resume_pending_direct_join_challenges(frozenset({-100123})))

        self.assertEqual(resumed, 1)
        self.assertEqual(len(self.gateway.group_challenges), 1)
        self.assertEqual(self.repository.onboarding_state(-100123, 66), OnboardingState.SECONDARY_VERIFICATION_PENDING)

    def test_startup_retries_second_stage_challenge_without_a_message(self) -> None:
        now = datetime.now(timezone.utc) - timedelta(minutes=3)
        self.repository.create_group_challenge(
            chat_id=-100123,
            user_id=67,
            username=None,
            token_hash="undelivered-token",
            answer_index=0,
            expires_at=(now + timedelta(minutes=2)).isoformat(),
            now=now.isoformat(),
            verification_flow="join_request",
        )

        resumed = asyncio.run(self.service.resume_undelivered_group_challenges(frozenset({-100123})))

        self.assertEqual(resumed, 1)
        self.assertEqual(len(self.gateway.group_challenges), 1)
        self.assertEqual(self.repository.onboarding_state(-100123, 67), OnboardingState.SECONDARY_VERIFICATION_PENDING)

    def test_failed_challenge_delivery_is_retried_without_banning_unprompted_user(self) -> None:
        now = datetime.now(timezone.utc)
        with patch.object(self.gateway, "send_group_vps_challenge", new=AsyncMock(
            side_effect=RuntimeError("Telegram temporarily unavailable")
        )):
            with self.assertRaises(RuntimeError):
                asyncio.run(self.service.start_direct_join(DirectJoinInput(
                    chat_id=-100123, user_id=68, joined_at=now,
                )))
        future = now + timedelta(minutes=3)
        self.assertEqual(asyncio.run(self.service.expire_pending_verifications(future)), 0)
        self.assertEqual(self.gateway.banned, [])
        self.assertFalse(self.repository.claim_expired_verification(
            -100123, 68, OnboardingState.SECONDARY_VERIFICATION_PENDING, future.isoformat(),
            self.service._undelivered_challenge_deadline(future),
        ))
        with patch("misakabot.onboarding.datetime", wraps=datetime) as clock:
            clock.now.return_value = future
            resumed = asyncio.run(self.service.resume_undelivered_group_challenges(
                frozenset({-100123})
            ))
        self.assertEqual(resumed, 1)
        self.assertEqual(len(self.gateway.group_challenges), 1)
        self.assertEqual(asyncio.run(self.service.expire_pending_verifications(
            future + timedelta(minutes=3)
        )), 1)
        self.assertEqual(self.gateway.banned, [(-100123, 68)])

    def test_recovery_does_not_overwrite_a_challenge_approved_since_scan(self) -> None:
        pending = asyncio.run(self.service.start_direct_join(DirectJoinInput(
            chat_id=-100123, user_id=69,
        )))
        asyncio.run(self.service.resolve_group_challenge_by_admin(
            pending.callback_data or "", approved=True
        ))
        with patch.object(self.repository, "undelivered_group_challenges", return_value=[
            (-100123, 69, None, "direct_join")
        ]):
            self.assertEqual(asyncio.run(self.service.resume_undelivered_group_challenges(
                frozenset({-100123})
            )), 0)
        self.assertEqual(len(self.gateway.group_challenges), 1)
        self.assertEqual(len(self.gateway.restricted), 1)
        self.assertEqual(self.repository.onboarding_state(-100123, 69),
                         OnboardingState.PENDING_FIRST_MESSAGE)

    def test_active_delivery_attempt_is_not_recovered_concurrently(self) -> None:
        now = datetime.now(timezone.utc)
        self.repository.create_group_challenge(
            chat_id=-100123, user_id=70, username=None, token_hash="active-send",
            answer_index=0, expires_at=(now + timedelta(minutes=2)).isoformat(),
            now=now.isoformat(), verification_flow="direct_join",
        )
        self.assertEqual(asyncio.run(self.service.resume_undelivered_group_challenges(
            frozenset({-100123})
        )), 0)
        self.assertEqual(self.gateway.group_challenges, [])

    def test_undelivered_challenge_stops_retrying_and_expires_after_grace(self) -> None:
        started = datetime.now(timezone.utc) - timedelta(hours=1)
        self.repository.create_group_challenge(
            chat_id=-100123, user_id=71, username=None, token_hash="never-sent",
            answer_index=0, expires_at=(started + timedelta(minutes=2)).isoformat(),
            now=started.isoformat(), verification_flow="direct_join",
        )
        self.assertEqual(asyncio.run(self.service.resume_undelivered_group_challenges(
            frozenset({-100123})
        )), 0)
        self.assertEqual(self.gateway.group_challenges, [])
        self.assertEqual(asyncio.run(self.service.expire_pending_verifications()), 1)
        self.assertEqual(self.gateway.banned, [(-100123, 71)])

    def test_first_observed_message_is_reviewed_for_existing_members_too(self) -> None:
        observed_at = datetime.now(timezone.utc)
        # No onboarding row: this represents someone already in the group before Bot joined.
        self.assertTrue(self.service.mark_first_observed_message(-100123, 99, observed_at))
        self.assertFalse(self.service.mark_first_observed_message(-100123, 99, observed_at))

    def test_expired_join_request_is_automatically_declined(self) -> None:
        now = datetime.now(timezone.utc)
        expired_request = JoinRequestInput(
            chat_id=-100123,
            user_id=77,
            user_chat_id=7700,
            requested_at=now,
        )
        asyncio.run(self.service.start(expired_request))
        expiry_check_at = now + timedelta(minutes=6)

        expired_count = asyncio.run(self.service.expire_pending_verifications(expiry_check_at))

        self.assertEqual(expired_count, 1)
        self.assertEqual(self.gateway.declined, [(-100123, 77)])
        self.assertEqual(self.gateway.initial_verification_results, [])
        self.assertIsNone(self.repository.onboarding_state(-100123, 77))
        self.assertEqual(asyncio.run(self.service.expire_pending_verifications(expiry_check_at)), 0)

    def test_admin_approval_wins_over_a_stale_timeout_worker_read(self) -> None:
        """A timeout worker must not ban someone after their group challenge was approved."""
        now = datetime.now(timezone.utc)
        pending = asyncio.run(
            self.service.start_direct_join(
                DirectJoinInput(chat_id=-100123, user_id=91, joined_at=now)
            )
        )
        # Simulate the worker having read the expired record before the administrator acts.
        expiry_check_at = now + timedelta(minutes=11)
        delivery_deadline = self.service._undelivered_challenge_deadline(expiry_check_at)
        expired = self.repository.expired_pending_verifications(expiry_check_at.isoformat(), delivery_deadline)
        self.assertEqual(len(expired), 1)

        approved = asyncio.run(
            self.service.resolve_group_challenge_by_admin(pending.callback_data or "", approved=True)
        )
        self.assertTrue(approved.accepted)

        # The worker's stale item is no longer claimable, so it cannot issue a late ban.
        chat_id, user_id, _, state, _ = expired[0]
        self.assertFalse(
            self.repository.claim_expired_verification(chat_id, user_id, state, expiry_check_at.isoformat(), delivery_deadline)
        )
        self.assertEqual(self.gateway.banned, [])
        self.assertEqual(self.repository.onboarding_state(-100123, 91), OnboardingState.PENDING_FIRST_MESSAGE)

    def test_manual_telegram_join_approval_closes_first_stage_before_second_stage(self) -> None:
        requested_at = datetime.now(timezone.utc)
        request = JoinRequestInput(
            chat_id=-100123,
            user_id=92,
            user_chat_id=9200,
            requested_at=requested_at,
        )
        asyncio.run(self.service.start(request))

        outcome = asyncio.run(
            self.service.start_direct_join(
                DirectJoinInput(chat_id=-100123, user_id=92, joined_at=requested_at + timedelta(minutes=5))
            )
        )

        self.assertEqual(outcome.state, OnboardingState.SECONDARY_VERIFICATION_PENDING)
        self.assertEqual(self.gateway.declined, [])
        self.assertEqual(self.gateway.manual_join_welcomes, [(-100123, 92)])
        self.assertEqual(len(self.gateway.group_challenges), 1)

    def test_expiry_worker_detects_a_manually_approved_member_when_join_event_is_missing(self) -> None:
        now = datetime.now(timezone.utc)
        request = JoinRequestInput(
            chat_id=-100123,
            user_id=93,
            user_chat_id=9300,
            requested_at=now,
        )
        asyncio.run(self.service.start(request))
        self.gateway.group_members.add((-100123, 93))

        completed = asyncio.run(self.service.expire_pending_verifications(now + timedelta(minutes=6)))

        self.assertEqual(completed, 0)
        self.assertEqual(self.gateway.declined, [])
        self.assertEqual(self.gateway.manual_join_welcomes, [(-100123, 93)])
        self.assertEqual(len(self.gateway.group_challenges), 1)
        self.assertEqual(
            self.repository.onboarding_state(-100123, 93), OnboardingState.SECONDARY_VERIFICATION_PENDING
        )

    def test_expiry_defers_when_member_status_cannot_be_checked(self) -> None:
        now = datetime.now(timezone.utc)
        self.gateway = MemberLookupFailingGateway()
        self.service = OnboardingService(
            self.repository, self.gateway, join_verify_url="https://dashboard.example/join-verify"
        )
        request = JoinRequestInput(
            chat_id=-100123,
            user_id=94,
            user_chat_id=9400,
            requested_at=now,
        )
        asyncio.run(self.service.start(request))

        completed = asyncio.run(self.service.expire_pending_verifications(now + timedelta(minutes=6)))

        self.assertEqual(completed, 0)
        self.assertEqual(self.gateway.declined, [])
        self.assertEqual(
            self.repository.onboarding_state(-100123, 94), OnboardingState.VERIFICATION_PENDING
        )

    def test_missing_member_lookup_proceeds_to_decline_instead_of_retrying_forever(self) -> None:
        now = datetime.now(timezone.utc)
        self.gateway = MissingMemberLookupGateway()
        self.service = OnboardingService(
            self.repository, self.gateway, join_verify_url="https://dashboard.example/join-verify"
        )
        request = JoinRequestInput(
            chat_id=-100123,
            user_id=95,
            user_chat_id=9500,
            requested_at=now,
        )
        asyncio.run(self.service.start(request))

        completed = asyncio.run(
            self.service.expire_pending_verifications(now + timedelta(minutes=6))
        )

        self.assertEqual(completed, 1)
        self.assertEqual(self.gateway.declined, [(-100123, 95)])
        self.assertIsNone(self.repository.onboarding_state(-100123, 95))

    def test_deactivated_user_expiry_is_closed_without_retrying(self) -> None:
        now = datetime.now(timezone.utc)
        self.gateway = DeactivatedUserGateway()
        self.service = OnboardingService(
            self.repository, self.gateway, join_verify_url="https://dashboard.example/join-verify"
        )
        expired_request = JoinRequestInput(
            chat_id=-100123,
            user_id=78,
            user_chat_id=7800,
            requested_at=now,
        )
        asyncio.run(self.service.start(expired_request))

        expiry_check_at = now + timedelta(minutes=6)
        self.assertEqual(asyncio.run(self.service.expire_pending_verifications(expiry_check_at)), 1)
        self.assertEqual(asyncio.run(self.service.expire_pending_verifications(expiry_check_at)), 0)
        self.assertEqual(self.gateway.initial_verification_results, [])
        self.assertIsNone(self.repository.onboarding_state(-100123, 78))

    def test_deactivated_membership_lookup_does_not_leave_pending_record(self) -> None:
        now = datetime.now(timezone.utc)
        gateway = DeactivatedMemberLookupGateway()
        service = OnboardingService(
            self.repository, gateway, join_verify_url="https://dashboard.example/join-verify"
        )
        request = JoinRequestInput(
            chat_id=-100123, user_id=78, user_chat_id=7800, requested_at=now,
        )
        asyncio.run(service.start(request))
        expiry_check_at = now + timedelta(minutes=6)
        self.assertEqual(asyncio.run(service.expire_pending_verifications(expiry_check_at)), 1)
        self.assertEqual(asyncio.run(service.expire_pending_verifications(expiry_check_at)), 0)
        self.assertIsNone(self.repository.onboarding_state(-100123, 78))
        self.assertEqual(gateway.initial_verification_results, [])

    def test_missing_hidden_requester_expiry_is_closed_without_retrying(self) -> None:
        now = datetime.now(timezone.utc)
        self.gateway = MissingHiddenRequesterGateway()
        self.service = OnboardingService(
            self.repository, self.gateway, join_verify_url="https://dashboard.example/join-verify"
        )
        expired_request = JoinRequestInput(
            chat_id=-100123,
            user_id=79,
            user_chat_id=7900,
            requested_at=now,
        )
        asyncio.run(self.service.start(expired_request))

        expiry_check_at = now + timedelta(minutes=6)
        self.assertEqual(asyncio.run(self.service.expire_pending_verifications(expiry_check_at)), 1)
        self.assertEqual(asyncio.run(self.service.expire_pending_verifications(expiry_check_at)), 0)
        self.assertEqual(self.gateway.initial_verification_results, [])
        self.assertIsNone(self.repository.onboarding_state(-100123, 79))

    def test_stale_replayed_join_request_with_missing_requester_is_acknowledged(self) -> None:
        now = datetime.now(timezone.utc)
        self.gateway = MissingHiddenRequesterGateway()
        self.service = OnboardingService(
            self.repository, self.gateway, join_verify_url="https://dashboard.example/join-verify"
        )
        replayed = JoinRequestInput(
            chat_id=-100123,
            user_id=80,
            user_chat_id=8000,
            requested_at=now - timedelta(hours=1),
        )

        outcome = asyncio.run(self.service.start(replayed))

        self.assertEqual(outcome.state, OnboardingState.DECLINED)
        self.assertIsNone(self.repository.onboarding_state(-100123, 80))

    def test_expired_direct_join_is_banned(self) -> None:
        now = datetime.now(timezone.utc)
        asyncio.run(
            self.service.start_direct_join(
                DirectJoinInput(
                    chat_id=-100123,
                    user_id=88,
                    joined_at=now,
                )
            )
        )

        # The answer timer starts when the prompt is delivered.
        expired_count = asyncio.run(
            self.service.expire_pending_verifications(now + timedelta(minutes=11))
        )

        self.assertEqual(expired_count, 1)
        self.assertEqual(self.gateway.banned, [(-100123, 88)])
        self.assertEqual(self.gateway.ban_durations, [1])
        self.assertEqual(self.gateway.replaced_group_delete_after[-1], 60)
