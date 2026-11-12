import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import downloader


CHILD = r'''
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

project, destination, control, vdhash = sys.argv[1:]
sys.path[:0] = [project, str(Path(project) / 'tests')]
import httpx
from yadisk.sessions.async_httpx_session import AsyncHTTPXSession
import downloader
from environment_fixtures import isolated_environment

destination, control = Path(destination), Path(control)
body = ('contents of ' + vdhash).encode('utf-8')
sessions = []

class ControlledStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield body[:1]
        (control / 'downloading').touch()
        command = await asyncio.to_thread(sys.stdin.readline)
        if command.strip() != 'finish':
            raise RuntimeError('Download was not released by the test')
        yield body[1:]

    async def aclose(self):
        pass

def handler(request):
    if request.url.host == 'download.example.invalid':
        return httpx.Response(200, stream=ControlledStream())
    if '/operations/' in request.url.path:
        return httpx.Response(404, json={'error': 'DiskOperationNotFoundError'})
    if request.url.path.endswith('/virtual-disks'):
        return httpx.Response(200, json={'used_space': len(body)})
    if request.url.path.endswith('/download'):
        return httpx.Response(200, json={'href': 'https://download.example.invalid/file'})
    assert request.url.path.endswith('/virtual-disks/resources'), request.url.path
    return httpx.Response(200, json={'type': 'dir', '_embedded': {
        'offset': 0, 'limit': 500, 'total': 1, 'items': [{
            'type': 'file', 'path': 'vd:' + vdhash + ':disk:/file.txt',
            'size': len(body), 'sha256': hashlib.sha256(body).hexdigest(),
        }],
    }})

def session_factory(**kwargs):
    session = AsyncHTTPXSession(transport=httpx.MockTransport(handler), **kwargs)
    sessions.append(session)
    return session

async def token(*args):
    now = downloader.monotonic()
    return downloader.ServiceToken('fake', now + 3600, now + 3300)

async def run():
    (control / 'booted').touch()
    command = await asyncio.to_thread(sys.stdin.readline)
    if command.strip() != 'start':
        raise RuntimeError('Download was not started by the test')
    try:
        return await downloader.download_shared_disks('reader@example.invalid', [vdhash])
    except downloader.DestinationLockError as error:
        downloader.log.error('Ошибка тестового запуска: %s', downloader.sanitized_error(error))
        return False

with (isolated_environment({'MAX_RETRIES': '0'}),
      patch.object(downloader, 'download_directory', return_value=destination),
      patch.object(downloader, 'load_credentials', return_value=('fake', 'fake')),
      patch.object(downloader, 'get_service_token', side_effect=token),
      patch.object(downloader, 'AsyncHTTPXSession', side_effect=session_factory)):
    success = asyncio.run(run())
assert all(session.httpx_session.is_closed for session in sessions)
(control / 'finished').write_text(json.dumps({'success': success}), encoding='utf-8')
raise SystemExit(0 if success else 1)
'''


class SharedConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.destination = self.workspace / 'downloads'
        self.processes = []
        self.addCleanup(self.stop_processes)

    def stop_processes(self):
        for process, control, output in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=15)
            process.stdin.close()
            output.close()

    def start(self, vdhash):
        control = self.workspace / f'process-{len(self.processes)}'
        control.mkdir()
        output = (control / 'output.log').open('wb')
        project = str(Path(downloader.__file__).resolve().parent)
        process = subprocess.Popen(
            [sys.executable, '-B', '-X', 'utf8', '-c', CHILD,
             project, str(self.destination), str(control), vdhash],
            cwd=self.workspace, stdin=subprocess.PIPE, stdout=output,
            stderr=subprocess.STDOUT,
        )
        child = (process, control, output)
        self.processes.append(child)
        self.wait_for(child, 'booted')
        return child

    def output(self, child):
        return (child[1] / 'output.log').read_text(encoding='utf-8', errors='replace')

    def wait_for(self, child, marker):
        process, control, _ = child
        deadline = time.monotonic() + 20
        while not (control / marker).exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                break
            threading.Event().wait(0.05)
        self.assertTrue((control / marker).exists(), self.output(child))

    def send(self, child, command):
        child[0].stdin.write((command + '\n').encode('ascii'))
        child[0].stdin.flush()

    def finish(self, child):
        self.send(child, 'finish')
        self.assertEqual(child[0].wait(timeout=20), 0, self.output(child))

    def mapping(self):
        state = self.destination / 'shared' / '.disk-downloader'
        scope = hashlib.sha256(b'.').hexdigest()
        payload = json.loads((state / f'{scope}.json').read_text(encoding='utf-8'))
        return {tuple(item['remote']): tuple(item['local']) for item in payload['paths']}

    def assert_downloaded(self, vdhash, mapping):
        path = self.destination / 'shared'
        path = path.joinpath(*mapping[(vdhash,)]) / 'file.txt'
        self.assertEqual(path.read_bytes(), ('contents of ' + vdhash).encode('utf-8'))

    def test_distinct_disks_download_simultaneously_and_keep_both_mappings(self):
        first = self.start('FirstDisk')
        self.send(first, 'start')
        self.wait_for(first, 'downloading')
        second = self.start('SecondDisk')
        self.send(second, 'start')
        self.wait_for(second, 'downloading')
        self.assertIsNone(first[0].poll(), self.output(first))
        mapping = self.mapping()
        self.assertEqual(set(mapping), {('FirstDisk',), ('SecondDisk',)})
        self.finish(first)
        self.finish(second)
        self.assert_downloaded('FirstDisk', mapping)
        self.assert_downloaded('SecondDisk', mapping)

    def test_simultaneous_colliding_names_reserve_distinct_folders(self):
        first, second = self.start('Disk'), self.start('disk')
        self.send(first, 'start')
        self.send(second, 'start')
        self.wait_for(first, 'downloading')
        self.wait_for(second, 'downloading')
        self.assertIsNone(first[0].poll(), self.output(first))
        self.assertIsNone(second[0].poll(), self.output(second))
        mapping = self.mapping()
        self.assertEqual(set(mapping), {('Disk',), ('disk',)})
        self.assertNotEqual(mapping[('Disk',)][0].casefold(), mapping[('disk',)][0].casefold())
        self.finish(first)
        self.finish(second)
        self.assert_downloaded('Disk', mapping)
        self.assert_downloaded('disk', mapping)
        self.assertEqual(self.mapping(), mapping)

    def test_same_disk_is_rejected_without_touching_running_download(self):
        first = self.start('SameDisk')
        self.send(first, 'start')
        self.wait_for(first, 'downloading')
        mapping = self.mapping()
        root = self.destination / 'shared'
        try:
            with downloader.LocalPaths(root, persistent=True, state_dir=root / '.disk-downloader'):
                pass
        except downloader.DestinationBusyError:
            self.fail('The shared directory stays locked while one disk is downloading')
        partial, = (root / 'SameDisk').glob('.disk-download-*.part')
        before = partial.read_bytes()
        second = self.start('SameDisk')
        self.send(second, 'start')
        self.assertEqual(second[0].wait(timeout=20), 1, self.output(second))
        self.assertFalse((second[1] / 'downloading').exists())
        self.assertIn('DestinationBusyError', self.output(second))
        self.assertIsNone(first[0].poll(), self.output(first))
        self.assertEqual(partial.read_bytes(), before)
        self.assertEqual(self.mapping(), mapping)
        self.finish(first)
        self.assert_downloaded('SameDisk', mapping)

    def test_terminated_owner_releases_disk_for_restart(self):
        first = self.start('RestartDisk')
        self.send(first, 'start')
        self.wait_for(first, 'downloading')
        mapping = self.mapping()
        first[0].terminate()
        first[0].wait(timeout=15)
        second = self.start('RestartDisk')
        self.send(second, 'start')
        self.wait_for(second, 'downloading')
        self.finish(second)
        self.assertEqual(self.mapping(), mapping)
        self.assert_downloaded('RestartDisk', mapping)
