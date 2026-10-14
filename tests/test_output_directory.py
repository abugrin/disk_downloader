import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from dotenv import dotenv_values, set_key
from yadisk.objects import ResourceObject

import downloader
from environment_fixtures import isolated_environment
from test_resume import CHILD
from token_fixtures import make_token


class OutputPathTests(unittest.TestCase):
    def test_posix_absolute_relative_and_default_paths(self):
        base = PurePosixPath('/home/user/project')
        cases = {'': base, '   ': base, '.': base,
                 'backups': base / 'backups', '../backups': PurePosixPath('/home/user/backups'),
                 '/mnt/data/../backup': PurePosixPath('/mnt/backup'),
                 'my backups/2026': base / 'my backups/2026'}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(downloader.resolve_download_path(value, base), expected)

    def test_windows_drive_unc_relative_and_extended_paths(self):
        base = PureWindowsPath('C:/Work/project')
        cases = {
            '': base, '.': base,
            r'D:\Backups': PureWindowsPath('D:/Backups'),
            'D:/Backups': PureWindowsPath('D:/Backups'),
            'D:\\': PureWindowsPath('D:/'),
            r'.\backups\2026': base / 'backups/2026',
            r'..\backups': PureWindowsPath('C:/Work/backups'),
            r'\backups': PureWindowsPath('C:/backups'),
            r'\\server\share\Backups': PureWindowsPath('//server/share/Backups'),
            r'\\?\D:\Backups\..\Data': PureWindowsPath('//?/D:/Data'),
            r'\\?\UNC\server\share\Backups': PureWindowsPath('//?/UNC/server/share/Backups'),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(downloader.resolve_download_path(value, base), expected)

    def test_ambiguous_or_foreign_windows_paths_and_control_characters_are_rejected(self):
        for value in ['D:', 'D:backup']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                downloader.resolve_download_path(value, PureWindowsPath('C:/Work'))
        for value in [r'D:\Backups', 'D:/Backups', r'\\server\share\Backups']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                downloader.resolve_download_path(value, PurePosixPath('/work'))
        for value in ['line\nbreak', 'line\rbreak', 'null\x00byte']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                downloader.resolve_download_path(value, PurePosixPath('/work'))


class OutputSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_cwd = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(os.chdir, self.old_cwd)
        self.enterContext(isolated_environment())
        self.output = self.enterContext(patch('sys.stdout', new_callable=io.StringIO))

    def answers(self, method, values):
        return patch.object(downloader.questionary, method,
                            return_value=SimpleNamespace(unsafe_ask=Mock(side_effect=values)))

    def test_setting_persists_and_reset_restores_working_directory(self):
        Path('.env').write_text('CLIENT_ID=fake-id\nCLIENT_SECRET=fake-secret\nMAX_RETRIES=0\n')
        with self.answers('select', ['custom']), self.answers('text', ['backups/2026']):
            downloader.configure_download_directory()
        self.assertEqual(downloader.download_directory(), Path.cwd() / 'backups/2026')
        with isolated_environment():
            self.assertEqual(downloader.download_directory(), Path.cwd() / 'backups/2026')
            self.assertEqual(downloader.retry_limit(), 0)
        with self.answers('select', ['default']):
            downloader.configure_download_directory()
        self.assertEqual(downloader.download_directory(), Path.cwd())
        saved = dotenv_values('.env', interpolate=False)
        self.assertEqual(saved, {'CLIENT_ID': 'fake-id', 'CLIENT_SECRET': 'fake-secret',
                                 'MAX_RETRIES': '0', 'DOWNLOAD_DIR': ''})
        self.assertNotIn('fake-secret', self.output.getvalue())

    def test_windows_and_quoted_paths_survive_env_roundtrip(self):
        paths = ['D:\\', 'D:\\Backups\\', r'\\server\share\Backups', r'\\?\D:\Backups',
                 r"D:\User's backups\2026", r"relative\folder's", '/mnt/my backups']
        for value in paths:
            with self.subTest(value=value):
                downloader.save_settings({'DOWNLOAD_DIR': value, 'OTHER': 'preserved'})
                saved = dotenv_values('.env', interpolate=False)
                self.assertEqual(saved, {'DOWNLOAD_DIR': value, 'OTHER': 'preserved'})
                self.assertEqual(os.environ['DOWNLOAD_DIR'], value)
                if value.startswith('D:') or value.startswith('\\\\'):
                    self.assertEqual(downloader.resolve_download_path(saved['DOWNLOAD_DIR'], PureWindowsPath('C:/work')),
                                     PureWindowsPath(value))

    def test_environment_priority_and_home_expansion(self):
        Path('.env').write_text('DOWNLOAD_DIR=from-file\n')
        with patch.dict(os.environ, {'DOWNLOAD_DIR': 'from-env'}):
            self.assertEqual(downloader.download_directory(), Path.cwd() / 'from-env')
        self.assertEqual(downloader.download_directory(), Path.cwd() / 'from-file')
        with patch.dict(os.environ, {'DOWNLOAD_DIR': '~/backups'}):
            self.assertEqual(downloader.download_directory(), Path.home() / 'backups')

    def test_cancel_invalid_input_and_failed_save_preserve_setting(self):
        original = "DOWNLOAD_DIR='original'\n"
        Path('.env').write_text(original)
        for value, error in [(KeyboardInterrupt(), KeyboardInterrupt), ('bad\x00path', ValueError), ('new', OSError)]:
            with self.subTest(error=error):
                with (self.answers('select', ['custom']), self.answers('text', [value]),
                      patch.object(downloader, 'set_key', side_effect=OSError('test')), self.assertRaises(error)):
                    downloader.configure_download_directory()
                self.assertEqual(Path('.env').read_text(), original)
                self.assertEqual(downloader.download_directory(), Path.cwd() / 'original')

    def test_existing_file_is_rejected_before_download(self):
        Path('file').write_text('keep')
        with patch.dict(os.environ, {'DOWNLOAD_DIR': 'file'}):
            with (patch.object(downloader, 'download_users') as download,
                  patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit) as error):
                downloader.cli(['--email', 'reader@example.invalid'])
            self.assertEqual(error.exception.code, 2)
            download.assert_not_called()
        self.assertEqual(Path('file').read_text(), 'keep')

    def test_menu_setting_is_used_for_next_download(self):
        async def download(users, **kwargs):
            self.assertEqual(downloader.download_directory(), Path.cwd() / 'new-output')
            return True
        with (patch.object(downloader, 'load_credentials', return_value=('fake-id', 'fake-secret')),
              self.answers('select', ['download_directory', 'custom', 'user', 'exit']),
              self.answers('text', ['new-output', 'reader@example.invalid']),
              patch.object(downloader, 'download_users', side_effect=download) as users):
            self.assertEqual(downloader.terminal_menu(10), 0)
        users.assert_awaited_once()
        self.assertIn(str(Path.cwd() / 'new-output'), self.output.getvalue())

    def test_direct_main_uses_configured_root_and_explicit_override(self):
        async def listdir(path):
            yield ResourceObject({'type': 'file', 'path': 'disk:/file.txt', 'size': 8})
        async def download(remote, local):
            Path(local).write_bytes(b'complete')
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.check_token.return_value = True
        client.get_disk_info.return_value = SimpleNamespace(used_space=8)
        client.listdir = listdir
        client.download.side_effect = download
        with (patch.dict(os.environ, {'DOWNLOAD_DIR': 'configured'}),
              patch.object(downloader, 'load_credentials', return_value=('fake-id', 'fake-secret')),
              patch.object(downloader, 'get_service_token', return_value=make_token()),
              patch.object(downloader, 'AsyncClient', return_value=client), self.assertLogs('Downloader')):
            self.assertTrue(asyncio.run(downloader.main('reader@example.invalid')))
            self.assertTrue(asyncio.run(downloader.main('reader@example.invalid', output_dir=Path('explicit'))))
        self.assertEqual(Path('configured/reader@example.invalid/file.txt').read_bytes(), b'complete')
        self.assertEqual(Path('explicit/file.txt').read_bytes(), b'complete')

    def test_all_cli_modes_use_configured_root_and_resume_without_moving_settings_or_logs(self):
        project = str(Path(downloader.__file__).resolve().parent)
        cases = [(False, False, False), (False, True, True), (True, False, True), (True, True, False)]
        for shared, batch, absolute in cases:
            with self.subTest(shared=shared, batch=batch, absolute=absolute), tempfile.TemporaryDirectory() as folder:
                cwd = Path(folder)
                root = cwd / 'backup folder'
                set_key(cwd / '.env', 'DOWNLOAD_DIR', str(root) if absolute else 'backup folder')
                set_key(cwd / '.env', 'MAX_RETRIES', '0')
                (cwd / 'users.csv').write_text('email\nreader@example.invalid\n')
                (cwd / 'disks.csv').write_text('VDHASH\nExampleHash\n')
                command = [sys.executable, '-B', '-c', CHILD, project, 'restart',
                           'shared' if shared else 'personal', 'csv' if batch else 'single']
                result = subprocess.run(command, cwd=cwd, capture_output=True, timeout=8)
                self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
                relative = Path('shared/ExampleHash' if shared else 'reader@example.invalid')
                destination = root / relative
                done = destination / 'done.txt'
                self.assertEqual(done.read_bytes(), b'complete')
                before = done.stat()
                (destination / 'partial-a.txt').write_bytes(b'partial')
                command[-3] = 'again'
                result = subprocess.run(command, cwd=cwd, capture_output=True, timeout=8)
                self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
                self.assertEqual(done.stat().st_mtime_ns, before.st_mtime_ns)
                self.assertEqual(done.stat().st_ino, before.st_ino)
                self.assertEqual((destination / 'partial-a.txt').read_bytes(), b'a' * 32768 + b'end')
                requests = [json.loads(line) for line in (cwd / 'requests.jsonl').read_text().splitlines()]
                self.assertEqual([item['name'] for item in requests if item['run'] == 'again'], ['partial-a.txt'])
                self.assertFalse((cwd / relative).exists())
                self.assertTrue((cwd / '.env').is_file())
                self.assertEqual(len(list((cwd / 'logs').glob('*.log'))), 2)
                self.assertFalse((root / 'logs').exists())
                self.assertFalse((root / '.env').exists())
