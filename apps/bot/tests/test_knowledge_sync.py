from __future__ import annotations

import unittest

from tools.sync_dmit_knowledge import DocumentationParser


class DocumentationParserTests(unittest.TestCase):
    def test_void_tags_do_not_leak_footer_or_scripts_into_content(self) -> None:
        parser = DocumentationParser()
        parser.feed(
            '<main><p>正文<br>下一行<img src="test.png"><br/>结尾</p></main>'
            '<footer>页脚内容</footer><script>window.noise = true;</script>'
        )
        self.assertIn("正文", parser.content)
        self.assertIn("结尾", parser.content)
        self.assertNotIn("页脚", parser.content)
        self.assertNotIn("window", parser.content)

    def test_excludes_executable_and_style_content_inside_main(self) -> None:
        parser = DocumentationParser()
        parser.feed(
            "<title>DMIT Guide</title><main><h1>SSH 指南</h1>"
            "<script>window.__SITE_DATA__ = 'noise';</script>"
            "<style>.hidden { display: none }</style>"
            "<p>使用密钥登录。</p></main>"
        )

        self.assertEqual(parser.title, "DMIT Guide")
        self.assertIn("SSH 指南", parser.content)
        self.assertIn("使用密钥登录。", parser.content)
        self.assertNotIn("window", parser.content)
        self.assertNotIn("display", parser.content)


if __name__ == "__main__":
    unittest.main()
