import asyncio
import csv
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import downloader
from environment_fixtures import isolated_environment


class EstimateMenuTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        previous = Path.cwd()
        os.chdir(directory.name)
        self.addCleanup(directory.cleanup)
        self.addCleanup(os.chdir, previous)
        self.enterContext(isolated_environment())
        self.enterContext(patch('sys.stdout', new_callable=io.StringIO))
        self.logs = self.enterContext(patch.object(downloader.log_handler, 'stream', io.StringIO()))

    def answers(self, method, values):
        return patch.object(downloader.questionary, method,
                            return_value=SimpleNamespace(unsafe_ask=Mock(side_effect=values)))

    def test_menu_routes_all_estimate_modes_and_back_without_downloading(self):
        Path('users.csv').write_text('email\nuser@example.invalid\n', encoding='utf-8')
        Path('disks.csv').write_text('VDHASH\nDiskA\nDiskB\n', encoding='utf-8')
        with (
            patch.object(downloader, 'load_credentials', return_value=('fake-id', 'fake-secret')),
            self.answers('select', ['estimate', 'back', 'estimate', 'user', 'estimate', 'users',
                                    'estimate', 'shared', 'estimate', 'shared_csv', 'exit']),
            self.answers('text', ['user@example.invalid', 'reader@example.invalid', 'DiskA',
                                  'reader@example.invalid']),
            self.answers('path', ['users.csv', 'disks.csv']),
            patch.object(downloader, 'estimate_disks', new=AsyncMock(return_value=True)) as estimate,
            patch.object(downloader, 'download_users') as users,
            patch.object(downloader, 'download_shared_disks') as shared,
        ):
            self.assertEqual(downloader.terminal_menu(30), 0)
        self.assertEqual([call.args for call in estimate.await_args_list],
                         [(['user@example.invalid'],), (['user@example.invalid'],),
                          (['DiskA'],), (['DiskA', 'DiskB'],)])
        self.assertEqual([call.kwargs for call in estimate.await_args_list], [
            {'status_interval': 30, 'reader_email': None, 'csv_report': False},
            {'status_interval': 30, 'reader_email': None, 'csv_report': True},
            {'status_interval': 30, 'reader_email': 'reader@example.invalid', 'csv_report': False},
            {'status_interval': 30, 'reader_email': 'reader@example.invalid', 'csv_report': True},
        ])
        users.assert_not_called()
        shared.assert_not_called()

    def test_menu_prompts_for_credentials_and_retains_incomplete_exit_status(self):
        with (
            patch.object(downloader, 'load_credentials', return_value=('', '')),
            self.answers('select', ['estimate', 'user', 'exit']),
            self.answers('text', ['user@example.invalid']),
            patch.object(downloader, 'configure_credentials') as configure,
            patch.object(downloader, 'estimate_disks', new=AsyncMock(return_value=False)),
        ):
            self.assertEqual(downloader.terminal_menu(30), 1)
        configure.assert_called_once_with()

    def test_cli_rejects_estimation_without_a_source_or_with_configuration(self):
        for args in (['--estimate'], ['--estimate', '--configure'], ['--estimate', '--vdhash', 'DiskA']):
            with self.subTest(args=args), patch('sys.stderr', new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as raised:
                    downloader.run_cli(args)
                self.assertEqual(raised.exception.code, 2)

    def test_report_write_error_fails_menu_run_and_is_logged(self):
        Path('users.csv').write_text('email\nuser@example.invalid\n', encoding='utf-8')
        with (
            patch.object(downloader, 'load_credentials', return_value=('fake-id', 'fake-secret')),
            self.answers('select', ['estimate', 'users', 'exit']),
            self.answers('path', ['users.csv']),
            patch.object(downloader.csv, 'writer', side_effect=OSError('No space left on device')),
            patch.object(downloader, 'estimate_disk', new=AsyncMock()) as estimate,
        ):
            self.assertEqual(downloader.terminal_menu(30), 1)
        estimate.assert_not_awaited()
        self.assertIn('Оценка не завершена', self.logs.getvalue())

    def test_report_open_failure_returns_failure_without_network(self):
        Path('estimates').write_bytes(b'existing file')
        with patch.object(downloader, 'estimate_disk', new=AsyncMock()) as estimate:
            self.assertFalse(asyncio.run(downloader.estimate_disks(['user@example.invalid'], csv_report=True)))
        estimate.assert_not_awaited()
        self.assertEqual(Path('estimates').read_bytes(), b'existing file')

    def test_csv_keeps_completed_rows_after_cancellation(self):
        result = downloader.VolumeEstimate('one@example.invalid', None, None, files=2, bytes=123)
        with patch.object(downloader, 'estimate_disk', new=AsyncMock(
            side_effect=[result, asyncio.CancelledError()],
        )):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(downloader.estimate_disks(['one@example.invalid', 'two@example.invalid'], csv_report=True))
        report, = Path('estimates').glob('*.csv')
        with report.open(encoding='utf-8-sig', newline='') as source:
            rows = list(csv.DictReader(source, delimiter=';'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['Email'], 'one@example.invalid')
        self.assertEqual(rows[0]['Объем к загрузке, байт'], '123')
        self.assertFalse(Path('.disk-downloader').exists())

    def test_csv_protects_formula_cells_and_each_run_preserves_previous_report(self):
        result = downloader.VolumeEstimate('=user@example.invalid', None, None, errors=1, error='+invalid')
        with patch.object(downloader, 'estimate_disk', new=AsyncMock(return_value=result)):
            self.assertFalse(asyncio.run(downloader.estimate_disks(['=user@example.invalid'], csv_report=True)))
            first, = Path('estimates').glob('*.csv')
            original = first.read_bytes()
            self.assertFalse(asyncio.run(downloader.estimate_disks(['=user@example.invalid'], csv_report=True)))
        self.assertEqual(len(list(Path('estimates').glob('*.csv'))), 2)
        self.assertEqual(first.read_bytes(), original)
        with first.open(encoding='utf-8-sig', newline='') as source:
            row, = csv.DictReader(source, delimiter=';')
        self.assertEqual(row['Email'], "'=user@example.invalid")
        self.assertEqual(row['Описание'], "'+invalid")
        self.assertEqual(row['Статус'], 'неполная оценка')

    def test_bad_destination_mapping_reports_every_disk_without_network(self):
        paths = downloader.LocalPaths(Path.cwd(), persistent=True, state_dir=Path.cwd() / '.disk-downloader')
        paths._state_dir.mkdir()
        paths._state_path.write_text('invalid json', encoding='utf-8')
        with patch.object(downloader, 'estimate_disk', new=AsyncMock()) as estimate:
            self.assertFalse(asyncio.run(downloader.estimate_disks(
                ['one@example.invalid', 'two@example.invalid'], csv_report=True,
            )))
        estimate.assert_not_awaited()
        report, = Path('estimates').glob('*.csv')
        with report.open(encoding='utf-8-sig', newline='') as source:
            rows = list(csv.DictReader(source, delimiter=';'))
        self.assertEqual([row['Email'] for row in rows], ['one@example.invalid', 'two@example.invalid'])
        self.assertTrue(all(row['Статус'] == 'неполная оценка' and row['Ошибок'] == '1' for row in rows))

    def test_estimate_menu_real_keyboard_navigation(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        original = downloader.questionary.select
        positions = iter((4, 4, 6))
        with create_pipe_input() as keyboard:
            def select(*args, **kwargs):
                keyboard.send_text('\x1b[B' * next(positions) + '\r')
                return original(*args, input=keyboard, output=DummyOutput(), **kwargs)
            with patch.object(downloader.questionary, 'select', side_effect=select):
                self.assertEqual(downloader.terminal_menu(30), 0)
        self.assertFalse(Path('estimates').exists())
