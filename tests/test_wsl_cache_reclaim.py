from __future__ import annotations

import errno
import importlib.util
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'integrations/wsl_cache_reclaim/reclaim.py'
spec = importlib.util.spec_from_file_location('wsl_cache_reclaim', SCRIPT)
reclaim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reclaim)
GIB = 1024 ** 3


class WslCacheReclaimTests(unittest.TestCase):
    def stats(self, file=18 * GIB, dirty=0):
        return {'file': file, 'shmem': 0, 'inactive_file': file, 'anon': 123,
                'file_dirty': dirty, 'file_writeback': 0}

    def test_hysteresis_keeps_cache_between_watermarks(self):
        self.assertEqual(reclaim.plan(self.stats(15 * GIB), 32 * GIB, False), (False, 0))
        self.assertEqual(reclaim.plan(self.stats(17 * GIB), 32 * GIB, False), (True, GIB))
        self.assertEqual(reclaim.plan(self.stats(14 * GIB), 32 * GIB, True), (True, GIB))
        self.assertEqual(reclaim.plan(self.stats(12 * GIB), 32 * GIB, True), (False, 0))

    def test_windows_pressure_triggers_before_high_watermark(self):
        self.assertEqual(reclaim.plan(self.stats(14 * GIB), 15 * GIB, False), (True, GIB))
        self.assertEqual(reclaim.plan(self.stats(12 * GIB), 15 * GIB, False), (False, 0))

    def test_does_not_request_dirty_or_shared_memory_budget(self):
        self.assertEqual(reclaim.plan(self.stats(dirty=18 * GIB), 32 * GIB, False), (True, 0))
        stats = self.stats()
        stats['shmem'] = 8 * GIB
        self.assertEqual(reclaim.plan(stats, 32 * GIB, False), (False, 0))
        self.assertEqual(reclaim.plan(self.stats(12 * GIB + 4096), 32 * GIB, True), (True, 4096))

    def test_missing_or_inactive_service_never_selects_global_cgroup(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(reclaim, 'CGROUP_ROOT', Path(directory)):
            self.assertIsNone(reclaim.target_group('download.service'))
            with self.assertRaisesRegex(ValueError, '根进程组'):
                reclaim.target_group('/')

    def test_active_file_cache_is_retained(self):
        stats = self.stats()
        stats['inactive_file'] = 0
        self.assertEqual(reclaim.plan(stats, 8 * GIB, False), (True, 0))

    def test_command_failures_include_actual_diagnostic(self):
        result = subprocess.CompletedProcess(['example'], 1, '', '具体失败原因')
        with patch.object(reclaim.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(reclaim.CommandError, '具体失败原因'):
                reclaim.command(['example'])

    def test_real_reclaim_uses_target_only_and_disables_swapping(self):
        with tempfile.TemporaryDirectory() as directory:
            group = Path(directory) / 'download'
            group.mkdir()
            (group / 'memory.stat').write_text('file %s\nshmem 0\ninactive_file %s\nfile_dirty 0\nfile_writeback 0\nanon 123\n' % (18 * GIB, 18 * GIB))
            (group / 'memory.reclaim').touch()
            state = Path(directory) / 'state.json'
            with patch.object(reclaim, 'target_group', return_value=group), \
                    patch.object(reclaim, 'windows_available', return_value=32 * GIB):
                result = reclaim.run('download.service', state)
            self.assertEqual(result['status'], 'reclaimed')
            self.assertEqual((group / 'memory.reclaim').read_text(), f'{GIB} swappiness=0\n')
            self.assertTrue(reclaim.load_state(state, group.stat().st_ino))
            self.assertFalse(reclaim.load_state(state, group.stat().st_ino + 1))

    def test_partial_reclaim_is_reported_and_other_errors_propagate(self):
        with tempfile.TemporaryDirectory() as directory:
            group = Path(directory)
            state = group / 'state.json'
            with patch.object(reclaim, 'target_group', return_value=group), \
                    patch.object(reclaim, 'windows_available', return_value=32 * GIB), \
                    patch.object(reclaim, 'read_stats', return_value=self.stats()), \
                    patch.object(reclaim, 'request_reclaim', side_effect=OSError(errno.EAGAIN, 'partial')):
                self.assertEqual(reclaim.run('download.service', state)['status'], 'partial')
            with patch.object(reclaim, 'target_group', return_value=group), \
                    patch.object(reclaim, 'windows_available', return_value=32 * GIB), \
                    patch.object(reclaim, 'read_stats', return_value=self.stats()), \
                    patch.object(reclaim, 'request_reclaim', side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError):
                    reclaim.run('download.service', state)

    def test_concurrent_download_writes_do_not_keep_reclaim_armed_forever(self):
        with tempfile.TemporaryDirectory() as directory:
            group = Path(directory)
            state = group / 'state.json'
            state.write_text('{"inode": %s, "armed": true}' % group.stat().st_ino)
            before = self.stats(12 * GIB + GIB // 2)
            after = self.stats(12 * GIB + 32 * 1024 ** 2)
            with patch.object(reclaim, 'target_group', return_value=group), \
                    patch.object(reclaim, 'windows_available', return_value=32 * GIB), \
                    patch.object(reclaim, 'read_stats', side_effect=[before, after]), \
                    patch.object(reclaim, 'request_reclaim') as request:
                self.assertEqual(reclaim.run('download.service', state)['status'], 'reclaimed')
            request.assert_called_once_with(group, GIB // 2)
            self.assertFalse(reclaim.load_state(state, group.stat().st_ino))


if __name__ == '__main__':
    unittest.main()
