from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from misakabot.domain import DirectJoinInput, JoinRequestInput, OnboardingState
from misakabot.onboarding import VPS_SAFETY_QUESTIONS, OnboardingService
from misakabot.repository import AuditRepository


class FakeGateway:
    def __init__(self) -> None:
        self.approved: list[tuple[int, int]] = []
        self.declined: list[tuple[int, int]] = []
        self.verifications: list[tuple[int, str, str | None]] = []
        self.group_challenges: list[tuple[int, int, str, tuple[tuple[str, str], ...], int]] = []
        self.restricted: list[tuple[int, int, int]] = []
        self.released: list[tuple[int, int]] = []
        self.banned: list[tuple[int, int]] = []
        self.replaced_group_verifications: list[tuple[int, int, str]] = []
        self.replaced_group_delete_after: list[int | None] = []
        self.replaced_group_channel_urls: list[str | None] = []
        self.initial_verification_results: list[tuple[int, int, str]] = []
        self.initial_result_delete_after: list[int | None] = []
        self.manual_join_welcomes: list[tuple[int, int]] = []
        self.group_members: set[tuple[int, int]] = set()

    async def approve_join_request(self, chat_id: int, user_id: int) -> None:
        self.approved.append((chat_id, user_id))

    async def decline_join_request(self, chat_id: int, user_id: int) -> None:
        self.declined.append((chat_id, user_id))

    async def send_join_verification(
        self, user_chat_id: int, callback_data: str, group_title: str | None
    ) -> None:
        self.verifications.append((user_chat_id, callback_data, group_title))

    async def send_group_vps_challenge(
        self,
        chat_id: int,
        user_id: int,
        username: str | None,
        prompt: str,
        choices: tuple[tuple[str, str], ...],
        ttl_minutes: int,
    ) -> int:
        self.group_challenges.append((chat_id, user_id, prompt, choices, ttl_minutes))
        return 1000 + len(self.group_challenges)

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

    async def ban_member(self, chat_id: int, user_id: int) -> None:
        self.banned.append((chat_id, user_id))

    async def get_member_info(self, chat_id: int, user_id: int) -> str:
        return f"用户 ID：{user_id}"

    async def is_group_member(self, chat_id: int, user_id: int) -> bool:
        return (chat_id, user_id) in self.group_members


class DeactivatedUserGateway(FakeGateway):
    async def decline_join_request(self, chat_id: int, user_id: int) -> None:
        raise RuntimeError("Telegram server says - Forbidden: user is deactivated")


class MissingHiddenRequesterGateway(FakeGateway):
    async def decline_join_request(self, chat_id: int, user_id: int) -> None:
        raise RuntimeError("Telegram server says - Bad Request: HIDE_REQUESTER_MISSING")


