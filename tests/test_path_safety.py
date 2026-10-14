import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import downloader


def resource_for(body):
    return SimpleNamespace(size=len(body), sha256=hashlib.sha256(body).hexdigest())


class PersistentPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'backup'
        self.root.mkdir()
        self.state = self.base / 'state'

    def mapping(self):
        return downloader.LocalPaths(self.root, persistent=True, state_dir=self.state)

    def prepare(self, remotes):
        with self.mapping() as paths:
            paths.prepare(remotes)
            result = {remote: paths.get(remote) for remote in remotes}
            paths.save()
        return result

    def test_mapping_survives_added_removed_and_reordered_collisions(self):
        original = 'disk:/report?.txt'
        first = self.prepare([original])
        first[original].write_bytes(b'complete original')
        remotes = ['disk:/report*.txt', original, 'disk:/REPORT_.TXT']
        second = self.prepare(remotes)
        self.assertEqual(second[original], first[original])
        self.assertEqual(len({str(path).casefold() for path in second.values()}), 3)
        self.prepare([remotes[0]])
        repeated = self.prepare(list(reversed(remotes)))
        self.assertEqual(repeated, second)
        self.assertEqual(first[original].read_bytes(), b'complete original')

    def test_nested_directory_collisions_keep_existing_children_in_place(self):
        original = 'disk:/Docs?/Sub?/old.txt'
        first = self.prepare([original])
        first[original].parent.mkdir(parents=True)
        first[original].write_bytes(b'existing child')
        remotes = [original, 'disk:/Docs*/Sub*/new.txt', 'disk:/Docs?/Sub*/new.txt']
        second = self.prepare(remotes)
        self.assertEqual(second[original], first[original])
        self.assertNotEqual(second[remotes[0]].parent.parent, second[remotes[1]].parent.parent)
        self.assertNotEqual(second[remotes[0]].parent, second[remotes[2]].parent)
        self.assertEqual(first[original].read_bytes(), b'existing child')

    def test_new_colliding_directory_does_not_take_existing_file_path(self):
        original = 'disk:/reports?'
        first = self.prepare([original])[original]
        first.write_bytes(b'complete file')
        mapped = self.prepare([original, 'disk:/reports*/child.txt'])
        self.assertEqual(mapped[original], first)
        self.assertNotEqual(mapped['disk:/reports*/child.txt'].parent, first)
        mapped['disk:/reports*/child.txt'].parent.mkdir()
        self.assertEqual(first.read_bytes(), b'complete file')

    def test_legacy_ambiguous_file_is_preserved_without_claiming_its_name(self):
        legacy = self.root / 'report_.txt'
        legacy.write_bytes(b'legacy complete file')
        remotes = ['disk:/report*.txt', 'disk:/report?.txt']
        mapped = self.prepare(remotes)
        self.assertNotIn(legacy, mapped.values())
        self.assertEqual(len(set(mapped.values())), 2)
        self.assertEqual(legacy.read_bytes(), b'legacy complete file')
        self.assertEqual(self.prepare(list(reversed(remotes))), mapped)

    def test_legacy_ambiguous_suffix_candidate_is_not_claimed(self):
        legacy = self.root / 'report_.txt'
        legacy.write_bytes(b'legacy complete file')
        occupied_suffix = self.root / 'report___449dd7c0.txt'
        occupied_suffix.write_bytes(b'other legacy file')
        mapped = self.prepare(['disk:/report*.txt', 'disk:/report?.txt'])
        self.assertNotIn(legacy, mapped.values())
        self.assertNotIn(occupied_suffix, mapped.values())
        self.assertEqual(occupied_suffix.read_bytes(), b'other legacy file')

    def test_lone_normalized_source_does_not_claim_unidentified_legacy_copy(self):
        legacy = self.root / 'report_.txt'
        legacy.write_bytes(b'backup of removed report?.txt')
        mapped = self.prepare(['disk:/report*.txt'])
        self.assertNotEqual(mapped['disk:/report*.txt'], legacy)
        self.assertEqual(legacy.read_bytes(), b'backup of removed report?.txt')

    def test_moving_backup_and_state_together_keeps_assignments(self):
        remotes = ['disk:/report?.txt', 'disk:/report*.txt']
        original = self.prepare(remotes)
        moved = self.base / 'moved'
        moved.mkdir()
        moved_root, moved_state = moved / 'backup', moved / 'state'
        self.root.rename(moved_root)
        self.state.rename(moved_state)
        with downloader.LocalPaths(moved_root, persistent=True, state_dir=moved_state) as paths:
            paths.prepare(list(reversed(remotes)))
            self.assertEqual({remote: paths.get(remote).name for remote in remotes},
                             {remote: path.name for remote, path in original.items()})

    def test_legacy_ambiguous_directory_and_contents_are_preserved(self):
        legacy = self.root / 'Docs_'
        legacy.mkdir()
        (legacy / 'keep.txt').write_bytes(b'legacy child')
        mapped = self.prepare(['disk:/Docs*/a.txt', 'disk:/Docs?/b.txt'])
        self.assertTrue(all(path.parent != legacy for path in mapped.values()))
        self.assertEqual((legacy / 'keep.txt').read_bytes(), b'legacy child')

    def test_new_source_does_not_claim_an_existing_untracked_file(self):
        self.prepare(['disk:/known.txt'])
        unrelated = self.root / 'new.txt'
        unrelated.write_bytes(b'local file')
        mapped = self.prepare(['disk:/known.txt', 'disk:/new.txt'])
        self.assertNotEqual(mapped['disk:/new.txt'], unrelated)
        self.assertEqual(unrelated.read_bytes(), b'local file')

    def test_corrupt_manifest_fails_without_changing_existing_backup(self):
        old = self.prepare(['disk:/keep.txt'])['disk:/keep.txt']
        old.write_bytes(b'complete backup')
        manifest, = self.state.glob('*.json')
        for invalid in ['{', '{}', '{"version":1,"paths":"invalid"}']:
            with self.subTest(invalid=invalid):
                manifest.write_text(invalid, encoding='utf-8')
                with self.assertRaises(ValueError):
                    with self.mapping() as paths:
                        paths.prepare(['disk:/new.txt'])
                        paths.save()
                self.assertEqual(old.read_bytes(), b'complete backup')
                self.assertEqual(manifest.read_text(encoding='utf-8'), invalid)

    def test_manifest_save_does_not_delete_external_file_after_state_symlink_swap(self):
        outside = self.base / 'outside'
        outside.mkdir()
        probe = self.base / 'symlink-probe'
        try:
            probe.symlink_to(outside, target_is_directory=True)
            probe.unlink()
        except (OSError, NotImplementedError) as error:
            self.skipTest(f'Symbolic links unavailable: {error}')
        original_dump = json.dump
        sentinel = None

        def swap_after_write(payload, file, **kwargs):
            nonlocal sentinel
            original_dump(payload, file, **kwargs)
            sentinel = outside / Path(file.name).name
            sentinel.write_bytes(b'outside file')
            self.state.rename(self.base / 'parked-state')
            self.state.symlink_to(outside, target_is_directory=True)

        with self.mapping() as paths:
            paths.prepare(['disk:/file.txt'])
            with patch.object(downloader.json, 'dump', side_effect=swap_after_write):
                with self.assertRaises(OSError):
                    paths.save()
        self.assertIsNotNone(sentinel)
        self.assertEqual(sentinel.read_bytes(), b'outside file')

    def test_manifest_path_traversal_and_duplicate_destinations_are_rejected(self):
        self.prepare(['disk:/first.txt'])
        manifest, = self.state.glob('*.json')
        cases = [
            [{'remote': ['file'], 'local': ['..']}],
            [{'remote': ['file'], 'local': ['/outside']}],
            [{'remote': ['a'], 'local': ['same']}, {'remote': ['b'], 'local': ['SAME']}],
            [{'remote': ['parent', 'child'], 'local': ['missing', 'child']}],
        ]
        for entries in cases:
            with self.subTest(entries=entries):
                manifest.write_text(json.dumps({'version': 1, 'paths': entries}), encoding='utf-8')
                with self.assertRaises(ValueError):
                    with self.mapping():
                        pass

    def test_second_writer_is_blocked_and_lock_is_released_after_error(self):
        class Interrupted(Exception):
            pass

        with self.assertRaises(Interrupted):
            with self.mapping() as first:
                first.prepare(['disk:/first.txt'])
                first.save()
                with self.assertRaises(OSError):
                    with self.mapping():
                        self.fail('A second writer acquired the same destination lock')
                raise Interrupted()
        with self.mapping() as reopened:
            reopened.prepare(['disk:/first.txt'])
            reopened.save()

    def test_different_destination_scopes_do_not_share_a_lock_or_manifest(self):
        other_root = self.base / 'another-backup'
        with self.mapping() as first:
            first.prepare(['disk:/same.txt'])
            first.save()
            with downloader.LocalPaths(other_root, persistent=True, state_dir=self.state) as second:
                second.prepare(['disk:/same.txt'])
                second.save()
                self.assertNotEqual(first.get('disk:/same.txt'), second.get('disk:/same.txt'))
        self.assertEqual(len(list(self.state.glob('*.json'))), 2)

    def test_process_termination_releases_destination_lock(self):
        program = '''
import sys
import time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import downloader
with downloader.LocalPaths(Path(sys.argv[2]), persistent=True, state_dir=Path(sys.argv[3])) as paths:
    paths.prepare(['disk:/complete.txt'])
    paths.save()
    Path(sys.argv[4]).touch()
    time.sleep(30)
'''
        ready = self.base / 'ready'
        process = subprocess.Popen(
            [sys.executable, '-B', '-c', program, str(Path(downloader.__file__).parent),
             str(self.root), str(self.state), str(ready)],
            cwd=self.base, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), 'Child process did not acquire the destination lock')
            with self.assertRaises(OSError):
                with self.mapping():
                    self.fail('The child process lock was ignored')
        finally:
            if process.poll() is None:
                process.kill()
            _, errors = process.communicate(timeout=5)
        self.assertEqual(errors, b'')
        with self.mapping() as resumed:
            resumed.prepare(['disk:/complete.txt'])
            resumed.save()


class DownloadPathSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'backup'
        self.root.mkdir()
        self.outside = self.base / 'outside'
        self.outside.mkdir()
        downloader.sem = asyncio.Semaphore(downloader.MAX_STREAMS)
        self.enterContext(patch.dict(os.environ, {'MAX_RETRIES': '0'}))

    def symlink(self, link, destination):
        try:
            link.symlink_to(destination, target_is_directory=destination.is_dir())
        except (OSError, NotImplementedError) as error:
            self.skipTest(f'Symbolic links unavailable: {error}')

    async def test_adding_colliding_file_does_not_download_or_overwrite_original(self):
        state = self.base / 'state'
        original = 'disk:/report?.txt'
        original_body = b'complete original backup'
        with downloader.LocalPaths(self.root, persistent=True, state_dir=state) as paths:
            paths.prepare([original])
            original_path = paths.get(original)
            paths.save()
        original_path.write_bytes(original_body)
        before = original_path.stat()
        new = 'disk:/report*.txt'
        bodies = {original: original_body, new: b'new remote file'}
        calls = []

        async def download(remote, local):
            calls.append(remote)
            if remote == original.removeprefix('disk:'):
                raise OSError('The already downloaded source is unavailable')
            Path(local).write_bytes(bodies['disk:' + remote])

        client = SimpleNamespace(download=download)
        with downloader.LocalPaths(self.root, persistent=True, state_dir=state) as paths:
            paths.prepare(sorted(bodies))
            paths.save()
            for remote in sorted(bodies):
                self.assertTrue(await downloader.safe_download(
                    client, remote.removeprefix('disk:'), paths.get(remote), 'test',
                    resource=resource_for(bodies[remote]), destination_root=self.root,
                ))
            self.assertEqual(paths.get(original), original_path)
            self.assertEqual(paths.get(new).read_bytes(), bodies[new])
        self.assertEqual(calls, [new.removeprefix('disk:')])
        self.assertEqual(original_path.read_bytes(), original_body)
        self.assertEqual(original_path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(original_path.stat().st_ino, before.st_ino)

    async def test_parent_symlink_cannot_overwrite_outside_file(self):
        outside_file = self.outside / 'file.txt'
        outside_file.write_bytes(b'unrelated local data')
        self.symlink(self.root / 'linked', self.outside)
        client = SimpleNamespace(download=AsyncMock())
        result = await downloader.safe_download(
            client, '/linked/file.txt', self.root / 'linked' / 'file.txt', 'test',
            resource=resource_for(b'remote data'), destination_root=self.root,
        )
        self.assertFalse(result)
        client.download.assert_not_awaited()
        self.assertEqual(outside_file.read_bytes(), b'unrelated local data')
        self.assertEqual(list(self.outside.iterdir()), [outside_file])

    async def test_final_symlink_is_not_replaced_or_followed(self):
        outside_file = self.outside / 'file.txt'
        outside_file.write_bytes(b'unrelated local data')
        link = self.root / 'file.txt'
        self.symlink(link, outside_file)
        client = SimpleNamespace(download=AsyncMock())
        self.assertFalse(await downloader.safe_download(
            client, '/file.txt', link, 'test', resource=resource_for(b'remote data'),
            destination_root=self.root,
        ))
        client.download.assert_not_awaited()
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside_file.read_bytes(), b'unrelated local data')

    async def test_outside_destination_is_rejected_before_network(self):
        destination = self.outside / 'file.txt'
        destination.write_bytes(b'keep')
        client = SimpleNamespace(download=AsyncMock())
        self.assertFalse(await downloader.safe_download(
            client, '/file.txt', destination, 'test', resource=resource_for(b'remote data'),
            destination_root=self.root,
        ))
        client.download.assert_not_awaited()
        self.assertEqual(destination.read_bytes(), b'keep')

    async def test_parent_replaced_with_symlink_during_download_preserves_outside_files(self):
        probe = self.root / 'symlink-probe'
        self.symlink(probe, self.outside)
        probe.unlink()
        parent = self.root / 'sub'
        parent.mkdir()
        outside_file = self.outside / 'file.txt'
        outside_file.write_bytes(b'outside final')
        temporary_sentinel = None
        calls = 0

        async def download(remote, local):
            nonlocal temporary_sentinel, calls
            calls += 1
            Path(local).write_bytes(b'remote data')
            temporary_sentinel = self.outside / Path(local).name
            temporary_sentinel.write_bytes(b'outside temporary name')
            parent.rename(self.root / 'parked')
            self.symlink(parent, self.outside)

        self.assertFalse(await downloader.safe_download(
            SimpleNamespace(download=download), '/sub/file.txt', parent / 'file.txt', 'test',
            resource=resource_for(b'remote data'), destination_root=self.root,
        ))
        self.assertEqual(calls, 1)
        self.assertEqual(outside_file.read_bytes(), b'outside final')
        self.assertIsNotNone(temporary_sentinel)
        self.assertEqual(temporary_sentinel.read_bytes(), b'outside temporary name')
        self.assertEqual(len(list(self.outside.iterdir())), 2)

    async def test_unsafe_cleanup_does_not_suppress_download_cancellation(self):
        parent = self.root / 'sub'
        parent.mkdir()
        probe = self.root / 'probe'
        self.symlink(probe, self.outside)
        probe.unlink()
        outside_file = None

        async def download(remote, local):
            nonlocal outside_file
            outside_file = self.outside / Path(local).name
            outside_file.write_bytes(b'keep')
            parent.rename(self.root / 'parked')
            self.symlink(parent, self.outside)
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await downloader.safe_download(SimpleNamespace(download=download), '/sub/file', parent / 'file',
                                           'test', resource=resource_for(b'new'), destination_root=self.root)
        self.assertEqual(outside_file.read_bytes(), b'keep')


if __name__ == '__main__':
    unittest.main()
