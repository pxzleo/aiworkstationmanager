"""Keep the AXIS manager available after an unexpected process exit."""
from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil

from .manager_logging import configure_manager_logging

RECOVERY_ENV = "WM_MANAGER_RECOVERY"
RETRY_SECONDS = 15


def log_memory(logger: logging.Logger) -> None:
    try:
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
    except OSError:
        logger.exception("无法读取故障时的系统内存状态")
        return
    logger.info("RAM available=%s MiB total=%s MiB; swap used=%s MiB total=%s MiB",
                memory.available // 1048576, memory.total // 1048576,
                swap.used // 1048576, swap.total // 1048576)


def stop_owned_manager(child: subprocess.Popen) -> None:
    # Stop only the manager and its sampling workers, never model-service descendants.
    try:
        pending = [psutil.Process(child.pid)] if child.poll() is None else []
    except psutil.NoSuchProcess:
        pending = []
    owned = []
    while pending:
        parent = pending.pop()
        try:
            children = parent.children()
        except psutil.NoSuchProcess:
            continue
        for process in children:
            try:
                arguments = process.cmdline()
            except psutil.NoSuchProcess:
                continue
            manager = any(arguments[index:index + 2] == ["-m", "workstation_manager"]
                          for index in range(len(arguments) - 1))
            sampler = any("multiprocessing.spawn" in argument or "resource_tracker" in argument
                          for argument in arguments)
            if manager or sampler:
                owned.append(process)
                pending.append(process)
    for process in reversed(owned):
        try:
            process.kill()
        except psutil.NoSuchProcess:
            continue
    if child.poll() is None:
        child.kill()
    child.wait(timeout=10)


def run_child(command: list[str], environment: dict[str, str], logger: logging.Logger) -> int:
    child = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        logger.info("AXIS child PID=%s recovery=%s", child.pid, environment.get(RECOVERY_ENV, "0"))
        assert child.stdout is not None
        for line in child.stdout:
            logger.info("runtime: %s", line.rstrip()[:8192])
        return child.wait()
    except BaseException:
        stop_owned_manager(child)
        raise
    finally:
        if child.stdout is not None:
            child.stdout.close()


def supervise(command: list[str], logger: logging.Logger, *, recovery: bool = False) -> None:
    environment = dict(os.environ)
    if recovery:
        environment[RECOVERY_ENV] = "1"
    while True:
        log_memory(logger)
        try:
            code = run_child(command, environment, logger)
        except (OSError, MemoryError) as error:
            # Windows can reject process creation when physical/commit memory is exhausted.
            if not isinstance(error, MemoryError) and error.errno != errno.ENOMEM and getattr(error, "winerror", None) not in {8, 14, 1455}:
                raise
            logger.exception("内存不足，AXIS 进程暂时无法启动")
        else:
            logger.error("AXIS exited: code=%s hex=0x%08x", code, code & 0xffffffff)
        log_memory(logger)
        environment[RECOVERY_ENV] = "1"
        logger.info("%s 秒后仅恢复管理器，不执行基础服务启动或默认场景切换", RETRY_SECONDS)
        time.sleep(RETRY_SECONDS)


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    logger = configure_manager_logging(root / "logs" / "manager-runtime.log")
    # A deployment restart may request manager-only startup for this one launch.
    recovery_flag = root / "logs" / "manager-recovery.once"
    recovery = recovery_flag.exists()
    if recovery:
        recovery_flag.unlink()
    try:
        supervise([sys.executable, "-u", "-X", "faulthandler", "-m", "workstation_manager"],
                  logger, recovery=recovery)
    except KeyboardInterrupt:
        logger.info("AXIS 恢复器被主动停止")
    except Exception:
        logger.exception("AXIS 恢复器异常退出")
        raise
    finally:
        for handler in tuple(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    main()
