"""Local title preservation tests adapted to the upstream per-announcement protocol."""
import hashlib
import json
import unittest
from support import _load_module


class NewsTitleTranslationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mod = _load_module()
        self.plugin = object.__new__(self.mod.SteamUpdatePush)
        self.plugin._cfg = lambda k, d=None: "{content}" if k == "llm_prompt" else d
        async def names(ids):
            return {"123": "Game"}
        self.plugin._resolve_app_names = names
        self.ident = hashlib.sha256(b"one").hexdigest()[:16]

    async def build(self, response, title="Summer Update"):
        async def call(*args):
            return response
        self.plugin._call_llm = call
        item = self.mod.NewsItem("one", title, "original-url", "original body", 123, "123")
        result = await self.plugin._build_sections_llm(["123"], {"123": [item]}, None)
        return result[0].updates[0]

    def payload(self, **changes):
        return json.dumps({"items": [{"id": self.ident, "title": "夏季更新", "content": "正文", **changes}]})

    async def test_json_title_and_body_translation_preserves_metadata(self):
        item = await self.build(self.payload())
        self.assertEqual((item.title, item.contents), ("夏季更新", "正文"))
        self.assertEqual((item.gid, item.url, item.date), ("one", "original-url", 123))
        self.assertEqual(item.llm_notice, "")

    async def test_original_han_title_and_brackets_are_preserved(self):
        for title in ("【新皮肤】预览", "普通标题", "𠀀更新", "\U0002ebf0更新", "\U000323b0更新"):
            with self.subTest(title=title):
                self.assertEqual((await self.build(self.payload(), title)).title, title)

    async def test_invalid_or_old_protocol_falls_back_without_losing_source(self):
        for response in ("【标题】标题\n【正文】内容", "not JSON", "", None,
                         self.payload(title=""), self.payload(content=""),
                         self.payload(id="unexpected"), '{"items": []}'):
            with self.subTest(response=response):
                item = await self.build(response)
                self.assertEqual(item.title, "Summer Update")
                self.assertEqual(item.contents, "original body")
                self.assertTrue(item.llm_notice)

    async def test_duplicate_ids_fall_back_independently(self):
        record = {"id": self.ident, "title": "标题", "content": "正文"}
        item = await self.build(json.dumps({"items": [record, record]}))
        self.assertEqual(item.contents, "original body")
        self.assertTrue(item.llm_notice)

    async def test_body_keeps_internal_newlines(self):
        item = await self.build(self.payload(content="第一段\n\n  - 项目"))
        self.assertEqual(item.contents, "第一段\n\n  - 项目")

    async def test_timeout_fallback_keeps_source(self):
        async def call(*args):
            raise TimeoutError()
        self.plugin._call_llm = call
        source = self.mod.NewsItem("one", "Title", "url", "Body", 123)
        result = await self.plugin._build_sections_llm(["123"], {"123": [source]}, None)
        self.assertEqual(result[0].updates[0].contents, "Body")
        self.assertTrue(result[0].updates[0].llm_notice)
