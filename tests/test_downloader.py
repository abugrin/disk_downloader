import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import downloader
from token_fixtures import make_token


class LocalPathTests(unittest.TestCase):
    def test_windows_names_and_unicode_are_normalized(self):
        cases = {
            'folder . ': 'folder',
            'a<>:"/\\|?*\x00\x1f.txt': 'a___________.txt',
            'CON.txt': '_CON.txt',
            'nul': '_nul',
            'COM1.log': '_COM1.log',
            'LPT².txt': '_LPT².txt',
            'COM10.txt': 'COM10.txt',
            'е\u0308лка.txt': 'ёлка.txt',
            '..': '_',
            '': '_',
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(downloader.normalize_name(original), expected)

    def test_long_unicode_names_preserve_extension_and_byte_limit(self):
        result = downloader.normalize_name('я' * 300 + '.txt')
        self.assertLessEqual(len(result.encode('utf-8')), downloader.MAX_NAME_BYTES)
        self.assertTrue(result.endswith('.txt'))

    def test_collisions_and_parent_mapping_preserve_distinct_files(self):
        mapper = downloader.LocalPaths(Path('output'))
        remotes = [
            'disk:/Folder /A?.txt', 'disk:/Folder /A*.txt',
            'disk:/Folder /a_.TXT', 'disk:/Folder/x.txt',
            'disk:/Folder /е\u0308.txt', 'disk:/Folder /ё.txt',
        ]
        with self.assertLogs('Downloader'):
            paths = [mapper.get(remote) for remote in remotes]
        self.assertEqual(len({str(path).casefold() for path in paths}), len(paths))
        self.assertEqual(paths[0].parent, paths[1].parent)
        self.assertNotEqual(paths[0].parent, paths[3].parent)
        for remote, path in zip(remotes, paths):
            self.assertEqual(mapper.get(remote.removeprefix('disk:')), path)

    def test_long_colliding_names_stay_within_byte_limit(self):
        mapper = downloader.LocalPaths(Path('output'))
        with self.assertLogs('Downloader'):
            paths = [mapper.get('/' + 'я' * 300 + suffix) for suffix in ['?.txt', '*.txt']]
        self.assertNotEqual(paths[0], paths[1])
        for path in paths:
            self.assertLessEqual(len(path.name.encode('utf-8')), downloader.MAX_NAME_BYTES)
            self.assertTrue(path.name.endswith('.txt'))

    def test_windows_extended_paths_support_drive_unc_and_existing_prefix(self):
        cases = {
            r'C:\output\file.txt': r'\\?\C:\output\file.txt',
            r'\\server\share\file.txt': r'\\?\UNC\server\share\file.txt',
            r'\\?\C:\output\file.txt': r'\\?\C:\output\file.txt',
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(downloader.windows_long_path(original), expected)

    def test_messenger_exclusion_uses_whole_path_components(self):
        self.assertTrue(downloader.is_excluded('disk:/Файлы Мессенджера/file.txt'))
        self.assertTrue(downloader.is_excluded('/folder/Файлы Мессенджера/child/file.txt'))
        self.assertFalse(downloader.is_excluded('/Файлы Мессенджера архив/file.txt'))
        self.assertFalse(downloader.is_excluded('/file.txt'))


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_cwd = Path.cwd()
        self.temp = tempfile.TemporaryDirectory()
        os.chdir(self.temp.name)
        downloader.directories.clear()
        downloader.sem = asyncio.Semaphore(downloader.MAX_STREAMS)
        self.email = 'test@example.invalid'
        self.listed_paths = []

    async def asyncTearDown(self):
        os.chdir(self.previous_cwd)
        self.temp.cleanup()
        downloader.directories.clear()

    async def run_main(self, tree, download):
        async def listdir(path):
            self.listed_paths.append(path)
            for kind, item_path in tree.get(path, []):
                yield SimpleNamespace(type=kind, path=item_path)

        client = AsyncMock()
        client.__aenter__.return_value = client
        client.check_token.return_value = True
        client.get_disk_info.return_value = SimpleNamespace(used_space=0)
        client.listdir = listdir
        client.download.side_effect = download
        with (
            patch.object(downloader, 'load_dotenv'),
            patch.dict(os.environ, {'CLIENT_ID': 'test-id', 'CLIENT_SECRET': 'test-secret'}, clear=True),
            patch.object(downloader, 'get_service_token') as service,
            patch.object(downloader, 'AsyncClient', return_value=client),
        ):
            service.return_value = make_token()
            await downloader.main(self.email)
        return client

    async def test_directory_and_file_errors_do_not_stop_other_downloads(self):
        tree = {
            '/': [('dir', 'disk:/blocked/child'), ('dir', 'disk:/good'),
                  ('file', 'disk:/failed.txt'), ('file', 'disk:/root.txt')],
            'disk:/blocked/child': [('file', 'disk:/blocked/child/file.txt')],
            'disk:/good': [('file', 'disk:/good/ok.txt')],
        }
        original_mkdir = Path.mkdir

        def mkdir(path, *args, **kwargs):
            if 'blocked' in path.parts:
                raise PermissionError('Simulated directory permission error')
            return original_mkdir(path, *args, **kwargs)

        async def download(remote, local):
            if remote == '/failed.txt':
                raise RuntimeError('https://example.invalid/download?token=must-not-be-logged')
            await asyncio.sleep(0)
            Path(local).write_text(remote)

        with patch.object(Path, 'mkdir', mkdir), self.assertLogs('Downloader') as logs:
            await self.run_main(tree, download)
        self.assertEqual(Path(self.email, 'root.txt').read_text(), '/root.txt')
        self.assertEqual(Path(self.email, 'good/ok.txt').read_text(), '/good/ok.txt')
        output = '\n'.join(logs.output)
        self.assertIn('Downloaded 2 files; failed files: 2; failed directories: 1', output)
        self.assertIn('blocked', output)
        self.assertIn('/failed.txt', output)
        self.assertNotIn('must-not-be-logged', output)

    async def test_root_only_disk_creates_destination(self):
        async def download(remote, local):
            Path(local).write_text('downloaded')

        with self.assertLogs('Downloader') as logs:
            await self.run_main({'/': [('file', 'disk:/root.txt')]}, download)
        self.assertEqual(Path(self.email, 'root.txt').read_text(), 'downloaded')
        self.assertIn('Downloaded 1 files; failed files: 0; failed directories: 0', '\n'.join(logs.output))

    async def test_cancellation_propagates(self):
        client = SimpleNamespace(download=AsyncMock(side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await downloader.safe_download(client, '/cancel.txt', Path(self.email, 'cancel.txt'), '[1/1]')

    async def test_downloads_use_original_remote_and_normalized_local_paths(self):
        tree = {
            '/': [('dir', 'disk:/bad . '), ('file', 'disk:/CON.txt')],
            'disk:/bad . ': [('file', 'disk:/bad . /a?.txt'), ('file', 'disk:/bad . /a*.txt')],
        }

        async def download(remote, local):
            Path(local).write_text(remote, encoding='utf-8')

        with self.assertLogs('Downloader') as logs:
            client = await self.run_main(tree, download)
        self.assertEqual(Path(self.email, '_CON.txt').read_text(), '/CON.txt')
        files = list(Path(self.email, 'bad').iterdir())
        self.assertEqual(len(files), 2)
        self.assertEqual({path.read_text() for path in files}, {'/bad . /a?.txt', '/bad . /a*.txt'})
        self.assertEqual(
            {call.args[0] for call in client.download.await_args_list},
            {'/CON.txt', '/bad . /a?.txt', '/bad . /a*.txt'},
        )
        self.assertIn('disk:/bad . ', self.listed_paths)
        self.assertIn('Downloaded 3 files; failed files: 0', '\n'.join(logs.output))

    async def test_messenger_folders_are_not_traversed_or_downloaded(self):
        tree = {
            '/': [('dir', 'disk:/Файлы Мессенджера'), ('dir', 'disk:/docs'),
                  ('dir', 'disk:/Файлы Мессенджера архив')],
            'disk:/docs': [('dir', 'disk:/docs/Файлы Мессенджера'), ('file', 'disk:/docs/keep.txt')],
            'disk:/Файлы Мессенджера архив': [('file', 'disk:/Файлы Мессенджера архив/keep.txt')],
        }

        async def download(remote, local):
            Path(local).write_text(remote, encoding='utf-8')

        with self.assertLogs('Downloader'):
            client = await self.run_main(tree, download)
        self.assertEqual(client.download.await_count, 2)
        self.assertNotIn('disk:/Файлы Мессенджера', self.listed_paths)
        self.assertNotIn('disk:/docs/Файлы Мессенджера', self.listed_paths)
        self.assertFalse(Path(self.email, 'Файлы Мессенджера').exists())
        self.assertFalse(Path(self.email, 'docs/Файлы Мессенджера').exists())
        self.assertTrue(Path(self.email, 'Файлы Мессенджера архив/keep.txt').is_file())

    async def test_filename_mapping_is_independent_of_api_order(self):
        files = [('file', 'disk:/A?.txt'), ('file', 'disk:/A*.txt'), ('file', 'disk:/a_.TXT')]
        mappings = []
        for ordered in [files, list(reversed(files))]:
            async def download(remote, local):
                Path(local).write_text(remote)

            with self.assertLogs('Downloader'):
                await self.run_main({'/': ordered}, download)
            mappings.append({p.read_text(): p.name for p in Path(self.email).iterdir()})
        self.assertEqual(mappings[0], mappings[1])
        self.assertEqual(len({name.casefold() for name in mappings[0].values()}), 3)

    async def test_empty_disk_creates_root_and_clears_previous_directories(self):
        downloader.directories.append(SimpleNamespace(path='disk:/stale'))
        with self.assertLogs('Downloader') as logs:
            client = await self.run_main({}, AsyncMock())
        client.download.assert_not_awaited()
        self.assertTrue(Path(self.email).is_dir())
        self.assertNotIn('disk:/stale', self.listed_paths)
        self.assertIn('Downloaded 0 files; failed files: 0', '\n'.join(logs.output))

    async def test_concurrent_downloads_respect_stream_limit(self):
        active = maximum = 0

        async def download(remote, local):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1

        files = [('file', f'disk:/{index}.txt') for index in range(downloader.MAX_STREAMS + 4)]
        with self.assertLogs('Downloader'):
            client = await self.run_main({'/': files}, download)
        self.assertEqual(client.download.await_count, len(files))
        self.assertEqual(maximum, downloader.MAX_STREAMS)
        self.assertEqual(active, 0)

    async def test_authentication_errors_stop_run_without_logging_secrets(self):
        with (
            patch.object(downloader, 'load_dotenv'),
            patch.dict(os.environ, {'CLIENT_ID': 'test-id', 'CLIENT_SECRET': 'test-secret'}, clear=True),
            patch.object(downloader, 'get_service_token', side_effect=RuntimeError('secret-must-not-be-logged')),
            patch.object(downloader, 'AsyncClient') as client,
            self.assertLogs('Downloader') as logs,
        ):
            completed = await downloader.main(self.email)
        self.assertFalse(completed)
        self.assertNotIn('secret-must-not-be-logged', '\n'.join(logs.output))
        client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
