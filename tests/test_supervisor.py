from __future__ import annotations

import errno
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from workstation_manager import supervisor
from workstation_manager.manager_logging import configure_manager_logging


class SupervisorTests(unittest.TestCase):
    def test_process_exit_retries_with_manager_only_recovery(self):
        environments = []
        def child(command, environment, logger):
            environments.append(dict(environment))
            if len(environments) == 2:
                raise KeyboardInterrupt
            return -1073741819
        logger = MagicMock()
        with patch.dict(os.environ, {supervisor.RECOVERY_ENV: "0"}), \
                patch.object(supervisor, "run_child", side_effect=child), \
                patch.object(supervisor, "log_memory"), \
                patch.object(supervisor.time, "sleep") as sleep:
            with self.assertRaises(KeyboardInterrupt):
                supervisor.supervise(["python", "-m", "workstation_manager"], logger)
        self.assertEqual(environments[0][supervisor.RECOVERY_ENV], "0")
        self.assertEqual(environments[1][supervisor.RECOVERY_ENV], "1")
        sleep.assert_called_once_with(15)
        logger.error.assert_called_once_with("AXIS exited: code=%s hex=0x%08x", -1073741819, 0xc0000005)

    def test_memory_shortage_retries_but_other_launch_errors_raise(self):
        with patch.object(supervisor, "run_child", side_effect=[OSError(errno.ENOMEM, "memory"), KeyboardInterrupt]), \
                patch.object(supervisor, "log_memory"), patch.object(supervisor.time, "sleep") as sleep:
            with self.assertRaises(KeyboardInterrupt):
                supervisor.supervise(["python"], MagicMock())
            sleep.assert_called_once_with(15)
        with patch.object(supervisor, "run_child", side_effect=FileNotFoundError("missing python")), \
                patch.object(supervisor, "log_memory"), patch.object(supervisor.time, "sleep") as sleep:
            with self.assertRaises(FileNotFoundError):
                supervisor.supervise(["python"], MagicMock())
            sleep.assert_not_called()

    def test_output_failure_cleans_up_owned_child_and_propagates(self):
        child = MagicMock()
        child.stdout.__iter__.side_effect = MemoryError("output allocation failed")
        with patch.object(supervisor.subprocess, "Popen", return_value=child), \
                patch.object(supervisor, "stop_owned_manager") as stop:
            with self.assertRaises(MemoryError):
                supervisor.run_child(["python"], {}, MagicMock())
        stop.assert_called_once_with(child)
        child.stdout.close.assert_called_once()

    def test_cleanup_does_not_traverse_model_service_descendants(self):
        child = MagicMock()
        child.poll.return_value = None
        parent, manager, sampler, model = [MagicMock() for _ in range(4)]
        parent.children.return_value = [manager]
        manager.cmdline.return_value = ["python", "-m", "workstation_manager"]
        manager.children.return_value = [sampler, model]
        sampler.cmdline.return_value = ["python", "-c", "from multiprocessing.spawn import spawn_main"]
        sampler.children.return_value = []
        model.cmdline.return_value = ["python", "main.py", "--port", "8189"]
        with patch.object(supervisor.psutil, "Process", return_value=parent):
            supervisor.stop_owned_manager(child)
        manager.kill.assert_called_once()
        sampler.kill.assert_called_once()
        model.kill.assert_not_called()
        model.children.assert_not_called()
        child.kill.assert_called_once()
        child.wait.assert_called_once_with(timeout=10)

    def test_windows_commit_shortage_retries(self):
        error = OSError("paging file too small")
        error.winerror = 1455
        with patch.object(supervisor, "run_child", side_effect=[error, KeyboardInterrupt]), \
                patch.object(supervisor, "log_memory"), patch.object(supervisor.time, "sleep") as sleep:
            with self.assertRaises(KeyboardInterrupt):
                supervisor.supervise(["python"], MagicMock())
            sleep.assert_called_once_with(15)

    def test_real_child_error_output_is_preserved_and_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.log"
            logger = configure_manager_logging(path)
            try:
                result = supervisor.run_child(
                    [sys.executable, "-c", "import sys;print('MemoryError: allocation failed',file=sys.stderr);print('api_key=secretvalue');sys.exit(7)"],
                    dict(os.environ), logger)
                self.assertEqual(result, 7)
                text = path.read_text(encoding="utf-8")
                self.assertIn("MemoryError: allocation failed", text)
                self.assertNotIn("secretvalue", text)
            finally:
                for handler in tuple(logger.handlers):
                    logger.removeHandler(handler)
                    handler.close()


if __name__ == "__main__":
    unittest.main()
