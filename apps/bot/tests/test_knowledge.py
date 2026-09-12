from __future__ import annotations

import unittest
from unittest.mock import patch

from misakabot.knowledge import dmit_knowledge_for_query


class DmitKnowledgeTests(unittest.TestCase):
    def test_selects_relevant_document_and_recognizes_alias(self) -> None:
        documents = (
            {
                "title": "关于实例的常见问题 | DMIT Docs",
                "url": "https://docs.dmit.io/zh/guide/faq/instance",
                "content": "传输额度每月自动重置。超量后可能暂停或限速。",
            },
            {
                "title": "关于退款的常见问题 | DMIT Docs",
                "url": "https://docs.dmit.io/zh/guide/faq/refund",
                "content": "退款前应备份数据。",
            },
        )
        with patch("misakabot.knowledge.dmit_documents", return_value=documents):
            results = dmit_knowledge_for_query("大妈流量超量后怎么办")

        self.assertGreaterEqual(len(results), 1)
        self.assertIn("关于实例", results[0])
        self.assertIn("“大妈”是 DMIT", results[0])

    def test_returns_source_boundary_when_no_document_matches(self) -> None:
        with patch("misakabot.knowledge.dmit_documents", return_value=()):
            results = dmit_knowledge_for_query("未知问题")
        self.assertIn("官方文档", results[0])
