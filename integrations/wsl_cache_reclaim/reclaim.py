"""Reclaim cold file cache from one WSL download service without restarting it."""
from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import subprocess


GIB = 1024 ** 3
HIGH = 16 * GIB
LOW = 12 * GIB
BATCH = GIB
HOST_LOW = 16 * GIB
CGROUP_ROOT = Path('/sys/fs/cgroup')
POWERSHELL = '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'


class CommandError(subprocess.CalledProcessError):
    def __str__(self) -> str:
        return f'{super().__str__()} {self.stderr or self.stdout}'


def command(arguments: list[str]) -> str:
    environment = os.environ.copy()
    # User systemd services do not inherit an interactive shell's WSL socket.
    environment.setdefault('WSL_INTEROP', '/run/WSL/1_interop')
    result = subprocess.run(arguments, capture_output=True, text=True,
                            timeout=10, env=environment)
    if result.returncode:
        raise CommandError(result.returncode, arguments, result.stdout, result.stderr)
    return result.stdout.strip()


def target_group(unit: str) -> Path | None:
    uid = os.getuid()
    parent = CGROUP_ROOT / f'user.slice/user-{uid}.slice/user@{uid}.service/app.slice'
    group = (parent / unit).resolve()
    if group.parent != parent:
        raise ValueError('拒绝回收根进程组或目标范围之外的内存')
    try:
        events = dict(line.split() for line in (group / 'cgroup.events').read_text().splitlines())
    except FileNotFoundError:
        return None
    if events['populated'] != '1':
        return None
    return group


def windows_available() -> int:
    output = command([POWERSHELL, '-NoProfile', '-NonInteractive', '-Command',
                      '$ErrorActionPreference="Stop"; '
                      '(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory'])
    return int(output) * 1024


def read_stats(group: Path) -> dict[str, int]:
    return {key: int(value) for key, value in
            (line.split() for line in (group / 'memory.stat').read_text().splitlines())}


def plan(stats: dict[str, int], available: int, armed: bool) -> tuple[bool, int]:
    cached = stats['file'] - stats['shmem']
    if cached <= LOW:
        return False, 0
    armed = armed or cached > HIGH or available < HOST_LOW
    cold_clean = max(0, stats['inactive_file'] - stats['file_dirty'] - stats['file_writeback'])
    amount = min(BATCH, cached - LOW, cold_clean) if armed else 0
    return armed, amount


def load_state(path: Path, inode: int) -> bool:
    if not path.exists():
        return False
    state = json.loads(path.read_text())
    return state['inode'] == inode and state['armed']


def request_reclaim(group: Path, amount: int) -> None:
    (group / 'memory.reclaim').write_text(f'{amount} swappiness=0\n')


def run(unit: str, state_path: Path) -> dict:
    group = target_group(unit)
    if group is None or not group.exists():
        state_path.unlink(missing_ok=True)
        return {'status': 'inactive', 'unit': unit}
    inode = group.stat().st_ino
    before = read_stats(group)
    available = windows_available()
    armed, amount = plan(before, available, load_state(state_path, inode))
    status = 'idle'
    if amount:
        try:
            request_reclaim(group, amount)
            status = 'reclaimed'
        except OSError as error:
            if error.errno != errno.EAGAIN:
                raise
            status = 'partial'
    after = read_stats(group)
    # New download writes can refill the cache during reclaim. Finish the cycle
    # once a successful request covers the excess measured at its start.
    if status == 'reclaimed' and amount == before['file'] - before['shmem'] - LOW:
        armed = False
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'inode': inode, 'armed': armed}))
    temporary.replace(state_path)
    return {'status': status, 'unit': unit, 'requested_bytes': amount,
            'file_before_bytes': before['file'], 'file_after_bytes': after['file'],
            'anon_before_bytes': before['anon'], 'anon_after_bytes': after['anon'],
            'windows_available_bytes': available}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unit', default='xolotl-prepare-1017-agentic.service')
    parser.add_argument('--state', type=Path,
                        default=Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
                        / 'wsl-download-cache-reclaim/state.json')
    arguments = parser.parse_args()
    print(json.dumps(run(arguments.unit, arguments.state)), flush=True)


if __name__ == '__main__':
    main()