class ReleaseFailingGateway(FakeGateway):
    async def release_member(self, chat_id: int, user_id: int) -> None:
        raise RuntimeError("temporary Telegram failure")


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
        correct_option = next(question.correct_option for question in VPS_SAFETY_QUESTIONS if question.prompt == prompt)
        return next(callback_data for text, callback_data in choices if text == correct_option)

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
        self.assertEqual(self.gateway.group_challenges[-1][-1], 2)
        self.assertEqual(self.gateway.verifications[-1][2], "测试 VPS 交流群")

        challenge = asyncio.run(self.service.verify_group_challenge(self._correct_group_callback(), 42))
        self.assertTrue(challenge.accepted)
        self.assertEqual(challenge.state, OnboardingState.PENDING_FIRST_MESSAGE)
        self.assertEqual(self.gateway.released, [(-100123, 42)])
        self.assertIn("欢迎加入群组", self.gateway.replaced_group_verifications[-1][2])

        self.assertTrue(self.service.mark_first_message(-100123, 42, datetime.now(timezone.utc)))
        self.assertFalse(self.service.mark_first_message(-100123, 42, datetime.now(timezone.utc)))

    def test_only_the_requesting_account_can_use_its_token(self) -> None:
        pending = asyncio.run(self.service.start(self.request))
        token = parse_qs(urlparse(pending.callback_data or "").query)["session"][0]
        outcome = asyncio.run(self.service.verify_token(token, 999))
        self.assertFalse(outcome.accepted)
        self.assertEqual(self.gateway.approved, [])

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
        correct_callback = self._correct_group_callback()
        _, _, _, choices, _ = self.gateway.group_challenges[-1]
        wrong_callback = next(callback_data for _, callback_data in choices if callback_data != correct_callback)

        for _ in range(2):
            outcome = asyncio.run(self.service.verify_group_challenge(wrong_callback, 43))
            self.assertFalse(outcome.accepted)
            self.assertEqual(outcome.state, OnboardingState.SECONDARY_VERIFICATION_PENDING)
        outcome = asyncio.run(self.service.verify_group_challenge(wrong_callback, 43))

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.state, OnboardingState.DECLINED)
        self.assertEqual(self.gateway.banned, [(-100123, 43)])
        self.assertIn("二次验证未通过", self.gateway.replaced_group_verifications[-1][2])

    def test_group_admin_can_approve_the_second_stage_verification(self) -> None:
        pending = asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))
        outcome = asyncio.run(self.service.resolve_group_challenge_by_admin(pending.callback_data or "", approved=True))

        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.state, OnboardingState.PENDING_FIRST_MESSAGE)
        self.assertEqual(self.gateway.released, [(-100123, 43)])
        self.assertIn("欢迎加入群组", self.gateway.replaced_group_verifications[-1][2])

    def test_group_admin_can_reject_the_second_stage_verification(self) -> None:
        pending = asyncio.run(self.service.start_direct_join(DirectJoinInput(chat_id=-100123, user_id=43)))
        outcome = asyncio.run(self.service.resolve_group_challenge_by_admin(pending.callback_data or "", approved=False))

        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.state, OnboardingState.DECLINED)
        self.assertEqual(self.gateway.banned, [(-100123, 43)])
        self.assertIn("二次验证未通过", self.gateway.replaced_group_verifications[-1][2])

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
            requested_at=now - timedelta(minutes=11),
        )
        asyncio.run(self.service.start(expired_request))

        expired_count = asyncio.run(self.service.expire_pending_verifications(now))

        self.assertEqual(expired_count, 1)
        self.assertEqual(self.gateway.declined, [(-100123, 77)])
        self.assertIn("申请已超时", self.gateway.initial_verification_results[-1][2])
        self.assertEqual(self.gateway.initial_result_delete_after[-1], 60)
        self.assertEqual(asyncio.run(self.service.expire_pending_verifications(now)), 0)

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
        expired = self.repository.expired_pending_verifications(expiry_check_at.isoformat())
        self.assertEqual(len(expired), 1)

        approved = asyncio.run(
            self.service.resolve_group_challenge_by_admin(pending.callback_data or "", approved=True)
        )
        self.assertTrue(approved.accepted)

        # The worker's stale item is no longer claimable, so it cannot issue a late ban.
        chat_id, user_id, _, state, _ = expired[0]
        self.assertFalse(
            self.repository.claim_expired_verification(chat_id, user_id, state, expiry_check_at.isoformat())
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
            requested_at=now - timedelta(minutes=11),
        )
        asyncio.run(self.service.start(request))
        self.gateway.group_members.add((-100123, 93))

        completed = asyncio.run(self.service.expire_pending_verifications(now))

        self.assertEqual(completed, 0)
        self.assertEqual(self.gateway.declined, [])
        self.assertEqual(self.gateway.manual_join_welcomes, [(-100123, 93)])
        self.assertEqual(len(self.gateway.group_challenges), 1)
        self.assertEqual(
            self.repository.onboarding_state(-100123, 93), OnboardingState.SECONDARY_VERIFICATION_PENDING
        )

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
            requested_at=now - timedelta(minutes=11),
        )
        asyncio.run(self.service.start(expired_request))

        self.assertEqual(asyncio.run(self.service.expire_pending_verifications(now)), 1)
        self.assertEqual(asyncio.run(self.service.expire_pending_verifications(now)), 0)

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
            requested_at=now - timedelta(minutes=11),
        )
        asyncio.run(self.service.start(expired_request))

        self.assertEqual(asyncio.run(self.service.expire_pending_verifications(now)), 1)
        self.assertEqual(asyncio.run(self.service.expire_pending_verifications(now)), 0)

    def test_expired_direct_join_is_banned(self) -> None:
        now = datetime.now(timezone.utc)
        asyncio.run(
            self.service.start_direct_join(
                DirectJoinInput(
                    chat_id=-100123,
                    user_id=88,
                    joined_at=now - timedelta(minutes=11),
                )
            )
        )

        expired_count = asyncio.run(self.service.expire_pending_verifications(now))

        self.assertEqual(expired_count, 1)
        self.assertEqual(self.gateway.banned, [(-100123, 88)])
        self.assertEqual(self.gateway.replaced_group_delete_after[-1], 60)
