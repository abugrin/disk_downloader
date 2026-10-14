import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from yadisk.objects import ResourceObject

import downloader
from test_retries import disk_client, download_link


def metadata(content, algorithm='sha256'):
    values = {'size': len(content)}
    if algorithm:
        values[algorithm] = hashlib.new(algorithm, content).hexdigest()
    return ResourceObject(values)


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / 'file.txt'
        downloader.sem = asyncio.Semaphore(downloader.MAX_STREAMS)
        self.enterContext(patch.dict(os.environ, {'MAX_RETRIES': '3'}))
        self.logs = self.enterContext(self.assertLogs('Downloader'))

    async def test_complete_files_are_skipped_without_network_or_modification(self):
        for body, algorithm in [(b'complete', 'sha256'), (b'complete', 'md5'),
                                (b'complete', None), (b'', 'sha256'), (b'', None)]:
            with self.subTest(body=body, algorithm=algorithm):
                self.file.write_bytes(body)
                os.utime(self.file, ns=(1000000000, 1000000000))
                before = self.file.stat()
                client = SimpleNamespace(download=AsyncMock())
                progress = downloader.ProgressReporter('reader@example.invalid', 10)
                progress.phase('скачивание')
                progress.total_files(1)
                self.assertTrue(await downloader.safe_download(client, '/file', self.file, '[1/1]',
                                                               progress, metadata(body, algorithm)))
                client.download.assert_not_awaited()
                self.assertEqual(self.file.read_bytes(), body)
                self.assertEqual(self.file.stat().st_mtime_ns, before.st_mtime_ns)
                self.assertEqual(self.file.stat().st_ino, before.st_ino)
                self.assertEqual(progress.skipped_files, 1)
                progress.report()
        output = '\n'.join(self.logs.output)
        self.assertIn('файлов завершено: 1/1 (100.0%)', output)
        self.assertIn('скачано: 0, ошибок: 0, уже скачано: 1', output)
        self.assertIn('в работе: 0, в очереди: 0', output)

    async def test_partial_wrong_size_or_wrong_checksum_is_downloaded_from_zero(self):
        body = b'complete'
        for previous in [b'', b'par', b'corrupt!', b'overlong previous content']:
            with self.subTest(previous=previous):
                self.file.write_bytes(previous)
                calls = []
                def handler(request):
                    self.assertEqual(self.file.read_bytes(), previous)
                    if request.url.host != 'download.example.invalid':
                        return download_link()
                    calls.append(request)
                    self.assertNotIn('Range', request.headers)
                    return httpx.Response(200, content=body)
                async with disk_client(handler) as client:
                    self.assertTrue(await downloader.safe_download(client, '/file', self.file, '[1/1]',
                                                                   resource=metadata(body)))
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.file.read_bytes(), body)
                self.assertEqual(list(self.root.iterdir()), [self.file])

    async def test_truncated_successful_http_response_is_retried_before_publish(self):
        for wrong in [b'par', b'corrupt!']:
            with self.subTest(wrong=wrong):
                self.file.write_bytes(b'old')
                calls = 0
                def handler(request):
                    nonlocal calls
                    self.assertEqual(self.file.read_bytes(), b'old')
                    if request.url.host != 'download.example.invalid':
                        return download_link()
                    calls += 1
                    return httpx.Response(200, content=wrong if calls == 1 else b'complete')
                with (patch.object(downloader.asyncio, 'sleep', new_callable=AsyncMock) as sleep,
                      patch.object(downloader, 'uniform', return_value=0)):
                    async with disk_client(handler) as client:
                        self.assertTrue(await downloader.safe_download(client, '/file', self.file, '[1/1]',
                                                                       resource=metadata(b'complete')))
                self.assertEqual(calls, 2)
                self.assertEqual([call.args[0] for call in sleep.await_args_list], [2])
                self.assertEqual(self.file.read_bytes(), b'complete')
                self.assertFalse(list(self.root.glob('*.part')))

    async def test_exhausted_integrity_failures_keep_previous_file_and_remove_partial(self):
        self.file.write_bytes(b'old')
        calls = 0
        def handler(request):
            nonlocal calls
            if request.url.host != 'download.example.invalid':
                return download_link()
            calls += 1
            return httpx.Response(200, content=b'partial')
        with (patch.object(downloader.asyncio, 'sleep', new_callable=AsyncMock) as sleep,
              patch.object(downloader, 'uniform', return_value=0)):
            async with disk_client(handler) as client:
                self.assertFalse(await downloader.safe_download(client, '/file', self.file, '[1/1]',
                                                                resource=metadata(b'complete')))
        self.assertEqual(calls, 4)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [2, 4, 8])
        self.assertEqual(self.file.read_bytes(), b'old')
        self.assertEqual(list(self.root.iterdir()), [self.file])

    async def test_missing_metadata_does_not_overwrite_an_existing_file(self):
        self.file.write_bytes(b'keep')
        client = SimpleNamespace(download=AsyncMock())
        self.assertFalse(await downloader.safe_download(client, '/file', self.file, '[1/1]',
                                                        resource=ResourceObject({})))
        client.download.assert_not_awaited()
        self.assertEqual(self.file.read_bytes(), b'keep')
        self.assertIn('Нельзя проверить существующий файл', '\n'.join(self.logs.output))

    async def test_nonregular_destination_is_not_replaced(self):
        self.file.mkdir()
        client = SimpleNamespace(download=AsyncMock())
        self.assertFalse(await downloader.safe_download(client, '/file', self.file, '[1/1]',
                                                        resource=metadata(b'complete')))
        client.download.assert_not_awaited()
        self.assertTrue(self.file.is_dir())

    async def test_cancel_during_checksum_stops_worker_before_returning(self):
        started, stopped = threading.Event(), threading.Event()
        def checking(local_path, resource, stop):
            started.set()
            stop.wait(3)
            stopped.set()
            return False
        with patch.object(downloader, 'matches_resource', side_effect=checking):
            task = asyncio.create_task(downloader.check_local_file(self.file, metadata(b'complete')))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(stopped.is_set())
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        downloader.log.info('Checksum cancellation checked')


