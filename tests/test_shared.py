import asyncio
import csv
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from yadisk.sessions.async_httpx_session import AsyncHTTPXSession

import downloader
from test_retries import BrokenStream


class SharedCsvTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'disks.csv'

    def test_headers_delimiters_bom_and_case_sensitive_deduplication(self):
        cases = [
            '\ufeffVDHASH;name\n Ab_C-1 ;First\nAb_C-1;Duplicate\nab_C-1;Different\n',
            'name,vd_hash\nFirst,Ab_C-1\nDuplicate,Ab_C-1\nDifferent,ab_C-1\n',
            '\nAb_C-1\n Ab_C-1 \n\nab_C-1\n',
        ]
        for content in cases:
            with self.subTest(content=content):
                self.path.write_text(content, encoding='utf-8')
                self.assertEqual(downloader.read_shared_disks_csv(self.path), ['Ab_C-1', 'ab_C-1'])

    def test_invalid_input_is_rejected_before_any_download(self):
        for content in ['', 'VDHASH\n', 'VDHASH,vd_hash\nA,B\n', 'name,VDHASH\nMissing\n',
                        'VDHASH\nGood\n../bad\n', 'VDHASH\nGood\nhttps://disk.yandex.ru/vd/id/\n',
                        'VDHASH\n"unterminated\n']:
            with self.subTest(content=content):
                self.path.write_text(content)
                with (patch.object(downloader, 'download_shared_disks') as download,
                      patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit) as error):
                    downloader.cli(['--email', 'reader@example.invalid', '--vdhash-csv', str(self.path)])
                self.assertEqual(error.exception.code, 2)
                download.assert_not_called()

    def test_single_and_csv_cli_dispatch_without_prompting(self):
        self.path.write_text('VDHASH\nFirstDisk\nSecondDisk\n')
        for flags, expected in [(['--vdhash', 'FirstDisk'], ['FirstDisk']),
                                (['--vdhash-csv', str(self.path)], ['FirstDisk', 'SecondDisk'])]:
            with (patch.object(downloader, 'download_shared_disks', new=AsyncMock(return_value=True)) as download,
                  patch.object(downloader, 'terminal_menu', side_effect=AssertionError('Must not prompt'))):
                self.assertEqual(downloader.cli(['--email', 'reader@example.invalid', '--status-interval', '3', *flags]), 0)
                download.assert_awaited_once_with('reader@example.invalid', expected, status_interval=3)

    def test_cli_requires_reader_and_rejects_conflicting_modes(self):
        for args in [['--vdhash', 'Disk'], ['--vdhash-csv', 'disks.csv'],
                     ['--email', 'a@example.invalid', '--csv', 'users.csv'],
                     ['--email', 'a@example.invalid', '--vdhash', 'Disk', '--vdhash-csv', 'disks.csv']]:
            with self.subTest(args=args), patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
                downloader.cli(args)
            self.assertEqual(error.exception.code, 2)


class SharedDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_cwd = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, self.old_cwd)
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.object(downloader, 'load_dotenv'))
        self.enterContext(patch.dict(os.environ, {'CLIENT_ID': 'test-id', 'CLIENT_SECRET': 'test-secret'}, clear=True))
        self.token = self.enterContext(patch.object(downloader, 'get_service_token', return_value='test-token'))
        self.logs = self.enterContext(self.assertLogs('Downloader'))
        self.sleep = self.enterContext(patch.object(downloader.asyncio, 'sleep', new_callable=AsyncMock))
        self.enterContext(patch.object(downloader, 'uniform', return_value=0))
        self.requests = []
        self.sessions = []

    def transport(self, handler):
        def factory(**kwargs):
            def capture(request):
                self.requests.append(request)
                self.assertEqual(request.headers.get('Authorization'), 'OAuth test-token')
                self.assertEqual(request.method, 'GET')
                if '/operations/' in request.url.path:
                    return httpx.Response(404, json={'error': 'DiskOperationNotFoundError'})
                if request.url.path == '/v1/disk/virtual-disks':
                    return httpx.Response(200, json={'used_space': 10, 'total_space': 100})
                return handler(request)
            session = AsyncHTTPXSession(transport=httpx.MockTransport(capture), **kwargs)
            self.sessions.append(session)
            return session
        return patch.object(downloader, 'AsyncHTTPXSession', side_effect=factory)

    def page(self, items, total=None):
        return httpx.Response(200, json={'type': 'dir', '_embedded': {
            'items': [{'type': kind, 'path': path} for kind, path in items],
            'total': len(items) if total is None else total,
        }})

    async def test_full_paginated_tree_both_path_formats_and_download_oauth(self):
        for prefix in ['vd:DiskHash:disk:', 'vd:/DiskHash/disk']:
            with self.subTest(prefix=prefix):
                self.requests.clear()

                def handler(request):
                    path = request.url.params.get('path')
                    if request.url.path.endswith('/resources/download'):
                        self.assertTrue(path.startswith('vd:DiskHash:disk:/'))
                        return httpx.Response(200, json={'href': 'https://download.example.invalid/file?token=not-for-logs'})
                    if request.url.host == 'download.example.invalid':
                        return httpx.Response(200, content=b'complete')
                    self.assertEqual(request.url.path, '/v1/disk/virtual-disks/resources')
                    self.assertEqual(request.url.params.get('sort'), 'path')
                    if path == 'vd:DiskHash:disk:/':
                        if request.url.params['offset'] == '0':
                            return self.page([('dir', prefix + '/Документы . ')], total=3)
                        return self.page([('file', prefix + '/CON.txt'), ('dir', prefix + '/Файлы Мессенджера')], total=3)
                    self.assertEqual(path, 'vd:DiskHash:disk:/Документы . ')
                    return self.page([('file', prefix + '/Документы . /отчёт?.txt')])

                with self.transport(handler):
                    self.assertTrue(await downloader.download_shared_disks('reader@example.invalid', ['DiskHash']))
                root = Path('shared/DiskHash')
                self.assertEqual((root / '_CON.txt').read_bytes(), b'complete')
                self.assertEqual((root / 'Документы/отчёт_.txt').read_bytes(), b'complete')
                self.assertFalse((root / 'Файлы Мессенджера').exists())
                self.assertFalse(list(root.rglob('*.part')))
                self.assertTrue(all(session.httpx_session.is_closed for session in self.sessions))
                self.assertTrue(all('/v1/disk/resources' not in request.url.path for request in self.requests))
        self.token.assert_awaited_with('test-id', 'test-secret', 'reader@example.invalid')
        self.assertNotIn('not-for-logs', '\n'.join(self.logs.output))

    async def test_retry_listing_link_and_broken_stream(self):
        listing_offsets = []
        links = {}
        stream = BrokenStream(httpx.RemoteProtocolError('not-for-logs'))

        def handler(request):
            if request.url.path.endswith('/resources/download'):
                file = request.url.params['path'].rsplit('/', 1)[-1]
                links[file] = links.get(file, 0) + 1
                if file == '0.txt' and links[file] == 1:
                    return httpx.Response(429, headers={'Retry-After': '10'})
                return httpx.Response(200, json={'href': f'https://download.example.invalid/file?file={file}&attempt={links[file]}'})
            if request.url.host == 'download.example.invalid':
                broken = request.url.params['file'] == '0.txt' and request.url.params['attempt'] == '2'
                return httpx.Response(200, stream=stream) if broken else httpx.Response(200, content=b'final')
            offset = int(request.url.params['offset'])
            listing_offsets.append(offset)
            if listing_offsets == [0, 1]:
                return httpx.Response(503)
            return self.page([('file', f'vd:RetryDisk:disk:/{offset}.txt')], total=2)

        with self.transport(handler):
            self.assertTrue(await downloader.download_shared_disks('reader@example.invalid', ['RetryDisk']))
        self.assertEqual(listing_offsets, [0, 1, 0, 1, 0, 1])
        self.assertEqual(links, {'0.txt': 3, '1.txt': 1})
        self.assertEqual(sorted(call.args[0] for call in self.sleep.await_args_list), [2, 4, 10])
        self.assertTrue(stream.closed)
        self.assertEqual({p.name: p.read_bytes() for p in Path('shared/RetryDisk').iterdir()},
                         {'0.txt': b'final', '1.txt': b'final'})

    async def test_denied_or_missing_disk_fails_and_batch_continues(self):
        for status in [403, 404]:
            with self.subTest(status=status):
                def handler(request):
                    if 'Denied' in request.url.params.get('path', ''):
                        return httpx.Response(status)
                    return self.page([])
                with self.transport(handler):
                    self.assertFalse(await downloader.download_shared_disks('reader@example.invalid', ['Denied', 'Good']))
                self.assertFalse(Path('shared/Denied').exists())
                self.assertTrue(Path('shared/Good').is_dir())
        self.sleep.assert_not_awaited()

    async def test_malformed_listing_and_foreign_disk_paths_cannot_report_success(self):
        payloads = [{}, {'type': 'dir', '_embedded': {'items': [], 'total': 1}},
                    {'type': 'dir', '_embedded': {'items': [{'type': 'file', 'path': 'vd:Other:disk:/file'}], 'total': 1}},
                    {'type': 'dir', '_embedded': {'items': [{'type': 'file'}], 'total': 1}}]
        for payload in payloads:
            with self.subTest(payload=payload), self.transport(lambda request: httpx.Response(200, json=payload)):
                self.assertFalse(await downloader.download_shared_disks('reader@example.invalid', ['Broken']))
        self.assertFalse(Path('shared/Broken').exists())
        self.sleep.assert_not_awaited()

    async def test_hashes_differing_only_by_case_get_separate_local_folders(self):
        with self.transport(lambda request: self.page([])):
            self.assertTrue(await downloader.download_shared_disks('reader@example.invalid', ['Disk', 'disk']))
        folders = [path.name.casefold() for path in Path('shared').iterdir()]
        self.assertEqual(len(set(folders)), 2)

    async def test_shared_download_exhausts_exactly_five_retries(self):
        count = 0
        def handler(request):
            nonlocal count
            count += 1
            return httpx.Response(504)
        with self.transport(handler):
            self.assertFalse(await downloader.download_shared_disks('reader@example.invalid', ['Fail']))
        self.assertEqual(count, 6)
        self.assertEqual([call.args[0] for call in self.sleep.await_args_list], [2, 4, 8, 16, 32])
