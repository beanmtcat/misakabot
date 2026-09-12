import unittest

from misakabot.normalizer import normalize_message
from misakabot.signals import ALLOWED_VPS_TRADE_REASON, detect_suspicion


class NormalizerTests(unittest.TestCase):
    def test_strips_zero_width_and_fire_text_separators(self) -> None:
        result = normalize_message("虚拟\u200d信用卡 \u200bCC/GP\u200cT 下单丝滑")
        self.assertTrue(result.had_invisible_chars)
        self.assertIn("虚拟信用卡", result.normalized_text)
        self.assertIn("cc/gpt", result.normalized_text)

    def test_keeps_links_for_evidence(self) -> None:
        result = normalize_message("优惠链接 https://t.me/example")
        self.assertEqual(result.urls, ("https://t.me/example",))

    def test_account_trade_is_suspicious_on_first_message(self) -> None:
        result = normalize_message("出微信号 可加好友 视频号正常")
        signals = detect_suspicion(result)
        self.assertTrue(signals.is_suspicious)
        self.assertGreaterEqual(signals.score, 4)

    def test_contact_request_with_daily_pay_is_suspicious(self) -> None:
        result = normalize_message("私我一天一百元")
        signals = detect_suspicion(result)
        self.assertTrue(signals.is_suspicious)
        self.assertIn("销售或引流意图", signals.reasons)
        self.assertIn("报酬或有偿服务信息", signals.reasons)

    def test_daily_settlement_part_time_recruitment_is_suspicious(self) -> None:
        result = normalize_message("没上班\u200b的来🙋 日结\u2060兼\u200c职")
        signals = detect_suspicion(result)
        self.assertTrue(signals.is_suspicious)
        self.assertIn("报酬或有偿服务信息", signals.reasons)
        self.assertIn("招募或兼职意图", signals.reasons)

    def test_fire_text_device_preorder_is_suspicious(self) -> None:
        result = normalize_message("苹\u2060‧果\u206018来预‧订")
        signals = detect_suspicion(result)
        self.assertEqual(result.normalized_text, "苹果18来预订")
        self.assertTrue(signals.is_suspicious)
        self.assertIn("数码商品预售或销售意图", signals.reasons)

    def test_advertising_display_name_is_suspicious_even_with_normal_message(self) -> None:
        result = normalize_message("大家好，今天服务器怎么样？", sender_name="苹\u2060‧果18来预‧订")
        signals = detect_suspicion(result)
        self.assertEqual(result.normalized_sender_name, "苹果18来预订")
        self.assertTrue(signals.is_suspicious)
        self.assertIn("发送者昵称或用户名含广告意图", signals.reasons)

    def test_photography_service_display_name_is_suspicious(self) -> None:
        result = normalize_message("大家好", sender_name="手📱机【拍照一百张】📸°寻我简介")
        signals = detect_suspicion(result)
        self.assertTrue(signals.is_suspicious)
        self.assertIn("发送者昵称或用户名含服务推广及引流词", signals.reasons)

    def test_discounted_device_display_name_is_suspicious_when_offer_precedes_product(self) -> None:
        result = normalize_message("正常讨论", sender_name="7~折~出!走私··苹果17")
        signals = detect_suspicion(result)
        self.assertTrue(signals.is_suspicious)
        self.assertIn("发送者昵称或用户名含广告意图", signals.reasons)

    def test_high_hourly_pay_recruitment_with_car_reward_is_suspicious(self) -> None:
        result = normalize_message(
            "赚点b钱那么难？来几个勤快的兄弟跟我好好干，"
            "一个小时三万多轻轻松松，一星期包提小米su7"
        )
        signals = detect_suspicion(result)
        self.assertTrue(signals.is_suspicious)
        self.assertIn("异常高时薪承诺", signals.reasons)
        self.assertIn("招工话术或收益引流", signals.reasons)
        self.assertIn("以高价值奖品作收益承诺", signals.reasons)

    def test_vps_subscription_trade_is_an_allowed_context(self) -> None:
        result = normalize_message("各位大佬收个闲置的带订阅的账号（LA PRO AS3 基础配置）。")
        signals = detect_suspicion(result)
        self.assertFalse(signals.is_suspicious)
        self.assertIn(ALLOWED_VPS_TRADE_REASON, signals.reasons)

    def test_vps_sale_with_contact_is_an_allowed_context(self) -> None:
        result = normalize_message("出售香港 VPS，月付 30，联系 @vpsseller")
        signals = detect_suspicion(result)
        self.assertIn(ALLOWED_VPS_TRADE_REASON, signals.reasons)


if __name__ == "__main__":
    unittest.main()
