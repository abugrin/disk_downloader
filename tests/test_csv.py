import asyncio
import csv
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import downloader
from environment_fixtures import isolated_environment
from token_fixtures import make_token


class CsvTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name, 'users.csv')

    def test_bom_semicolon_header_extra_columns_and_duplicates(self):
        self.path.write_text(
            '\nName;EMAIL\n"A, B"; first@example.invalid \n'
            '"C; D";second@example.invalid\nAgain;FIRST@example.invalid\n;\n',
            encoding='utf-8-sig',
        )
        self.assertEqual(downloader.read_users_csv(self.path), ['first@example.invalid', 'second@example.invalid'])

    def test_comma_header_and_quoted_fields(self):
        self.path.write_text('name,email\n"A, B",first@example.invalid\nC,second@example.invalid\n', encoding='utf-8')
        self.assertEqual(downloader.read_users_csv(self.path), ['first@example.invalid', 'second@example.invalid'])

    def test_headerless_list_preserves_order_and_skips_empty_rows(self):
        self.path.write_text('\n b@example.invalid \n\n"a@example.invalid"\nB@example.invalid\n', encoding='utf-8')
        self.assertEqual(downloader.read_users_csv(self.path), ['b@example.invalid', 'a@example.invalid'])

    def test_invalid_csv_is_rejected(self):
        cases = [
            ('', 'no users'),
            ('email\n\n', 'no users'),
            ('email,email\na@example.invalid,b@example.invalid\n', 'more than one'),
            ('login,name\na@example.invalid,A\n', 'column header'),
            ('email\na@example.invalid\nnot-an-email\n', 'line 3'),
            ('name,email\nA\n', 'line 2'),
            ('name,email\nA,\n', 'line 2'),
            ('../a@example.invalid\n', 'line 1'),
            ('a@example.invalid\nb@example.invalid,c@example.invalid\n', 'line 2'),
        ]
        for content, message in cases:
            with self.subTest(content=content):
                self.path.write_text(content, encoding='utf-8')
                with self.assertRaisesRegex(ValueError, message):
                    downloader.read_users_csv(self.path)

    def test_unclosed_quote_is_rejected(self):
        self.path.write_text('email\n"first@example.invalid\n', encoding='utf-8')
        with self.assertRaises(csv.Error):
            downloader.read_users_csv(self.path)

    def test_cli_validates_entire_csv_before_downloading(self):
        self.path.write_text('email\nfirst@example.invalid\ninvalid\n', encoding='utf-8')
        with (
            patch.object(downloader, 'download_users') as download,
            patch('sys.stderr', new_callable=io.StringIO) as stderr,
            self.assertRaises(SystemExit) as error,
        ):
            downloader.cli(['--csv', str(self.path)])
        self.assertEqual(error.exception.code, 2)
        self.assertIn('line 3', stderr.getvalue())
        download.assert_not_called()

    def test_cli_handles_missing_file_and_invalid_encoding(self):
        for content, expected in [(None, 'Cannot access file'), (b'email\n\xff\n', 'UTF-8')]:
            with self.subTest(content=content):
                if content is not None:
                    self.path.write_bytes(content)
                with (
                    patch.object(downloader, 'download_users') as download,
                    patch('sys.stderr', new_callable=io.StringIO) as stderr,
                    self.assertRaises(SystemExit) as error,
                ):
                    downloader.cli(['--csv', str(self.path)])
                self.assertEqual(error.exception.code, 2)
                self.assertIn(expected, stderr.getvalue())
                download.assert_not_called()

    def test_interactive_launch_and_exit_status(self):
        for completed, expected in [(True, 0), (False, 1)]:
            with (
                self.subTest(completed=completed),
                patch('builtins.input', return_value=' first@example.invalid '),
                patch.object(downloader, 'download_users', new=AsyncMock(return_value=completed)) as download,
            ):
                self.assertEqual(downloader.cli(['--email', ' first@example.invalid ']), expected)
                download.assert_awaited_once_with(['first@example.invalid'], status_interval=downloader.STATUS_INTERVAL)


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_cwd = Path.cwd()
        os.chdir(self.temp.name)
        downloader.sem = asyncio.Semaphore(downloader.MAX_STREAMS)
        self.listed = []

    def tearDown(self):
        os.chdir(self.previous_cwd)
        self.temp.cleanup()

    def make_client(self, email, tree, failed_file=None, listing_error=False):
        async def listdir(path):
            self.listed.append((email, path))
            if listing_error:
                raise RuntimeError('https://example.invalid/?token=must-not-be-logged')
            for kind, remote in tree.get(path, []):
                yield SimpleNamespace(type=kind, path=remote)

        async def download(remote, local):
            if remote == failed_file:
                raise OSError('Simulated download failure')
            Path(local).write_text(email, encoding='utf-8')

        client = AsyncMock()
        client.__aenter__.return_value = client
        client.check_token.return_value = True
        client.get_disk_info.return_value = SimpleNamespace(used_space=0)
        client.listdir = listdir
        client.download.side_effect = download
        return client

    def test_csv_to_downloads_continues_after_auth_listing_and_file_errors(self):
        users = ['auth@example.invalid', 'listing@example.invalid', 'partial@example.invalid', 'good@example.invalid']
        Path('users.csv').write_text('email\n' + '\n'.join(users), encoding='utf-8')
        clients = {
            users[1]: self.make_client(users[1], {}, listing_error=True),
            users[2]: self.make_client(users[2], {
                '/': [('dir', 'disk:/old'), ('file', 'disk:/failed.txt'), ('file', 'disk:/same.txt')],
                'disk:/old': [('file', 'disk:/old/keep.txt')],
            }, failed_file='/failed.txt'),
            users[3]: self.make_client(users[3], {'/': [('file', 'disk:/same.txt')]}),
        }

        def token(client_id, client_secret, email):
            if email == users[0]:
                raise RuntimeError('secret-must-not-be-logged')
            return make_token(email)

        with (
            patch('builtins.input', side_effect=AssertionError('CSV launch must not prompt')),
            patch.object(downloader, 'load_dotenv'),
            isolated_environment({'CLIENT_ID': 'test-id', 'CLIENT_SECRET': 'test-secret'}),
            patch.object(downloader, 'get_service_token') as service,
            patch.object(downloader, 'AsyncClient', side_effect=lambda token, **kwargs: clients[token]),
            self.assertLogs('Downloader') as logs,
        ):
            service.side_effect = token
            status = downloader.cli(['--csv', 'users.csv'])
        self.assertEqual(status, 1)
        self.assertEqual(
            [call.args[-1] for call in service.await_args_list],
            users,
        )
        self.assertEqual(service.await_count, len(users))
        for email in users[2:]:
            self.assertEqual(Path(email, 'same.txt').read_text(), email)
        self.assertNotIn((users[3], 'disk:/old'), self.listed)
        self.assertFalse(Path(users[3], 'old').exists())
        output = '\n'.join(logs.output)
        self.assertIn('Пользователей: 4; успешно: 1; с ошибками: 3', output)
        self.assertNotIn('must-not-be-logged', output)

    def test_colliding_user_folder_names_do_not_overwrite_each_other(self):
        users = ['a?@example.invalid', 'a*@example.invalid']
        Path('users.csv').write_text('\n'.join(users), encoding='utf-8')
        clients = {email: self.make_client(email, {'/': [('file', 'disk:/same.txt')]}) for email in users}
        with (
            patch.object(downloader, 'load_dotenv'),
            isolated_environment({'CLIENT_ID': 'test-id', 'CLIENT_SECRET': 'test-secret'}),
            patch.object(downloader, 'get_service_token') as service,
            patch.object(downloader, 'AsyncClient', side_effect=lambda token, **kwargs: clients[token]),
            self.assertLogs('Downloader') as logs,
        ):
            service.side_effect = lambda client_id, client_secret, email: make_token(email)
            self.assertEqual(downloader.cli(['--csv', 'users.csv']), 0)
        files = list(Path.cwd().glob('*/same.txt'))
        self.assertEqual(len(files), 2)
        self.assertEqual({path.read_text() for path in files}, set(users))
        self.assertIn('Пользователей: 2; успешно: 2; с ошибками: 0', '\n'.join(logs.output))

    def test_cancellation_stops_batch(self):
        with (
            patch.object(downloader, 'main', new=AsyncMock(side_effect=asyncio.CancelledError())) as download,
            self.assertLogs('Downloader'),
            self.assertRaises(asyncio.CancelledError),
        ):
            asyncio.run(downloader.download_users(['first@example.invalid', 'second@example.invalid']))
        self.assertEqual(download.await_count, 1)
