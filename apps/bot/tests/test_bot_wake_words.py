from __future__ import annotations

from types import SimpleNamespace
import unittest

from misakabot.group_reply import _is_explicit_bot_reply_trigger, _reply_prompt_text


class BotWakeWordTests(unittest.TestCase):
    def test_cat_wake_word_at_message_start_triggers_and_is_removed(self) -> None:
        message = SimpleNamespace(text="猫猫，DMIT 流量超额怎么办？", caption=None, reply_to_message=None)

        self.assertTrue(_is_explicit_bot_reply_trigger(message, bot_user_id=1, bot_username="MisakaBot"))
        self.assertEqual(_reply_prompt_text(message.text, "MisakaBot"), "DMIT 流量超额怎么办？")

    def test_wake_word_inside_normal_sentence_does_not_trigger(self) -> None:
        message = SimpleNamespace(text="我家的猫猫今天很可爱", caption=None, reply_to_message=None)

        self.assertFalse(_is_explicit_bot_reply_trigger(message, bot_user_id=1, bot_username="MisakaBot"))
