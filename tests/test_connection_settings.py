import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from dotenv import dotenv_values

import downloader
from environment_fixtures import isolated_environment


class ConnectionSettingsTests(unittest.TestCase):
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

    def test_verification_is_enabled_when_setting_is_absent_or_blank(self):
        self.assertIs(downloader.tls_verification(), True)
        for value in ('', ' ', '\t'):
            with self.subTest(value=value), patch.dict(os.environ, {'VERIFY_TLS': value}):
                self.assertIs(downloader.tls_verification(), True)

    def test_boolean_values_are_case_insensitive_and_trimmed(self):
        for value, expected in (('true', True), (' TRUE ', True), ('false', False), (' FaLsE ', False)):
            with self.subTest(value=value), patch.dict(os.environ, {'VERIFY_TLS': value}):
                self.assertIs(downloader.tls_verification(), expected)

    def test_file_is_loaded_from_current_directory_and_environment_has_priority(self):
        Path('.env').write_text('VERIFY_TLS=false\n', encoding='utf-8')
        for value in ('true', ''):
            with self.subTest(value=value), patch.dict(os.environ, {'VERIFY_TLS': value}):
                self.assertIs(downloader.tls_verification(), True)
        self.assertIs(downloader.tls_verification(), False)

    def test_invalid_values_do_not_disable_verification(self):
        for value in ('0', '1', 'no', 'yes', 'off', 'none', 'invalid', 'false\ntrue'):
            with self.subTest(value=value), patch.dict(os.environ, {'VERIFY_TLS': value}):
                with self.assertRaisesRegex(ValueError, 'VERIFY_TLS должен быть true или false'):
                    downloader.tls_verification()

    def test_file_setting_is_not_interpolated(self):
        Path('.env').write_text('OTHER=false\nVERIFY_TLS=${OTHER}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'VERIFY_TLS должен быть true или false'):
            downloader.tls_verification()

    def test_setting_persists_and_preserves_credentials_and_other_settings(self):
        original = {'CLIENT_ID': 'fake-id', 'CLIENT_SECRET': 'fake${OTHER}secret', 'MAX_RETRIES': '2',
                    'DOWNLOAD_DIR': 'backups', 'MODIFIED_SINCE': '2026-10-01', 'OTHER': 'preserved'}
        Path('.env').write_text(''.join(f'{key}={value}\n' for key, value in original.items()), encoding='utf-8')
        with self.answers('select', ['false']) as select:
            downloader.configure_connection()
        self.assertEqual(select.call_args.kwargs['default'], 'true')
        self.assertEqual([(choice.title, choice.value) for choice in select.call_args.kwargs['choices']], [
            ('Нет — проверять сертификаты (по умолчанию)', 'true'),
            ('Да — игнорировать ошибки сертификата', 'false'),
            ('Назад', 'back'),
        ])
        self.assertEqual(dotenv_values('.env', interpolate=False), original | {'VERIFY_TLS': 'false'})
        self.assertEqual(os.environ['VERIFY_TLS'], 'false')
        self.assertIs(downloader.tls_verification(), False)
        with isolated_environment():
            self.assertIs(downloader.tls_verification(), False)
            self.assertEqual(downloader.load_credentials(), ('fake-id', 'fake${OTHER}secret'))
        self.assertNotIn(original['CLIENT_SECRET'], self.output.getvalue())

    def test_reenable_verification_survives_reload(self):
        Path('.env').write_text('VERIFY_TLS=false\nOTHER=preserved\n', encoding='utf-8')
        with self.answers('select', ['true']) as select:
            downloader.configure_connection()
        self.assertEqual(select.call_args.kwargs['default'], 'false')
        self.assertEqual(dotenv_values('.env', interpolate=False), {'VERIFY_TLS': 'true', 'OTHER': 'preserved'})
        self.assertIs(downloader.tls_verification(), True)
        with isolated_environment():
            self.assertIs(downloader.tls_verification(), True)

    def test_back_does_not_write_setting(self):
        with self.answers('select', ['back']), patch.object(downloader, 'save_settings') as save:
            downloader.configure_connection()
        save.assert_not_called()
        self.assertFalse(Path('.env').exists())
        original = 'VERIFY_TLS=false\nOTHER=preserved\n'
        Path('.env').write_text(original, encoding='utf-8')
        with self.answers('select', ['back']):
            downloader.configure_connection()
        self.assertEqual(Path('.env').read_text(encoding='utf-8'), original)
        self.assertIs(downloader.tls_verification(), False)

    def test_cancel_invalid_choice_and_failed_save_preserve_previous_setting(self):
        original = 'VERIFY_TLS=true\nCLIENT_SECRET=fake-secret\n'
        Path('.env').write_text(original, encoding='utf-8')
        cases = ((KeyboardInterrupt(), KeyboardInterrupt), (EOFError(), EOFError),
                 ('invalid', ValueError), ('false', OSError))
        for answer, error in cases:
            with self.subTest(error=error):
                with (self.answers('select', [answer]),
                      patch.object(downloader, 'set_key', side_effect=OSError('test')),
                      self.assertRaises(error)):
                    downloader.configure_connection()
                self.assertEqual(Path('.env').read_text(encoding='utf-8'), original)
                self.assertIs(downloader.tls_verification(), True)
                self.assertFalse(list(Path.cwd().glob('.env-*.tmp')))

    def test_invalid_setting_defaults_to_verification_when_repaired(self):
        Path('.env').write_text('VERIFY_TLS=invalid\n', encoding='utf-8')
        with self.answers('select', ['true']) as select:
            downloader.configure_connection()
        self.assertEqual(select.call_args.kwargs['default'], 'true')
        self.assertIs(downloader.tls_verification(), True)

    def test_selection_with_real_keyboard_input(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        original_select = downloader.questionary.select
        with create_pipe_input() as keyboard:
            keyboard.send_text('\x1b[B\r')

            def select(*args, **kwargs):
                return original_select(*args, input=keyboard, output=DummyOutput(), **kwargs)

            with patch.object(downloader.questionary, 'select', side_effect=select):
                downloader.configure_connection()
        self.assertIs(downloader.tls_verification(), False)
        self.assertEqual(dotenv_values('.env', interpolate=False)['VERIFY_TLS'], 'false')

    def test_menu_changes_apply_immediately_to_personal_and_shared_downloads(self):
        modes = []

        async def download(*args, **kwargs):
            modes.append(downloader.tls_verification())
            return True

        with (
            patch.object(downloader, 'load_credentials', return_value=('fake-id', 'fake-secret')),
            self.answers('select', ['settings', 'connection', 'false', 'back', 'user',
                                    'settings', 'connection', 'true', 'back', 'shared', 'exit']) as select,
            self.answers('text', ['reader@example.invalid', 'reader@example.invalid', 'ExampleHash']),
            patch.object(downloader, 'download_users', side_effect=download) as users,
            patch.object(downloader, 'download_shared_disks', side_effect=download) as shared,
        ):
            self.assertEqual(downloader.terminal_menu(30), 0)
        self.assertEqual(modes, [False, True])
        users.assert_awaited_once_with(['reader@example.invalid'], status_interval=30)
        shared.assert_awaited_once_with('reader@example.invalid', ['ExampleHash'], status_interval=30)
        choice = next(choice for call in select.call_args_list for choice in call.kwargs['choices']
                      if choice.value == 'connection')
        self.assertEqual(choice.title, 'Настройки соединения')
        self.assertIn('Проверка сертификатов: включена', self.output.getvalue())
        self.assertIn('Проверка сертификатов: отключена', self.output.getvalue())

    def test_invalid_setting_blocks_menu_download_then_can_be_repaired(self):
        Path('.env').write_text('VERIFY_TLS=invalid\n', encoding='utf-8')
        with (
            patch.object(downloader, 'load_credentials', return_value=('fake-id', 'fake-secret')),
            self.answers('select', ['user', 'users', 'shared', 'shared_csv', 'settings',
                                    'connection', 'true', 'back', 'user', 'exit']),
            self.answers('text', ['reader@example.invalid']),
            patch.object(downloader, 'download_users', new=AsyncMock(return_value=True)) as users,
            patch.object(downloader, 'download_shared_disks', new=AsyncMock(return_value=True)) as shared,
        ):
            self.assertEqual(downloader.terminal_menu(30), 0)
        users.assert_awaited_once_with(['reader@example.invalid'], status_interval=30)
        shared.assert_not_called()
        self.assertIs(downloader.tls_verification(), True)
        self.assertIn('VERIFY_TLS должен быть true или false', self.output.getvalue())

    def test_invalid_setting_blocks_every_cli_download_mode(self):
        Path('users.csv').write_text('email\nreader@example.invalid\n', encoding='utf-8')
        Path('disks.csv').write_text('VDHASH\nExampleHash\n', encoding='utf-8')
        modes = (
            ['--email', 'reader@example.invalid'],
            ['--csv', 'users.csv'],
            ['--email', 'reader@example.invalid', '--vdhash', 'ExampleHash'],
            ['--email', 'reader@example.invalid', '--vdhash-csv', 'disks.csv'],
        )
        for args in modes:
            with self.subTest(args=args), patch.dict(os.environ, {'VERIFY_TLS': 'invalid'}):
                with (patch.object(downloader, 'download_users') as users,
                      patch.object(downloader, 'download_shared_disks') as shared,
                      patch('sys.stderr', new_callable=io.StringIO),
                      self.assertRaises(SystemExit) as error):
                    downloader.cli(args)
                self.assertEqual(error.exception.code, 2)
                users.assert_not_called()
                shared.assert_not_called()
