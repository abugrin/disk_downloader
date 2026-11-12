import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import httpx
from yadisk.sessions.async_httpx_session import AsyncHTTPXSession

import downloader
from environment_fixtures import isolated_environment
from token_fixtures import make_token


class SharedCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        previous_directory = Path.cwd()
        os.chdir(temporary.name)
        self.addCleanup(temporary.cleanup)
        self.addCleanup(os.chdir, previous_directory)
        self.enterContext(isolated_environment({
            'CLIENT_ID': 'test-id', 'CLIENT_SECRET': 'test-secret', 'MAX_RETRIES': '0',
        }))
        self.enterContext(patch.object(downloader, 'load_dotenv'))
        self.enterContext(patch.object(downloader, 'get_service_token', return_value=make_token()))
        self.enterContext(self.assertLogs('Downloader'))
        self.root = Path.cwd() / 'shared'
        self.state = self.root / '.disk-downloader'
        self.state.mkdir(parents=True)
        self.downloads = []
        self.links = []
        self.sessions = []
        self.bodies = {
            'Disk': {
                'CON.txt': b'reserved name',
                'Документы . /report*.txt': b'first report',
                'Документы . /report?.txt': b'second report',
            },
            'disk': {'summary.txt': b'other disk'},
        }
        self.old_maps = {
            '.': {
                ('Disk',): ('Disk',),
                ('disk',): ('disk__1044dec7',),
            },
            'Disk': {
                ('CON.txt',): ('_CON.txt',),
                ('Документы . ',): ('Документы',),
                ('Документы . ', 'report*.txt'): ('Документы', 'report_.txt'),
                ('Документы . ', 'report?.txt'): ('Документы', 'report___0b8e4273.txt'),
            },
            'disk__1044dec7': {('summary.txt',): ('summary.txt',)},
        }
        for scope, paths in self.old_maps.items():
            manifest = self.manifest(scope)
            manifest.write_text(json.dumps({
                'version': 1,
                'paths': [{'remote': list(remote), 'local': list(local)}
                          for remote, local in paths.items()],
            }), encoding='utf-8')
            manifest.with_suffix('.lock').write_bytes(b'\0')
        self.existing_files = {}
        for disk, files in self.bodies.items():
            folder = self.old_maps['.'][(disk,)][0]
            for remote, body in files.items():
                local = self.root / folder / Path(*self.old_maps[folder][tuple(remote.split('/'))])
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(body)
                os.utime(local, ns=(1_000_000_000, 1_000_000_000))
                self.existing_files[local] = (body, local.stat().st_mtime_ns, local.stat().st_ino)
        self.initial_paths = set(self.root.rglob('*'))
        self.enterContext(patch.object(downloader, 'AsyncHTTPXSession', side_effect=self.session))

    def manifest(self, scope):
        return self.state / (hashlib.sha256(scope.encode()).hexdigest() + '.json')

    def read_map(self, scope):
        payload = json.loads(self.manifest(scope).read_text(encoding='utf-8'))
        self.assertEqual(payload['version'], 1)
        entries = payload['paths']
        result = {tuple(entry['remote']): tuple(entry['local']) for entry in entries}
        self.assertEqual(len(result), len(entries))
        return result

    def assert_existing_files_unchanged(self, except_path=None):
        for path, (body, modified, inode) in self.existing_files.items():
            if path == except_path:
                continue
            with self.subTest(path=str(path.relative_to(self.root))):
                self.assertEqual(path.read_bytes(), body)
                self.assertEqual(path.stat().st_mtime_ns, modified)
                self.assertEqual(path.stat().st_ino, inode)

    def session(self, **kwargs):
        session = AsyncHTTPXSession(transport=httpx.MockTransport(self.handle), **kwargs)
        self.sessions.append(session)
        return session

    def handle(self, request):
        self.assertEqual(request.method, 'GET')
        self.assertNotIn('Range', request.headers)
        if request.url.host == 'download.example.invalid':
            disk, remote = request.url.params['disk'], request.url.params['file']
            self.downloads.append((disk, remote))
            return httpx.Response(200, content=self.bodies[disk][remote])
        if '/operations/' in request.url.path:
            return httpx.Response(404, json={'error': 'DiskOperationNotFoundError'})
        if request.url.path == '/v1/disk/virtual-disks':
            disk = request.url.params['vd_hash']
            return httpx.Response(200, json={'used_space': sum(map(len, self.bodies[disk].values()))})
        disk, separator, remote = request.url.params['path'].removeprefix('vd:').partition(':disk:')
        self.assertEqual(separator, ':disk:')
        remote = remote.strip('/')
        if request.url.path.endswith('/resources/download'):
            self.links.append((disk, remote))
            url = httpx.URL('https://download.example.invalid/file', params={'disk': disk, 'file': remote})
            return httpx.Response(200, json={'href': str(url)})
        self.assertEqual(request.url.path, '/v1/disk/virtual-disks/resources')
        items = {}
        parent = PurePosixPath(remote)
        for name, body in self.bodies[disk].items():
            path = PurePosixPath(name)
            if path.parent == parent:
                items[name] = {
                    'type': 'file', 'path': f'vd:{disk}:disk:/{name}', 'size': len(body),
                    'sha256': hashlib.sha256(body).hexdigest(),
                }
            for directory in path.parents:
                if directory != PurePosixPath('.') and directory.parent == parent:
                    items[str(directory)] = {'type': 'dir', 'path': f'vd:{disk}:disk:/{directory}'}
        return httpx.Response(200, json={'type': 'dir', '_embedded': {
            'items': [items[name] for name in sorted(items)], 'total': len(items),
        }})

    async def test_previous_maps_and_complete_files_survive_reordered_batch_and_repeat(self):
        for disks in [['disk', 'Disk'], ['Disk'], ['disk']]:
            self.assertTrue(await downloader.download_shared_disks('reader@example.invalid', disks))
            self.assert_existing_files_unchanged()
            self.assertEqual(set(self.root.rglob('*')), self.initial_paths)
            for scope, paths in self.old_maps.items():
                self.assertEqual(self.read_map(scope), paths)
        self.assertEqual(self.links, [])
        self.assertEqual(self.downloads, [])
        self.assertTrue(all(session.httpx_session.is_closed for session in self.sessions))

    async def test_partial_file_restarts_in_its_saved_location_without_changing_other_files(self):
        remote = 'Документы . /report?.txt'
        target = self.root / 'Disk' / 'Документы' / 'report___0b8e4273.txt'
        target.write_bytes(b'second')
        self.assertTrue(await downloader.download_shared_disks('reader@example.invalid', ['Disk', 'disk']))
        self.assertEqual(target.read_bytes(), self.bodies['Disk'][remote])
        self.assertEqual(self.links, [('Disk', remote)])
        self.assertEqual(self.downloads, [('Disk', remote)])
        self.assert_existing_files_unchanged(except_path=target)
        self.assertEqual(set(self.root.rglob('*')), self.initial_paths)
        for scope, paths in self.old_maps.items():
            self.assertEqual(self.read_map(scope), paths)

    async def test_adding_case_colliding_disk_preserves_previous_maps_and_downloads(self):
        self.bodies['DISK'] = {'new.txt': b'new disk content'}
        previous_disk_maps = {scope: self.manifest(scope).read_bytes()
                              for scope in self.old_maps if scope != '.'}
        self.assertTrue(await downloader.download_shared_disks('reader@example.invalid', ['DISK']))
        destinations = self.read_map('.')
        for remote, local in self.old_maps['.'].items():
            self.assertEqual(destinations[remote], local)
        self.assertEqual(len(destinations), 3)
        self.assertEqual(len({local[0].casefold() for local in destinations.values()}), 3)
        target = self.root.joinpath(*destinations[('DISK',)]) / 'new.txt'
        self.assertEqual(target.read_bytes(), self.bodies['DISK']['new.txt'])
        self.assertEqual(self.downloads, [('DISK', 'new.txt')])
        self.assert_existing_files_unchanged()
        for scope, contents in previous_disk_maps.items():
            self.assertEqual(self.manifest(scope).read_bytes(), contents)
        self.assertTrue(self.initial_paths <= set(self.root.rglob('*')))
