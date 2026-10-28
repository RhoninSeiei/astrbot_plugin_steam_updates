import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from support import _load_module


class ProcessingBudgetsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.m = _load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch.object(self.m.StarTools, 'get_data_dir', return_value=Path(self.tmp.name)):
            self.p = self.m.Main(None, {'free_games_enable':True})
        self.p._client = object()

    def test_llm_defaults_and_user_adjustable_limits(self):
        self.assertEqual(self.p._llm_timeout_sec(), 90)
        self.assertEqual(self.p._llm_round_timeout_sec(), 240)
        self.p.config.update(llm_timeout_sec=120, llm_round_timeout_sec=500)
        self.assertEqual(self.p._llm_timeout_sec(), 120)
        self.assertEqual(self.p._llm_round_timeout_sec(), 500)
        self.p.config.update(llm_timeout_sec=999, llm_round_timeout_sec=999)
        self.assertEqual(self.p._llm_timeout_sec(), 180)
        self.assertEqual(self.p._llm_round_timeout_sec(), 600)

    async def test_free_api_uses_larger_timeout_and_connection_retry(self):
        response = httpx.Response(200, json={'status':0,'status_message':'No active giveaways available'}, request=httpx.Request('GET','https://example.test'))
        request = AsyncMock(return_value=response)
        self.p._request_with_network_fallback = request
        self.assertEqual(await self.p._fetch_free_game_items(), [])
        kwargs = request.await_args.kwargs
        self.assertTrue(kwargs['retry_connect'])
        self.assertEqual(kwargs['timeout'].read, 20)
        self.assertEqual(kwargs['timeout'].connect, 15)
        self.assertEqual(kwargs['timeout'].pool, 5)

    async def test_free_api_total_budget_cancels_slow_request(self):
        stopped = asyncio.Event()

        async def slow(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        self.p._request_with_network_fallback = slow
        self.p._free_games_request_budget_sec = lambda: 0.01
        try:
            result = await asyncio.wait_for(self.p._fetch_free_game_items(), timeout=0.15)
        except asyncio.TimeoutError:
            self.fail('free API total request budget was not applied')
        self.assertIsNone(result)
        self.assertTrue(stopped.is_set())

    async def test_free_api_caller_cancellation_propagates(self):
        entered, stopped = asyncio.Event(), asyncio.Event()

        async def slow(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        self.p._request_with_network_fallback = slow
        task = asyncio.create_task(self.p._fetch_free_game_items())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())

    async def test_llm_applies_expanded_timeout_and_returns_completion(self):
        self.p.context = SimpleNamespace(llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text='整理后的正文')))
        real_wait_for = asyncio.wait_for
        seen = []

        async def observe(coro, timeout):
            seen.append(timeout)
            return await real_wait_for(coro, timeout=timeout)

        with patch.object(self.m.asyncio, 'wait_for', side_effect=observe):
            self.assertEqual(await self.p._request_llm('provider','prompt',256), '整理后的正文')
        self.assertEqual(seen, [90])
        self.p.context.llm_generate.assert_awaited_once()

    async def test_llm_timeout_still_cancels_provider_and_falls_back(self):
        stopped = asyncio.Event()

        async def slow(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        self.p.context = SimpleNamespace(llm_generate=slow)
        self.p._llm_timeout_sec = lambda: 0.01
        self.assertIsNone(await self.p._request_llm('provider','prompt',256))
        self.assertTrue(stopped.is_set())

    def test_free_api_grouped_settings_and_limits(self):
        self.p.config = {'free_games':{'free_games_timeout_sec':45,'free_games_request_budget_sec':120}}
        self.assertEqual(self.p._free_games_timeout_sec(),45)
        self.assertEqual(self.p._free_games_request_budget_sec(),120)
        self.p.config = {'free_games_timeout_sec':-1,'free_games_request_budget_sec':999}
        self.assertEqual(self.p._free_games_timeout_sec(),5)
        self.assertEqual(self.p._free_games_request_budget_sec(),180)
        self.p.config = {'free_games_timeout_sec':None,'free_games_request_budget_sec':'bad'}
        self.assertEqual(self.p._free_games_timeout_sec(),20)
        self.assertEqual(self.p._free_games_request_budget_sec(),90)
