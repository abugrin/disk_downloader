import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from yadisk import AsyncClient
from yadisk.sessions.async_httpx_session import AsyncHTTPXSession

import downloader


class BrokenStream(httpx.AsyncByteStream):
    def __init__(self, error):
        self.error = error
        self.closed = False

    async def __aiter__(self):
        yield b'partial data' * 2000
        raise self.error

    async def aclose(self):
        self.closed = True


def disk_client(handler):
    session = AsyncHTTPXSession(
        transport=httpx.MockTransport(handler),
        event_hooks={'response': [downloader.check_transient_response]},
    )
    return AsyncClient(token='test-token', session=session, default_args={'n_retries': 0})


def download_link():
    return httpx.Response(200, json={
        'href': 'https://download.example.invalid/file?token=must-not-be-logged',
        'method': 'GET', 'templated': False,
    })


def directory_page(offset, kind='file', total=2):
    return httpx.Response(200, json={
        'type': 'dir', 'path': 'disk:/',
        '_embedded': {
            'offset': offset, 'limit': 1, 'total': total,
            'items': [{'type': kind, 'path': f'disk:/{offset}.txt'}] if total else [],
        },
    })


class RetryAfterTests(unittest.TestCase):
    def test_seconds_dates_and_invalid_headers(self):
        now = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc).timestamp()
        values = {
            None: 0, '0': 0, ' 90 ': 90, '-1': 0, '+2': 0, '1.5': 0,
            'nan': 0, 'inf': 0, 'tomorrow': 0, '': 0, '9' * 400: 0,
            'Mon, 12 Oct 2026 10:01:00 GMT': 60,
            'Mon, 12 Oct 2026 09:59:00 GMT': 0,
        }
        with patch.object(downloader, 'time', return_value=now):
            for value, expected in values.items():
                with self.subTest(value=value):
                    self.assertEqual(downloader.retry_after_seconds(value), expected)


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sleep = self.enterContext(patch.object(downloader.asyncio, 'sleep', new_callable=AsyncMock))
        self.jitter = self.enterContext(patch.object(downloader, 'uniform', return_value=0))
        self.logs = self.enterContext(self.assertLogs('Downloader'))
        downloader.sem = asyncio.Semaphore(downloader.MAX_STREAMS)
        downloader.directories.clear()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(downloader.directories.clear)

    def delays(self):
        return [call.args[0] for call in self.sleep.await_args_list]

    async def test_all_transient_statuses_have_six_attempts_without_nested_sdk_retries(self):
        for status in [408, 429, 500, 502, 503, 504]:
            for endpoint in ['metadata', 'download_link', 'stream']:
                with self.subTest(status=status, endpoint=endpoint):
                    calls = []
                    responses = []
                    self.sleep.reset_mock()

                    def handler(request):
                        calls.append(request)
                        if endpoint == 'stream' and request.url.host != 'download.example.invalid':
                            return download_link()
                        response = httpx.Response(status, text='non-JSON error body')
                        responses.append(response)
                        return response

                    async with disk_client(handler) as client:
                        with self.assertRaises(Exception):
                            if endpoint == 'metadata':
                                await downloader.with_retries(client.get_disk_info, 'метаданные')
                            else:
                                await downloader.download_file(client, '/file', self.root / 'file', '[1/1]')
                    self.assertEqual(len(responses), 6)
                    self.assertEqual(len(calls), 12 if endpoint == 'stream' else 6)
                    self.assertTrue(all(response.is_closed for response in responses))
                    self.assertEqual(self.delays(), [2, 4, 8, 16, 32])
                    self.assertFalse(list(self.root.iterdir()))
        output = '\n'.join(self.logs.output)
        self.assertNotIn('must-not-be-logged', output)
        self.assertIn('Повтор 5/5', output)
        self.assertNotIn('Повтор 6/5', output)

    async def test_jitter_bounds_and_retry_after_larger_than_randomized_backoff(self):
        for fraction in [0, 0.125, 0.25]:
            self.sleep.reset_mock()
            self.jitter.reset_mock()
            self.jitter.side_effect = lambda low, high: high * fraction / 0.25
            operation = AsyncMock(side_effect=TimeoutError('must-not-be-logged'))
            with self.assertRaises(TimeoutError):
                await downloader.with_retries(operation, 'тайм-аут')
            self.assertEqual(operation.await_count, 6)
            self.assertEqual(self.delays(), [base * (1 + fraction) for base in [2, 4, 8, 16, 32]])
            self.assertEqual([c.args for c in self.jitter.call_args_list],
                             [(0, base * 0.25) for base in [2, 4, 8, 16, 32]])

        now = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc).timestamp()
        cases = [('60', 60), ('1', 2.5), ('invalid', 2.5),
                 (format_datetime(datetime.fromtimestamp(now + 90, timezone.utc), usegmt=True), 90)]
        for header, expected in cases:
            with self.subTest(header=header):
                self.sleep.reset_mock()
                calls = 0

                def handler(request):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        return httpx.Response(429, headers={'retry-after': header})
                    return httpx.Response(200, json={'used_space': 0})

                with patch.object(downloader, 'time', return_value=now):
                    async with disk_client(handler) as client:
                        await downloader.with_retries(client.get_disk_info, 'метаданные')
                self.assertEqual(self.delays(), [expected])
                self.assertEqual(calls, 2)

    async def test_permanent_http_errors_are_not_retried(self):
        for status in [400, 401, 403, 404, 409, 501, 507]:
            with self.subTest(status=status):
                calls = 0
                self.sleep.reset_mock()

                def handler(request):
                    nonlocal calls
                    calls += 1
                    return httpx.Response(status, json={'error': 'PermanentError', 'message': 'failed'})

                async with disk_client(handler) as client:
                    with self.assertRaises(Exception):
                        await downloader.with_retries(client.get_disk_info, 'метаданные')
                self.assertEqual(calls, 1)
                self.sleep.assert_not_awaited()
        downloader.log.info('Permanent errors checked')

    async def test_network_failures_are_retried_through_sdk_exception_wrappers(self):
        errors = [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
                  httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError]
        for error_type in errors:
            with self.subTest(error=error_type):
                self.sleep.reset_mock()
                calls = 0

                def handler(request):
                    nonlocal calls
                    calls += 1
                    raise error_type('must-not-be-logged', request=request)

                async with disk_client(handler) as client:
                    with self.assertRaises(Exception):
                        await downloader.with_retries(client.get_disk_info, 'метаданные')
                self.assertEqual(calls, 6)
                self.assertEqual(self.delays(), [2, 4, 8, 16, 32])
        self.assertNotIn('must-not-be-logged', '\n'.join(self.logs.output))

    async def test_sixth_attempt_can_succeed_and_no_wait_follows_success(self):
        operation = AsyncMock(side_effect=[TimeoutError()] * 5 + ['done'])
        self.assertEqual(await downloader.with_retries(operation, 'операция'), 'done')
        self.assertEqual(operation.await_count, 6)
        self.assertEqual(self.delays(), [2, 4, 8, 16, 32])

    async def test_local_errors_and_cancellation_are_not_retried(self):
        for error in [PermissionError(), OSError('disk full'), ValueError(), asyncio.CancelledError()]:
            operation = AsyncMock(side_effect=error)
            with self.assertRaises(type(error)):
                await downloader.with_retries(operation, 'операция')
            operation.assert_awaited_once()
        self.sleep.assert_not_awaited()
        downloader.log.info('Local errors and cancellation checked')

    async def test_interrupted_stream_restarts_from_zero_then_atomically_replaces_destination(self):
        for error_type in [httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError]:
            with self.subTest(error=error_type):
                self.sleep.reset_mock()
                destination = self.root / 'file'
                destination.write_bytes(b'existing copy')
                stream = BrokenStream(error_type('must-not-be-logged'))
                calls = 0

                def handler(request):
                    nonlocal calls
                    self.assertEqual(destination.read_bytes(), b'existing copy')
                    if request.url.host != 'download.example.invalid':
                        return download_link()
                    calls += 1
                    return httpx.Response(200, stream=stream) if calls == 1 else httpx.Response(200, content=b'complete')

                async with disk_client(handler) as client:
                    self.assertTrue(await downloader.safe_download(client, '/file', destination, '[1/1]'))
                self.assertEqual(destination.read_bytes(), b'complete')
                self.assertEqual(calls, 2)
                self.assertEqual(self.delays(), [2])
                self.assertTrue(stream.closed)
                self.assertEqual(list(self.root.iterdir()), [destination])

    async def test_exhausted_stream_retries_preserve_existing_copy_and_remove_partial_file(self):
        destination = self.root / 'file'
        destination.write_bytes(b'existing copy')
        streams = []

        def handler(request):
            if request.url.host != 'download.example.invalid':
                return download_link()
            stream = BrokenStream(httpx.RemoteProtocolError('broken stream'))
            streams.append(stream)
            return httpx.Response(200, stream=stream)

        progress = Mock()
        async with disk_client(handler) as client:
            self.assertFalse(await downloader.safe_download(client, '/file', destination, '[1/1]', progress))
        self.assertEqual(len(streams), 6)
        self.assertTrue(all(stream.closed for stream in streams))
        self.assertEqual(destination.read_bytes(), b'existing copy')
        self.assertEqual(list(self.root.iterdir()), [destination])
        progress.finish_file.assert_called_once_with('/file', False)

    async def test_cancel_during_retry_wait_removes_partial_file(self):
        stream = BrokenStream(httpx.ReadError('broken stream'))

        def handler(request):
            return download_link() if request.url.host != 'download.example.invalid' else httpx.Response(200, stream=stream)

        self.sleep.side_effect = asyncio.CancelledError
        progress = Mock()
        async with disk_client(handler) as client:
            with self.assertRaises(asyncio.CancelledError):
                await downloader.safe_download(client, '/file', self.root / 'file', '[1/1]', progress)
        self.assertTrue(stream.closed)
        self.assertFalse(list(self.root.iterdir()))
        progress.finish_file.assert_called_once_with('/file', None)
        self.assertEqual(downloader.sem._value, downloader.MAX_STREAMS)

    async def test_failure_on_second_listing_page_does_not_duplicate_files_or_counters(self):
        offsets = []

        def handler(request):
            offset = int(request.url.params.get('offset', '0'))
            offsets.append(offset)
            if offsets == [0, 1]:
                return httpx.Response(503)
            return directory_page(offset)

        progress = Mock()
        async with disk_client(handler) as client:
            files = await downloader.list_files(client, progress)
        self.assertEqual(offsets, [0, 1, 0, 1])
        self.assertEqual([file.path for file in files], ['disk:/0.txt', 'disk:/1.txt'])
        self.assertEqual(sum(c.kwargs.get('files', 0) for c in progress.count.call_args_list), 2)
        self.assertEqual(self.delays(), [2])

    async def test_service_token_retries_statuses_and_network_failures_without_sdk(self):
        for failure in [408, 429, 500, 502, 503, 504, httpx.ReadTimeout, httpx.ConnectError]:
            with self.subTest(failure=failure):
                calls = 0
                self.sleep.reset_mock()

                def handler(request):
                    nonlocal calls
                    calls += 1
                    self.assertEqual(request.method, 'POST')
                    self.assertEqual(str(request.url), 'https://oauth.yandex.ru/token')
                    self.assertIn(b'grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Atoken-exchange', request.content)
                    if isinstance(failure, int):
                        return httpx.Response(failure, headers={'Retry-After': '40'})
                    raise failure('must-not-be-logged', request=request)

                client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                with patch.object(downloader.httpx, 'AsyncClient', return_value=client):
                    with self.assertRaises(httpx.HTTPError):
                        await downloader.get_service_token('test-id', 'test-secret', 'user@example.invalid')
                self.assertTrue(client.is_closed)
                self.assertEqual(calls, 6)
                self.assertEqual(self.delays(), [40] * 5 if isinstance(failure, int) else [2, 4, 8, 16, 32])

    async def test_authentication_rejections_and_invalid_tokens_are_not_retried(self):
        cases = [(400, {}), (401, {}), (403, {}), (200, {}), (200, {'access_token': ''}),
                 (200, {'access_token': 123})]
        for status, body in cases:
            with self.subTest(status=status, body=body):
                calls = 0

                def handler(request):
                    nonlocal calls
                    calls += 1
                    return httpx.Response(status, json=body)

                client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                with patch.object(downloader.httpx, 'AsyncClient', return_value=client):
                    with self.assertRaises((httpx.HTTPStatusError, KeyError, ValueError)):
                        await downloader.get_service_token('test-id', 'test-secret', 'user@example.invalid')
                self.assertEqual(calls, 1)
                self.assertTrue(client.is_closed)
        self.sleep.assert_not_awaited()
        downloader.log.info('Authentication rejections checked')

    async def test_full_user_download_uses_retry_policy_in_all_phases(self):
        counts = {}
        destination = self.root / 'out'
        streams = []

        def auth_handler(request):
            counts['auth'] = counts.get('auth', 0) + 1
            return httpx.Response(503) if counts['auth'] == 1 else httpx.Response(200, json={'access_token': 'test-token'})

        def disk_handler(request):
            path = request.url.path
            counts[path] = counts.get(path, 0) + 1
            if counts[path] == 1:
                return httpx.Response(408)
            if '/operations/' in path:
                return httpx.Response(404, json={'error': 'DiskOperationNotFoundError'})
            if path.endswith('/disk'):
                return httpx.Response(200, json={'used_space': 8})
            if path.endswith('/resources'):
                return directory_page(0, total=1)
            if path.endswith('/resources/download'):
                return download_link()
            if counts[path] == 2:
                stream = BrokenStream(httpx.RemoteProtocolError('broken stream'))
                streams.append(stream)
                return httpx.Response(200, stream=stream)
            return httpx.Response(200, content=b'complete')

        auth = httpx.AsyncClient(transport=httpx.MockTransport(auth_handler))
        session = AsyncHTTPXSession(transport=httpx.MockTransport(disk_handler),
                                   event_hooks={'response': [downloader.check_transient_response]})
        with (
            patch.object(downloader, 'load_dotenv'),
            patch.dict(os.environ, {'CLIENT_ID': 'test-id', 'CLIENT_SECRET': 'test-secret'}, clear=True),
            patch.object(downloader.httpx, 'AsyncClient', return_value=auth),
            patch.object(downloader, 'AsyncHTTPXSession', return_value=session),
        ):
            completed = await downloader.main('user@example.invalid', destination)
        self.assertTrue(completed)
        self.assertEqual((destination / '0.txt').read_bytes(), b'complete')
        self.assertEqual(counts, {
            'auth': 2, '/v1/disk/operations/0000': 2, '/v1/disk': 2,
            '/v1/disk/resources': 3, '/v1/disk/resources/download': 4, '/file': 3,
        })
        self.assertEqual(self.delays(), [2, 2, 2, 2, 2, 4, 8])
        self.assertTrue(auth.is_closed)
        self.assertTrue(session.httpx_session.is_closed)
        self.assertTrue(all(stream.closed for stream in streams))
