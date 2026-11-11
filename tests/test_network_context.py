import asyncio
import io
import os
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from yadisk import AsyncClient
from yadisk.objects import ResourceObject
from yadisk.sessions.async_httpx_session import AsyncHTTPXSession

import downloader
from environment_fixtures import isolated_environment


class NetworkContextTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_environment())

    def test_connect_timeout_reports_host_operation_and_effective_timeouts(self):
        request = httpx.Request('GET', 'https://cdn.example.invalid/private/path?token=private-query',
                                extensions={'timeout': {'connect': 10.0, 'read': 15.0}})
        error = httpx.ConnectTimeout('Connection timed out', request=request)
        result = downloader.sanitized_error(error)
        for marker in ('метод=GET', 'узел=cdn.example.invalid:443',
                       'операция=скачивание содержимого', 'этап=подключение (TCP/TLS)',
                       'подключение=10 с', 'чтение=15 с'):
            self.assertIn(marker, result)
        for hidden in ('https://', '/private/path', 'private-query'):
            self.assertNotIn(hidden, result)

    def test_operations_are_distinguished_without_logging_request_paths(self):
        cases = (
            ('https://oauth.yandex.ru/token', 'получение токена'),
            ('https://cloud-api.yandex.net/v1/disk/resources/download', 'получение ссылки на скачивание'),
            ('https://cloud-api.yandex.net/v1/disk/virtual-disks/resources/download', 'получение ссылки на скачивание'),
            ('https://cloud-api.yandex.net/v1/disk/resources', 'запрос API Диска'),
        )
        for url, operation in cases:
            with self.subTest(operation=operation, url=url):
                request = httpx.Request('GET', url, params={'path': 'private-remote-name', 'token': 'fake-token'})
                result = downloader.sanitized_error(httpx.ConnectTimeout('Timed out', request=request))
                self.assertIn('операция=' + operation, result)
                self.assertNotIn(url, result)
                self.assertNotIn('private-remote-name', result)
                self.assertNotIn('fake-token', result)

    def test_timeout_phases_are_distinguished(self):
        cases = (
            (httpx.ConnectTimeout, 'подключение (TCP/TLS)'),
            (httpx.ReadTimeout, 'чтение ответа'),
            (httpx.WriteTimeout, 'отправка запроса'),
            (httpx.PoolTimeout, 'ожидание свободного соединения'),
        )
        for kind, phase in cases:
            with self.subTest(kind=kind.__name__):
                request = httpx.Request('GET', 'https://cdn.example.invalid/file')
                result = downloader.sanitized_error(kind('Timed out', request=request))
                self.assertIn('этап=' + phase, result)

    def test_certificate_error_is_not_reported_as_a_connect_timeout(self):
        request = httpx.Request('GET', 'https://cdn.example.invalid/file')
        cause = ssl.SSLCertVerificationError(1, 'certificate verify failed')
        cause.verify_code = 20
        error = httpx.ConnectError('TLS failed', request=request)
        error.__cause__ = cause
        result = downloader.sanitized_error(error)
        self.assertIn('узел=cdn.example.invalid:443', result)
        self.assertIn('SSLCertVerificationError', result)
        self.assertIn('verify_code=20', result)
        self.assertNotIn('ConnectTimeout', result)

    def test_request_credentials_path_query_and_fragment_are_not_disclosed(self):
        secret = 'synthetic-header-secret'
        request = httpx.Request(
            'GET', 'https://private-user:private-password@cdn.example.invalid:8443/private-path?'
            'token=private-query#private-fragment', headers={'Authorization': 'Bearer ' + secret},
        )
        error = httpx.ConnectTimeout('Timed out; echo: ' + secret, request=request)
        result = downloader.sanitized_error(error)
        self.assertIn('узел=cdn.example.invalid:8443', result)
        for hidden in ('private-user', 'private-password', 'private-path', 'private-query',
                       'private-fragment', secret):
            self.assertNotIn(hidden, result)

    def test_invalid_timeout_extension_values_cannot_inject_diagnostic_text(self):
        request = httpx.Request('GET', 'https://cdn.example.invalid/file', extensions={'timeout': {
            'connect': 'injected-secret\nforged-log', 'read': float('nan'),
            'write': float('inf'), 'pool': {'private': 'payload'},
        }})
        result = downloader.sanitized_error(httpx.ConnectTimeout('Timed out', request=request))
        self.assertIn('узел=cdn.example.invalid:443', result)
        for hidden in ('injected-secret', 'forged-log', 'payload', '=nan', '=inf'):
            self.assertNotIn(hidden, result)
        self.assertEqual(len(result.splitlines()), 1)


