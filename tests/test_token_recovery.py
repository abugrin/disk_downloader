import asyncio
import io
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import downloader


class TokenRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = 1000.0
        self.wall_clock = 100000.0
        self.enterContext(patch.object(downloader, 'monotonic', lambda: self.clock))
        self.enterContext(patch.object(downloader, 'time', lambda: self.wall_clock))
        self.enterContext(patch.object(downloader.log_handler, 'stream', io.StringIO()))
        self.manager = downloader.ServiceTokenManager('id', 'secret', 'reader@example.invalid')

    def token(self, value):
        return downloader.ServiceToken(value, self.clock + 3600, self.clock + 3300,
                                       self.wall_clock + 3600, self.wall_clock + 3300)

    async def prepare_expiry_during_cooldown(self):
        with patch.object(downloader, 'get_service_token', return_value=self.token('old')):
            await self.manager.get_token()
        self.clock += 3590
        self.wall_clock += 3590
        with patch.object(downloader, 'get_service_token', side_effect=TimeoutError):
            self.assertEqual(await self.manager.get_token(), 'old')
        self.clock += 10
        self.wall_clock += 10

    async def test_expired_token_waits_for_one_shared_recovery_without_draining_queue(self):
        await self.prepare_expiry_during_cooldown()
        waiting, release = asyncio.Event(), asyncio.Event()
        waits = []

        async def pause(delay):
            waits.append(delay)
            waiting.set()
            await release.wait()
            self.clock += delay
            self.wall_clock += delay

        with (patch.object(downloader.asyncio, 'sleep', side_effect=pause),
              patch.object(downloader, 'get_service_token', side_effect=lambda *args: self.token('new')) as issue):
            tasks = [asyncio.create_task(self.manager.get_token()) for _ in range(100)]
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                self.assertTrue(all(not task.done() for task in tasks))
                issue.assert_not_awaited()
                release.set()
                self.assertEqual(await asyncio.gather(*tasks), ['new'] * 100)
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(waits, [20])
        issue.assert_awaited_once()

    async def test_shared_recovery_wait_is_promptly_cancellable(self):
        await self.prepare_expiry_during_cooldown()
        waiting = asyncio.Event()

        async def pause(delay):
            waiting.set()
            await asyncio.Event().wait()

        with (patch.object(downloader.asyncio, 'sleep', side_effect=pause),
              patch.object(downloader, 'get_service_token') as issue):
            task = asyncio.create_task(self.manager.get_token())
            await asyncio.wait_for(waiting.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            issue.assert_not_awaited()
        self.assertFalse(self.manager._lock.locked())

    async def test_recovery_after_exhausted_retries_is_shared_and_honors_limit(self):
        for limit in [0, 1, 3]:
            with self.subTest(limit=limit):
                manager = downloader.ServiceTokenManager('id', 'secret', 'reader@example.invalid')
                manager._token = downloader.ServiceToken('expired', self.clock, self.clock)
                calls = 0

                def handler(request):
                    nonlocal calls
                    calls += 1
                    if calls <= limit + 1:
                        return httpx.Response(503, json={'error': 'ServiceUnavailableError'})
                    return httpx.Response(200, json={'access_token': 'recovered', 'expires_in': 3600})

                clients = [httpx.AsyncClient(transport=httpx.MockTransport(handler)) for _ in range(2)]
                with (patch.dict(os.environ, {'MAX_RETRIES': str(limit)}),
                      patch.object(downloader.httpx, 'AsyncClient', side_effect=clients),
                      patch.object(downloader, 'uniform', return_value=0),
                      patch.object(downloader.asyncio, 'sleep', new_callable=AsyncMock) as sleep):
                    results = await asyncio.gather(*(manager.get_token() for _ in range(100)), return_exceptions=True)
                errors = [result for result in results if isinstance(result, downloader.TokenRefreshError)]
                self.assertEqual(len(errors), 1)
                self.assertFalse(errors[0].terminal)
                self.assertIn('HTTP 503', downloader.sanitized_error(errors[0]))
                self.assertEqual(results.count('recovered'), 99)
                self.assertEqual(calls, limit + 2)
                self.assertEqual([call.args[0] for call in sleep.await_args_list],
                                 [*downloader.RETRY_DELAYS[:limit], 30])
                self.assertTrue(all(client.is_closed for client in clients))

    async def test_permanent_auth_rejection_is_terminal_without_repeated_exchange(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(401, json={'error': 'invalid_client'})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with (patch.object(downloader.httpx, 'AsyncClient', return_value=client),
              patch.object(downloader.asyncio, 'sleep', new_callable=AsyncMock) as sleep):
            results = await asyncio.gather(*(self.manager.get_token() for _ in range(100)), return_exceptions=True)
            self.clock += 10000
            self.wall_clock += 10000
            await asyncio.wait_for(self.manager._run(), 1)
            with self.assertRaises(downloader.TokenRefreshError) as caught:
                await self.manager.get_token()
        self.assertTrue(all(isinstance(result, downloader.TokenRefreshError) and result.terminal for result in results))
        self.assertTrue(caught.exception.terminal)
        self.assertEqual(calls, 1)
        sleep.assert_not_awaited()
        self.assertIn('invalid_client', downloader.sanitized_error(caught.exception))
        self.assertTrue(client.is_closed)

    async def test_retry_after_on_exhausted_exchange_controls_shared_cooldown(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={'Retry-After': '120'})
            return httpx.Response(200, json={'access_token': 'recovered', 'expires_in': 3600})

        clients = [httpx.AsyncClient(transport=httpx.MockTransport(handler)) for _ in range(2)]
        with (patch.dict(os.environ, {'MAX_RETRIES': '0'}),
              patch.object(downloader.httpx, 'AsyncClient', side_effect=clients),
              patch.object(downloader.asyncio, 'sleep', new_callable=AsyncMock) as sleep):
            with self.assertRaises(downloader.TokenRefreshError):
                await self.manager.get_token()
            self.assertEqual(await self.manager.get_token(), 'recovered')
        sleep.assert_awaited_once_with(120)
        self.assertEqual(calls, 2)
        self.assertTrue(all(client.is_closed for client in clients))

    async def test_suspend_refreshes_before_an_expired_token_reaches_transport(self):
        issued = []
        seen = []

        async def issue(*args):
            value = f'token-{len(issued)}'
            issued.append(value)
            return self.token(value)

        def handler(request):
            seen.append(request.headers.get('Authorization'))
            return httpx.Response(200)

        with patch.object(downloader, 'get_service_token', side_effect=issue):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                         event_hooks={'request': [self.manager.authorize]}) as client:
                await self.manager.get_token()
                self.wall_clock += 7200
                await asyncio.gather(*(client.get('https://example.invalid',
                                                  headers={'Authorization': 'OAuth token-0'}) for _ in range(100)))
        self.assertEqual(issued, ['token-0', 'token-1'])
        self.assertEqual(seen, ['OAuth token-1'] * 100)

    async def test_clock_moving_backward_does_not_extend_token_lifetime(self):
        with patch.object(downloader, 'get_service_token', side_effect=lambda *args: self.token('token')) as issue:
            await self.manager.get_token()
            self.wall_clock -= 7200
            self.clock += 3601
            await self.manager.get_token()
        self.assertEqual(issue.await_count, 2)

    async def test_background_refresh_notices_suspend_without_an_api_request(self):
        refreshed = asyncio.Event()
        calls = 0

        async def issue(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                refreshed.set()
            return self.token(f'token-{calls}')

        with (patch.object(downloader, 'get_service_token', side_effect=issue),
              patch.object(downloader, 'TOKEN_REFRESH_RETRY_INTERVAL', 0.01)):
            async with self.manager:
                self.wall_clock += 7200
                await asyncio.wait_for(refreshed.wait(), 1)
        self.assertEqual(calls, 2)
        self.assertIsNone(self.manager._task)

    async def test_exchange_that_expires_during_suspend_is_rejected(self):
        def handler(request):
            self.wall_clock += 3601
            return httpx.Response(200, json={'access_token': 'expired', 'expires_in': 3600})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.object(downloader.httpx, 'AsyncClient', return_value=client), self.assertRaises(ValueError):
            await downloader.get_service_token('id', 'secret', 'reader@example.invalid')
        self.assertTrue(client.is_closed)
