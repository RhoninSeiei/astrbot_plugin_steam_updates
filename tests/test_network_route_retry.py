import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from support import _load_module


class NetworkRouteRetryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.m = _load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch.object(self.m.StarTools, 'get_data_dir', return_value=Path(self.tmp.name)):
            self.p = self.m.Main(None, {'proxy_mode':'custom','proxy_url':'http://proxy.test:7890'})
        self.p._ensure_http_client = AsyncMock()
        self.request = AsyncMock()
        self.p._client = SimpleNamespace(request=self.request)
        self.response = httpx.Response(200, json={'ok':True})
        self.fallback_request = AsyncMock(return_value=self.response)
        self.fallback = Mock()
        self.fallback.return_value.__aenter__ = AsyncMock(return_value=SimpleNamespace(request=self.fallback_request))
        self.fallback.return_value.__aexit__ = AsyncMock(return_value=False)
        self.client_patch = patch.object(self.m.httpx, 'AsyncClient', self.fallback)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.sleep_patch = patch.object(self.m.asyncio, 'sleep', AsyncMock())
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    async def invoke(self, method='GET'):
        return await self.p._request_with_network_fallback(method, 'https://example.test/news', params={'appid':'3092530'}, timeout=10)

    async def test_success_does_not_retry(self):
        self.request.return_value = self.response
        self.assertIs(await self.invoke(), self.response)
        self.assertEqual(self.request.await_count, 1)
        self.fallback.assert_not_called()

    async def test_proxy_read_timeout_retries_same_request_before_direct(self):
        self.request.side_effect = [httpx.ReadTimeout('slow'), self.response]
        self.assertIs(await self.invoke(), self.response)
        self.assertEqual(self.request.await_count, 2)
        self.assertEqual(self.request.await_args_list[0], self.request.await_args_list[1])
        self.fallback.assert_not_called()

    async def test_exhausted_proxy_retry_uses_direct_once(self):
        self.request.side_effect = httpx.ReadTimeout('slow')
        self.assertIs(await self.invoke(), self.response)
        self.assertEqual(self.request.await_count, 2)
        self.assertEqual(self.fallback_request.await_count, 1)
        self.assertFalse(self.fallback.call_args.kwargs['trust_env'])
        self.assertNotIn('proxy', self.fallback.call_args.kwargs)

    async def test_connection_failure_falls_back_without_proxy_retry(self):
        self.request.side_effect = httpx.ConnectError('unavailable')
        self.assertIs(await self.invoke(), self.response)
        self.assertEqual(self.request.await_count, 1)
        self.assertEqual(self.fallback_request.await_count, 1)

    async def test_post_does_not_gain_an_extra_retry(self):
        self.request.side_effect = httpx.ReadTimeout('slow')
        self.assertIs(await self.invoke('POST'), self.response)
        self.assertEqual(self.request.await_count, 1)
        self.assertEqual(self.fallback_request.await_count, 1)

    async def test_cancellation_during_retry_is_not_swallowed(self):
        self.request.side_effect = [httpx.ReadTimeout('slow'), asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await self.invoke()
        self.assertEqual(self.request.await_count, 2)
        self.fallback.assert_not_called()

    async def test_non_network_retry_error_is_not_hidden_by_direct_fallback(self):
        self.request.side_effect = [httpx.ReadTimeout('slow'), ValueError('bad request')]
        with self.assertRaises(ValueError):
            await self.invoke()
        self.fallback.assert_not_called()

    async def test_direct_mode_does_not_gain_proxy_retry(self):
        self.p.config = {'proxy_mode':'off'}
        self.request.side_effect = httpx.ReadTimeout('slow')
        with self.assertRaises(httpx.ReadTimeout):
            await self.invoke()
        self.assertEqual(self.request.await_count, 1)
        self.fallback.assert_not_called()
