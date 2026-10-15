import asyncio
import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import downloader
from environment_fixtures import isolated_environment


HTTPX_CLIENT = httpx.AsyncClient
CERTIFICATE_WARNING = 'Проверка сертификатов отключена'


class FileStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'complete '
        yield b'file'


class TLSConnectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        previous = Path.cwd()
        os.chdir(temporary.name)
        self.addCleanup(os.chdir, previous)
        self.enterContext(isolated_environment())
        os.environ.pop('VERIFY_TLS', None)
        self.enterContext(patch.object(downloader.log_handler, 'stream', io.StringIO()))
        self.requests = []
        self.clients = []
        self.verification = []
        self.token_count = 0
        self.fail_listing = False
        self.enterContext(patch.object(downloader.httpx, 'AsyncClient', side_effect=self.client))

    def client(self, *args, **kwargs):
        verify = kwargs.get('verify', True)
        index = len(self.clients)

        def respond(request):
            self.requests.append((request, verify, index))
            return self.respond(request)

        client = HTTPX_CLIENT(*args, transport=httpx.MockTransport(respond), **kwargs)
        self.clients.append(client)
        self.verification.append(verify)
        return client

    def respond(self, request):
        host, path = request.url.host, request.url.path
        if host == 'oauth.yandex.ru':
            self.token_count += 1
            return httpx.Response(200, json={'access_token': f'test-token-{self.token_count}',
                                            'expires_in': 3600})
        if host == 'download.example.invalid':
            return httpx.Response(302, headers={'Location': 'https://cdn.example.invalid/file'})
        if host == 'cdn.example.invalid':
            self.assertNotIn('Authorization', request.headers)
            return httpx.Response(200, stream=FileStream())
        self.assertEqual(host, 'cloud-api.yandex.net')
        self.assertTrue(request.headers.get('Authorization', '').startswith('OAuth test-token-'))
        if '/operations/' in path:
            return httpx.Response(404, json={'error': 'DiskOperationNotFoundError'})
        if path.endswith('/disk') or path.endswith('/virtual-disks'):
            return httpx.Response(200, json={'used_space': 13})
        if path.endswith('/download'):
            return httpx.Response(200, json={'href': 'https://download.example.invalid/file'})
        self.assertTrue(path.endswith('/resources'), str(request.url))
        if self.fail_listing:
            return httpx.Response(500, json={'error': 'InternalServerError'})
        remote = request.url.params['path']
        prefix = remote.split(':disk:', 1)[0] + ':disk:' if remote.startswith('vd:') else 'disk:'
        body = b'complete file'
        return httpx.Response(200, json={'type': 'dir', '_embedded': {
            'offset': 0, 'limit': 500, 'total': 1,
            'items': [{'type': 'file', 'path': prefix + '/sample.txt', 'size': len(body),
                       'sha256': hashlib.sha256(body).hexdigest()}],
        }})

    def settings(self, verification, directory='downloads'):
        text = ("CLIENT_ID='test-id'\nCLIENT_SECRET='test-secret'\n"
                f"DOWNLOAD_DIR='{directory}'\nMAX_RETRIES='0'\n")
        if verification is not None:
            text += f"VERIFY_TLS='{verification}'\n"
        Path('.env').write_text(text, encoding='utf-8')
        for name in ('CLIENT_ID', 'CLIENT_SECRET', 'DOWNLOAD_DIR', 'MAX_RETRIES', 'VERIFY_TLS'):
            os.environ.pop(name, None)

    def test_connection_setting_reaches_oauth_api_streams_and_redirects_in_all_modes(self):
        Path('users.csv').write_text('email\nreader@example.invalid\nsecond@example.invalid\n', encoding='utf-8')
        Path('disks.csv').write_text('VDHASH\nFirstHash\nSecondHash\n', encoding='utf-8')
        modes = [
            (['--email', 'reader@example.invalid'], ['reader@example.invalid']),
            (['--csv', 'users.csv'], ['reader@example.invalid', 'second@example.invalid']),
            (['--email', 'reader@example.invalid', '--vdhash', 'FirstHash'], ['shared/FirstHash']),
            (['--email', 'reader@example.invalid', '--vdhash-csv', 'disks.csv'],
             ['shared/FirstHash', 'shared/SecondHash']),
        ]
        for setting in (None, 'true', 'false'):
            expected = setting != 'false'
            for number, (arguments, destinations) in enumerate(modes):
                with self.subTest(setting=setting, arguments=arguments):
                    directory = f'mode-{setting}-{number}'
                    self.settings(setting, directory)
                    self.requests.clear()
                    first_client = len(self.clients)
                    with self.assertLogs('Downloader') as logs:
                        self.assertEqual(downloader.cli(arguments), 0)
                    self.assertEqual(self.verification[first_client:], [expected] * (2 * len(destinations)))
                    self.assertTrue(all(client.is_closed for client in self.clients))
                    self.assertTrue(all(verify is expected for _, verify, _ in self.requests))
                    warnings = [record for record in logs.records if CERTIFICATE_WARNING in record.getMessage()]
                    self.assertEqual(len(warnings), int(not expected))
                    for record in warnings:
                        self.assertEqual(record.levelname, 'WARNING')
                    for destination in destinations:
                        self.assertEqual((Path(directory) / destination / 'sample.txt').read_bytes(), b'complete file')
                    for index in range(first_client, len(self.clients)):
                        requests = [request for request, _, owner in self.requests if owner == index]
                        hosts = {request.url.host for request in requests}
                        if 'oauth.yandex.ru' in hosts:
                            self.assertEqual(hosts, {'oauth.yandex.ru'})
                        else:
                            self.assertEqual(hosts, {'cloud-api.yandex.net', 'download.example.invalid',
                                                     'cdn.example.invalid'})
                            self.assertTrue(any(request.url.path.endswith('/resources/download') for request in requests))

    def test_renewed_token_uses_same_connection_setting_and_closes_both_clients(self):
        clock = [1000.0]

        async def renew():
            manager = downloader.ServiceTokenManager('test-id', 'test-secret', 'reader@example.invalid')
            first = await manager.get_token()
            clock[0] += 3300
            second = await manager.get_token()
            self.assertNotEqual(first, second)

        with (patch.dict(os.environ, {'VERIFY_TLS': 'false', 'MAX_RETRIES': '0'}),
              patch.object(downloader, 'monotonic', side_effect=lambda: clock[0]),
              self.assertLogs('Downloader')):
            asyncio.run(renew())
        self.assertEqual(self.token_count, 2)
        self.assertEqual(self.verification, [False, False])
        self.assertTrue(all(client.is_closed for client in self.clients))

    def test_oauth_connection_failure_closes_client(self):
        def fail(request):
            raise httpx.ConnectError('synthetic connection failure', request=request)

        with (patch.dict(os.environ, {'VERIFY_TLS': 'false', 'MAX_RETRIES': '0'}),
              patch.object(self, 'respond', side_effect=fail), self.assertRaises(httpx.ConnectError)):
            asyncio.run(downloader.get_service_token('test-id', 'test-secret', 'reader@example.invalid'))
        self.assertEqual(self.verification, [False])
        self.assertTrue(self.clients[0].is_closed)

    def test_failed_listing_closes_session_and_warns_once_per_batch(self):
        self.settings('false')
        self.fail_listing = True
        Path('users.csv').write_text('email\nreader@example.invalid\nsecond@example.invalid\n', encoding='utf-8')
        with self.assertLogs('Downloader') as logs:
            self.assertEqual(downloader.cli(['--csv', 'users.csv']), 1)
        self.assertEqual(self.verification, [False] * 4)
        self.assertTrue(all(client.is_closed for client in self.clients))
        self.assertEqual(sum(CERTIFICATE_WARNING in record.getMessage() for record in logs.records), 1)


if __name__ == '__main__':
    unittest.main()