class InterruptedBody(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        yield b'part'
        raise httpx.ReadTimeout('Download body timed out')

    async def aclose(self):
        self.closed = True


class NetworkContextIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(os.chdir, self.previous)
        self.enterContext(isolated_environment({'MAX_RETRIES': '0'}))
        self.enterContext(patch.object(downloader.log_handler, 'stream', io.StringIO()))
        self.sessions = []
        self.requests = []
        self.failure = 'cdn'
        self.remaining_failures = None
        self.streams = []
        self.enterContext(patch.object(downloader, 'AsyncHTTPXSession', side_effect=self.session))
        downloader.sem = asyncio.Semaphore(downloader.MAX_STREAMS)

    def session(self, **kwargs):
        session = AsyncHTTPXSession(transport=httpx.MockTransport(self.respond), **kwargs)
        self.sessions.append(session)
        return session

    def respond(self, request):
        self.requests.append(request)
        should_fail = self.remaining_failures is None or self.remaining_failures > 0
        if request.url.host == 'cloud-api.yandex.net':
            if self.failure == 'api' and should_fail:
                if self.remaining_failures is not None:
                    self.remaining_failures -= 1
                raise httpx.ConnectTimeout('API connection timed out', request=request)
            return httpx.Response(200, json={
                'href': 'https://download.example.invalid/private-download-path?token=private-link-secret',
                'method': 'GET', 'templated': False,
            })
        if request.url.host == 'download.example.invalid':
            return httpx.Response(302, headers={
                'Location': 'https://cdn.example.invalid:8443/private-redirect-path?signature=private-cdn-secret',
            })
        self.assertEqual(request.url.host, 'cdn.example.invalid')
        if should_fail:
            if self.remaining_failures is not None:
                self.remaining_failures -= 1
            if self.failure == 'body':
                stream = InterruptedBody()
                self.streams.append(stream)
                return httpx.Response(200, stream=stream)
            raise httpx.ConnectTimeout('CDN connection timed out', request=request)
        return httpx.Response(200, content=b'complete')

    async def download(self, shared=False):
        if shared:
            client = downloader.SharedDiskClient('test-token', 'ExampleHash')
        else:
            client = AsyncClient(token='test-token', session=downloader.disk_session(), default_args={'n_retries': 0})
        with self.assertLogs('Downloader') as logs:
            async with client:
                success = await downloader.safe_download(
                    client, '/sample.txt', Path.cwd() / 'sample.txt', '[1/1]',
                    resource=ResourceObject({'size': 8}), destination_root=Path.cwd(),
                )
        self.assertTrue(all(session.httpx_session.is_closed for session in self.sessions))
        output = '\n'.join(logs.output)
        for hidden in ('private-download-path', 'private-link-secret', 'private-redirect-path', 'private-cdn-secret'):
            self.assertNotIn(hidden, output)
        return success, output, logs.records

    async def test_api_link_timeout_is_distinct_for_personal_and_shared_disks(self):
        self.failure = 'api'
        for shared in (False, True):
            with self.subTest(shared=shared):
                success, output, _ = await self.download(shared)
                self.assertFalse(success)
                self.assertIn('узел=cloud-api.yandex.net:443', output)
                self.assertIn('операция=получение ссылки на скачивание', output)
                self.assertIn('этап=подключение (TCP/TLS)', output)
                self.assertIn('подключение=30 с' if shared else 'подключение=10 с', output)
                self.assertRegex(output, r'прошло=\d+\.\d{2} с')
                self.assertFalse(Path('sample.txt').exists())
                self.assertFalse(list(Path.cwd().glob('.disk-download-*.part')))

    async def test_redirected_cdn_connection_timeout_reports_effective_host(self):
        success, output, _ = await self.download()
        self.assertFalse(success)
        self.assertIn('узел=cdn.example.invalid:8443', output)
        self.assertIn('операция=скачивание содержимого', output)
        self.assertIn('подключение=10 с', output)
        self.assertIn('чтение=15 с', output)
        self.assertNotIn('узел=download.example.invalid', output)
        self.assertRegex(output, r'прошло=\d+\.\d{2} с')

    async def test_body_timeout_keeps_origin_and_closes_partial_stream(self):
        self.failure = 'body'
        success, output, _ = await self.download()
        self.assertFalse(success)
        self.assertIn('узел=cdn.example.invalid:8443', output)
        self.assertIn('этап=чтение ответа', output)
        self.assertIn('операция=скачивание содержимого', output)
        self.assertIn('чтение=15 с', output)
        self.assertTrue(self.streams)
        self.assertTrue(all(stream.closed for stream in self.streams))
        self.assertFalse(Path('sample.txt').exists())
        self.assertFalse(list(Path.cwd().glob('.disk-download-*.part')))

    async def test_retry_logs_failed_request_context_then_saves_successful_result(self):
        self.remaining_failures = 1
        with (patch.dict(os.environ, {'MAX_RETRIES': '1'}),
              patch.object(downloader.asyncio, 'sleep', new_callable=AsyncMock) as sleep,
              patch.object(downloader, 'uniform', return_value=0)):
            success, output, records = await self.download()
        self.assertTrue(success)
        self.assertEqual(Path('sample.txt').read_bytes(), b'complete')
        sleep.assert_awaited_once_with(2)
        warnings = [record.getMessage() for record in records if record.levelname == 'WARNING']
        self.assertEqual(len(warnings), 1)
        self.assertIn('Повтор 1/1', warnings[0])
        self.assertIn('узел=cdn.example.invalid:8443', warnings[0])
        self.assertIn('этап=подключение (TCP/TLS)', warnings[0])
        self.assertFalse(any(record.levelname == 'ERROR' for record in records))

    async def test_oauth_hook_records_elapsed_time_without_exposing_form_credentials(self):
        original_client = httpx.AsyncClient
        clients = []

        def respond(request):
            self.assertEqual(request.url.host, 'oauth.yandex.ru')
            raise httpx.ConnectTimeout('OAuth connection timed out', request=request)

        def client(*args, **kwargs):
            instance = original_client(*args, transport=httpx.MockTransport(respond), **kwargs)
            clients.append(instance)
            return instance

        with patch.object(downloader.httpx, 'AsyncClient', side_effect=client):
            with self.assertRaises(httpx.ConnectTimeout) as raised:
                await downloader.get_service_token('test-id', 'synthetic-form-secret', 'reader@example.invalid')
        result = downloader.sanitized_error(raised.exception)
        self.assertIn('метод=POST', result)
        self.assertIn('узел=oauth.yandex.ru:443', result)
        self.assertIn('операция=получение токена', result)
        self.assertIn('подключение=30 с', result)
        self.assertRegex(result, r'прошло=\d+\.\d{2} с')
        self.assertNotIn('synthetic-form-secret', result)
        self.assertNotIn('reader@example.invalid', result)
        self.assertTrue(clients)
        self.assertTrue(all(instance.is_closed for instance in clients))
