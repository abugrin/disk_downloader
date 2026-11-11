import errno
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import downloader
from environment_fixtures import isolated_environment


class WindowsOS:
    name = 'nt'

    def __getattr__(self, name):
        return getattr(os, name)


class LockDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(os.chdir, self.previous)
        self.enterContext(isolated_environment())
        self.console = io.StringIO()
        self.enterContext(patch.object(downloader.log_handler, 'stream', self.console))
        self.root = Path.cwd() / 'shared'

    def mapping(self):
        return downloader.LocalPaths(self.root, persistent=True, state_dir=self.root / '.disk-downloader')

    def test_shared_destination_conflict_identifies_scope_and_retained_lock_reopens(self):
        with self.mapping():
            with self.assertRaises(downloader.DestinationBusyError) as raised:
                with self.mapping():
                    self.fail('A second writer acquired the destination')
        error = raised.exception
        description = getattr(error, 'diagnostic', '')
        self.assertIn(repr(str(self.root)), description)
        self.assertIn('.lock', description)
        self.assertIn('захват блокировки', description)
        self.assertIn('другой запуск', description)
        self.assertIsInstance(error.__cause__, OSError)
        lock_path, = (self.root / '.disk-downloader').glob('*.lock')
        with self.mapping():
            self.assertTrue(lock_path.exists())

    def test_windows_lock_failure_preserves_cause_without_asserting_a_process_owner(self):
        cause = PermissionError(errno.EACCES, 'private server detail')
        cause.winerror = 33
        windows = SimpleNamespace(LK_NBLCK=2, locking=Mock(side_effect=cause))
        paths = self.mapping()
        with patch.object(downloader, 'os', WindowsOS()), patch.dict(sys.modules, {'msvcrt': windows}):
            with self.assertRaises(downloader.DestinationBusyError) as raised:
                with paths:
                    self.fail('Denied Windows lock was ignored')
        description = getattr(raised.exception, 'diagnostic', '')
        self.assertIn('SMB', description)
        self.assertIn('захват блокировки', description)
        self.assertNotIn('private server detail', description)
        self.assertIs(raised.exception.__cause__, cause)
        self.assertIsNone(paths._lock_file)
        self.assertEqual(windows.locking.call_args.args[1:], (2, 1))

    def test_windows_lock_file_write_failure_is_not_reported_as_contention(self):
        cause = PermissionError(errno.EACCES, 'private write failure')
        opened = []
        real_fdopen = os.fdopen

        class UnwritableFile:
            def __init__(self, file):
                self.file = file

            def __getattr__(self, name):
                return getattr(self.file, name)

            def write(self, data):
                raise cause

        def fdopen(descriptor, mode):
            file = real_fdopen(descriptor, mode)
            opened.append(file)
            return UnwritableFile(file)

        windows_os = WindowsOS()
        windows_os.fdopen = fdopen
        windows = SimpleNamespace(LK_NBLCK=2, locking=Mock())
        with patch.object(downloader, 'os', windows_os), patch.dict(sys.modules, {'msvcrt': windows}):
            with self.assertRaises(OSError) as raised:
                with self.mapping():
                    self.fail('Failed preparation was ignored')
        self.assertEqual(type(raised.exception).__name__, 'DestinationLockError')
        self.assertIn('подготовка файла блокировки', getattr(raised.exception, 'diagnostic', ''))
        self.assertIs(raised.exception.__cause__, cause)
        self.assertTrue(all(file.closed for file in opened))
        self.assertFalse(windows.locking.called)

    def test_lock_file_open_failure_reports_preparation_and_keeps_original_cause(self):
        cause = PermissionError(errno.EACCES, 'private open failure')
        with patch.object(downloader.os, 'open', side_effect=cause):
            with self.assertRaises(OSError) as raised:
                with self.mapping():
                    self.fail('Denied open was ignored')
        error = raised.exception
        self.assertEqual(type(error).__name__, 'DestinationLockError')
        self.assertIn(repr(str(self.root)), getattr(error, 'diagnostic', ''))
        self.assertIn('подготовка файла блокировки', getattr(error, 'diagnostic', ''))
        self.assertIs(error.__cause__, cause)

    def test_terminal_lock_failure_is_logged_to_file_and_returns_failure(self):
        with (
            self.mapping(),
            patch('sys.stdout', new_callable=io.StringIO),
            patch('sys.stdin.isatty', return_value=True),
            patch('sys.stdout.isatty', return_value=True),
            patch.object(downloader, 'load_credentials', return_value=('fake-id', 'fake-secret')),
            patch.object(downloader.questionary, 'select', return_value=SimpleNamespace(
                unsafe_ask=Mock(side_effect=['shared', 'exit']))),
            patch.object(downloader.questionary, 'text', return_value=SimpleNamespace(
                unsafe_ask=Mock(side_effect=['reader@example.invalid', 'AnotherDisk']))),
        ):
            self.assertEqual(downloader.cli([]), 1)
        file, = Path('logs').glob('*.log')
        content = file.read_text(encoding='utf-8')
        for output in (content, self.console.getvalue()):
            self.assertIn('DestinationBusyError', output)
            self.assertIn(repr(str(self.root)), output)
            self.assertIn('errno=', output)
            self.assertIn('.lock', output)
        self.assertIn('код: 1', content)

    def test_cli_lock_failure_reaches_log_before_argument_error(self):
        with self.mapping(), patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as raised:
                downloader.cli(['--email', 'reader@example.invalid', '--vdhash', 'AnotherDisk'])
        self.assertNotEqual(raised.exception.code, 0)
        file, = Path('logs').glob('*.log')
        content = file.read_text(encoding='utf-8')
        self.assertIn('DestinationBusyError', content)
        self.assertIn(repr(str(self.root)), content)
        self.assertIn('errno=', content)

    def test_settings_file_failure_is_logged_without_raw_exception_details(self):
        with (
            patch('sys.stdout', new_callable=io.StringIO),
            patch('sys.stdin.isatty', return_value=True),
            patch('sys.stdout.isatty', return_value=True),
            patch.object(downloader.questionary, 'select', return_value=SimpleNamespace(
                unsafe_ask=Mock(side_effect=['settings', 'configure', 'back', 'exit']))),
            patch.object(downloader, 'configure_credentials', side_effect=PermissionError(
                errno.EACCES, 'private secret detail', '/private/secret-file')),
        ):
            downloader.cli([])
        file, = Path('logs').glob('*.log')
        content = file.read_text(encoding='utf-8')
        self.assertIn('PermissionError', content)
        self.assertIn('errno=13', content)
        self.assertNotIn('private secret detail', content)
        self.assertNotIn('/private/secret-file', content)
