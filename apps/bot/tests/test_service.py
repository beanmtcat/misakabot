import asyncio
import tempfile
import unittest
from pathlib import Path

from misakabot.domain import Action, MessageInput, ModerationVerdict, ReviewStatus
from misakabot.llm import RuleBasedModerationClient
from misakabot.repository import AuditRepository
from misakabot.service import ModerationService


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.calls.append(("delete", chat_id, message_id))

    async def restrict_member(self, chat_id: int, user_id: int, minutes: int) -> None:
        self.calls.append(("restrict", chat_id, user_id))

    async def ban_member(self, chat_id: int, user_id: int) -> None:
        self.calls.append(("ban", chat_id, user_id))

    async def send_moderation_notice(self, chat_id: int, user_id: int) -> None:
        self.calls.append(("notice", chat_id, user_id))

    async def release_member(self, chat_id: int, user_id: int) -> None:
        self.calls.append(("release", chat_id, user_id))


class IncorrectHighRiskVerdictClient:
    async def judge(self, message: object, signals: object) -> ModerationVerdict:
        return ModerationVerdict(
            is_ad=True,
            category="account_trade",
            confidence=0.99,
            evidence=("账号",),
            reason="误将自用订阅配置求购判为账号交易",
        )


class ModerationServiceTests(unittest.TestCase):
    def test_ad_requires_administrator_approval_before_ban(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = AuditRepository(Path(directory) / "audit.sqlite3")
            repository.initialize()
            gateway = FakeGateway()
            service = ModerationService(repository, gateway, RuleBasedModerationClient())
            outcome = asyncio.run(
                service.moderate(
                    MessageInput(
                        chat_id=-1001,
                        message_id=42,
                        user_id=9,
                        text="出 微信号 抖音号 快手号 QQ号",
                    )
                )
            )
        self.assertEqual(outcome.action, Action.NEEDS_REVIEW)
        self.assertEqual(gateway.calls, [])

    def test_normal_message_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = AuditRepository(Path(directory) / "audit.sqlite3")
            repository.initialize()
            gateway = FakeGateway()
            service = ModerationService(repository, gateway, RuleBasedModerationClient())
            outcome = asyncio.run(
                service.moderate(
                    MessageInput(chat_id=-1001, message_id=43, user_id=10, text="这个服务延迟怎么样？")
                )
            )
        self.assertEqual(outcome.action, Action.ALLOW)
        self.assertEqual(gateway.calls, [])

    def test_advertising_display_name_is_sent_to_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = AuditRepository(Path(directory) / "audit.sqlite3")
            repository.initialize()
            gateway = FakeGateway()
            service = ModerationService(repository, gateway, RuleBasedModerationClient())
            outcome = asyncio.run(
                service.moderate(
                    MessageInput(
                        chat_id=-1001,
                        message_id=45,
                        user_id=12,
                        display_name="苹果18来预订",
                        text="大家好，怎么使用？",
                    )
                )
            )
        self.assertEqual(outcome.action, Action.NEEDS_REVIEW)
        self.assertEqual(gateway.calls, [])

    def test_rule_hit_that_kimi_clears_keeps_message_visible(self) -> None:
        class ClearedByKimiClient:
            async def judge(self, message: object, signals: object) -> ModerationVerdict:
                return ModerationVerdict(
                    is_ad=False,
                    category="normal_discussion",
                    confidence=0.90,
                    evidence=(),
                    reason="内容属于正常讨论",
                )

        with tempfile.TemporaryDirectory() as directory:
            repository = AuditRepository(Path(directory) / "audit.sqlite3")
            repository.initialize()
            gateway = FakeGateway()
            service = ModerationService(repository, gateway, ClearedByKimiClient())
            outcome = asyncio.run(
                service.moderate(
                    MessageInput(
                        chat_id=-1001,
                        message_id=49,
                        user_id=16,
                        text="我也是招商，不过我是伪造证件下的信用卡",
                    )
                )
            )

        self.assertEqual(outcome.action, Action.ALLOW)
        self.assertEqual(gateway.calls, [])

    def test_normal_first_message_is_audited_without_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = AuditRepository(Path(directory) / "audit.sqlite3")
            repository.initialize()
            gateway = FakeGateway()
            service = ModerationService(repository, gateway, RuleBasedModerationClient())
            outcome = asyncio.run(
                service.moderate(
                    MessageInput(chat_id=-1001, message_id=44, user_id=11, text="大家好，怎么使用？"),
                    force_llm_review=True,
                )
            )
        self.assertEqual(outcome.action, Action.ALLOW)
        self.assertEqual(gateway.calls, [])

    def test_model_only_ad_verdict_on_first_observed_message_needs_review_not_ban(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = AuditRepository(Path(directory) / "audit.sqlite3")
            repository.initialize()
            gateway = FakeGateway()
            service = ModerationService(repository, gateway, IncorrectHighRiskVerdictClient())
            outcome = asyncio.run(
                service.moderate(
                    MessageInput(chat_id=-1001, message_id=47, user_id=14, text="你好，群规是什么？"),
                    force_llm_review=True,
                )
            )
        self.assertEqual(outcome.action, Action.NEEDS_REVIEW)
        self.assertEqual(gateway.calls, [])

    def test_first_observed_sticker_is_audited_without_deletion_or_mute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = AuditRepository(Path(directory) / "audit.sqlite3")
            repository.initialize()
            gateway = FakeGateway()
            service = ModerationService(repository, gateway, RuleBasedModerationClient())
            outcome = asyncio.run(
                service.moderate(
                    MessageInput(
                        chat_id=-1001,
                        message_id=48,
                        user_id=15,
                        text="",
                        media_type="sticker",
                    ),
                    force_llm_review=True,
                )
            )
        self.assertEqual(outcome.action, Action.ALLOW)
        self.assertEqual(gateway.calls, [])

    def test_vps_trade_model_verdict_requires_review_not_direct_ban(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = AuditRepository(Path(directory) / "audit.sqlite3")
            repository.initialize()
            gateway = FakeGateway()
            service = ModerationService(repository, gateway, IncorrectHighRiskVerdictClient())
            outcome = asyncio.run(
                service.moderate(
                    MessageInput(
                        chat_id=-1001,
                        message_id=46,
                        user_id=13,
                        text="各位大佬收个闲置的带订阅的账号（LA PRO AS3 基础配置）。",
                    ),
                    force_llm_review=True,
                )
            )
        self.assertEqual(outcome.action, Action.NEEDS_REVIEW)
        self.assertEqual(gateway.calls, [])

    def test_semantic_payment_service_ad_requires_review_not_direct_ban(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = AuditRepository(Path(directory) / "audit.sqlite3")
            repository.initialize()
            gateway = FakeGateway()
            service = ModerationService(repository, gateway, IncorrectHighRiskVerdictClient())
            outcome = asyncio.run(
                service.moderate(
                    MessageInput(
                        chat_id=-1001,
                        message_id=51,
                        user_id=18,
                        text="搞定 GPT-Plus、Kiro、Claude，任何都能付费",
                    )
                )
            )
        self.assertEqual(outcome.action, Action.NEEDS_REVIEW)
        self.assertEqual(gateway.calls, [])

    def test_first_observed_llm_failure_requires_review(self) -> None:
        class FailingClient:
            async def judge(self, message: object, signals: object) -> ModerationVerdict:
                raise TimeoutError("provider timeout")

        with tempfile.TemporaryDirectory() as directory:
            repository = AuditRepository(Path(directory) / "audit.sqlite3")
            repository.initialize()
            gateway = FakeGateway()
            service = ModerationService(repository, gateway, FailingClient())
            outcome = asyncio.run(
                service.moderate(
                    MessageInput(chat_id=-1001, message_id=50, user_id=17, text="正常发言"),
                    force_llm_review=True,
                )
            )
        self.assertEqual(outcome.action, Action.NEEDS_REVIEW)
        self.assertEqual(gateway.calls, [])

    def test_pending_review_can_only_be_claimed_once_and_records_admin_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = AuditRepository(Path(directory) / "audit.sqlite3")
            repository.initialize()
            gateway = FakeGateway()
            service = ModerationService(repository, gateway, IncorrectHighRiskVerdictClient())
            outcome = asyncio.run(
                service.moderate(
                    MessageInput(
                        chat_id=-1001,
                        message_id=52,
                        user_id=19,
                        text="搞定 GPT-Plus、Kiro、Claude，任何都能付费",
                    )
                )
            )
            assert outcome.event_id is not None
            target = repository.claim_moderation_review(outcome.event_id, -1001, "2026-09-16T12:00:00+00:00")
            duplicate = repository.claim_moderation_review(outcome.event_id, -1001, "2026-09-16T12:00:01+00:00")
            completed = repository.complete_moderation_review(
                outcome.event_id, -1001, 99, Action.ALLOW, ReviewStatus.FALSE_POSITIVE,
                "2026-09-16T12:00:02+00:00",
            )
            stored = repository.query_one(
                "SELECT action,review_status,reviewed_by_user_id FROM moderation_events WHERE id=%s",
                (outcome.event_id,),
            )
        self.assertIsNotNone(target)
        self.assertEqual(target.user_id if target else None, 19)
        self.assertIsNone(duplicate)
        self.assertTrue(completed)
        self.assertEqual(stored, {
            "action": Action.ALLOW.value,
            "review_status": ReviewStatus.FALSE_POSITIVE.value,
            "reviewed_by_user_id": 99,
        })


if __name__ == "__main__":
    unittest.main()
