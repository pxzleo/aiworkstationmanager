from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from workstation_manager import supervisor


class MemoryPressureTests(unittest.TestCase):
    def test_pressure_records_largest_resident_processes(self):
        mib = 1024 * 1024
        memory = SimpleNamespace(available=22 * mib, total=128 * 1024 * mib, percent=99.98)
        swap = SimpleNamespace(used=17 * 1024 * mib, total=128 * 1024 * mib)
        processes = [SimpleNamespace(info={
            'pid': i, 'name': f'process-{i}',
            'memory_info': SimpleNamespace(rss=i * mib, private=i * 2 * mib),
        }) for i in range(1, 8)]
        logger = MagicMock()
        with patch.object(supervisor.psutil, 'virtual_memory', return_value=memory), \
                patch.object(supervisor.psutil, 'swap_memory', return_value=swap), \
                patch.object(supervisor.psutil, 'process_iter', return_value=iter(processes)):
            supervisor.log_memory(logger)
        logger.warning.assert_called_once()
        entries = logger.warning.call_args.args[1]
        self.assertEqual([item['pid'] for item in entries], [7, 6, 5, 4, 3])
        self.assertEqual(entries[0]['private_mib'], 14)

    def test_normal_memory_does_not_scan_processes(self):
        memory = SimpleNamespace(available=64, total=128, percent=50)
        swap = SimpleNamespace(used=0, total=128)
        logger = MagicMock()
        with patch.object(supervisor.psutil, 'virtual_memory', return_value=memory), \
                patch.object(supervisor.psutil, 'swap_memory', return_value=swap), \
                patch.object(supervisor.psutil, 'process_iter') as processes:
            supervisor.log_memory(logger)
        processes.assert_not_called()
        logger.warning.assert_not_called()

    def test_live_child_records_memory_without_waiting_for_exit(self):
        child = MagicMock()
        child.stdout = io.BytesIO()
        child.poll.side_effect = [None, None, 0]
        with patch.object(supervisor.subprocess, 'Popen', return_value=child), \
                patch.object(supervisor, 'output_available', return_value=0), \
                patch.object(supervisor, 'log_memory') as report, \
                patch.object(supervisor.time, 'monotonic', side_effect=[0, 31, 62]), \
                patch.object(supervisor.time, 'sleep'):
            self.assertEqual(supervisor.run_child(['python'], {}, MagicMock()), 0)
        self.assertEqual(report.call_count, 2)

    def test_pressure_scan_allocation_failure_keeps_child_running(self):
        memory = SimpleNamespace(available=22, total=128, percent=99)
        swap = SimpleNamespace(used=17, total=128)
        child = MagicMock()
        child.stdout = io.BytesIO()
        child.poll.side_effect = [None, 0]
        logger = MagicMock()
        with patch.object(supervisor.subprocess, 'Popen', return_value=child), \
                patch.object(supervisor, 'output_available', return_value=0), \
                patch.object(supervisor.time, 'monotonic', side_effect=[0, 31]), \
                patch.object(supervisor.time, 'sleep'), \
                patch.object(supervisor.psutil, 'virtual_memory', return_value=memory), \
                patch.object(supervisor.psutil, 'swap_memory', return_value=swap), \
                patch.object(supervisor.psutil, 'process_iter', side_effect=MemoryError('scan')), \
                patch.object(supervisor, 'stop_owned_manager') as stop:
            self.assertEqual(supervisor.run_child(['python'], {}, logger), 0)
        stop.assert_not_called()
        logger.exception.assert_called_once()

    def test_diagnostic_allocation_failure_does_not_prevent_start(self):
        logger = MagicMock()
        with patch.object(supervisor.psutil, 'virtual_memory', side_effect=MemoryError('memory')), \
                patch.object(supervisor, 'run_child', side_effect=KeyboardInterrupt) as run:
            with self.assertRaises(KeyboardInterrupt):
                supervisor.supervise(['python'], logger)
        run.assert_called_once()
        logger.exception.assert_called_once()
