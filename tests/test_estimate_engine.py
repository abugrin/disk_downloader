import asyncio
import hashlib
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import downloader
from token_fixtures import make_token


class EstimateClient:
    def __init__(self, tree):
        self.tree = tree
        self.visited = []
        self.closed = False
        self.check_token = AsyncMock(return_value=True)
        self.download = AsyncMock(side_effect=AssertionError('Оценка не должна скачивать файл'))
        self.get_download_link = AsyncMock(side_effect=AssertionError('Оценка не должна запрашивать ссылку'))
        self.get_disk_info = AsyncMock(side_effect=AssertionError('Объём берётся из файлов'))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def listdir(self, path):
        self.visited.append(path)
        for item in self.tree[path]:
            if isinstance(item, BaseException):
                raise item
            yield item


def resource(name, size=4, *, modified='2026-10-02T00:00:00+03:00', sha256=None, kind='file'):
    if isinstance(modified, str):
        modified = datetime.fromisoformat(modified)
    return SimpleNamespace(path='disk:/' + name, type=kind, size=size, modified=modified,
                           sha256=sha256, md5=None)


class EstimateEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'reader@example.invalid'
        self.client = EstimateClient({'/': []})
        self.cutoff = downloader.parse_modified_since('2026-10-01')
        self.enterContext(patch.object(downloader, 'modified_since', side_effect=lambda: self.cutoff))
        self.enterContext(patch.object(downloader, 'load_credentials', return_value=('id', 'secret')))
        self.enterContext(patch.object(downloader, 'retry_limit', return_value=0))
        self.enterContext(patch.object(downloader, 'get_service_token', new=AsyncMock(return_value=make_token())))
        self.enterContext(patch.object(downloader, 'disk_session', return_value=None))
        self.personal_client = self.enterContext(patch.object(downloader, 'AsyncClient', return_value=self.client))
        self.shared_client = self.enterContext(patch.object(downloader, 'SharedDiskClient', return_value=self.client))
        self.logs = self.enterContext(self.assertLogs('Downloader'))

    async def estimate(self, vdhash=None):
        result = await downloader.estimate_disk('reader@example.invalid', self.root, vdhash=vdhash)
        self.client.download.assert_not_awaited()
        self.client.get_download_link.assert_not_awaited()
        self.client.get_disk_info.assert_not_awaited()
        return result

    async def test_both_disk_types_filter_inclusively_and_visit_old_directories(self):
        self.client.tree = {
            '/': [resource('old.txt', 999, modified='2026-10-01T00:00:00+03:00'),
                  resource('equal.txt', 3, modified='2026-09-30T21:00:01+00:00'),
                  resource('folder', kind='dir', modified='2020-01-01T00:00:00+03:00'),
                  resource('Файлы Мессенджера', kind='dir')],
            'disk:/folder': [resource('folder/new.txt', 7)],
        }
        for vdhash in (None, 'TestHash'):
            with self.subTest(vdhash=vdhash):
                result = await self.estimate(vdhash)
                self.assertTrue(result.complete)
                self.assertEqual((result.files, result.bytes, result.skipped_by_date), (2, 10, 1))
                self.assertEqual(result.vdhash, vdhash)
                self.assertTrue(self.client.closed)
        self.assertEqual(self.client.visited, ['/', 'disk:/folder'] * 2)
        self.personal_client.assert_called_once()
        self.shared_client.assert_called_once()
        self.assertEqual(list(self.base.iterdir()), [])

    async def test_existing_complete_files_excluded_and_partial_counted_in_full(self):
        self.root.mkdir()
        content = b'complete'
        checksum = hashlib.sha256(content).hexdigest()
        self.client.tree['/'] = [resource(name, len(content), sha256=checksum)
                                 for name in ('complete.txt', 'partial.txt', 'corrupt.txt', 'new.txt')]
        for name, body in [('complete.txt', content), ('partial.txt', b'part'), ('corrupt.txt', b'corrupt!')]:
            (self.root / name).write_bytes(body)
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}
        result = await self.estimate()
        self.assertTrue(result.complete)
        self.assertEqual((result.files, result.bytes, result.skipped_existing), (3, 24, 1))
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()})
        self.assertFalse((self.base / '.disk-downloader').exists())

    async def test_zero_length_file_counts_when_missing_and_skips_when_present(self):
        self.root.mkdir()
        (self.root / 'present').touch()
        self.client.tree['/'] = [resource('present', 0), resource('absent', 0)]
        result = await self.estimate()
        self.assertTrue(result.complete)
        self.assertEqual((result.files, result.bytes, result.skipped_existing), (1, 0, 1))

    async def test_missing_dates_only_error_when_filter_enabled(self):
        self.client.tree['/'] = [resource('good', 7), resource('missing', 4, modified=None),
                                 resource('naive', 5, modified=datetime(2026, 10, 3))]
        result = await self.estimate()
        self.assertFalse(result.complete)
        self.assertEqual((result.files, result.bytes, result.errors), (1, 7, 2))
        self.cutoff = None
        result = await self.estimate()
        self.assertTrue(result.complete)
        self.assertEqual((result.files, result.bytes), (3, 16))

    async def test_unknown_invalid_sizes_keep_known_part_and_mark_incomplete(self):
        self.client.tree['/'] = [resource('known', 12)] + [
            resource(f'unknown-{index}', size) for index, size in enumerate((None, -1, '4', True, 2.5))
        ]
        result = await self.estimate()
        self.assertFalse(result.complete)
        self.assertEqual((result.files, result.bytes, result.errors), (6, 12, 5))
        self.assertIn('Размер', result.error)

    async def test_complete_checksum_can_confirm_no_download_without_size(self):
        self.root.mkdir()
        (self.root / 'complete').write_bytes(b'done')
        self.client.tree['/'] = [resource('complete', None, sha256=hashlib.sha256(b'done').hexdigest())]
        result = await self.estimate()
        self.assertTrue(result.complete)
        self.assertEqual((result.files, result.bytes, result.skipped_existing), (0, 0, 1))

    async def test_partial_listing_preserves_observed_files_and_other_directories(self):
        self.client.tree = {
            '/': [resource('good-folder', kind='dir'), resource('partial', kind='dir')],
            'disk:/partial': [resource('partial/known', 3), OSError('Listing failed')],
            'disk:/good-folder': [resource('good-folder/known', 5)],
        }
        result = await self.estimate()
        self.assertFalse(result.complete)
        self.assertEqual((result.files, result.bytes, result.errors), (2, 8, 1))

    async def test_local_error_does_not_discard_other_files(self):
        self.root.mkdir()
        (self.root / 'directory-instead-of-file').mkdir()
        self.client.tree['/'] = [resource('directory-instead-of-file', 9), resource('good', 4)]
        result = await self.estimate()
        self.assertFalse(result.complete)
        self.assertEqual((result.files, result.bytes, result.errors), (1, 4, 1))

    async def test_symlink_is_not_read_and_other_files_are_counted(self):
        self.root.mkdir()
        target = self.base / 'outside'
        target.write_bytes(b'keep')
        try:
            (self.root / 'link').symlink_to(target)
        except OSError:
            self.skipTest('Symbolic links unavailable')
        self.client.tree['/'] = [resource('link'), resource('good', 5)]
        with patch.object(downloader, 'check_local_file', wraps=downloader.check_local_file) as check:
            result = await self.estimate()
        self.assertFalse(result.complete)
        self.assertEqual((result.files, result.bytes, result.errors), (1, 5, 1))
        self.assertEqual([call.args[0].name for call in check.await_args_list], ['good'])
        self.assertEqual(target.read_bytes(), b'keep')

    async def test_symlink_change_after_local_check_is_rejected(self):
        self.client.tree['/'] = [resource('file')]
        original = downloader.ensure_safe_path
        def check_path(path, root, **kwargs):
            if Path(path).name == 'file' and checking.await_count:
                raise downloader.UnsafeDestinationError('Replaced after check')
            return original(path, root, **kwargs)
        with (patch.object(downloader, 'check_local_file', new=AsyncMock(return_value=False)) as checking,
              patch.object(downloader, 'ensure_safe_path', side_effect=check_path)):
            result = await self.estimate()
        self.assertFalse(result.complete)
        self.assertEqual((result.files, result.bytes, result.errors), (0, 0, 1))

    async def test_cancel_propagates_and_closes_client(self):
        self.client.tree['/'] = [resource('file')]
        with patch.object(downloader, 'check_local_file', new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.estimate()
        self.assertTrue(self.client.closed)
        self.assertFalse(any(task.get_name() == 'downloader-token-refresh' for task in asyncio.all_tasks()))

    async def test_failed_token_returns_incomplete_without_starting_disk_client(self):
        with patch.object(downloader, 'get_service_token', new=AsyncMock(side_effect=ValueError('bad credentials'))):
            result = await self.estimate()
        self.assertFalse(result.complete)
        self.assertEqual(result.errors, 1)
        self.personal_client.assert_not_called()
        self.assertFalse(self.root.exists())

    async def test_invalid_token_is_incomplete_and_closes_client(self):
        self.client.check_token.return_value = False
        result = await self.estimate()
        self.assertFalse(result.complete)
        self.assertEqual(result.errors, 1)
        self.assertEqual(self.client.visited, [])
        self.assertTrue(self.client.closed)


class ReadOnlyPathsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'disk'

    def test_absent_destination_and_state_are_not_created(self):
        with downloader.LocalPaths(self.root, persistent=True, read_only=True) as paths:
            paths.prepare(['/folder/file', '/a:b', '/a?b'])
            self.assertEqual(paths.get('/folder/file'), self.root / 'folder/file')
            self.assertNotEqual(paths.get('/a:b'), paths.get('/a?b'))
            with self.assertRaises(RuntimeError):
                paths.save()
        self.assertEqual(list(self.base.iterdir()), [])

    def test_saved_collision_mapping_is_reused_without_changing_state(self):
        remotes = ['/a:b', '/a?b']
        with downloader.LocalPaths(self.root, persistent=True) as paths:
            paths.prepare(remotes)
            paths.save()
            expected = {remote: paths.get(remote) for remote in remotes}
        self.root.mkdir()
        for path in expected.values():
            path.write_bytes(b'complete')
        before = {str(p.relative_to(self.base)): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.base.rglob('*') if p.is_file()}
        with downloader.LocalPaths(self.root, persistent=True, read_only=True) as paths:
            paths.prepare(remotes)
            self.assertEqual({remote: paths.get(remote) for remote in remotes}, expected)
        after = {str(p.relative_to(self.base)): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.base.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
