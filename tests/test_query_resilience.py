import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

import httpx
import test_free_games


class QueryResilienceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mod = test_free_games._load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        with patch.object(self.mod.StarTools, "get_data_dir", return_value=self.root):
            self.plugin = self.mod.SteamUpdatePush(None, {})
        self.plugin._appid_map_path = lambda: self.root / "appid_map.json"
        self.plugin._client = object()

    async def asyncTearDown(self):
        self.plugin._client = None
        await self.plugin.terminate()

    def response(self, status=200, name="Counter-Strike"):
        return httpx.Response(status, json={"730": {"success": True, "data": {"name": name}}},
                              request=httpx.Request("GET", "https://example.test/appdetails"))

    async def test_transient_error_retries_and_saves_successful_name(self):
        request = AsyncMock(side_effect=[httpx.ConnectError("TLS"), self.response()])
        self.plugin._request_with_network_fallback = request
        with patch.object(self.mod.asyncio, "sleep", new=AsyncMock()):
            result = await self.plugin._get_app_name_by_lang("730", "english")
        self.assertEqual(result, "Counter-Strike")
        self.assertEqual(request.await_count, 2)
        self.assertEqual(json.loads(self.plugin._name_cache_path.read_text())["730:english"], result)

    async def test_exhausted_retry_cache_expires_without_polluting_manual_map(self):
        self.plugin.config["appdetails_retry_attempts"] = 2
        request = AsyncMock(side_effect=httpx.ConnectError("TLS"))
        self.plugin._request_with_network_fallback = request
        with patch.object(self.mod.asyncio, "sleep", new=AsyncMock()):
            with patch.object(self.mod.time, "time", return_value=1000):
                self.assertEqual(await self.plugin._get_app_name_by_lang("730", "english"), "AppID 730")
                self.assertEqual(request.await_count, 2)
                self.assertEqual(await self.plugin._get_app_name_by_lang("730", "english"), "AppID 730")
            self.assertEqual(request.await_count, 2)
            self.assertFalse(self.plugin._appid_map_path().exists())
            request.side_effect = None
            request.return_value = self.response()
            with patch.object(self.mod.time, "time", return_value=1601):
                self.assertEqual(await self.plugin._get_app_name_by_lang("730", "english"), "Counter-Strike")

    async def test_manual_mapping_and_legacy_string_cache_are_respected(self):
        self.plugin._appid_map_path().write_text(json.dumps({"730": {"english": "Manual"}}))
        self.plugin._name_cache_path.write_text(json.dumps({"730:english": "Cached", "440:english": "Team Fortress"}))
        request = AsyncMock(side_effect=AssertionError("unexpected network"))
        self.plugin._request_with_network_fallback = request
        self.assertEqual(await self.plugin._get_app_name_by_lang("730", "english"), "Manual")
        self.assertEqual(await self.plugin._get_app_name_by_lang("440", "english"), "Team Fortress")

    async def test_loaded_negative_cache_expires_and_old_string_remains_readable(self):
        self.plugin._name_cache_path.write_text(json.dumps({
            "730:english": {"value": "AppID 730", "status": "retry_exhausted", "retry_after": 1600},
            "440:english": "Team Fortress",
        }))
        request = AsyncMock(return_value=self.response())
        self.plugin._request_with_network_fallback = request
        with patch.object(self.mod.time, "time", return_value=1000):
            self.assertEqual(await self.plugin._get_app_name_by_lang("730", "english"), "AppID 730")
            self.assertEqual(await self.plugin._get_app_name_by_lang("440", "english"), "Team Fortress")
            self.assertEqual(request.await_count, 0)
        with patch.object(self.mod.time, "time", return_value=1601):
            self.assertEqual(await self.plugin._get_app_name_by_lang("730", "english"), "Counter-Strike")
        self.assertEqual(request.await_count, 1)

    async def test_http_retryable_and_permanent_errors(self):
        for status in (429, 503, 404):
            with self.subTest(status=status):
                self.plugin._name_cache = {}
                self.plugin._appid_name_map = {}
                request = AsyncMock(side_effect=[self.response(status), self.response()])
                self.plugin._request_with_network_fallback = request
                with patch.object(self.mod.asyncio, "sleep", new=AsyncMock()):
                    name = await self.plugin._get_app_name_by_lang("730", "english")
                self.assertEqual(name, "AppID 730" if status == 404 else "Counter-Strike")
                self.assertEqual(request.await_count, 1 if status == 404 else 2)

    async def test_success_with_empty_or_invalid_appid_never_queries(self):
        request = AsyncMock(side_effect=AssertionError("unexpected network"))
        self.plugin._request_with_network_fallback = request
        for appid in ("", "not-an-appid"):
            self.assertEqual(await self.plugin._get_app_name_by_lang(appid, "english"), "AppID " + appid)
        self.assertEqual(request.await_count, 0)

    async def test_shared_lookup_survives_caller_cancellation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def request(*args, **kwargs):
            entered.set()
            await release.wait()
            return self.response()
        fetch = AsyncMock(side_effect=request)
        self.plugin._request_with_network_fallback = fetch
        first = asyncio.create_task(self.plugin._get_app_name_by_lang("730", "english"))
        await entered.wait()
        second = asyncio.create_task(self.plugin._get_app_name_by_lang("730", "english"))
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        third = asyncio.create_task(self.plugin._get_app_name_by_lang("730", "english"))
        release.set()
        results = await asyncio.gather(second, third, return_exceptions=True)
        self.assertEqual(results, ["Counter-Strike", "Counter-Strike"])
        self.assertEqual(fetch.await_count, 1)

    async def test_terminate_cancels_shared_lookup_before_closing_client(self):
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def request(*args, **kwargs):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                stopped.set()
        closed = []
        class Client:
            async def aclose(inner):
                closed.append(stopped.is_set())
        self.plugin._client = Client()
        self.plugin._request_with_network_fallback = request
        caller = asyncio.create_task(self.plugin._get_app_name_by_lang("730", "english"))
        await entered.wait()
        await self.plugin.terminate()
        was_stopped = stopped.is_set()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        self.assertTrue(was_stopped)
        self.assertEqual(closed, [True])

    async def test_news_batches_share_limit_preserve_order_and_isolate_failure(self):
        self.plugin.config["game_query_concurrency"] = 2
        active, maximum = 0, 0
        async def fetch(appid, count, only_today=True):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.001 if appid == "2" else 0.005)
                if appid == "bad":
                    raise RuntimeError("failed")
                return [appid]
            finally:
                active -= 1
        self.plugin._fetch_news = fetch
        one, two = await asyncio.gather(
            self.plugin._fetch_news_for_appids(["1", "2", "bad"], 2, only_today=True),
            self.plugin._fetch_news_for_appids(["3", "4"], 2, only_today=False),
        )
        self.assertEqual(list(one), ["1", "2"])
        self.assertEqual(list(two), ["3", "4"])
        self.assertEqual(maximum, 2)
        self.assertEqual(self.plugin._normalize_appids(["1", " 2 ", "1", ""]), ["1", "2"])

    async def test_cancelled_news_batch_stops_all_children(self):
        entered = asyncio.Event()
        active = 0
        async def fetch(*args, **kwargs):
            nonlocal active
            active += 1
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1
        self.plugin._fetch_news = fetch
        task = asyncio.create_task(self.plugin._fetch_news_for_appids(["1", "2"], 2, only_today=True))
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(active, 0)
