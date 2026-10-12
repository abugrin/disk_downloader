import asyncio
import io
import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import downloader
from token_fixtures import make_token


class StatusObserver(logging.Handler):
    def __init__(self, matches):
        super().__init__()
        self.matches = matches
        self.events = {name: threading.Event() for name in matches}
        self.messages = {name: [] for name in matches}

    def emit(self, record):
        message = record.getMessage()
        for name, (parts, required) in self.matches.items():
            if all(part in message for part in parts):
                self.messages[name].append(message)
                if len(self.messages[name]) >= required:
                    self.events[name].set()


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_cwd = Path.cwd()
        os.chdir(self.temp.name)
        downloader.directories.clear()
        downloader.sem = asyncio.Semaphore(downloader.MAX_STREAMS)

    async def asyncTearDown(self):
        os.chdir(self.previous_cwd)
        self.temp.cleanup()
        downloader.directories.clear()
        self.assertFalse(any(thread.name == 'downloader-status' for thread in threading.enumerate()))

    def client(self, listdir, download):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.check_token.return_value = True
        client.get_disk_info.return_value = SimpleNamespace(used_space=0)
        client.listdir = listdir
        client.download.side_effect = download
        return client

    async def test_status_repeats_during_blocking_auth_listing_and_downloads(self):
        observer = StatusObserver({
            'auth': (['этап: получение токена'], 3),
            'directories': (['этап: поиск папок', "текущий путь: 'disk:/slow'"], 2),
            'files': (['этап: поиск файлов', "текущий путь: '/'"], 2),
            'downloads': (['этап: скачивание', 'файлов завершено: 2/3', 'скачано: 1, ошибок: 1',
                           'в работе: 1, в очереди: 0', "самый долгий файл: '/slow/slow.txt'"], 2),
        })
        calls = {}
        auth_reported = False

        def token(client_id, client_secret, email):
            nonlocal auth_reported
            auth_reported = observer.events['auth'].wait(2)
            return make_token()

        async def wait_for_status(name):
            self.assertTrue(await asyncio.to_thread(observer.events[name].wait, 2), name)

        async def listdir(path):
            calls[path] = calls.get(path, 0) + 1
            if path == '/':
                if calls[path] == 2:
                    await wait_for_status('files')
                yield SimpleNamespace(type='dir', path='disk:/Файлы Мессенджера')
                yield SimpleNamespace(type='dir', path='disk:/slow')
            else:
                if calls[path] == 1:
                    await wait_for_status('directories')
                for name in ['fast.txt', 'failed.txt', 'slow.txt']:
                    yield SimpleNamespace(type='file', path=f'disk:/slow/{name}')

        async def download(remote, local):
            if remote.endswith('failed.txt'):
                raise OSError('Simulated download failure')
            if remote.endswith('slow.txt'):
                await wait_for_status('downloads')
            Path(local).write_text(remote)

        client = self.client(listdir, download)
        with (
            patch.object(downloader, 'load_dotenv'),
            patch.dict(os.environ, {'CLIENT_ID': 'test-id', 'CLIENT_SECRET': 'test-secret'}, clear=True),
            patch.object(downloader, 'get_service_token') as service,
            patch.object(downloader, 'AsyncClient', return_value=client),
            self.assertLogs('Downloader') as logs,
        ):
            service.side_effect = token
            downloader.log.addHandler(observer)
            try:
                completed = await downloader.main('test@example.invalid', status_interval=0.01)
            finally:
                downloader.log.removeHandler(observer)
        self.assertFalse(completed)
        self.assertTrue(auth_reported)
        self.assertTrue(all(event.is_set() for event in observer.events.values()))
        self.assertTrue(all('папок найдено: 1, просмотрено: 1' in msg for msg in observer.messages['directories']))
        self.assertNotIn('disk:/Файлы Мессенджера', calls)
        self.assertEqual(Path('test@example.invalid/slow/slow.txt').read_text(), '/slow/slow.txt')
        self.assertIn('файлов завершено: 3/3 (100.0%)', '\n'.join(logs.output))
        self.assertIn('скачано: 2, ошибок: 1', '\n'.join(logs.output))

    async def test_cancellation_stops_status_output(self):
        observer = StatusObserver({'waiting': (['этап: поиск папок', "текущий путь: '/'"], 2)})
        never = asyncio.Event()

        async def listdir(path):
            await never.wait()
            yield SimpleNamespace(type='file', path='disk:/unused')

        client = self.client(listdir, AsyncMock())
        with (
            patch.object(downloader, 'load_dotenv'),
            patch.dict(os.environ, {'CLIENT_ID': 'test-id', 'CLIENT_SECRET': 'test-secret'}, clear=True),
            patch.object(downloader, 'get_service_token') as service,
            patch.object(downloader, 'AsyncClient', return_value=client),
            self.assertLogs('Downloader'),
        ):
            service.return_value = make_token()
            downloader.log.addHandler(observer)
            task = asyncio.create_task(downloader.main('test@example.invalid', status_interval=0.01))
            try:
                self.assertTrue(await asyncio.to_thread(observer.events['waiting'].wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                downloader.log.removeHandler(observer)

    async def test_status_continues_during_retry_wait(self):
        observer = StatusObserver({'retry': (['этап: скачивание', 'в работе: 1'], 3)})
        attempts = 0

        async def listdir(path):
            yield SimpleNamespace(type='file', path='disk:/file.txt')

        async def download(remote, local):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError('test timeout')
            Path(local).write_text('complete')

        async def wait_for_status(delay):
            self.assertGreaterEqual(delay, 2)
            self.assertTrue(await asyncio.to_thread(observer.events['retry'].wait, 2))

        client = self.client(listdir, download)
        with (
            patch.object(downloader, 'load_dotenv'),
            patch.dict(os.environ, {'CLIENT_ID': 'test-id', 'CLIENT_SECRET': 'test-secret'}, clear=True),
            patch.object(downloader, 'get_service_token', return_value=make_token()),
            patch.object(downloader, 'AsyncClient', return_value=client),
            patch.object(downloader.asyncio, 'sleep', side_effect=wait_for_status),
            self.assertLogs('Downloader') as logs,
        ):
            downloader.log.addHandler(observer)
            try:
                self.assertTrue(await downloader.main('test@example.invalid', status_interval=0.01))
            finally:
                downloader.log.removeHandler(observer)
        self.assertEqual(attempts, 2)
        self.assertEqual(Path('test@example.invalid/file.txt').read_text(), 'complete')
        self.assertIn('Повтор 1/5', '\n'.join(logs.output))

    async def test_exception_stops_status_thread(self):
        with self.assertLogs('Downloader'), self.assertRaisesRegex(RuntimeError, 'test'):
            with downloader.ProgressReporter('test@example.invalid', 0.01):
                raise RuntimeError('test')


class StatusCliTests(unittest.TestCase):
    def test_custom_interval_is_forwarded(self):
        with (
            patch('builtins.input', return_value='test@example.invalid'),
            patch.object(downloader, 'download_users', new=AsyncMock(return_value=True)) as download,
        ):
            self.assertEqual(downloader.cli(['--email', 'test@example.invalid', '--status-interval', '2.5']), 0)
        download.assert_awaited_once_with(['test@example.invalid'], status_interval=2.5)

    def test_invalid_intervals_fail_before_download(self):
        for value in ['0', '-1', 'nan', 'inf', 'invalid']:
            with (
                self.subTest(value=value),
                patch.object(downloader, 'download_users') as download,
                patch('sys.stderr', new_callable=io.StringIO),
                self.assertRaises(SystemExit) as error,
            ):
                downloader.cli(['--status-interval', value])
            self.assertEqual(error.exception.code, 2)
            download.assert_not_called()
