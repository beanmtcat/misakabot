import asyncio
import tempfile
import unittest
from pathlib import Path

from misakabot.domain import Action, MessageInput, ModerationVerdict
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
    def test_ad_is_deleted_and_banned_on_first_message(self) -> None:
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
        self.assertEqual(outcome.action, Action.PERMANENT_BAN)
        self.assertEqual([call[0] for call in gateway.calls], ["delete", "ban", "notice"])

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
        self.assertIn(outcome.action, {Action.NEEDS_REVIEW, Action.PERMANENT_BAN})
        if outcome.action is Action.PERMANENT_BAN:
            self.assertEqual([call[0] for call in gateway.calls], ["delete", "ban", "notice"])
        else:
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

    def test_allowed_vps_trade_stays_visible_on_a_model_misclassification(self) -> None:
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
        self.assertEqual(outcome.action, Action.ALLOW)
        self.assertEqual(gateway.calls, [])


if __name__ == "__main__":
    unittest.main()
