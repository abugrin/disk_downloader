import io
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import downloader


class FileLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_cwd = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(os.chdir, self.old_cwd)
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.console = io.StringIO()
        self.enterContext(patch.object(downloader.log_handler, 'stream', self.console))
        self.previous_handlers = list(downloader.log.handlers)

    def tearDown(self):
        self.assertEqual(downloader.log.handlers, self.previous_handlers)

    def logs(self):
        return sorted(Path('logs').glob('*.log'))

    def test_cli_logs_to_utf8_file_and_console_in_current_directory(self):
        async def download(*args, **kwargs):
            downloader.log.info('Скачан файл: отчёт.txt')
            downloader.log.warning('Повтор запроса')
            return True
        with patch.object(downloader, 'download_users', side_effect=download):
            self.assertEqual(downloader.cli(['--email', 'reader@example.invalid']), 0)
        files = self.logs()
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r'^downloader_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{6}\.log$')
        content = files[0].read_text(encoding='utf-8')
        self.assertEqual(content, self.console.getvalue())
        self.assertIn(f'Версия {downloader.VERSION} | Лог запуска:', content.splitlines()[0])
        self.assertIn('Скачан файл: отчёт.txt', content)
        self.assertIn('Повтор запроса', content)
        self.assertIn(str(files[0].resolve()), content)
        self.assertIn('Завершение работы; код: 0', content)

    def test_equal_start_timestamps_do_not_overwrite_or_duplicate_log_records(self):
        handlers = []
        original = downloader.create_run_log
        def create():
            handler = original()
            handlers.append(handler)
            return handler
        with (patch.object(downloader, 'datetime') as clock,
              patch.object(downloader, 'create_run_log', side_effect=create),
              patch.object(downloader, 'run_cli', return_value=0)):
            clock.now.return_value = datetime(2026, 10, 13, 12, 34, 56, 123456)
            self.assertEqual(downloader.cli([]), 0)
            first = self.logs()[0]
            original_content = first.read_bytes()
            self.assertEqual(downloader.cli([]), 0)
        self.assertEqual([path.name for path in self.logs()], [
            'downloader_2026-10-13_12-34-56_123456.log',
            'downloader_2026-10-13_12-34-56_123456_1.log',
        ])
        self.assertEqual(first.read_bytes(), original_content)
        self.assertTrue(all(handler.stream is None for handler in handlers))
        for file in self.logs():
            content = file.read_text(encoding='utf-8')
            self.assertEqual(content.count('Лог запуска:'), 1)
            self.assertEqual(content.count(f'Версия {downloader.VERSION}'), 1)
            self.assertEqual(content.count('Завершение работы;'), 1)

    def test_multiple_downloads_from_menu_use_one_log(self):
        async def download(users, **kwargs):
            downloader.log.info('Download for %s', users[0])
            return True
        with (patch('sys.stdout', new_callable=io.StringIO),
              patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True),
              patch.object(downloader, 'load_credentials', return_value=('fake-id', 'fake-secret')),
              patch.object(downloader.questionary, 'select', return_value=SimpleNamespace(
                  unsafe_ask=Mock(side_effect=['user', 'user', 'exit']))),
              patch.object(downloader.questionary, 'text', return_value=SimpleNamespace(
                  unsafe_ask=Mock(side_effect=['first@example.invalid', 'second@example.invalid']))),
              patch.object(downloader, 'download_users', side_effect=download)):
            self.assertEqual(downloader.cli([]), 0)
        self.assertEqual(len(self.logs()), 1)
        content = self.logs()[0].read_text(encoding='utf-8')
        self.assertEqual(content.count('Download for '), 2)
        self.assertNotIn('fake-secret', content)

    def test_cancellation_and_unexpected_failure_close_log_without_secret_values(self):
        for error, status in [(KeyboardInterrupt(), 130), (RuntimeError('fake-secret'), 1), (SystemExit(2), 2)]:
            with self.subTest(error=type(error).__name__):
                before = set(self.logs())
                with patch.object(downloader, 'run_cli', side_effect=error):
                    if status == 130:
                        self.assertEqual(downloader.cli([]), 130)
                    else:
                        with self.assertRaises(type(error)):
                            downloader.cli([])
                file, = set(self.logs()) - before
                content = file.read_text(encoding='utf-8')
                self.assertIn(f'Завершение работы; код: {status}', content)
                self.assertNotIn('fake-secret', content)
                if status == 130:
                    self.assertIn('Работа прервана пользователем', content)
                if status == 1:
                    self.assertIn('Непредвиденная ошибка (RuntimeError)', content)
                self.assertFalse(any(isinstance(handler, logging.FileHandler) for handler in downloader.log.handlers))

    def test_log_creation_failure_stops_before_download(self):
        with (patch.object(downloader, 'create_run_log', side_effect=PermissionError('fake-secret')),
              patch.object(downloader, 'run_cli') as run):
            self.assertEqual(downloader.cli(['--email', 'reader@example.invalid']), 1)
        run.assert_not_called()
        self.assertIn(f'Версия {downloader.VERSION}', self.console.getvalue())
        self.assertIn('Не удалось создать файл журнала', self.console.getvalue())
        self.assertNotIn('fake-secret', self.console.getvalue())

    def test_script_launches_from_another_directory_get_separate_logs(self):
        command = [sys.executable, '-B', str(Path(downloader.__file__).resolve()), '--help']
        for count in [1, 2]:
            result = subprocess.run(command, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(len(self.logs()), count)
            self.assertIn(b'usage:', result.stdout)
            self.assertIn(f'Версия {downloader.VERSION}'.encode('utf-8'), result.stdout)
        for file in self.logs():
            self.assertIn('Завершение работы; код: 0', file.read_text(encoding='utf-8'))
