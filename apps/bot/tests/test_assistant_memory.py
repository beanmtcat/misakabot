from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from misakabot.repository import AuditRepository


class AssistantMemoryRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repository = AuditRepository(Path(self.tempdir.name) / "assistant.sqlite3")
        self.repository.initialize()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_context_is_scoped_to_group_and_member(self) -> None:
        for index in range(14):
            self.repository.record_assistant_message(-100, 10, "user", f"u{index}", f"t{index}")
        self.repository.record_assistant_message(-100, 11, "user", "other-user", "t")
        self.repository.record_assistant_message(-200, 10, "user", "other-group", "t")

        context = self.repository.assistant_context(-100, 10)

        self.assertEqual(len(context), 12)
        self.assertEqual(context[0], ("user", "u2"))
        self.assertEqual(context[-1], ("user", "u13"))

    def test_memory_is_opt_in_and_can_be_cleared(self) -> None:
        self.assertFalse(self.repository.assistant_memory_enabled(-100, 10))
        self.repository.set_assistant_memory_enabled(-100, 10, True, "now")
        self.repository.remember_assistant_fact(-100, 10, "偏好简洁中文回答", "now")

        self.assertEqual(self.repository.assistant_memories(-100, 10), ["偏好简洁中文回答"])
        self.assertEqual(self.repository.clear_assistant_memories(-100, 10), 1)
        self.assertEqual(self.repository.assistant_memories(-100, 10), [])

