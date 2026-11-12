import asyncio
import errno
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import downloader
from environment_fixtures import isolated_environment
from test_lock_diagnostics import WindowsOS


class SharedLockWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'shared'
        self.enterContext(isolated_environment({'DOWNLOAD_DIR': str(self.base), 'MAX_RETRIES': '0'}))
        self.enterContext(patch.object(downloader, 'main', new_callable=AsyncMock, return_value=True))
        self.enterContext(patch.object(downloader, 'SHARED_LOCK_TIMEOUT', 1.0, create=True))
        self.enterContext(patch.object(downloader, 'SHARED_LOCK_POLL_INTERVAL', 0.01, create=True))
        self.output = io.StringIO()
        self.enterContext(patch.object(downloader.log_handler, 'stream', self.output))

    def mapping(self):
        return downloader.LocalPaths(self.root, persistent=True, state_dir=self.root / '.disk-downloader')

    async def download(self):
        return await downloader.download_shared_disks('reader@example.invalid', ['Disk'])

    async def test_waits_for_root_owner_then_uses_released_mapping_even_with_zero_network_retries(self):
        task = None
        try:
            with self.mapping():
                task = asyncio.create_task(self.download())
                await asyncio.sleep(0)
                self.assertFalse(task.done(), 'A temporary root lock must wait instead of failing')
                self.assertFalse(list(self.root.rglob('*.json')))
            self.assertTrue(await asyncio.wait_for(task, 2))
            manifest, = (self.root / '.disk-downloader').glob('*.json')
            self.assertEqual(json.loads(manifest.read_text())['paths'], [
                {'remote': ['Disk'], 'local': ['Disk']},
            ])
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_root_wait_expires_without_changing_the_owners_state(self):
        with self.mapping() as owner:
            owner.prepare(['/Existing'])
            owner.save()
            before = {path.name: path.read_bytes() for path in self.root.rglob('*.json')}
            with patch.object(downloader, 'SHARED_LOCK_TIMEOUT', 0.03, create=True):
                with self.assertRaises(downloader.DestinationBusyError) as raised:
                    await asyncio.wait_for(self.download(), 2)
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertEqual({path.name: path.read_bytes() for path in self.root.rglob('*.json')}, before)
            self.assertIn('ожидания', self.output.getvalue())
        self.assertTrue(await self.download())

    async def test_cancel_while_waiting_leaves_no_mapping_or_held_lock(self):
        task = None
        try:
            with self.mapping():
                task = asyncio.create_task(self.download())
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(list(self.root.rglob('*.json')))
            self.assertTrue(await self.download())
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_windows_lock_violation_waits_and_recovers(self):
        for winerror in (None, 33):
            with self.subTest(winerror=winerror):
                cause = PermissionError(errno.EACCES, 'locked')
                if winerror is not None:
                    cause.winerror = winerror
                windows = SimpleNamespace(LK_NBLCK=2, locking=Mock(side_effect=[cause, None]))
                with patch.object(downloader, 'os', WindowsOS()), patch.dict(sys.modules, {'msvcrt': windows}):
                    self.assertTrue(await self.download())

    async def test_windows_empty_lock_does_not_write_into_a_competing_byte_lock(self):
        real_fdopen = os.fdopen

        class CompetingFile:
            def __init__(self, file):
                self.file = file

            def __getattr__(self, name):
                return getattr(self.file, name)

            def write(self, data):
                raise PermissionError(errno.EACCES, 'Another process locked the byte after the size check')

        windows_os = WindowsOS()
        windows_os.fdopen = lambda descriptor, mode: CompetingFile(real_fdopen(descriptor, mode))
        conflict = PermissionError(errno.EACCES, 'locked')
        windows = SimpleNamespace(LK_NBLCK=2, locking=Mock(side_effect=[conflict, None]))
        with patch.object(downloader, 'os', windows_os), patch.dict(sys.modules, {'msvcrt': windows}):
            self.assertTrue(await self.download())
        lock, = (self.root / '.disk-downloader').glob('*.lock')
        self.assertEqual(lock.read_bytes(), b'')

    async def test_windows_explicit_access_denial_is_not_retried_as_a_lock_conflict(self):
        cause = PermissionError(errno.EACCES, 'denied')
        cause.winerror = 5
        windows = SimpleNamespace(LK_NBLCK=2, locking=Mock(side_effect=cause))
        with (patch.object(downloader, 'os', WindowsOS()),
              patch.dict(sys.modules, {'msvcrt': windows}),
              patch.object(downloader.asyncio, 'sleep', side_effect=AssertionError('Access denial was retried'))):
            with self.assertRaises(downloader.DestinationBusyError) as raised:
                await self.download()
        self.assertIs(raised.exception.__cause__, cause)

    @unittest.skipIf(os.name == 'nt', 'POSIX flock error classification')
    async def test_posix_permission_failure_is_not_retried_as_a_lock_conflict(self):
        cause = PermissionError(errno.EACCES, 'denied')
        with (patch('fcntl.flock', side_effect=cause),
              patch.object(downloader.asyncio, 'sleep', side_effect=AssertionError('Permission failure was retried'))):
            with self.assertRaises(downloader.DestinationBusyError) as raised:
                await self.download()
        self.assertIs(raised.exception.__cause__, cause)

    async def test_preparation_failure_is_not_retried(self):
        cause = PermissionError(errno.EACCES, 'denied')
        with (patch.object(downloader.os, 'open', side_effect=cause),
              patch.object(downloader.asyncio, 'sleep', side_effect=AssertionError('Preparation was retried'))):
            with self.assertRaises(downloader.DestinationLockError) as raised:
                await self.download()
        self.assertEqual(raised.exception.phase, 'подготовка файла блокировки')
        self.assertIs(raised.exception.__cause__, cause)

    async def test_corrupt_mapping_fails_without_rewriting_it(self):
        with self.mapping():
            pass
        lock, = (self.root / '.disk-downloader').glob('*.lock')
        manifest = lock.with_suffix('.json')
        manifest.write_bytes(b'{broken')
        with self.assertRaises(ValueError):
            await self.download()
        self.assertEqual(manifest.read_bytes(), b'{broken')
        manifest.unlink()
        self.assertTrue(await self.download())
