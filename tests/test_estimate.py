import asyncio
import csv
import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from yadisk.sessions.async_httpx_session import AsyncHTTPXSession

import downloader
from environment_fixtures import isolated_environment
from token_fixtures import make_token


class VolumeEstimateTests(unittest.TestCase):
    email = 'reader@example.invalid'
    cutoff = '2026-10-01T00:00:01+03:00'
    columns = [
        'Тип диска', 'Email', 'VDHASH', 'Дата изменения от', 'Файлов к загрузке',
        'Объем к загрузке, байт', 'Уже скачано, файлов', 'Пропущено по дате, файлов',
        'Статус', 'Ошибок', 'Описание',
    ]

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        previous_cwd = Path.cwd()
        os.chdir(temporary.name)
        self.addCleanup(temporary.cleanup)
        self.addCleanup(os.chdir, previous_cwd)
        self.enterContext(isolated_environment())
        self.console = io.StringIO()
        self.enterContext(patch.object(downloader.log_handler, 'stream', self.console))
        self.enterContext(patch('sys.stdout', new_callable=io.StringIO))
        self.logs = self.enterContext(self.assertLogs('Downloader'))
        self.token = self.enterContext(patch.object(
            downloader, 'get_service_token', side_effect=self.service_token,
        ))
        self.enterContext(patch.object(downloader, 'AsyncHTTPXSession', side_effect=self.session))
        self.tree = {'/': []}
        self.contents = {}
        self.requests = []
        self.sessions = []
        self.broken_paths = set()
        self.failed_emails = set()
        self.failed_disks = set()
        self.allow_downloads = False
        self.settings()

    def settings(self, date='2026-10-01', directory='downloads'):
        Path('.env').write_text(
            "CLIENT_ID='test-id'\nCLIENT_SECRET='test-secret'\nMAX_RETRIES='0'\n"
            f"MODIFIED_SINCE='{date}'\nDOWNLOAD_DIR='{directory}'\n",
            encoding='utf-8',
        )
        for name in ('MODIFIED_SINCE', 'DOWNLOAD_DIR'):
            os.environ.pop(name, None)

    async def service_token(self, client_id, client_secret, email):
        if email in self.failed_emails:
            request = httpx.Request('POST', 'https://oauth.yandex.ru/token')
            response = httpx.Response(400, request=request, json={
                'error': 'invalid_grant', 'error_description': 'User does not exist',
            })
            response.raise_for_status()
        return make_token(email)

    def session(self, **kwargs):
        session = AsyncHTTPXSession(transport=httpx.MockTransport(self.respond), **kwargs)
        self.sessions.append(session)
        return session

    @staticmethod
    def relative_path(value):
        return value.split(':disk:', 1)[1] if value.startswith('vd:') else value.removeprefix('disk:')

    def file(self, name, body=b'complete', modified='2026-10-02T00:00:00+03:00'):
        path = '/' + name
        self.contents[path] = body
        item = {
            'type': 'file', 'path': 'disk:' + path, 'size': len(body),
            'sha256': hashlib.sha256(body).hexdigest(),
        }
        if modified is not None:
            item['modified'] = modified
        return item

    def respond(self, request):
        self.requests.append(request)
        if request.url.host == 'download.example.invalid':
            self.assertTrue(self.allow_downloads, 'Оценка не должна скачивать содержимое файлов')
            return httpx.Response(200, content=self.contents[request.url.params['file']])
        self.assertTrue(request.headers.get('Authorization', '').startswith('OAuth '))
        path = request.url.path
        if '/operations/' in path:
            return httpx.Response(404, json={'error': 'DiskOperationNotFoundError'})
        if path.endswith('/disk') or path.endswith('/virtual-disks'):
            return httpx.Response(200, json={'used_space': 99999999})
        remote = request.url.params['path']
        relative = self.relative_path(remote)
        if path.endswith('/download'):
            self.assertTrue(self.allow_downloads, 'Оценка не должна запрашивать ссылку на скачивание')
            href = httpx.URL('https://download.example.invalid/file', params={'file': relative})
            return httpx.Response(200, json={'href': str(href)})
        self.assertTrue(path.endswith('/resources'), str(request.url))
        disk = remote.split(':', 2)[1] if remote.startswith('vd:') else None
        if relative in self.broken_paths or disk in self.failed_disks:
            return httpx.Response(503, json={
                'error': 'DiskUnavailableError', 'description': 'Directory unavailable',
            })
        prefix = f'vd:{disk}:disk:' if disk is not None else 'disk:'
        items = [dict(item, path=prefix + item['path'].removeprefix('disk:'))
                 for item in self.tree[relative]]
        return httpx.Response(200, json={'type': 'dir', '_embedded': {
            'offset': 0, 'limit': 500, 'total': len(items), 'items': items,
        }})

    def run_cli(self, args, *, estimate=True):
        result = downloader.cli((['--estimate'] if estimate else []) + args)
        self.assertTrue(all(session.httpx_session.is_closed for session in self.sessions))
        return result

    def direct(self, vdhash=None):
        downloader.load_credentials()
        root = Path('downloads/shared/FirstHash' if vdhash is not None else 'downloads')
        result = asyncio.run(downloader.estimate_disk(self.email, root, vdhash=vdhash))
        self.assertTrue(all(session.httpx_session.is_closed for session in self.sessions))
        return result

    def reports(self):
        return list(Path('estimates').glob('*.csv'))

    def read_report(self):
        report, = self.reports()
        self.assertRegex(report.name, r'^estimate_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{6}\.csv$')
        self.assertTrue(report.read_bytes().startswith(b'\xef\xbb\xbf'))
        with report.open(encoding='utf-8-sig', newline='') as source:
            reader = csv.DictReader(source, delimiter=';')
            self.assertEqual(reader.fieldnames, self.columns)
            rows = list(reader)
        self.assertFalse(Path('downloads').exists())
        return rows

    @staticmethod
    def snapshot(root):
        return {str(path.relative_to(root)): (path.is_dir(), None if path.is_dir() else path.read_bytes(),
                                            path.stat().st_mtime_ns)
                for path in [root, *root.rglob('*')]}

    def test_date_boundary_and_old_folder_are_counted_for_both_disk_types(self):
        self.tree['/'] = [
            self.file('before.txt', b'old', '2026-10-01T00:00:00+03:00'),
            self.file('equal.txt', b'12', self.cutoff),
            self.file('equal-utc.txt', b'123', '2026-09-30T21:00:01Z'),
            self.file('after.txt', b'1234'),
            {'type': 'dir', 'path': 'disk:/old-folder', 'modified': '2020-01-01T00:00:00+03:00'},
            {'type': 'dir', 'path': 'disk:/Файлы Мессенджера'},
        ]
        self.tree['/old-folder'] = [self.file('old-folder/new.txt', b'12345')]
        for disk in (None, 'FirstHash'):
            with self.subTest(vdhash=disk):
                self.requests.clear()
                result = self.direct(disk)
                self.assertTrue(result.complete)
                self.assertEqual((result.files, result.bytes, result.skipped_by_date), (4, 14, 1))
                self.assertEqual(result.email, self.email)
                self.assertEqual(result.vdhash, disk)
                self.assertEqual(result.cutoff.isoformat(), self.cutoff)
                paths = [self.relative_path(request.url.params['path']) for request in self.requests
                         if 'path' in request.url.params]
                self.assertIn('/old-folder', paths)
                self.assertNotIn('/Файлы Мессенджера', paths)
                self.assertFalse(Path('downloads').exists())
                self.assertFalse(Path('.disk-downloader').exists())

    def test_single_estimate_prints_result_without_csv_or_download_directory(self):
        self.tree['/'] = [self.file('new.txt', b'12345')]
        for selector in (['--email', self.email], ['--email', self.email, '--vdhash', 'FirstHash']):
            with self.subTest(selector=selector):
                start = len(self.logs.output)
                self.assertEqual(self.run_cli(selector), 0)
                messages = '\n'.join(self.logs.output[start:]).lower()
                self.assertIn('оцен', messages)
                self.assertIn(self.email, messages)
                self.assertIn('файлов к загрузке: 1', messages)
                self.assertIn('(5 байт)', messages)
                self.assertFalse(self.reports())
                self.assertFalse(Path('downloads').exists())

    def test_empty_and_fully_filtered_disks_return_zero(self):
        for files, skipped in (([], 0), ([self.file('old.txt', modified='2020-01-01T00:00:00Z')], 1)):
            with self.subTest(skipped=skipped):
                self.tree['/'] = files
                result = self.direct()
                self.assertTrue(result.complete)
                self.assertEqual((result.files, result.bytes, result.skipped_by_date), (0, 0, skipped))
                self.assertFalse(Path('downloads').exists())

    def test_disabled_filter_accepts_missing_modified_and_zero_byte_file(self):
        self.settings(date='')
        self.tree['/'] = [self.file('unknown-date.txt', b'123', modified=None), self.file('empty.txt', b'')]
        result = self.direct()
        self.assertTrue(result.complete)
        self.assertIsNone(result.cutoff)
        self.assertEqual((result.files, result.bytes), (2, 3))

    def test_missing_modified_is_incomplete_and_good_files_still_count(self):
        self.tree['/'] = [self.file('unknown-date.txt', modified=None), self.file('good.txt', b'123')]
        result = self.direct()
        self.assertFalse(result.complete)
        self.assertEqual((result.files, result.bytes, result.errors), (1, 3, 1))
        self.assertEqual(self.run_cli(['--email', self.email]), 1)

    def test_invalid_sizes_cannot_produce_a_complete_total(self):
        for size in (None, -1, True, '7'):
            with self.subTest(size=size):
                item = SimpleNamespace(
                    path='disk:/bad-size.txt', type='file', size=size,
                    modified=downloader.parse_modified_since('2026-10-02'),
                )
                listing = downloader.DiskListing(files=[item])
                with patch.object(downloader, 'list_resources', new=AsyncMock(return_value=listing)):
                    result = self.direct()
                self.assertFalse(result.complete)
                self.assertEqual((result.files, result.bytes, result.errors), (1, 0, 1))

    def test_listing_error_marks_partial_total_and_preserves_successful_files(self):
        self.tree['/'] = [self.file('good.txt', b'12345'), {'type': 'dir', 'path': 'disk:/broken'}]
        self.broken_paths.add('/broken')
        for disk in (None, 'FirstHash'):
            with self.subTest(vdhash=disk):
                result = self.direct(disk)
                self.assertFalse(result.complete)
                self.assertEqual((result.files, result.bytes, result.errors), (1, 5, 1))

    def test_batch_with_one_row_still_writes_csv_for_both_disk_types(self):
        self.tree['/'] = [self.file('new.txt', b'12345')]
        Path('users.csv').write_text(f'email\n{self.email}\n', encoding='utf-8')
        Path('disks.csv').write_text('VDHASH\nFirstHash\n', encoding='utf-8')
        for selector, expected_hash in ((['--csv', 'users.csv'], ''),
                                        (['--email', self.email, '--vdhash-csv', 'disks.csv'], 'FirstHash')):
            with self.subTest(selector=selector):
                for report in self.reports():
                    report.unlink()
                self.assertEqual(self.run_cli(selector), 0)
                row, = self.read_report()
                self.assertEqual(row['Email'], self.email)
                self.assertEqual(row['VDHASH'], expected_hash)
                self.assertEqual(row['Дата изменения от'], self.cutoff)
                self.assertEqual(row['Файлов к загрузке'], '1')
                self.assertEqual(row['Объем к загрузке, байт'], '5')
                self.assertEqual(row['Статус'], 'готово')
                self.assertEqual(row['Ошибок'], '0')

    def test_personal_batch_keeps_failed_authorization_and_continues(self):
        self.tree['/'] = [self.file('new.txt', b'12345')]
        self.failed_emails.add('missing@example.invalid')
        Path('users.csv').write_text(
            'email\nfirst@example.invalid\nmissing@example.invalid\nlast@example.invalid\n', encoding='utf-8',
        )
        self.assertEqual(self.run_cli(['--csv', 'users.csv']), 1)
        rows = self.read_report()
        self.assertEqual([row['Email'] for row in rows],
                         ['first@example.invalid', 'missing@example.invalid', 'last@example.invalid'])
        self.assertEqual([row['Статус'] for row in rows], ['готово', 'неполная оценка', 'готово'])
        self.assertEqual([row['Объем к загрузке, байт'] for row in rows], ['5', '0', '5'])
        self.assertIn('User does not exist', rows[1]['Описание'])
        self.assertGreater(int(rows[1]['Ошибок']), 0)

    def test_shared_batch_keeps_listing_failure_and_continues(self):
        self.tree['/'] = [self.file('new.txt', b'12345')]
        self.failed_disks.add('MissingHash')
        Path('disks.csv').write_text('VDHASH\nFirstHash\nMissingHash\nLastHash\n', encoding='utf-8')
        self.assertEqual(self.run_cli(['--email', self.email, '--vdhash-csv', 'disks.csv']), 1)
        rows = self.read_report()
        self.assertEqual([row['VDHASH'] for row in rows], ['FirstHash', 'MissingHash', 'LastHash'])
        self.assertEqual([row['Статус'] for row in rows], ['готово', 'неполная оценка', 'готово'])
        self.assertEqual([row['Объем к загрузке, байт'] for row in rows], ['5', '0', '5'])
        self.assertGreater(int(rows[1]['Ошибок']), 0)

    def test_complete_partial_and_same_size_corrupt_files_are_classified_without_mutation(self):
        self.tree['/'] = [self.file('done.txt', b'complete'), self.file('partial.txt', b'complete'),
                          self.file('corrupt.txt', b'complete')]
        for disk in (None, 'FirstHash'):
            with self.subTest(vdhash=disk):
                selectors = ['--email', self.email] + (['--vdhash', disk] if disk else [])
                self.allow_downloads = True
                self.assertEqual(self.run_cli(selectors, estimate=False), 0)
                self.allow_downloads = False
                root = Path('downloads') / ('shared/' + disk if disk else self.email)
                (root / 'partial.txt').write_bytes(b'part')
                (root / 'corrupt.txt').write_bytes(b'xxxxxxxx')
                (root / '.disk-download-orphan.part').write_bytes(b'orphan')
                before = self.snapshot(Path('downloads'))
                downloader.load_credentials()
                result = asyncio.run(downloader.estimate_disk(self.email, root, vdhash=disk))
                self.assertTrue(result.complete)
                self.assertEqual((result.files, result.bytes, result.skipped_existing), (2, 16, 1))
                self.assertEqual(self.snapshot(Path('downloads')), before)

    def test_saved_collision_mapping_survives_changed_filter_without_mutation(self):
        self.tree['/'] = [self.file('a*.txt', b'old', '2026-09-01T12:00:00+03:00'),
                          self.file('a?.txt', b'new')]
        selectors = ['--email', self.email]
        self.allow_downloads = True
        self.assertEqual(self.run_cli(selectors, estimate=False), 0)
        self.allow_downloads = False
        self.settings(date='2026-09-01')
        before = self.snapshot(Path('downloads'))
        downloader.load_credentials()
        result = asyncio.run(downloader.estimate_disk(self.email, Path('downloads') / self.email))
        self.assertTrue(result.complete)
        self.assertEqual((result.files, result.bytes, result.skipped_existing), (1, 3, 1))
        self.assertEqual(self.snapshot(Path('downloads')), before)

    def test_invalid_date_rejected_before_authorization(self):
        self.settings(date='2026-02-30')
        with patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
            self.run_cli(['--email', self.email])
        self.assertEqual(error.exception.code, 2)
        self.token.assert_not_awaited()
        self.assertFalse(self.requests)
        self.assertFalse(Path('downloads').exists())
        self.assertFalse(self.reports())
