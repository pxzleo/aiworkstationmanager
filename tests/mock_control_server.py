"""仅供浏览器验收：所有生命周期状态与动作都在进程内存中模拟。"""
from __future__ import annotations

import tempfile
from pathlib import Path

import uvicorn

from workstation_manager.app import create_app
from workstation_manager.config import Settings
from workstation_manager.control import (
    CommandResult, ControlConfig, ControlPlane, DrainHttpJsonCheck, EnvironmentConfig,
    HealthCheckConfig, HealthResult, SceneConfig, WslSystemdConfig,
)
from workstation_manager.database import Database
from workstation_manager.history import Sampler


def snapshot(_: Settings) -> dict:
    return {"sampled_at": "2099-01-01T00:00:00+00:00", "host": {}, "gpus": [],
            "docker": {"containers": []}, "ports": [], "collector_errors": []}


class MockRunner:
    def __init__(self) -> None:
        self.states = {"development.service": "running", "video.service": "stopped"}

    def __call__(self, args: list[str], _: float) -> CommandResult:
        verb, unit = args[-2:]
        if verb == "is-active":
            running = self.states[unit] == "running"
            return CommandResult(0 if running else 3, "active\n" if running else "inactive\n", "")
        if verb in {"start", "restart"}: self.states[unit] = "running"
        elif verb == "stop": self.states[unit] = "stopped"
        return CommandResult(0, "", "")


def environment(environment_id: str, *, gpu_ai: bool = False) -> EnvironmentConfig:
    return EnvironmentConfig(
        id=environment_id, name=f"Mock {environment_id}", configured=True,
        gpu_ai=gpu_ai,
        adapter=WslSystemdConfig(type="wsl_systemd", distro="MockDistro", scope="system",
                                 service=f"{environment_id}.service", timeout_seconds=2),
        health_checks=(HealthCheckConfig(type="adapter_status"),),
        preflight_checks=((DrainHttpJsonCheck(
            type="drain_http_json", purpose="active_requests",
            url="http://127.0.0.1:19109/mock-active", json_paths=("active",),
            wait_timeout_seconds=1, poll_interval_seconds=.1),) if gpu_ai else ()),
        allowed_actions=("start", "stop", "restart"),
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="wm-mock-browser-") as temporary:
        root = Path(temporary)
        settings = Settings(host="127.0.0.1", port=19109, database_path=root / "manager.db",
                            discovery_scripts_path=root / "scripts", scan_scripts_on_startup=False,
                            sample_interval_seconds=60)
        database = Database(settings.database_path)
        control = ControlPlane(ControlConfig(
            control_enabled=True,
            # development 故意缺少启动所需 GPU 健康检查，用于验证 stop 仍可安全执行。
            environments=(environment("development", gpu_ai=True), environment("video")),
            scenes=(
                SceneConfig(id="development", name="开发/agent场景", desired=("development",), conflicts=("video",)),
                SceneConfig(id="video", name="视频制作场景", desired=("video",), conflicts=("development",)),
            ),
        ), database, MockRunner(), safety_probe=type(
            "MockSafety", (), {"check": lambda self, check: HealthResult(True, "mock zero")})())
        app = create_app(settings, Sampler(settings, collector=snapshot), database=database,
                         control_plane=control)
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
