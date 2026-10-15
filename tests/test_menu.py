import asyncio
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from dotenv import dotenv_values

import downloader
from environment_fixtures import isolated_environment
from token_fixtures import make_token


class MenuTests(unittest.TestCase):
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

    def test_credentials_preserve_other_settings_and_literal_secret(self):
        Path('.env').write_text("OTHER=value\nCLIENT_ID='old'\nCLIENT_SECRET='old'\n")
        secret = "fake'\\${OTHER}secret"
        downloader.save_credentials('new-id', secret)
        saved = dotenv_values('.env', interpolate=False)
        self.assertEqual(saved, {'OTHER': 'value', 'CLIENT_ID': 'new-id', 'CLIENT_SECRET': secret})
        os.environ.pop('CLIENT_ID')
        os.environ.pop('CLIENT_SECRET')
        self.assertEqual(downloader.load_credentials(), ('new-id', secret))
        self.assertFalse(list(Path.cwd().glob('.env-*.tmp')))
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(Path('.env').stat().st_mode), 0o600)
        self.assertNotIn(secret, self.output.getvalue())

    def test_credentials_are_loaded_from_working_directory_and_env_has_priority(self):
        Path('.env').write_text('CLIENT_ID=file-id\nCLIENT_SECRET=file-secret\n')
        with patch.dict(os.environ, {'CLIENT_ID': 'env-id', 'CLIENT_SECRET': 'env-secret'}):
            self.assertEqual(downloader.load_credentials(), ('env-id', 'env-secret'))
        self.assertEqual(downloader.load_credentials(), ('file-id', 'file-secret'))

    def test_configuration_keeps_secret_on_blank_password(self):
        Path('.env').write_text('CLIENT_ID=old-id\nCLIENT_SECRET=fake-secret\n')
        with self.answers('text', ['new-id']), self.answers('password', ['']):
            downloader.configure_credentials()
        self.assertEqual(downloader.load_credentials(), ('new-id', 'fake-secret'))
        self.assertNotIn('fake-secret', self.output.getvalue())

    def test_failed_save_is_atomic_and_does_not_change_process_credentials(self):
        original = 'CLIENT_ID=old-id\nCLIENT_SECRET=old-secret\n'
        Path('.env').write_text(original)
        self.assertEqual(downloader.load_credentials(), ('old-id', 'old-secret'))
        with patch.object(downloader, 'set_key', side_effect=[None, OSError('test')]), self.assertRaises(OSError):
            downloader.save_credentials('new-id', 'new-secret')
        self.assertEqual(Path('.env').read_text(), original)
        self.assertEqual(downloader.load_credentials(), ('old-id', 'old-secret'))
        self.assertFalse(list(Path.cwd().glob('.env-*.tmp')))

    def test_invalid_credentials_are_rejected_without_writing(self):
        for client_id, secret in [('', 'secret'), ('id', ''), ('id', 'line\nbreak'), ('id\x00', 'secret')]:
            with self.subTest(client_id=client_id), self.assertRaises(ValueError):
                downloader.save_credentials(client_id, secret)
        self.assertFalse(Path('.env').exists())

    def test_retry_setting_persists_and_preserves_credentials(self):
        Path('.env').write_text('CLIENT_ID=test-id\nCLIENT_SECRET=fake-secret\nMAX_RETRIES=2\nOTHER=value\n')
        with self.answers('select', ['3']) as select:
            downloader.configure_retries()
        self.assertEqual(select.call_args.kwargs['choices'], ['0', '1', '2', '3'])
        self.assertEqual(select.call_args.kwargs['default'], '2')
        self.assertEqual(downloader.retry_limit(), 3)
        with isolated_environment():
            self.assertEqual(downloader.load_credentials(), ('test-id', 'fake-secret'))
            self.assertEqual(downloader.retry_limit(), 3)
        downloader.save_credentials('new-id', 'new-fake-secret')
        self.assertEqual(dotenv_values('.env', interpolate=False), {
            'CLIENT_ID': 'new-id', 'CLIENT_SECRET': 'new-fake-secret', 'MAX_RETRIES': '3', 'OTHER': 'value',
        })
        self.assertNotIn('fake-secret', self.output.getvalue())

    def test_retry_setting_applies_to_download_from_same_menu(self):
        operation = AsyncMock(side_effect=TimeoutError)
        async def download(*args, **kwargs):
            try:
                await downloader.with_retries(operation, 'test request')
            except TimeoutError:
                return False
        with (
            patch.object(downloader, 'load_credentials', return_value=('fake-id', 'fake-secret')),
            self.answers('select', ['retries', '1', 'user', 'exit']),
            self.answers('text', ['user@example.invalid']),
            patch.object(downloader, 'download_users', side_effect=download),
            patch.object(downloader.asyncio, 'sleep', new_callable=AsyncMock) as sleep,
            patch.object(downloader, 'uniform', return_value=0),
            self.assertLogs('Downloader') as logs,
        ):
            self.assertEqual(downloader.terminal_menu(10), 1)
        self.assertEqual(operation.await_count, 2)
        sleep.assert_awaited_once_with(2)
        self.assertIn('Повтор 1/1', '\n'.join(logs.output))
        self.assertIn('Повторов после первой попытки: 1', self.output.getvalue())

    def test_invalid_retry_setting_can_be_repaired_from_menu(self):
        Path('.env').write_text('MAX_RETRIES=invalid\n')
        with (self.answers('select', ['user', 'retries', '3', 'exit']),
              patch.object(downloader, 'download_users') as download):
            self.assertEqual(downloader.terminal_menu(10), 0)
        download.assert_not_called()
        self.assertEqual(downloader.retry_limit(), 3)
        self.assertIn('MAX_RETRIES должен быть', self.output.getvalue())

    def test_retry_menu_defaults_to_zero_for_invalid_setting(self):
        Path('.env').write_text('MAX_RETRIES=invalid\n')
        with self.answers('select', ['0']) as select:
            downloader.configure_retries()
        self.assertEqual(select.call_args.kwargs['default'], '0')
        with isolated_environment():
            downloader.load_credentials()
            self.assertEqual(downloader.retry_limit(), 0)

    def test_retry_menu_caps_existing_setting_without_saving_on_cancel(self):
        original = 'MAX_RETRIES=100\nOTHER=value\n'
        Path('.env').write_text(original)
        with self.answers('select', [KeyboardInterrupt()]) as select, self.assertRaises(KeyboardInterrupt):
            downloader.configure_retries()
        self.assertEqual(select.call_args.kwargs['default'], '3')
        self.assertEqual(select.call_args.kwargs['choices'], ['0', '1', '2', '3'])
        self.assertEqual(Path('.env').read_text(), original)
        self.assertEqual(downloader.retry_limit(), 3)

    def test_retry_menu_rejects_values_above_three_without_saving(self):
        original = 'MAX_RETRIES=2\nOTHER=value\n'
        Path('.env').write_text(original)
        for value in ('4', '5'):
            with self.subTest(value=value), self.answers('select', [value]), self.assertRaises(ValueError):
                downloader.configure_retries()
            self.assertEqual(Path('.env').read_text(), original)
            self.assertEqual(downloader.retry_limit(), 2)

    def test_invalid_retry_setting_stops_cli_before_network_calls(self):
        for value in ['-1', '1.5', 'invalid', '']:
            with self.subTest(value=value), patch.dict(os.environ, {'MAX_RETRIES': value}):
                with (patch.object(downloader, 'download_users') as download,
                      patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit) as error):
                    downloader.cli(['--email', 'user@example.invalid'])
                self.assertEqual(error.exception.code, 2)
                download.assert_not_called()

    def test_cli_defaults_to_no_retries_and_caps_env_without_rewriting_it(self):
        for value, retries in [(None, 0), ('4', 3), ('5', 3), ('100', 3)]:
            with self.subTest(value=value), isolated_environment():
                original = 'OTHER=value\n' if value is None else f'OTHER=value\nMAX_RETRIES={value}\n'
                Path('.env').write_text(original)
                operation = AsyncMock(side_effect=TimeoutError)

                async def download(*args, **kwargs):
                    try:
                        await downloader.with_retries(operation, 'test request')
                    except TimeoutError:
                        return False

                with (
                    patch.object(downloader, 'download_users', side_effect=download),
                    patch.object(downloader.asyncio, 'sleep', new_callable=AsyncMock) as sleep,
                    patch.object(downloader, 'uniform', return_value=0),
                    self.assertLogs('Downloader'),
                ):
                    self.assertEqual(downloader.cli(['--email', 'user@example.invalid']), 1)
                self.assertEqual(operation.await_count, retries + 1)
                self.assertEqual([call.args[0] for call in sleep.await_args_list], [2, 4, 8][:retries])
                self.assertEqual(Path('.env').read_text(), original)

    def test_retry_cancel_and_failed_save_preserve_previous_setting(self):
        original = 'MAX_RETRIES=2\nCLIENT_SECRET=fake-secret\n'
        Path('.env').write_text(original)
        for error in [KeyboardInterrupt, OSError]:
            with self.subTest(error=error):
                selection = KeyboardInterrupt() if error is KeyboardInterrupt else '1'
                with (self.answers('select', [selection]),
                      patch.object(downloader, 'set_key', side_effect=OSError('test')),
                      self.assertRaises(error)):
                    downloader.configure_retries()
                self.assertEqual(Path('.env').read_text(), original)
                self.assertEqual(downloader.retry_limit(), 2)
                self.assertFalse(list(Path.cwd().glob('.env-*.tmp')))

    def test_retry_selection_with_real_keyboard_input(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        original_select = downloader.questionary.select
        with create_pipe_input() as keyboard:
            keyboard.send_text('\r')
            def select(*args, **kwargs):
                return original_select(*args, input=keyboard, output=DummyOutput(), **kwargs)
            with patch.object(downloader.questionary, 'select', side_effect=select):
                downloader.configure_retries()
        self.assertEqual(downloader.retry_limit(), 0)
        self.assertEqual(dotenv_values('.env')['MAX_RETRIES'], '0')

    def test_menu_all_modes_and_settings_dispatch(self):
        Path('users.csv').write_text('email\nuser@example.invalid\n')
        Path('disks.csv').write_text('VDHASH\nDiskA\nDiskB\n')
        with (
            patch.object(downloader, 'load_credentials', return_value=('id', 'secret')),
            self.answers('select', ['user', 'users', 'shared', 'shared_csv', 'configure', 'exit']),
            self.answers('text', ['user@example.invalid', 'reader@example.invalid', 'DiskA', 'reader@example.invalid']),
            self.answers('path', ['users.csv', 'disks.csv']),
            patch.object(downloader, 'configure_credentials') as configure,
            patch.object(downloader, 'download_users', new=AsyncMock(return_value=True)) as users,
            patch.object(downloader, 'download_shared_disks', new=AsyncMock(return_value=True)) as shared,
        ):
            self.assertEqual(downloader.terminal_menu(3), 0)
            self.assertEqual(users.await_count, 2)
            users.assert_awaited_with(['user@example.invalid'], status_interval=3)
            self.assertEqual([call.args for call in shared.await_args_list],
                             [('reader@example.invalid', ['DiskA']), ('reader@example.invalid', ['DiskA', 'DiskB'])])
            configure.assert_called_once()

    def test_first_download_prompts_for_missing_credentials(self):
        with (
            patch.object(downloader, 'load_credentials', return_value=('', '')),
            self.answers('select', ['shared', 'exit']),
            self.answers('text', ['reader@example.invalid', 'DiskA']),
            patch.object(downloader, 'configure_credentials') as configure,
            patch.object(downloader, 'download_shared_disks', new=AsyncMock(return_value=True)),
        ):
            self.assertEqual(downloader.terminal_menu(10), 0)
            configure.assert_called_once()

    def test_invalid_input_returns_to_menu_without_download(self):
        with (
            patch.object(downloader, 'load_credentials', return_value=('id', 'secret')),
            self.answers('select', ['shared', 'exit']),
            self.answers('text', ['reader@example.invalid', '../bad']),
            patch.object(downloader, 'download_shared_disks') as download,
        ):
            self.assertEqual(downloader.terminal_menu(10), 0)
            download.assert_not_called()
            self.assertIn('Invalid VDHASH', self.output.getvalue())

    def test_failed_download_returns_to_menu_and_preserves_failure_exit_status(self):
        with (
            patch.object(downloader, 'load_credentials', return_value=('id', 'secret')),
            self.answers('select', ['user', 'exit']),
            self.answers('text', ['user@example.invalid']),
            patch.object(downloader, 'download_users', new=AsyncMock(return_value=False)),
        ):
            self.assertEqual(downloader.terminal_menu(10), 1)

    def test_cli_uses_menu_and_ctrl_c_does_not_save_partial_settings(self):
        with (patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True),
              patch.object(downloader, 'terminal_menu', return_value=0) as menu):
            self.assertEqual(downloader.cli(['--status-interval', '4']), 0)
            menu.assert_called_once_with(4)
        with (patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True),
              self.answers('text', ['new-id']), self.answers('password', [KeyboardInterrupt()]),
              self.assertLogs('Downloader')):
            self.assertEqual(downloader.cli(['--configure']), 130)
        self.assertFalse(Path('.env').exists())

    def test_noninteractive_no_arguments_gives_actionable_error(self):
        with (patch('sys.stdin.isatty', return_value=False), patch('sys.stderr', new_callable=io.StringIO) as stderr,
              self.assertRaises(SystemExit) as error):
            downloader.cli([])
        self.assertEqual(error.exception.code, 2)
        self.assertIn('--email', stderr.getvalue())

    def test_repeated_menu_downloads_with_more_than_ten_files_use_new_semaphore(self):
        runs = []
        file_count = downloader.MAX_STREAMS + 4
        async def listdir(path):
            for number in range(file_count):
                yield SimpleNamespace(type='file', path=f'disk:/{number}.txt', size=8)
        def client_factory(**kwargs):
            client = AsyncMock()
            state = SimpleNamespace(active=0, maximum=0, client=client,
                                    semaphore=downloader.sem, loop=asyncio.get_running_loop())
            runs.append(state)
            started = asyncio.Event()
            async def download(remote, local):
                state.active += 1
                state.maximum = max(state.active, state.maximum)
                if state.active == downloader.MAX_STREAMS:
                    started.set()
                try:
                    async with asyncio.timeout(5):
                        await started.wait()
                    Path(local).write_text('complete')
                finally:
                    state.active -= 1
            client.__aenter__.return_value = client
            client.check_token.return_value = True
            client.get_disk_info.return_value = SimpleNamespace(used_space=0)
            client.listdir = listdir
            client.download.side_effect = download
            return client
        with (
            patch.object(downloader, 'load_credentials', return_value=('id', 'secret')),
            self.answers('select', ['user', 'user', 'exit']),
            self.answers('text', ['user@example.invalid', 'another@example.invalid']),
            patch.object(downloader, 'get_service_token', return_value=make_token()),
            patch.object(downloader, 'AsyncClient', side_effect=client_factory),
            self.assertLogs('Downloader') as logs,
        ):
            result = downloader.terminal_menu(10)
        self.assertEqual(result, 0, '\n'.join(logs.output))
        self.assertEqual(len(runs), 2)
        self.assertIsNot(runs[0].semaphore, runs[1].semaphore)
        self.assertIsNot(runs[0].loop, runs[1].loop)
        for run in runs:
            self.assertEqual(run.maximum, downloader.MAX_STREAMS)
            self.assertEqual(run.client.download.await_count, file_count)
            self.assertEqual(run.active, 0)
        self.assertEqual(len(list(Path('user@example.invalid').iterdir())), file_count)
        self.assertEqual(len(list(Path('another@example.invalid').iterdir())), file_count)