CHILD = r'''
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
import httpx
from yadisk.sessions.async_httpx_session import AsyncHTTPXSession
import downloader

mode, shared = sys.argv[2], sys.argv[3] == 'shared'
batch = len(sys.argv) > 4 and sys.argv[4] == 'csv'
downloader.MAX_STREAMS = 2
bodies = {'done.txt': b'complete', 'partial-a.txt': b'a' * 32768 + b'end',
          'partial-b.txt': b'b' * 32768 + b'end', 'queued.txt': b'queued'}
opened = set()
sessions = []
class Stream(httpx.AsyncByteStream):
    def __init__(self, name):
        self.name = name
    async def __aiter__(self):
        yield bodies[self.name][:-3]
        opened.add(self.name)
        if len(opened) == 2:
            Path('ready').touch()
        await asyncio.Event().wait()
    async def aclose(self):
        with Path('closed-streams').open('a') as file:
            file.write(self.name + '\n')
def handler(request):
    path = request.url.path
    if request.url.host == 'download.example.invalid':
        name = path.lstrip('/')
        assert 'Range' not in request.headers
        with Path('requests.jsonl').open('a') as file:
            file.write(json.dumps({'run': mode, 'name': name}) + '\n')
        if mode == 'stop' and name.startswith('partial-'):
            return httpx.Response(200, stream=Stream(name))
        return httpx.Response(200, content=bodies[name])
    if '/operations/' in path:
        return httpx.Response(404, json={'error': 'DiskOperationNotFoundError'})
    if path.endswith('/disk') or path.endswith('/virtual-disks'):
        return httpx.Response(200, json={'used_space': sum(map(len, bodies.values()))})
    if path.endswith('/download'):
        name = request.url.params['path'].rsplit('/', 1)[-1]
        return httpx.Response(200, json={'href': 'https://download.example.invalid/' + name})
    prefix = 'vd:ExampleHash:disk:' if shared else 'disk:'
    return httpx.Response(200, json={'type': 'dir', '_embedded': {
        'offset': 0, 'limit': 500, 'total': len(bodies), 'items': [
        {'type': 'file', 'path': prefix + '/' + name, 'size': len(body),
         'sha256': hashlib.sha256(body).hexdigest()} for name, body in bodies.items()]}})
def session_factory(**kwargs):
    session = AsyncHTTPXSession(transport=httpx.MockTransport(handler), **kwargs)
    sessions.append(session)
    return session
async def token(*args):
    now = downloader.monotonic()
    return downloader.ServiceToken('fake', now + 3600, now + 3300)
with (patch.object(downloader, 'load_credentials', return_value=('fake', 'fake')),
      patch.object(downloader, 'get_service_token', side_effect=token),
      patch.object(downloader, 'AsyncHTTPXSession', side_effect=session_factory)):
    args = ['--email', 'reader@example.invalid']
    if shared:
        args += ['--vdhash-csv', 'disks.csv'] if batch else ['--vdhash', 'ExampleHash']
    elif batch:
        args = ['--csv', 'users.csv']
    result = downloader.cli(args)
assert all(session.httpx_session.is_closed for session in sessions)
Path('closed-sessions').touch()
raise SystemExit(result)
'''


