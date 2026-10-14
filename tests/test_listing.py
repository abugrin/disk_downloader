import asyncio
import hashlib
import io
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import downloader
from environment_fixtures import isolated_environment
from token_fixtures import make_token


def resource(kind, path, size=4):
    return SimpleNamespace(type=kind, path=path, size=size)


class ListingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(isolated_environment({'MAX_RETRIES': '0'}))
        self.logs = patch.object(downloader.log_handler, 'stream', io.StringIO())
        self.logs.start()
        self.addCleanup(self.logs.stop)

    async def test_each_directory_is_read_once_and_first_observed_files_are_retained(self):
        calls = Counter()

        async def listdir(path):
            calls[path] += 1
            if calls[path] > 1:
                raise AssertionError('A second traversal would lose the original listing')
            if path == '/':
                yield resource('file', 'disk:/root.txt')
                yield resource('dir', 'disk:/child')
            else:
                yield resource('file', 'disk:/child/nested.txt')

        listing = await downloader.list_resources(SimpleNamespace(listdir=listdir))
        self.assertEqual(calls, {'/': 1, 'disk:/child': 1})
        self.assertEqual([item.path for item in listing.files], ['disk:/root.txt', 'disk:/child/nested.txt'])
        self.assertEqual(listing.failed_directories, 0)

    async def test_exhausted_partial_listing_keeps_observed_files_and_sibling_directories(self):
        calls = Counter()

        async def listdir(path):
            calls[path] += 1
            if path == '/':
                yield resource('file', 'disk:/root.txt')
                yield resource('dir', 'disk:/child')
                raise TimeoutError('Second page unavailable')
            yield resource('file', 'disk:/child/nested.txt')

        with patch.dict(os.environ, {'MAX_RETRIES': '1'}), patch.object(downloader.asyncio, 'sleep', new=AsyncMock()):
            listing = await downloader.list_resources(SimpleNamespace(listdir=listdir))
        self.assertEqual(calls, {'/': 2, 'disk:/child': 1})
        self.assertEqual([item.path for item in listing.files], ['disk:/root.txt', 'disk:/child/nested.txt'])
        self.assertEqual(listing.failed_directories, 1)
        self.assertEqual(len(listing.directories), 1)

    async def test_deep_tree_does_not_use_recursive_python_calls(self):
        depth_limit = 1100

        async def listdir(path):
            depth = path.count('/') if path.startswith('disk:') else 0
            if depth < depth_limit:
                yield resource('dir', 'disk:/' + '/'.join(['a'] * (depth + 1)))
            else:
                yield resource('file', path + '/last.txt')

        listing = await downloader.list_resources(SimpleNamespace(listdir=listdir))
        self.assertEqual(len(listing.directories), depth_limit)
        self.assertEqual(len(listing.files), 1)
        self.assertEqual(listing.failed_directories, 0)

    async def test_duplicate_paths_do_not_repeat_traversal_or_downloads(self):
        calls = []

        async def listdir(path):
            calls.append(path)
            yield resource('dir', 'disk:/')
            yield resource('dir', 'disk:/child')
            yield resource('file', 'disk:/same.txt')
            yield resource('file', 'disk:/same.txt')

        listing = await downloader.list_resources(SimpleNamespace(listdir=listdir))
        self.assertEqual(calls, ['/', 'disk:/child'])
        self.assertEqual(len(listing.directories), 1)
        self.assertEqual(len(listing.files), 1)


class ListingDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='disk-listing-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'download'
        self.patches = [
            isolated_environment({'MAX_RETRIES': '0'}),
            patch.object(downloader, 'load_credentials', return_value=('test-id', 'test-secret')),
            patch.object(downloader, 'get_service_token', return_value=make_token()),
            patch.object(downloader, 'disk_session', return_value=None),
            patch.object(downloader.log_handler, 'stream', io.StringIO()),
        ]
        for context in self.patches:
            self.enterContext(context)

    def client(self, listdir, download):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.check_token.return_value = True
        client.get_disk_info.return_value = SimpleNamespace(used_space=0)
        client.listdir = listdir
        client.download.side_effect = download
        return client

    async def test_unreadable_branch_does_not_block_available_files_and_marks_run_incomplete(self):
        async def listdir(path):
            if path == '/':
                yield resource('file', 'disk:/root.txt')
                yield resource('dir', 'disk:/good')
                yield resource('dir', 'disk:/bad')
            elif path == 'disk:/bad':
                raise PermissionError('Synthetic inaccessible directory')
            else:
                yield resource('file', 'disk:/good/nested.txt')

        async def download(remote, local):
            Path(local).write_text('data')

        client = self.client(listdir, download)
        with patch.object(downloader, 'AsyncClient', return_value=client):
            completed = await downloader.main('test@example.invalid', output_dir=self.root)
        self.assertFalse(completed)
        self.assertEqual((self.root / 'root.txt').read_text(), 'data')
        self.assertEqual((self.root / 'good/nested.txt').read_text(), 'data')
        self.assertEqual(client.download.await_count, 2)

    async def test_large_listing_uses_fixed_worker_pool(self):
        count = 2000
        processed = []
        worker_counts = []

        async def listdir(path):
            for index in range(count):
                yield resource('file', f'disk:/{index}.txt')

        async def safe_download(*, path, progress, **kwargs):
            worker_counts.append(sum(task.get_name().startswith('downloader-worker-')
                                     for task in asyncio.all_tasks()))
            progress.start_file(path)
            await asyncio.sleep(0)
            processed.append(path)
            progress.finish_file(path, True, skipped=True)
            return True

        client = self.client(listdir, AsyncMock())
        with patch.object(downloader, 'AsyncClient', return_value=client), patch.object(downloader, 'safe_download', new=safe_download):
            completed = await downloader.main('test@example.invalid', output_dir=self.root)
        self.assertTrue(completed)
        self.assertEqual(len(set(processed)), count)
        self.assertEqual(max(worker_counts), downloader.MAX_STREAMS)
        self.assertFalse(any(task.get_name().startswith('downloader-worker-') for task in asyncio.all_tasks()))

    async def test_full_restart_with_new_collision_preserves_completed_file(self):
        bodies = {'disk:/report?.txt': b'old!'}
        downloaded = []

        async def listdir(path):
            for remote, body in bodies.items():
                yield SimpleNamespace(type='file', path=remote, size=len(body),
                                      sha256=hashlib.sha256(body).hexdigest())

        async def download(remote, local):
            downloaded.append(remote)
            Path(local).write_bytes(bodies['disk:' + remote])

        client = self.client(listdir, download)
        with patch.object(downloader, 'AsyncClient', return_value=client):
            self.assertTrue(await downloader.main('test@example.invalid', output_dir=self.root))
            original = self.root / 'report_.txt'
            before = original.stat()
            bodies['disk:/report*.txt'] = b'new!'
            self.assertTrue(await downloader.main('test@example.invalid', output_dir=self.root))
        self.assertEqual(downloaded, ['/report?.txt', '/report*.txt'])
        self.assertEqual(original.read_bytes(), b'old!')
        self.assertEqual((original.stat().st_ino, original.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        self.assertEqual({path.read_bytes() for path in self.root.iterdir()}, {b'old!', b'new!'})

    async def test_permanent_auth_failure_cancels_pool_without_draining_queue(self):
        started = 0
        active = 0

        async def listdir(path):
            for index in range(100):
                yield resource('file', f'disk:/{index}.txt')

        async def download(remote, local):
            nonlocal started, active
            started += 1
            active += 1
            first = started == 1
            try:
                if first:
                    await asyncio.sleep(0)
                    raise downloader.TokenRefreshError('HTTP 401; invalid_client', terminal=True)
                await asyncio.Event().wait()
            finally:
                active -= 1

        async def close(*args):
            self.assertEqual(active, 0)

        client = self.client(listdir, download)
        client.__aexit__.side_effect = close
        with patch.object(downloader, 'AsyncClient', return_value=client):
            with self.assertRaises(downloader.TokenRefreshError):
                await downloader.main('test@example.invalid', output_dir=self.root)
        self.assertLessEqual(started, downloader.MAX_STREAMS)
        self.assertEqual(list(self.root.glob('*.part')), [])
        self.assertFalse(any(task.get_name().startswith('downloader-worker-') for task in asyncio.all_tasks()))

    async def test_cancellation_waits_for_workers_before_closing_client(self):
        ready = asyncio.Event()
        active = 0
        cancelled = 0

        async def listdir(path):
            for index in range(downloader.MAX_STREAMS * 3):
                yield resource('file', f'disk:/{index}.txt')

        async def download(remote, local):
            nonlocal active, cancelled
            active += 1
            if active == downloader.MAX_STREAMS:
                ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1
                cancelled += 1

        async def close(*args):
            self.assertEqual(active, 0)

        client = self.client(listdir, download)
        client.__aexit__.side_effect = close
        with patch.object(downloader, 'AsyncClient', return_value=client):
            task = asyncio.create_task(downloader.main('test@example.invalid', output_dir=self.root))
            try:
                await asyncio.wait_for(ready.wait(), timeout=5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(cancelled, downloader.MAX_STREAMS)
        self.assertEqual(list(self.root.glob('.disk-download-*.part')), [])
        self.assertFalse(any(task.get_name().startswith('downloader-worker-') for task in asyncio.all_tasks()))


if __name__ == '__main__':
    unittest.main()
