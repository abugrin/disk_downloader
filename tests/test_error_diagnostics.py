import errno
import unittest

import httpx
from yadisk.exceptions import ForbiddenError

import downloader


class ErrorDiagnosticTests(unittest.TestCase):
    def test_full_response_masks_signed_url_and_echoed_request_secret(self):
        response = httpx.Response(403, json={
            'error': 'DiskForbiddenError', 'message': 'private-secret', 'description': 'token=private-secret',
        }, request=httpx.Request('GET', 'https://download.example.invalid/file?token=private-secret'))
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            result = downloader.sanitized_error(error)
        self.assertTrue(result.startswith('HTTPStatusError; HTTP 403; DiskForbiddenError;'))
        self.assertIn('заголовки ответа=', result)
        self.assertIn('тело ответа=', result)
        self.assertIn('"message": "[скрыто]"', result)
        self.assertNotIn('private-secret', result)
        self.assertNotIn('https', result)

    def test_provider_fields_are_kept_with_secrets_and_control_characters_masked(self):
        for code, expected in [
            ('https://example.invalid/private-secret', '[URL скрыт]'),
            ('Provider rejected this operation', 'Provider rejected this operation'),
            ('Token=secret', 'Token=[скрыто]'),
            ('Bad\n\x1b[31mSecretError', 'Bad\\n\\u001b[31mSecretError'),
        ]:
            with self.subTest(code=code):
                response = httpx.Response(500, json={'error': code}, request=httpx.Request('GET', 'https://example.invalid'))
                error = httpx.HTTPStatusError('private-secret', request=response.request, response=response)
                result = downloader.sanitized_error(error)
                self.assertTrue(result.startswith('HTTPStatusError; HTTP 500; заголовки ответа='))
                self.assertIn(expected, result)
                self.assertNotIn('private-secret', result)
                self.assertNotIn('\n', result)
                self.assertNotIn('\x1b', result)

    def test_sdk_errors_and_wrapped_http_errors(self):
        response = httpx.Response(403)
        wrapped = ForbiddenError(error_type='DiskForbiddenError', msg='private-secret', response=response)
        self.assertEqual(downloader.sanitized_error(wrapped),
                         'ForbiddenError; HTTP 403; DiskForbiddenError; заголовки ответа={}; тело ответа=""')
        outer = RuntimeError('signed-url=private-secret')
        outer.__cause__ = wrapped
        self.assertEqual(downloader.sanitized_error(outer),
                         'RuntimeError; HTTP 403; DiskForbiddenError; заголовки ответа={}; тело ответа=""')

    def test_filesystem_errno_without_paths_or_error_text(self):
        error = OSError(errno.ENOSPC, 'private-secret', '/private/path')
        self.assertEqual(downloader.sanitized_error(error), 'OSError; errno=28')

    def test_unconsumed_stream_does_not_require_reading_body(self):
        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                raise AssertionError('Diagnostic must not consume a response stream')
        response = httpx.Response(503, stream=Stream(), request=httpx.Request('GET', 'https://example.invalid'))
        error = httpx.HTTPStatusError('private-secret', request=response.request, response=response)
        self.assertEqual(downloader.sanitized_error(error),
                         'HTTPStatusError; HTTP 503; заголовки ответа={}; тело ответа=тело ответа недоступно')
        response.close()
