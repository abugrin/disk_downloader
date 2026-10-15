import errno
import socket
import ssl
import unittest
import unicodedata
from urllib.parse import quote, quote_plus

import httpcore
import httpx

import downloader
from environment_fixtures import isolated_environment


class NetworkDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_environment())

    def test_certificate_verification_details_survive_transport_wrapper(self):
        certificate_error = ssl.SSLCertVerificationError(
            1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed',
        )
        certificate_error.verify_code = 20
        certificate_error.verify_message = 'unable to get local issuer certificate'
        error = httpx.ConnectError('TLS connection failed')
        error.__cause__ = certificate_error

        result = downloader.sanitized_error(error)

        for marker in ('ConnectError', 'SSLCertVerificationError', 'CERTIFICATE_VERIFY_FAILED',
                       'verify_code=20', 'unable to get local issuer certificate'):
            self.assertIn(marker, result)

    def test_httpcore_keeps_tls_details_in_args_after_suppressing_cause(self):
        certificate_error = ssl.SSLCertVerificationError(
            1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed',
        )
        certificate_error.verify_code = 18
        certificate_error.verify_message = 'self-signed certificate'
        core_error = httpcore.ConnectError(certificate_error)
        core_error.__cause__ = None
        core_error.__suppress_context__ = True
        error = httpx.ConnectError('TLS connection failed')
        error.__cause__ = core_error

        result = downloader.sanitized_error(error)

        self.assertIn('SSLCertVerificationError', result)
        self.assertIn('verify_code=18', result)
        self.assertIn('self-signed certificate', result)

    def test_cycle_through_exception_args_is_bounded(self):
        certificate_error = ssl.SSLCertVerificationError(1, 'certificate verify failed')
        certificate_error.verify_code = 20
        certificate_error.verify_message = 'unable to get local issuer certificate'
        core_error = httpcore.ConnectError(certificate_error)
        error = httpx.ConnectError('TLS connection failed')
        error.__cause__ = core_error
        core_error.args = (certificate_error, error)

        result = downloader.sanitized_error(error)

        self.assertIn('verify_code=20', result)
        self.assertIn('unable to get local issuer certificate', result)
        self.assertLess(len(result), 3000)

    def test_dns_and_tcp_errors_have_distinct_actionable_diagnostics(self):
        failures = (
            (socket.gaierror(socket.EAI_NONAME, 'Name or service not known'),
             ('gaierror', f'errno={socket.EAI_NONAME}', 'Name or service not known')),
            (ConnectionRefusedError(errno.ECONNREFUSED, 'Connection refused'),
             ('ConnectionRefusedError', f'errno={errno.ECONNREFUSED}', 'Connection refused')),
        )
        for cause, markers in failures:
            with self.subTest(cause=type(cause).__name__):
                error = httpx.ConnectError('Could not connect')
                error.__cause__ = cause
                result = downloader.sanitized_error(error)
                for marker in markers:
                    self.assertIn(marker, result)

    def test_windows_error_code_is_preserved_on_any_test_platform(self):
        cause = OSError('No connection could be made because the target actively refused it')
        cause.winerror = 10061
        error = httpx.ConnectError('Connection failed')
        error.__cause__ = cause

        result = downloader.sanitized_error(error)

        self.assertIn('winerror=10061', result)
        self.assertIn('target actively refused it', result)

    def test_transport_message_is_available_without_lower_level_exception(self):
        cases = (
            (httpx.ConnectError, 'CERTIFICATE_VERIFY_FAILED: certificate has expired'),
            (httpx.ReadError, 'peer closed connection without sending complete body'),
            (httpx.ReadTimeout, 'timed out while reading response'),
            (httpx.ProxyError, 'proxy returned 407 Proxy Authentication Required'),
        )
        for exception_type, message in cases:
            with self.subTest(exception_type=exception_type.__name__):
                result = downloader.sanitized_error(exception_type(message))
                self.assertIn(exception_type.__name__, result)
                self.assertIn(message, result)

    def test_context_is_used_only_when_explicit_cause_is_absent(self):
        implicit = httpx.ConnectError('Connection failed')
        implicit.__context__ = socket.gaierror(socket.EAI_NONAME, 'DNS lookup failed')
        explicit = httpx.ConnectError('Connection failed')
        explicit.__cause__ = ConnectionRefusedError(errno.ECONNREFUSED, 'Connection refused')
        explicit.__context__ = socket.gaierror(socket.EAI_NONAME, 'unrelated context detail')

        implicit_result = downloader.sanitized_error(implicit)
        explicit_result = downloader.sanitized_error(explicit)

        self.assertIn('DNS lookup failed', implicit_result)
        self.assertIn('Connection refused', explicit_result)
        self.assertNotIn('unrelated context detail', explicit_result)

    def test_exception_group_includes_each_network_failure_and_handles_cycles(self):
        dns = socket.gaierror(socket.EAI_NONAME, 'DNS lookup failed')
        connection = ConnectionRefusedError(errno.ECONNREFUSED, 'Connection refused')
        group = ExceptionGroup('multiple connection attempts', [dns, connection])
        error = httpx.ConnectError('All connection attempts failed')
        error.__cause__ = group
        dns.__context__ = error
        connection.__cause__ = dns

        result = downloader.sanitized_error(error)

        for marker in ('ExceptionGroup', 'gaierror', 'ConnectionRefusedError',
                       'DNS lookup failed', 'Connection refused'):
            self.assertIn(marker, result)
        self.assertLess(len(result), 3000)

    def test_filesystem_errors_do_not_disclose_messages_or_paths(self):
        error = OSError(errno.ENOSPC, 'sensitive file content', '/private/account/file.txt')

        result = downloader.sanitized_error(error)

        self.assertIn(f'errno={errno.ENOSPC}', result)
        self.assertNotIn('sensitive file content', result)
        self.assertNotIn('/private/account/file.txt', result)

    def test_request_secrets_are_redacted_in_causes_and_encoded_echoes(self):
        secrets = ('synthetic/auth+credential', 'synthetic-cookie-value',
                   'synthetic-query/key', 'synthetic/client secret+value',
                   'synthetic-subject+token@example.invalid', 'synthetic-api-key')
        request = httpx.Request(
            'POST', 'https://oauth.example.invalid/token',
            params={'token': secrets[2]},
            headers={'Authorization': f'Bearer {secrets[0]}',
                     'Cookie': f'session={secrets[1]}', 'X-API-Key': secrets[5]},
            data={'client_secret': secrets[3], 'subject_token': secrets[4]},
        )
        echoes = [representation for secret in secrets
                  for representation in (secret, quote(secret, safe=''), quote_plus(secret))]
        cause = OSError('Connection refused; echoes: ' + ' '.join(echoes))
        error = httpx.ConnectError('Transport failure', request=request)
        error.__cause__ = cause

        result = downloader.sanitized_error(error)

        self.assertIn('Connection refused', result)
        for secret in echoes:
            self.assertNotIn(secret, result)

    def test_json_body_and_environment_secrets_are_redacted_without_request_fields(self):
        body_secret = 'synthetic/json+secret'
        environment_secret = 'synthetic/env secret+value'
        request = httpx.Request('POST', 'https://oauth.example.invalid/token', json={
            'nested': [{'client_secret': body_secret}],
        })
        echoes = [representation for secret in (body_secret, environment_secret)
                  for representation in (secret, quote(secret, safe=''), quote_plus(secret))]
        error = httpx.ConnectError('TLS handshake failed; echo: ' + ' '.join(echoes), request=request)
        cause = ssl.SSLCertVerificationError(1, 'certificate verify failed')
        cause.verify_code = 20
        cause.verify_message = 'unable to get issuer; echo: ' + ' '.join(echoes)
        error.__cause__ = cause

        with isolated_environment({'CLIENT_SECRET': environment_secret}):
            result = downloader.sanitized_error(error)

        self.assertIn('TLS handshake failed', result)
        self.assertIn('unable to get issuer', result)
        for secret in echoes:
            self.assertNotIn(secret, result)

    def test_unstructured_urls_and_sensitive_fields_are_redacted(self):
        messages = (
            ('Unable to connect to http://proxy-user:proxy-password@proxy.invalid:8080/path?token=url-secret',
             ('proxy-user', 'proxy-password', 'proxy.invalid', 'url-secret')),
            ('Authorization: Basic synthetic-basic-credential\nCERTIFICATE_VERIFY_FAILED',
             ('synthetic-basic-credential',)),
            ('Authorization: Token synthetic-custom-token\nCERTIFICATE_VERIFY_FAILED',
             ('synthetic-custom-token',)),
            ("{'Cookie': 'session=synthetic-quoted-cookie'}; CERTIFICATE_VERIFY_FAILED",
             ('synthetic-quoted-cookie',)),
            ('Cookie: session=synthetic-session; tracking=synthetic-tracking\nCERTIFICATE_VERIFY_FAILED',
             ('synthetic-session', 'synthetic-tracking')),
            ('client_secret="synthetic-secret" access_token=synthetic-token password=synthetic-password',
             ('synthetic-secret', 'synthetic-token', 'synthetic-password')),
        )
        for message, secrets in messages:
            with self.subTest(message=message):
                result = downloader.sanitized_error(httpx.ConnectError('network failure; ' + message))
                self.assertIn('network failure', result)
                for secret in secrets:
                    self.assertNotIn(secret, result)
                if 'CERTIFICATE_VERIFY_FAILED' in message:
                    self.assertIn('CERTIFICATE_VERIFY_FAILED', result)

    def test_transport_and_certificate_messages_cannot_inject_log_lines(self):
        cause = ssl.SSLCertVerificationError(1, 'certificate verify failed\nforged log\r\t\x00\x1b[31m\x7f')
        cause.verify_code = 62
        cause.verify_message = 'hostname mismatch\nforged certificate log'
        error = httpx.ConnectError('connection failure\nforged transport log\x85\u2028\u2029\u202e\u2066')
        error.__cause__ = cause

        result = downloader.sanitized_error(error)

        self.assertIn('hostname mismatch', result)
        self.assertIn('certificate verify failed', result)
        self.assertIn('connection failure', result)
        self.assertFalse(any(unicodedata.category(character) in {'Cc', 'Cf', 'Zl', 'Zp'}
                             for character in result))
        self.assertEqual(len(result.splitlines()), 1)

    def test_known_secret_with_lowercase_percent_escapes_is_redacted(self):
        secret = 'synthetic/private+credential'
        request = httpx.Request('GET', 'https://example.invalid', headers={
            'Authorization': f'Bearer {secret}',
        })
        encoded = quote(secret, safe='').replace('%2F', '%2f').replace('%2B', '%2b')

        result = downloader.sanitized_error(httpx.ConnectError(
            'certificate verify failed; echo: ' + encoded, request=request,
        ))

        self.assertIn('certificate verify failed', result)
        self.assertNotIn(encoded, result)
        self.assertNotIn(secret, result)

    def test_network_sibling_does_not_expose_unrelated_filesystem_error(self):
        group = ExceptionGroup('multiple operations failed', [
            httpx.ConnectError('connection refused'),
            OSError(errno.EACCES, 'synthetic private file detail', '/private/account/file.txt'),
        ])

        result = downloader.sanitized_error(group)

        self.assertIn('connection refused', result)
        self.assertIn(f'errno={errno.EACCES}', result)
        self.assertNotIn('synthetic private file detail', result)
        self.assertNotIn('/private/account/file.txt', result)

    def test_http_error_response_remains_complete_after_network_diagnostic_changes(self):
        request = httpx.Request('POST', 'https://oauth.example.invalid/token')
        response = httpx.Response(400, request=request, headers={'X-Request-ID': 'synthetic-lookup-42'}, json={
            'error': 'invalid_grant', 'error_description': 'User does not exist',
        })
        error = httpx.HTTPStatusError('HTTP request failed', request=request, response=response)

        result = downloader.sanitized_error(error)

        for marker in ('HTTP 400', 'invalid_grant', 'User does not exist', 'synthetic-lookup-42'):
            self.assertIn(marker, result)
