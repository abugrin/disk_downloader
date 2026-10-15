import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from dotenv import dotenv_values

import downloader
from environment_fixtures import isolated_environment


class ModifiedSinceParsingTests(unittest.TestCase):
    def test_date_uses_requested_second_and_fixed_moscow_offset(self):
        expected = datetime(2026, 10, 1, 0, 0, 1, tzinfo=timezone(timedelta(hours=3)))
        for value in ('2026-10-01', ' 2026-10-01 '):
            with self.subTest(value=value):
                parsed = downloader.parse_modified_since(value)
                self.assertEqual(parsed, expected)
                self.assertEqual(parsed.isoformat(), '2026-10-01T00:00:01+03:00')

    def test_empty_date_disables_filter(self):
        for value in ('', '  ', '\t'):
            with self.subTest(value=value):
                self.assertIsNone(downloader.parse_modified_since(value))

    def test_impossible_dates_and_non_date_formats_are_rejected(self):
        values = (
            '2026-02-29', '1900-02-29', '2026-04-31', '2026-00-01', '2026-13-01',
            '2026-10-00', '0000-01-01', '2026-1-01', '2026-10-1', '26-10-01',
            '20261001', '01-10-2026', '2026/10/01', '2026-10-01T00:00:01+03:00',
            '2026-10-01Z', '２０２６-１０-０１', '2026-10-01\n2026-10-02', 'invalid',
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                downloader.parse_modified_since(value)

    def test_leap_day_and_four_digit_year_boundaries(self):
        for value in ('2024-02-29', '2000-02-29', '0001-01-01', '9999-12-31'):
            with self.subTest(value=value):
                result = downloader.parse_modified_since(value)
                self.assertEqual(result.date().isoformat(), value)
                self.assertEqual(result.utcoffset(), timedelta(hours=3))
                self.assertEqual((result.hour, result.minute, result.second, result.microsecond), (0, 0, 1, 0))


class ModifiedSinceSettingsTests(unittest.TestCase):
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

    def test_default_and_environment_priority_including_disabled_filter(self):
        self.assertIsNone(downloader.modified_since())
        Path('.env').write_text('MODIFIED_SINCE=2026-10-01\n')
        with patch.dict(os.environ, {'MODIFIED_SINCE': '2026-10-02'}):
            self.assertEqual(downloader.modified_since().date().isoformat(), '2026-10-02')
        with patch.dict(os.environ, {'MODIFIED_SINCE': ''}):
            self.assertIsNone(downloader.modified_since())
        self.assertEqual(downloader.modified_since().date().isoformat(), '2026-10-01')

    def test_file_value_is_loaded_without_environment_interpolation(self):
        Path('.env').write_text('OTHER_DATE=2026-10-01\nMODIFIED_SINCE=${OTHER_DATE}\n')
        with self.assertRaises(ValueError):
            downloader.modified_since()

    def test_setting_persists_trimmed_date_and_preserves_other_settings(self):
        original = {'CLIENT_ID': 'fake-id', 'CLIENT_SECRET': 'fake-secret', 'MAX_RETRIES': '2',
                    'DOWNLOAD_DIR': 'backups', 'OTHER': 'preserved'}
        Path('.env').write_text(''.join(f'{key}={value}\n' for key, value in original.items()))
        with self.answers('text', [' 2026-10-01 ']):
            downloader.configure_modified_since()
        self.assertEqual(dotenv_values('.env', interpolate=False), original | {'MODIFIED_SINCE': '2026-10-01'})
        self.assertEqual(os.environ['MODIFIED_SINCE'], '2026-10-01')
        with isolated_environment():
            self.assertEqual(downloader.modified_since().isoformat(), '2026-10-01T00:00:01+03:00')
        self.assertNotIn('fake-secret', self.output.getvalue())

    def test_empty_input_resets_previous_date_and_survives_reload(self):
        Path('.env').write_text('MODIFIED_SINCE=2026-10-01\nOTHER=preserved\n')
        with self.answers('text', ['   ']) as text:
            downloader.configure_modified_since()
        self.assertEqual(text.call_args.kwargs['default'], '2026-10-01')
        self.assertIsNone(downloader.modified_since())
        self.assertEqual(dotenv_values('.env', interpolate=False), {'MODIFIED_SINCE': '', 'OTHER': 'preserved'})
        with isolated_environment():
            self.assertIsNone(downloader.modified_since())

    def test_prompt_uses_current_environment_value(self):
        Path('.env').write_text('MODIFIED_SINCE=2026-10-01\n')
        with patch.dict(os.environ, {'MODIFIED_SINCE': '2026-10-02'}), self.answers('text', ['2026-10-03']) as text:
            downloader.configure_modified_since()
        self.assertEqual(text.call_args.kwargs['default'], '2026-10-02')
        self.assertEqual(dotenv_values('.env', interpolate=False)['MODIFIED_SINCE'], '2026-10-03')

    def test_cancel_invalid_input_and_failed_save_preserve_previous_setting(self):
        original = 'MODIFIED_SINCE=2026-10-01\nOTHER=preserved\n'
        Path('.env').write_text(original)
        cases = ((KeyboardInterrupt(), KeyboardInterrupt), (EOFError(), EOFError),
                 ('2026-02-29', ValueError), ('2026-10-02', OSError))
        for answer, error in cases:
            with self.subTest(error=error):
                with (self.answers('text', [answer]),
                      patch.object(downloader, 'set_key', side_effect=OSError('test')),
                      self.assertRaises(error)):
                    downloader.configure_modified_since()
                self.assertEqual(Path('.env').read_text(), original)
                self.assertEqual(downloader.modified_since().date().isoformat(), '2026-10-01')
                self.assertFalse(list(Path.cwd().glob('.env-*.tmp')))

    def test_setting_is_applied_to_download_from_same_menu(self):
        async def download(users, **kwargs):
            self.assertEqual(downloader.modified_since().isoformat(), '2026-10-01T00:00:01+03:00')
            return True
        with (
            patch.object(downloader, 'load_credentials', return_value=('fake-id', 'fake-secret')),
            self.answers('select', ['modified_since', 'user', 'exit']) as select,
            self.answers('text', ['2026-10-01', 'reader@example.invalid']),
            patch.object(downloader, 'download_users', side_effect=download) as users,
        ):
            self.assertEqual(downloader.terminal_menu(30), 0)
        users.assert_awaited_once()
        choice = next(choice for choice in select.call_args.kwargs['choices'] if choice.value == 'modified_since')
        self.assertEqual(choice.title, 'Настроить дату начала выгрузки')
        self.assertIn('2026-10-01', self.output.getvalue())

    def test_invalid_existing_date_can_be_repaired_from_menu(self):
        Path('.env').write_text('MODIFIED_SINCE=invalid\n')
        with (
            patch.object(downloader, 'load_credentials', return_value=('fake-id', 'fake-secret')),
            self.answers('select', ['user', 'modified_since', 'user', 'exit']),
            self.answers('text', ['2026-10-01', 'reader@example.invalid']),
            patch.object(downloader, 'download_users', new=AsyncMock(return_value=True)) as users,
        ):
            self.assertEqual(downloader.terminal_menu(30), 0)
        users.assert_awaited_once_with(['reader@example.invalid'], status_interval=30)
        self.assertEqual(downloader.modified_since().date().isoformat(), '2026-10-01')

    def test_invalid_date_blocks_every_cli_download_mode(self):
        Path('users.csv').write_text('email\nreader@example.invalid\n')
        Path('disks.csv').write_text('VDHASH\nExampleHash\n')
        modes = (
            ['--email', 'reader@example.invalid'],
            ['--csv', 'users.csv'],
            ['--email', 'reader@example.invalid', '--vdhash', 'ExampleHash'],
            ['--email', 'reader@example.invalid', '--vdhash-csv', 'disks.csv'],
        )
        for args in modes:
            with self.subTest(args=args), patch.dict(os.environ, {'MODIFIED_SINCE': '2026-02-29'}):
                with (patch.object(downloader, 'download_users') as users,
                      patch.object(downloader, 'download_shared_disks') as shared,
                      patch('sys.stderr', new_callable=io.StringIO),
                      self.assertRaises(SystemExit) as error):
                    downloader.cli(args)
                self.assertEqual(error.exception.code, 2)
                users.assert_not_called()
                shared.assert_not_called()
