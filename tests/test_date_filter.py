import hashlib
import io
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from yadisk.objects import ResourceObject
from yadisk.sessions.async_httpx_session import AsyncHTTPXSession

import downloader
from environment_fixtures import isolated_environment
from token_fixtures import make_token


class DateFilterDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_cwd = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(os.chdir, self.old_cwd)
        self.enterContext(isolated_environment())
        self.enterContext(patch.object(downloader.log_handler, 'stream', io.StringIO()))
        self.enterContext(patch.object(downloader, 'get_service_token', return_value=make_token()))
        self.logs = self.enterContext(self.assertLogs('Downloader'))
        self.tree = {'/': []}
        self.contents = {}
        self.requests = []
        self.sessions = []
        self.enterContext(patch.object(downloader, 'AsyncHTTPXSession', side_effect=self.session))
        self.settings('2026-10-01')

    def settings(self, date, directory=''):
        Path('.env').write_text(
            "CLIENT_ID='test-id'\nCLIENT_SECRET='test-secret'\n"
            f"MODIFIED_SINCE='{date}'\nDOWNLOAD_DIR='{directory}'\n",
            encoding='utf-8',
        )
        for name in ('MODIFIED_SINCE', 'DOWNLOAD_DIR'):
            os.environ.pop(name, None)

    @staticmethod
    def relative_path(value):
        return value.split(':disk:', 1)[1] if value.startswith('vd:') else value.removeprefix('disk:')

    def file(self, name, modified, body=b'complete'):
        path = '/' + name
        self.contents[path] = body
        resource = {'type': 'file', 'path': 'disk:' + path, 'size': len(body),
                    'sha256': hashlib.sha256(body).hexdigest()}
        if modified is not None:
            resource['modified'] = modified
        return resource

    def session(self, **kwargs):
        session = AsyncHTTPXSession(transport=httpx.MockTransport(self.respond), **kwargs)
        self.sessions.append(session)
        return session

    def respond(self, request):
        self.requests.append(request)
        if request.url.host == 'download.example.invalid':
            self.assertNotIn('Range', request.headers)
            return httpx.Response(200, content=self.contents[request.url.params['file']])
        self.assertEqual(request.headers.get('Authorization'), 'OAuth test-token')
        path = request.url.path
        if '/operations/' in path:
            return httpx.Response(404, json={'error': 'DiskOperationNotFoundError'})
        if path.endswith('/disk') or path.endswith('/virtual-disks'):
            return httpx.Response(200, json={'used_space': sum(map(len, self.contents.values()))})
        remote = request.url.params['path']
        relative = self.relative_path(remote)
        if path.endswith('/download'):
            href = httpx.URL('https://download.example.invalid/file', params={'file': relative})
            return httpx.Response(200, json={'href': str(href)})
        self.assertTrue(path.endswith('/resources'), str(request.url))
        prefix = remote.split(':disk:', 1)[0] + ':disk:' if remote.startswith('vd:') else 'disk:'
        items = [dict(item, path=prefix + item['path'].removeprefix('disk:'))
                 for item in self.tree[relative]]
        return httpx.Response(200, json={'type': 'dir', '_embedded': {
            'offset': 0, 'limit': 500, 'total': len(items), 'items': items,
        }})

    def download(self, args=None):
        result = downloader.cli(args or ['--email', 'reader@example.invalid'])
        self.assertTrue(all(session.httpx_session.is_closed for session in self.sessions))
        return result

    def link_paths(self):
        return [self.relative_path(request.url.params['path']) for request in self.requests
                if request.url.path.endswith('/resources/download')]

    def content_paths(self):
        return [request.url.params['file'] for request in self.requests
                if request.url.host == 'download.example.invalid']

    def test_inclusive_threshold_timezone_conversion_and_old_directory_traversal(self):
        self.tree['/'] = [
            self.file('before.txt', '2026-10-01T00:00:00+03:00'),
            self.file('equal.txt', '2026-10-01T00:00:01+03:00'),
            self.file('equal-utc.txt', '2026-09-30T21:00:01Z'),
            self.file('after.txt', '2026-10-01T00:00:02+03:00'),
            {'type': 'dir', 'path': 'disk:/old-folder', 'modified': '2020-01-01T00:00:00+03:00'},
        ]
        self.tree['/old-folder'] = [self.file('old-folder/new.txt', '2026-10-02T00:00:00+03:00')]
        self.assertEqual(self.download(), 0)
        expected = {'/equal.txt', '/equal-utc.txt', '/after.txt', '/old-folder/new.txt'}
        self.assertEqual(set(self.link_paths()), expected)
        self.assertEqual(set(self.content_paths()), expected)
        root = Path('reader@example.invalid')
        self.assertFalse((root / 'before.txt').exists())
        self.assertEqual((root / 'old-folder/new.txt').read_bytes(), b'complete')
        self.assertTrue(any(request.url.params.get('path') == 'disk:/old-folder' for request in self.requests))

    def test_saved_setting_applies_to_personal_shared_single_and_csv_modes(self):
        self.tree['/'] = [self.file('old.txt', '2026-09-30T23:59:59+03:00'),
                          self.file('new.txt', '2026-10-01T12:00:00+03:00')]
        Path('users.csv').write_text('email\nreader@example.invalid\nsecond@example.invalid\n', encoding='utf-8')
        Path('disks.csv').write_text('VDHASH\nFirstHash\nSecondHash\n', encoding='utf-8')
        cases = [
            (['--email', 'reader@example.invalid'], ['reader@example.invalid']),
            (['--csv', 'users.csv'], ['reader@example.invalid', 'second@example.invalid']),
            (['--email', 'reader@example.invalid', '--vdhash', 'FirstHash'], ['shared/FirstHash']),
            (['--email', 'reader@example.invalid', '--vdhash-csv', 'disks.csv'],
             ['shared/FirstHash', 'shared/SecondHash']),
        ]
        for index, (args, roots) in enumerate(cases):
            with self.subTest(args=args):
                self.settings('2026-10-01', f'mode-{index}')
                self.requests.clear()
                self.assertEqual(self.download(args), 0)
                self.assertEqual(self.link_paths(), ['/new.txt'] * len(roots))
                self.assertEqual(self.content_paths(), ['/new.txt'] * len(roots))
                for root in roots:
                    destination = Path(f'mode-{index}') / root
                    self.assertEqual((destination / 'new.txt').read_bytes(), b'complete')
                    self.assertFalse((destination / 'old.txt').exists())

    def test_disabled_filter_accepts_missing_modified_and_active_filter_can_select_nothing(self):
        self.tree['/'] = [self.file('missing.txt', None), self.file('old.txt', '2020-01-01T00:00:00+03:00')]
        self.settings('')
        self.assertEqual(self.download(), 0)
        self.assertEqual(set(self.content_paths()), {'/missing.txt', '/old.txt'})
        self.tree['/'] = [self.file('old.txt', '2020-01-01T00:00:00+03:00')]
        self.settings('2026-10-01', 'filtered')
        self.requests.clear()
        self.assertEqual(self.download(), 0)
        self.assertEqual(self.link_paths(), [])
        self.assertEqual(self.content_paths(), [])

    def test_bad_modified_fails_result_but_valid_files_are_downloaded(self):
        good = ResourceObject(self.file('good.txt', '2026-10-01T12:00:00+03:00'))
        invalid = [SimpleNamespace(type='file', path='disk:/missing.txt', modified=None),
                   SimpleNamespace(type='file', path='disk:/naive.txt', modified=datetime(2026, 10, 2)),
                   SimpleNamespace(type='file', path='disk:/invalid.txt', modified='not-a-date')]
        listing = downloader.DiskListing(files=[*invalid, good])
        with patch.object(downloader, 'list_resources', new=AsyncMock(return_value=listing)):
            self.assertEqual(self.download(), 1)
        self.assertEqual(self.link_paths(), ['/good.txt'])
        self.assertEqual(self.content_paths(), ['/good.txt'])
        self.assertEqual(Path('reader@example.invalid/good.txt').read_bytes(), b'complete')
        errors = '\n'.join(message for message in self.logs.output if message.startswith('ERROR:'))
        self.assertIn('ошибок даты: 3', '\n'.join(self.logs.output))
        for resource in invalid:
            self.assertIn(resource.path, errors)
            self.assertFalse((Path('reader@example.invalid') / resource.path.rsplit('/', 1)[-1]).exists())

    def test_repeated_download_skips_complete_and_restarts_partial_selected_file(self):
        self.tree['/'] = [self.file('done.txt', '2026-10-02T00:00:00+03:00'),
                          self.file('partial.txt', '2026-10-02T00:00:00+03:00'),
                          self.file('old.txt', '2020-01-01T00:00:00+03:00')]
        self.assertEqual(self.download(), 0)
        root = Path('reader@example.invalid')
        completed = root / 'done.txt'
        os.utime(completed, ns=(1000000000, 1000000000))
        before = completed.stat()
        (root / 'partial.txt').write_bytes(b'part')
        self.requests.clear()
        self.assertEqual(self.download(), 0)
        self.assertEqual(self.link_paths(), ['/partial.txt'])
        self.assertEqual(self.content_paths(), ['/partial.txt'])
        self.assertEqual(completed.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual((root / 'partial.txt').read_bytes(), b'complete')
        self.assertFalse((root / 'old.txt').exists())
        self.assertFalse(list(root.glob('*.part')))

    def test_filter_changes_keep_collision_mapping_and_completed_files(self):
        self.tree['/'] = [self.file('a*.txt', '2026-09-01T12:00:00+03:00', b'old'),
                          self.file('a?.txt', '2026-10-02T00:00:00+03:00', b'new')]
        self.assertEqual(self.download(), 0)
        root = Path('reader@example.invalid')
        completed, = root.iterdir()
        self.assertEqual(completed.read_bytes(), b'new')
        os.utime(completed, ns=(1000000000, 1000000000))
        before = completed.stat()
        self.settings('2026-09-01')
        self.requests.clear()
        self.assertEqual(self.download(), 0)
        self.assertEqual(self.content_paths(), ['/a*.txt'])
        self.assertEqual(completed.read_bytes(), b'new')
        self.assertEqual(completed.stat().st_mtime_ns, before.st_mtime_ns)
        mapped = {file.read_bytes(): file.name for file in root.iterdir()}
        self.assertEqual(len(mapped), 2)
        self.settings('', 'unfiltered')
        self.assertEqual(self.download(), 0)
        unfiltered = Path('unfiltered/reader@example.invalid')
        self.assertEqual({file.read_bytes(): file.name for file in unfiltered.iterdir()}, mapped)
