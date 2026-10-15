import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from time import monotonic
from unittest.mock import AsyncMock, patch

import httpx
from yadisk.sessions.async_httpx_session import AsyncHTTPXSession

import downloader


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def token(self, value):
        return downloader.ServiceToken(value, self.now + 3600, self.now + 3300)


class TokenResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_expiry_uses_monotonic_request_start_and_server_ttl(self):
        for payload, ttl, margin in [
            ({'expires_in': 3600}, 3600, 300), ({}, 3600, 300),
            ({'expires_in': '1800'}, 1800, 180), ({'expires_in': 60}, 60, 6),
        ]:
            with self.subTest(payload=payload):
                clock = Clock()
                def handler(request):
                    clock.now += 2
                    return httpx.Response(200, json={'access_token': 'private-token', **payload})
                client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                with (patch.object(downloader, 'monotonic', clock),
                      patch.object(downloader, 'time', return_value=-1000000),
                      patch.object(downloader.httpx, 'AsyncClient', return_value=client)):
                    token = await downloader.get_service_token('id', 'secret', 'reader@example.invalid')
                self.assertEqual(token.expires_at, 1000 + ttl)
                self.assertEqual(token.refresh_at, 1000 + ttl - margin)
                self.assertEqual(token.expires_at_wall, -1000000 + ttl)
                self.assertEqual(token.refresh_at_wall, -1000000 + ttl - margin)
                self.assertNotIn('private-token', repr(token))
                self.assertTrue(client.is_closed)

    async def test_invalid_expiry_is_rejected_without_retries(self):
        for lifetime in [None, True, False, 0, -1, 'invalid', 'NaN', 'inf', [], {}]:
            with self.subTest(lifetime=lifetime):
                calls = 0
                def handler(request):
                    nonlocal calls
                    calls += 1
                    return httpx.Response(200, json={'access_token': 'private-token', 'expires_in': lifetime})
                client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                with (patch.object(downloader.httpx, 'AsyncClient', return_value=client),
                      self.assertRaises(ValueError)):
                    await downloader.get_service_token('id', 'secret', 'reader@example.invalid')
                self.assertEqual(calls, 1)
                self.assertTrue(client.is_closed)

    async def test_token_that_expired_during_exchange_is_rejected(self):
        clock = Clock()
        def handler(request):
            clock.now += 60
            return httpx.Response(200, json={'access_token': 'private-token', 'expires_in': 60})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with (patch.object(downloader, 'monotonic', clock),
              patch.object(downloader.httpx, 'AsyncClient', return_value=client),
              self.assertRaises(ValueError)):
            await downloader.get_service_token('id', 'secret', 'reader@example.invalid')


class TokenManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = Clock()
        self.enterContext(patch.object(downloader, 'monotonic', self.clock))
        self.enterContext(patch.object(downloader, 'time', self.clock))
        self.logs = self.enterContext(self.assertLogs('Downloader'))
        self.manager = downloader.ServiceTokenManager('id', 'secret', 'reader@example.invalid')

    async def test_refresh_at_55_minutes_is_single_for_concurrent_requests(self):
        refreshing, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def issue(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                refreshing.set()
                await release.wait()
            return self.clock.token(f'private-{calls}')
        with patch.object(downloader, 'get_service_token', side_effect=issue) as fetch:
            self.assertEqual(await self.manager.get_token(), 'private-1')
            self.clock.now += 3299
            self.assertEqual(await self.manager.get_token(), 'private-1')
            self.clock.now += 1
            tasks = [asyncio.create_task(self.manager.get_token()) for _ in range(20)]
            try:
                await asyncio.wait_for(refreshing.wait(), 1)
                self.assertEqual(fetch.await_count, 2)
                release.set()
                self.assertEqual(await asyncio.gather(*tasks), ['private-2'] * 20)
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
            self.assertEqual(fetch.await_count, 2)
        self.assertNotIn('private-', '\n'.join(self.logs.output))
        self.assertIn('обновлён', '\n'.join(self.logs.output))

    async def test_failed_refresh_has_shared_cooldown_and_never_returns_expired_token(self):
        self.manager = downloader.ServiceTokenManager('id', 'private-secret', 'reader@example.invalid')
        rejection = httpx.HTTPStatusError('private-secret', request=httpx.Request('POST', 'https://example.invalid'),
                                          response=httpx.Response(401))
        with patch.object(downloader, 'get_service_token', side_effect=[
            self.clock.token('private-old'), TimeoutError('private-secret'), rejection,
        ]) as fetch:
            await self.manager.get_token()
            self.clock.now += 3300
            self.assertEqual(await self.manager.get_token(), 'private-old')
            self.clock.now += 1
            self.assertEqual(await asyncio.gather(*(self.manager.get_token() for _ in range(10))), ['private-old'] * 10)
            self.assertEqual(fetch.await_count, 2)
            self.clock.now += 299
            errors = await asyncio.gather(*(self.manager.get_token() for _ in range(10)), return_exceptions=True)
            self.assertTrue(all(isinstance(error, downloader.TokenRefreshError) for error in errors))
            self.assertEqual(fetch.await_count, 3)
        self.assertNotIn('private-', '\n'.join(self.logs.output))

    async def test_refresh_recovers_after_cooldown(self):
        with patch.object(downloader, 'get_service_token', return_value=self.clock.token('private-old')):
            await self.manager.get_token()
        self.clock.now += 3300
        with patch.object(downloader, 'get_service_token', side_effect=TimeoutError):
            self.assertEqual(await self.manager.get_token(), 'private-old')
        self.clock.now += 30
        with patch.object(downloader, 'get_service_token', side_effect=lambda *args: self.clock.token('private-new')) as fetch:
            self.assertEqual(await self.manager.get_token(), 'private-new')
            fetch.assert_awaited_once()

    async def test_failed_refresh_does_not_multiply_http_retries_per_waiting_download(self):
        with patch.object(downloader, 'get_service_token', return_value=self.clock.token('private-old')):
            await self.manager.get_token()
        self.clock.now += 3600
        calls = 0
        def handler(request):
            nonlocal calls
            calls += 1
            if calls <= 4:
                return httpx.Response(503)
            return httpx.Response(200, json={'access_token': 'private-new', 'expires_in': 3600})
        clients = [httpx.AsyncClient(transport=httpx.MockTransport(handler)) for _ in range(2)]
        with (patch.dict(os.environ, {'MAX_RETRIES': '3'}),
              patch.object(downloader.httpx, 'AsyncClient', side_effect=clients),
              patch.object(downloader.asyncio, 'sleep', new_callable=AsyncMock) as sleep,
              patch.object(downloader, 'uniform', return_value=0)):
            errors = await asyncio.gather(*(
                downloader.with_retries(self.manager.get_token, 'test request') for _ in range(10)
            ), return_exceptions=True)
        self.assertEqual(calls, 5)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [2, 4, 8, 30])
        self.assertEqual(sum(isinstance(error, downloader.TokenRefreshError) for error in errors), 1)
        self.assertEqual(errors.count('private-new'), 9)
        self.assertTrue(all(client.is_closed for client in clients))

    async def test_request_hook_does_not_add_auth_to_personal_download_or_cross_host_redirect(self):
        seen = []
        def handler(request):
            seen.append((request.url.host, request.headers.get('Authorization')))
            if request.url.path == '/redirect':
                return httpx.Response(302, headers={'Location': 'https://other.example.invalid/file'})
            return httpx.Response(200)
        with patch.object(downloader, 'get_service_token', side_effect=lambda *args: self.clock.token('private-current')) as fetch:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                         event_hooks={'request': [self.manager.authorize]}, follow_redirects=True) as client:
                await client.get('https://api.example.invalid/api', headers={'Authorization': 'OAuth stale'})
                await client.get('https://download.example.invalid/file')
                await client.get('https://api.example.invalid/redirect', headers={'Authorization': 'OAuth stale'})
            self.assertEqual(fetch.await_count, 1)
        self.assertEqual(seen, [('api.example.invalid', 'OAuth private-current'),
                                ('download.example.invalid', None), ('api.example.invalid', 'OAuth private-current'),
                                ('other.example.invalid', None)])

    async def test_no_expired_authorization_reaches_transport(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200)
        with patch.object(downloader, 'get_service_token', return_value=self.clock.token('private-old')):
            await self.manager.get_token()
        self.clock.now += 3600
        with patch.object(downloader, 'get_service_token', side_effect=ValueError('private-secret')):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                         event_hooks={'request': [self.manager.authorize]}) as client:
                with self.assertRaises(downloader.TokenRefreshError):
                    await client.get('https://api.example.invalid/api', headers={'Authorization': 'OAuth stale'})
        self.assertEqual(requests, [])


class TokenLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_refresh_runs_during_open_download_and_stops_on_exit(self):
        opened, refreshed = asyncio.Event(), asyncio.Event()
        calls = 0
        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                opened.set()
                yield b'a' * 8192
                await asyncio.wait_for(refreshed.wait(), 2)
                yield b'complete'
            async def aclose(self):
                pass
        async def issue(*args):
            nonlocal calls
            calls += 1
            now = monotonic()
            if calls == 1:
                return downloader.ServiceToken('private-old', now + 3600, now + 0.1)
            self.assertTrue(opened.is_set())
            refreshed.set()
            return downloader.ServiceToken('private-new', now + 3600, now + 3300)
        manager = downloader.ServiceTokenManager('id', 'secret', 'reader@example.invalid')
        with patch.object(downloader, 'get_service_token', side_effect=issue), self.assertLogs('Downloader'):
            async with manager:
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowStream()))) as client:
                    async with client.stream('GET', 'https://download.example.invalid/file') as response:
                        self.assertEqual(await response.aread(), b'a' * 8192 + b'complete')
                self.assertEqual(await manager.get_token(), 'private-new')
            self.assertEqual(calls, 2)
            self.assertIsNone(manager._task)
        self.assertFalse(any(task.get_name() == 'downloader-token-refresh' for task in asyncio.all_tasks()))

    async def test_cancel_during_background_exchange_closes_oauth_session(self):
        started, closed = asyncio.Event(), asyncio.Event()
        now = monotonic()
        async def handler(request):
            started.set()
            await asyncio.Event().wait()
        class Transport(httpx.MockTransport):
            async def aclose(self):
                closed.set()
        client = httpx.AsyncClient(transport=Transport(handler))
        manager = downloader.ServiceTokenManager('id', 'secret', 'reader@example.invalid')
        with (patch.object(downloader, 'get_service_token', return_value=downloader.ServiceToken('private-old', now + 3600, now + 0.03)),
              self.assertLogs('Downloader')):
            await manager.get_token()
        with patch.object(downloader.httpx, 'AsyncClient', return_value=client), self.assertLogs('Downloader'):
            with self.assertRaises(asyncio.CancelledError):
                async with manager:
                    await asyncio.wait_for(started.wait(), 1)
                    raise asyncio.CancelledError
        self.assertTrue(client.is_closed)
        self.assertTrue(closed.is_set())
        self.assertIsNone(manager._task)

    async def test_personal_and_shared_downloads_refresh_across_hours_without_restarting(self):
        for shared in [False, True]:
            with self.subTest(shared=shared), tempfile.TemporaryDirectory() as directory:
                clock = Clock()
                issued, sessions = [], []
                async def issue(*args):
                    token = clock.token(f'private-{len(issued) + 1}')
                    issued.append(token)
                    return token
                def handler(request):
                    if request.url.host == 'download.example.invalid':
                        expected = f'OAuth {issued[-1].access_token}' if shared else None
                        self.assertEqual(request.headers.get('Authorization'), expected)
                        clock.now += 3301
                        return httpx.Response(200, content=b'complete')
                    self.assertEqual(request.headers.get('Authorization'), f'OAuth {issued[-1].access_token}')
                    self.assertLess(clock.now, issued[-1].expires_at)
                    path = request.url.path
                    if '/operations/' in path:
                        return httpx.Response(404, json={'error': 'DiskOperationNotFoundError'})
                    if path.endswith('/disk') or path.endswith('/virtual-disks'):
                        return httpx.Response(200, json={'used_space': 16})
                    if path.endswith('/download'):
                        return httpx.Response(200, json={'href': 'https://download.example.invalid/file'})
                    offset = int(request.url.params.get('offset', 0))
                    prefix = 'vd:SharedHash:disk:' if shared else 'disk:'
                    clock.now += 3301
                    return httpx.Response(200, json={'type': 'dir', '_embedded': {
                        'total': 2, 'limit': 1, 'offset': offset,
                        'items': [{'type': 'file', 'path': f'{prefix}/{offset}.txt'}],
                    }})
                def session_factory(**kwargs):
                    session = AsyncHTTPXSession(transport=httpx.MockTransport(handler), **kwargs)
                    sessions.append(session)
                    return session
                with (
                    patch.object(downloader, 'monotonic', clock),
                    patch.object(downloader, 'MAX_STREAMS', 1),
                    patch.object(downloader, 'load_credentials', return_value=('id', 'secret')),
                    patch.object(downloader, 'get_service_token', side_effect=issue),
                    patch.object(downloader, 'AsyncHTTPXSession', side_effect=session_factory),
                    self.assertLogs('Downloader') as logs,
                ):
                    self.assertTrue(await downloader.main('reader@example.invalid', Path(directory),
                                                          vdhash='SharedHash' if shared else None))
                self.assertGreaterEqual(len(issued), 4)
                self.assertGreater(clock.now - 1000, 3600)
                self.assertEqual({path.name: path.read_bytes() for path in Path(directory).iterdir()},
                                 {'0.txt': b'complete', '1.txt': b'complete'})
                self.assertTrue(all(session.httpx_session.is_closed for session in sessions))
                self.assertNotIn('private-', '\n'.join(logs.output))
                self.assertFalse(any(task.get_name() == 'downloader-token-refresh' for task in asyncio.all_tasks()))