@unittest.skipUnless(os.name == 'posix', 'POSIX process signals')
class ProcessResumeTests(unittest.TestCase):
    def test_stop_and_restart_preserves_complete_files_and_restarts_partial_streams(self):
        project = str(Path(downloader.__file__).resolve().parent)
        for shared in [False, True]:
            for stop_signal in [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]:
                with self.subTest(shared=shared, stop_signal=stop_signal), tempfile.TemporaryDirectory() as temp:
                    cwd = Path(temp)
                    destination = cwd / ('shared/ExampleHash' if shared else 'reader@example.invalid')
                    command = [sys.executable, '-B', '-c', CHILD, project, 'stop', 'shared' if shared else 'personal']
                    with (cwd / 'first.log').open('wb') as log:
                        process = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
                        try:
                            deadline = time.monotonic() + 8
                            while not (cwd / 'ready').exists() and process.poll() is None and time.monotonic() < deadline:
                                time.sleep(0.01)
                            self.assertTrue((cwd / 'ready').exists(), (cwd / 'first.log').read_text())
                            completed = destination / 'done.txt'
                            before = completed.stat()
                            partials = list(destination.glob('.disk-download-*.part'))
                            self.assertEqual(len(partials), 2)
                            self.assertTrue(all(path.stat().st_size > 0 for path in partials))
                            process.send_signal(stop_signal)
                            code = process.wait(timeout=5)
                            self.assertEqual(code, 130 if stop_signal == signal.SIGINT else -stop_signal)
                        finally:
                            if process.poll() is None:
                                process.kill()
                            process.wait(timeout=5)
                    self.assertFalse((destination / 'partial-a.txt').exists())
                    self.assertFalse((destination / 'partial-b.txt').exists())
                    self.assertFalse((destination / 'queued.txt').exists())
                    if stop_signal == signal.SIGINT:
                        self.assertFalse(list(destination.glob('*.part')))
                        self.assertTrue((cwd / 'closed-sessions').exists())
                        self.assertEqual(set((cwd / 'closed-streams').read_text().splitlines()),
                                         {'partial-a.txt', 'partial-b.txt'})
                        log_file, = (cwd / 'logs').glob('*.log')
                        self.assertIn('Работа прервана пользователем', log_file.read_text(encoding='utf-8'))
                        self.assertIn('Завершение работы; код: 130', log_file.read_text(encoding='utf-8'))
                    else:
                        self.assertEqual(set(destination.glob('*.part')), set(partials))
                    command[-2] = 'restart'
                    resumed = subprocess.run(command, cwd=cwd, capture_output=True, timeout=8)
                    self.assertEqual(resumed.returncode, 0, resumed.stdout.decode() + resumed.stderr.decode())
                    self.assertEqual(completed.read_bytes(), b'complete')
                    self.assertEqual(completed.stat().st_mtime_ns, before.st_mtime_ns)
                    self.assertEqual(completed.stat().st_ino, before.st_ino)
                    self.assertEqual((destination / 'partial-a.txt').read_bytes(), b'a' * 32768 + b'end')
                    self.assertEqual((destination / 'partial-b.txt').read_bytes(), b'b' * 32768 + b'end')
                    self.assertEqual((destination / 'queued.txt').read_bytes(), b'queued')
                    requests = [json.loads(line) for line in (cwd / 'requests.jsonl').read_text().splitlines()]
                    self.assertEqual({item['name'] for item in requests if item['run'] == 'restart'},
                                     {'partial-a.txt', 'partial-b.txt', 'queued.txt'})
                    self.assertIn('уже скачано: 1'.encode('utf-8'), resumed.stdout)
                    self.assertTrue((cwd / 'closed-sessions').exists())
                    self.assertNotIn(b'Task was destroyed', resumed.stderr)
                    self.assertNotIn(b'was never awaited', resumed.stderr)
                    command[-2] = 'again'
                    repeated = subprocess.run(command, cwd=cwd, capture_output=True, timeout=8)
                    self.assertEqual(repeated.returncode, 0, repeated.stdout.decode() + repeated.stderr.decode())
                    self.assertIn('уже скачано: 4'.encode('utf-8'), repeated.stdout)
                    self.assertEqual(len(list((cwd / 'logs').glob('*.log'))), 3)
                    self.assertEqual((cwd / 'requests.jsonl').read_text().splitlines(),
                                     [json.dumps(item) for item in requests])
