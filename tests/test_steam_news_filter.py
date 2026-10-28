import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from support import _load_module


class SteamNewsFilterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.m = _load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch.object(self.m.StarTools, "get_data_dir", return_value=Path(self.tmp.name)):
            self.p = self.m.Main(None, {})
        self.url = "https://steamstore-a.akamaihd.net/news/externalpost/steam_community_announcements/1847286114044216"

    def response(self, *, data=None, text=None):
        return httpx.Response(200, json=data, text=text, request=httpx.Request("GET", "https://example.test"))

    async def assert_news_source_retries_connection(self, source, response):
        self.p.config.update(proxy_mode='custom', proxy_url='http://proxy.test:7890')
        request = AsyncMock(side_effect=[httpx.ConnectTimeout('temporary connection failure'), response])
        self.p._client = SimpleNamespace(request=request)
        self.p._ensure_http_client = AsyncMock()
        direct = Mock()
        direct.return_value.__aenter__ = AsyncMock(return_value=SimpleNamespace(request=AsyncMock(return_value=response)))
        direct.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch.object(self.m.httpx,'AsyncClient',direct), patch.object(self.m.asyncio,'sleep',AsyncMock()):
            items = await getattr(self.p, source)('1049590',50)
        self.assertEqual([i.gid for i in items], ['1847286114044216'])
        self.assertEqual(request.await_count, 2)
        direct.assert_not_called()

    async def test_news_api_retries_connection_before_direct_fallback(self):
        response = self.response(data={'appnews':{'newsitems':[
            {'gid':'1847286114044216','url':self.url,'title':'Official','date':123,'contents':'Body','is_external_url':True}
        ]}})
        await self.assert_news_source_retries_connection('_fetch_news_api', response)

    async def test_news_feed_retries_connection_before_direct_fallback(self):
        response = self.response(text=f'<rss><channel><item><title>Official</title><link>{self.url}</link><description>Body</description></item></channel></rss>')
        await self.assert_news_source_retries_connection('_fetch_news_feed', response)

    async def test_api_retains_official_announcement_marked_external(self):
        self.p._request_with_network_fallback = AsyncMock(return_value=self.response(data={
            "appnews": {"newsitems": [
                {"gid": "1847286114044216", "url": self.url, "is_external_url": True,
                 "feedname": "steam_community_announcements", "title": "Official", "contents": "Body", "date": 123},
                {"gid": "media", "url": "https://steamstore-a.akamaihd.net/news/externalpost/PCGamer/123", "is_external_url": True},
            ]}
        }))
        items = await self.p._fetch_news_api("236390", 50)
        self.assertEqual([(i.gid, i.title, i.contents) for i in items], [("1847286114044216", "Official", "Body")])

    def test_filter_rejects_media_and_deceptive_official_links(self):
        for url in (
            "https://evil.test/news/externalpost/steam_community_announcements/123",
            "https://steamstore-a.akamaihd.net.evil.test/news/externalpost/steam_community_announcements/123",
            "https://store.steampowered.com/news/externalpost/PCGamer/123",
            "https://store.steampowered.com/news/externalpost/steam_community_announcements/123/extra",
        ):
            with self.subTest(url=url):
                self.assertTrue(self.p._is_external_news(url, True))
        self.assertFalse(self.p._is_external_news("https://store.steampowered.com/news/app/236390/view/123"))

    async def test_feed_retains_official_bridge_and_uses_same_gid_as_api(self):
        xml = f"""<rss><channel><item><title>Official</title><link>{self.url}</link>
        <description>Body</description><pubDate>Mon, 26 Oct 2026 08:00:00 GMT</pubDate></item>
        <item><title>Media</title><link>https://store.steampowered.com/news/externalpost/PCGamer/123</link></item>
        </channel></rss>"""
        self.p._request_with_network_fallback = AsyncMock(return_value=self.response(text=xml))
        items = await self.p._fetch_news_feed("236390", 50)
        self.assertEqual([i.gid for i in items], ["1847286114044216"])

    async def test_api_timeout_recovers_through_feed_without_failure_placeholder(self):
        self.p._client = object()
        self.p._fetch_news_api = AsyncMock(side_effect=httpx.ReadTimeout("slow response"))
        official = self.m.NewsItem("1847286114044216", "Official", self.url, "Body", 123, appid="236390")
        self.p._fetch_news_feed = AsyncMock(return_value=[official])
        result, failed = await self.p._fetch_news_for_appids(["236390"], 50, only_today=False)
        self.assertEqual(result, {"236390": [official]})
        self.assertEqual(failed, set())

    async def test_empty_api_and_feed_are_successful_empty_results(self):
        self.p._client = object()
        self.p._fetch_news_api = AsyncMock(return_value=[])
        self.p._fetch_news_feed = AsyncMock(return_value=[])
        result, failed = await self.p._fetch_news_for_appids(["236390"], 50, only_today=False)
        self.assertEqual(result, {"236390": []})
        self.assertEqual(failed, set())

    async def test_both_sources_failing_remains_a_failure(self):
        self.p._client = object()
        self.p._fetch_news_api = AsyncMock(side_effect=httpx.ReadTimeout("API timeout"))
        self.p._fetch_news_feed = AsyncMock(side_effect=httpx.ReadTimeout("feed timeout"))
        result, failed = await self.p._fetch_news_for_appids(["236390"], 50, only_today=False)
        self.assertEqual(result, {})
        self.assertEqual(failed, {"236390"})
