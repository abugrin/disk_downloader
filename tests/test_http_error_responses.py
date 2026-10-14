import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from yadisk import AsyncClient
from yadisk.sessions.async_httpx_session import AsyncHTTPXSession

import downloader
from environment_fixtures import isolated_environment


class ObservedStream(httpx.AsyncByteStream):
    def __init__(self, content=b'example response', error=None):
        self.content = content
        self.error = error
        self.started = False
        self.closed = False

    async def __aiter__(self):
        self.started = True
        yield self.content
        if self.error is not None:
            raise self.error

    async def aclose(self):
        self.closed = True


class HTTPErrorResponseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(isolated_environment({'MAX_RETRIES': '0'}))

    def sdk(self, handler):
        session = AsyncHTTPXSession(
            transport=httpx.MockTransport(handler),
            event_hooks={'response': [downloader.check_transient_response]},
        )
        return AsyncClient(token='synthetic-oauth-secret', session=session,
                           default_args={'n_retries': 0})

    async def test_sdk_permanent_error_keeps_all_fields_and_response_headers(self):
        payload = {
            'error': 'DiskForbiddenError',
            'message': 'The shared disk is unavailable',
            'description': 'Read access was removed',
            'details': {'request_id': 'synthetic-request-123', 'required_role': 'reader'},
            'access_token': 'synthetic-body-secret',
            'echo': 'synthetic-oauth-secret',
        }
        stream = ObservedStream(httpx.Response(403, json=payload).content)

        def handler(request):
            return httpx.Response(403, stream=stream, headers={
                'Content-Type': 'application/json', 'X-Request-Id': 'synthetic-request-123',
                'Set-Cookie': 'session=synthetic-cookie-secret',
            })

        async with self.sdk(handler) as client:
            with self.assertRaises(Exception) as caught:
                await client.get_disk_info()
        self.assertTrue(stream.started)
        self.assertTrue(stream.closed)
        diagnostic = downloader.sanitized_error(caught.exception)
        for value in ('HTTP 403', 'DiskForbiddenError', 'The shared disk is unavailable',
                      'Read access was removed', 'synthetic-request-123', 'required_role',
                      'reader', 'заголовки ответа=', 'тело ответа='):
            self.assertIn(value, diagnostic)
        for value in ('synthetic-body-secret', 'synthetic-oauth-secret', 'synthetic-cookie-secret'):
            self.assertNotIn(value, diagnostic)

    async def test_sdk_unknown_status_codes_keep_status_headers_and_body(self):
        for status in (501, 405, 418, 451, 404):
            with self.subTest(status=status):
                async with self.sdk(lambda request: httpx.Response(status, headers={
                    'X-Request-Id': f'synthetic-status-{status}',
                }, json={
                    'error': 'RequestRejected', 'description': f'Upstream rejected request: {status}',
                })) as client:
                    with self.assertRaises(Exception) as caught:
                        await client.get_disk_info()
                diagnostic = downloader.sanitized_error(caught.exception)
                self.assertIn(f'HTTP {status}', diagnostic)
                self.assertIn(f'synthetic-status-{status}', diagnostic)
                self.assertIn(f'Upstream rejected request: {status}', diagnostic)
                self.assertFalse(downloader.retry_details(caught.exception)[0])

    async def test_sdk_malformed_404_keeps_raw_response(self):
        for body in ('not json: upstream route missing', '["unexpected response shape"]',
                     '"unexpected response shape"', '42'):
            with self.subTest(body=body):
                async with self.sdk(lambda request: httpx.Response(404, text=body)) as client:
                    with self.assertRaises(Exception) as caught:
                        await client.get_disk_info()
                diagnostic = downloader.sanitized_error(caught.exception)
                self.assertIn('HTTP 404', diagnostic)
                if 'unexpected' in body:
                    self.assertIn('unexpected response shape', diagnostic)
                else:
                    self.assertIn(body, diagnostic)
    async def test_each_retry_logs_full_response_and_honors_retry_after(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(503, headers={'Retry-After': '7'}, json={
                'error': 'ServiceUnavailableError',
                'description': f'Unavailable on attempt {calls}',
                'details': {'message': 'z' * 1400, 'retry_trace': f'trace-{calls}'},
            })

        with (patch.dict(os.environ, {'MAX_RETRIES': '2'}),
              patch.object(downloader, 'uniform', return_value=0),
              patch.object(downloader.asyncio, 'sleep', new_callable=AsyncMock) as sleep,
              self.assertLogs('Downloader', level='WARNING') as logs):
            async with self.sdk(handler) as client:
                with self.assertRaises(Exception) as caught:
                    await downloader.with_retries(client.get_disk_info, 'получение информации о диске')
        self.assertEqual(calls, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [7, 7])
        text = '\n'.join(logs.output)
        for attempt in range(1, 3):
            self.assertIn(f'Unavailable on attempt {attempt}', text)
            self.assertIn(f'trace-{attempt}', text)
        self.assertIn('z' * 1400, text)
        self.assertIn('retry-after', text.lower())
        final_diagnostic = downloader.sanitized_error(caught.exception)
        self.assertIn('HTTP 503', final_diagnostic)
        self.assertIn('Unavailable on attempt 3', final_diagnostic)
        self.assertIn('trace-3', final_diagnostic)
        self.assertIn('z' * 1400, final_diagnostic)

    async def test_oauth_missing_user_description_reaches_manager_log_and_wrapper(self):
        def handler(request):
            return httpx.Response(400, json={
                'error': 'invalid_grant', 'error_description': 'User does not exist',
                'support_code': 'synthetic-user-lookup-404',
            })

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        manager = downloader.ServiceTokenManager('synthetic-id', 'synthetic-secret', 'missing@example.invalid')
        with (patch.object(downloader.httpx, 'AsyncClient', return_value=client),
              self.assertLogs('Downloader') as logs):
            with self.assertRaises(downloader.TokenRefreshError) as caught:
                await manager.get_token()
        diagnostic = downloader.sanitized_error(caught.exception)
        text = '\n'.join(logs.output)
        for output in (diagnostic, text):
            self.assertIn('HTTP 400', output)
            self.assertIn('invalid_grant', output)
            self.assertIn('User does not exist', output)
            self.assertIn('synthetic-user-lookup-404', output)
        self.assertTrue(caught.exception.terminal)
        self.assertTrue(client.is_closed)

    async def test_plain_text_and_xml_responses_are_complete_and_secret_safe(self):
        for body in (
            'Gateway refused request\nReason: upstream unavailable\n' + 'x' * 1800,
            '<Error><Code>AccessDenied</Code><Message>Owner removed access</Message>'
            '<Trace>synthetic-xml-trace</Trace></Error>',
        ):
            with self.subTest(body=body[:30]):
                request = httpx.Request('GET', 'https://example.invalid/error',
                                        headers={'Authorization': 'OAuth synthetic-auth-secret'})
                response = httpx.Response(400, text=body + '\nEcho: synthetic-auth-secret', request=request)
                error = httpx.HTTPStatusError('request failed', request=request, response=response)
                diagnostic = downloader.sanitized_error(error)
                self.assertIn('HTTP 400', diagnostic)
                self.assertIn('тело ответа=', diagnostic)
                self.assertNotIn('synthetic-auth-secret', diagnostic)
                self.assertNotIn('\n', diagnostic)
                if body.startswith('Gateway'):
                    self.assertIn('upstream unavailable', diagnostic)
                    self.assertIn('x' * 1800, diagnostic)
                else:
                    self.assertIn('<Code>AccessDenied</Code>', diagnostic)
                    self.assertIn('synthetic-xml-trace', diagnostic)

    async def test_response_only_json_credentials_are_redacted_in_echoed_fields(self):
        payload = {
            'error': 'invalid_request',
            'details': [{'access_token': 'synthetic-response-only-secret'}],
            'echo': 'Rejected credential synthetic-response-only-secret',
            'hint': 'Supply a valid credential',
        }
        request = httpx.Request('GET', 'https://example.invalid/error')
        response = httpx.Response(400, json=payload, request=request)
        error = httpx.HTTPStatusError('failed', request=request, response=response)
        diagnostic = downloader.sanitized_error(error)
        self.assertNotIn('synthetic-response-only-secret', diagnostic)
        self.assertIn('Rejected credential', diagnostic)
        self.assertIn('Supply a valid credential', diagnostic)

    async def test_xml_credentials_are_redacted_without_removing_error_details(self):
        body = ('<Error><Code>InvalidCredential</Code>'
                '<access_token>synthetic-xml-secret</access_token>'
                '<Message>Check application settings</Message></Error>')
        request = httpx.Request('GET', 'https://example.invalid/error')
        response = httpx.Response(400, text=body, request=request)
        error = httpx.HTTPStatusError('failed', request=request, response=response)
        diagnostic = downloader.sanitized_error(error)
        self.assertNotIn('synthetic-xml-secret', diagnostic)
        self.assertIn('InvalidCredential', diagnostic)
        self.assertIn('Check application settings', diagnostic)

    async def test_sdk_token_check_keeps_refusal_body_and_accepts_expected_404(self):
        async with self.sdk(lambda request: httpx.Response(401, json={
            'error': 'UnauthorizedError', 'description': 'Token has been revoked',
        })) as client:
            with self.assertRaises(Exception) as caught:
                await client.check_token()
        diagnostic = downloader.sanitized_error(caught.exception)
        self.assertIn('HTTP 401', diagnostic)
        self.assertIn('Token has been revoked', diagnostic)

        def expected_probe(request):
            self.assertEqual(request.url.path, '/v1/disk/operations/0000')
            return httpx.Response(404, json={
                'error': 'DiskOperationNotFoundError', 'description': 'Expected operation probe',
            })

        async with self.sdk(expected_probe) as client:
            self.assertTrue(await client.check_token())

    async def test_partial_error_body_preserves_status_and_retry_after(self):
        stream = ObservedStream(b'partial body', httpx.ReadError('synthetic-private-transport-detail'))

        def handler(request):
            return httpx.Response(503, stream=stream, headers={'Retry-After': '42'})

        async with self.sdk(handler) as client:
            with self.assertRaises(Exception) as caught:
                await client.get_disk_info()
        retryable, delay, reason = downloader.retry_details(caught.exception)
        self.assertTrue(retryable)
        self.assertEqual(delay, 42)
        self.assertEqual(reason, 'HTTP 503')
        diagnostic = downloader.sanitized_error(caught.exception)
        self.assertIn('HTTP 503', diagnostic)
        self.assertIn('тело ответа недоступно', diagnostic)
        self.assertIn('ReadError', diagnostic)
        self.assertNotIn('synthetic-private-transport-detail', diagnostic)
        self.assertTrue(stream.closed)

    async def test_successful_download_stream_is_not_preconsumed(self):
        stream = ObservedStream(b'download payload')
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)),
            event_hooks={'response': [downloader.check_transient_response]},
        ) as client:
            response = await client.send(client.build_request('GET', 'https://example.invalid/download'), stream=True)
            try:
                self.assertFalse(stream.started)
                self.assertEqual(await response.aread(), b'download payload')
            finally:
                await response.aclose()
        self.assertTrue(stream.closed)

    async def test_cancellation_while_reading_error_body_closes_stream(self):
        entered = asyncio.Event()

        class WaitingStream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b'unreachable'

            async def aclose(self):
                self.closed = True

        stream = WaitingStream()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503, stream=stream)),
            event_hooks={'response': [downloader.check_transient_response]},
        ) as client:
            task = asyncio.create_task(client.get('https://example.invalid/error'))
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(stream.closed)

