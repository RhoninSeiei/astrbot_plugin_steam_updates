import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
import httpcore
from support import _load_module


class HttpClientRecoveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.m = _load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch.object(self.m.StarTools, 'get_data_dir', return_value=Path(self.tmp.name)):
            self.p = self.m.Main(None, {'proxy_mode':'custom','proxy_url':'http://proxy.test:7890'})
        self.response = httpx.Response(200, json={'ok':True})
        self.old = SimpleNamespace(request=AsyncMock(side_effect=httpx.PoolTimeout('exhausted')), get=AsyncMock(), aclose=AsyncMock())
        self.fresh = SimpleNamespace(request=AsyncMock(return_value=self.response), aclose=AsyncMock())
        self.real_client_type = httpx.AsyncClient
        self.p._client = self.old
        self.p._client_signature = self.p._http_client_signature()
        self.p._build_http_client = Mock(return_value=self.fresh)
        self.direct = Mock()
        self.direct.return_value.__aenter__ = AsyncMock(return_value=SimpleNamespace(request=AsyncMock(return_value=self.response)))
        self.direct.return_value.__aexit__ = AsyncMock(return_value=False)
        patched = patch.object(self.m.httpx, 'AsyncClient', self.direct)
        patched.start()
        self.addCleanup(patched.stop)

    async def invoke(self):
        return await self.p._request_with_network_fallback('GET', 'https://example.test/news', timeout=10)

    async def test_pool_timeout_recovers_via_fresh_proxy_pool_before_direct(self):
        self.assertIs(await self.invoke(), self.response)
        self.assertIs(self.p._client, self.fresh)
        self.p._build_http_client.assert_called_once()
        self.old.aclose.assert_awaited_once()
        self.direct.assert_not_called()

    async def test_concurrent_failures_replace_the_same_pool_once(self):
        entered = 0
        gate = asyncio.Event()

        async def fail(*args, **kwargs):
            nonlocal entered
            entered += 1
            if entered == 2:
                gate.set()
            await gate.wait()
            raise httpx.PoolTimeout('exhausted')

        self.old.request.side_effect = fail
        results = await asyncio.gather(self.invoke(), self.invoke())
        self.assertEqual(results, [self.response, self.response])
        self.p._build_http_client.assert_called_once()
        self.old.aclose.assert_awaited_once()
        self.direct.assert_not_called()

    async def test_replacement_waits_for_inflight_image_request_before_close(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def image(*args, **kwargs):
            entered.set()
            await release.wait()
            return self.response

        self.old.get.side_effect = image
        peer = asyncio.create_task(self.p._call_http_client(self.old, 'get', 'https://example.test/image'))
        await entered.wait()
        self.assertIs(await self.invoke(), self.response)
        self.old.aclose.assert_not_awaited()
        release.set()
        self.assertIs(await peer, self.response)
        self.old.aclose.assert_awaited_once()

    async def test_cancelled_old_request_releases_retired_pool(self):
        entered = asyncio.Event()

        async def image(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.old.get.side_effect = image
        peer = asyncio.create_task(self.p._call_http_client(self.old, 'get', 'https://example.test/image'))
        await entered.wait()
        await self.invoke()
        peer.cancel()
        await asyncio.gather(peer, return_exceptions=True)
        self.old.aclose.assert_awaited_once()
        self.assertFalse(self.p._retired_http_clients)

    async def test_second_pool_failure_uses_existing_fallback_without_reset_loop(self):
        self.fresh.request.side_effect = httpx.PoolTimeout('still busy')
        self.assertIs(await self.invoke(), self.response)
        self.p._build_http_client.assert_called_once()
        self.assertEqual(self.fresh.request.await_count, 1)
        self.direct.assert_called_once()

    async def test_config_change_preserves_inflight_request_until_completion(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def image(*args, **kwargs):
            entered.set()
            await release.wait()
            return self.response

        self.old.get.side_effect = image
        peer = asyncio.create_task(self.p._call_http_client(self.old, 'get', 'https://example.test/image'))
        await entered.wait()
        self.p.config['proxy_url'] = 'http://other-proxy.test:7890'
        await self.p._ensure_http_client()
        self.old.aclose.assert_not_awaited()
        release.set()
        await peer
        self.old.aclose.assert_awaited_once()

    async def test_stopped_plugin_does_not_recreate_client(self):
        self.p._stop_event.set()
        with self.assertRaises(RuntimeError):
            await self.invoke()
        self.p._build_http_client.assert_not_called()

    async def test_terminate_closes_current_and_retired_clients(self):
        self.p._client = self.fresh
        self.p._retired_http_clients[id(self.old)] = self.old
        await self.p.terminate()
        self.old.aclose.assert_awaited_once()
        self.fresh.aclose.assert_awaited_once()
        self.assertFalse(self.p._retired_http_clients)

    async def test_real_proxy_tls_failures_exhaust_pool_and_recover(self):
        # httpcore 1.0.9 leaves failed CONNECT/TLS connections ACTIVE. Reproduce
        # with a memory backend, then exercise the plugin's actual recovery.
        class Stream:
            sent = False

            async def read(self, max_bytes, timeout=None):
                if not self.sent:
                    self.sent = True
                    return b'HTTP/1.1 200 Connection established\r\n\r\n'
                return b''

            async def write(self, buffer, timeout=None):
                pass

            async def aclose(self):
                pass

            async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
                raise httpcore.ConnectError('controlled TLS handshake failure')

            def get_extra_info(self, info):
                return False if info == 'is_readable' else None

        class Backend:
            async def connect_tcp(self, *args, **kwargs):
                return Stream()

            async def sleep(self, seconds):
                await asyncio.sleep(seconds)

        transport = httpx.AsyncHTTPTransport()
        await transport._pool.aclose()
        pool = httpcore.AsyncHTTPProxy(proxy_url='http://proxy.test:7890', max_connections=2, network_backend=Backend())
        transport._pool = pool
        old = self.real_client_type(transport=transport, trust_env=False)
        fresh = self.real_client_type(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'recovered':True})), trust_env=False)
        try:
            for _ in range(2):
                with self.assertRaises(httpx.ConnectError):
                    await old.get('https://api.example.test/news', timeout=0.02)
            self.assertEqual(len(pool.connections), 2)
            self.assertEqual(len(pool._requests), 0)
            self.p._client = old
            self.p._build_http_client.return_value = fresh
            result = await self.p._request_with_network_fallback('GET', 'https://api.example.test/news', timeout=0.02)
            self.assertEqual(result.json(), {'recovered':True})
            self.assertTrue(old.is_closed)
            self.assertEqual(pool.connections, [])
            self.direct.assert_not_called()
        finally:
            await old.aclose()
            await fresh.aclose()
