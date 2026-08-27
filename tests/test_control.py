from __future__ import annotations

import asyncio
import multiprocessing
import ntpath
import sqlite3
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from workstation_manager.app import create_app
from workstation_manager.config import Settings
from workstation_manager.control import (
    CommandResult, ControlConfig, ControlError, ControlPlane, DefaultHealthProbe,
    DefaultWindowsProcessApi,
    DefaultSafetyProbe, DrainHttpJsonArraysCheck, DrainHttpJsonCheck,
    DrainHttpPrometheusCheck, EnvironmentConfig, H3VideoProfileCheck,
    HealthCheckConfig, HealthResult, LoopbackHttpHealthCheck, LoopbackPortAvailableCheck,
    LoopbackTcpHealthCheck, NvidiaGpuMemoryCheck, NvidiaGpuProcessHealthCheck,
    HttpJsonObjectHasKeysCheck, PrometheusSeries, WindowsComfyCapabilityHealthCheck,
    WindowsComfyProcessAdapter, WindowsComfyProcessConfig, WindowsPathDiskCheck,
    RequiredDependencyCheck, SceneConfig, SubprocessRunner, WslDockerComposeAdapter,
    WslDockerComposeConfig, WslSystemdAdapter, WslSystemdConfig, WslSystemdRootAdapter,
    WslSystemdRootConfig, load_control_config,
    WslDockerComposeGpuBindingHealthCheck, WslPathDiskCheck, WslPortAvailableCheck,
    WslSystemdGpuBindingHealthCheck,
    _windows_reparse_error,
)
from workstation_manager.database import Database, DatabaseError
from workstation_manager.history import Sampler


def fake_snapshot(_: Settings) -> dict:
    return {"sampled_at": "2099-01-01T00:00:00+00:00", "host": {}, "gpus": [],
            "docker": {"containers": []}, "ports": [], "collector_errors": []}


def env(environment_id: str, adapter: WslSystemdConfig, actions=("start", "stop")) -> EnvironmentConfig:
    return EnvironmentConfig(id=environment_id, name=environment_id, configured=True,
                             adapter=adapter, health_checks=(HealthCheckConfig(type="adapter_status"),),
                             allowed_actions=actions)


GPU_UUID = "GPU-12345678-1234-1234-1234-123456789abc"


def healthy_windows_path_probe(path: str):
    return True, 20 * 1024 ** 3, None


def slow_windows_path_probe(path: str):
    time.sleep(10)
    return True, 20 * 1024 ** 3, None


def result_then_lingering_thread_windows_path_probe(path: str):
    threading.Thread(target=time.sleep, args=(10,), daemon=False).start()
    return True, 20 * 1024 ** 3, None


def ai_preflight_checks(environment_id: str, *, drain: bool = True):
    checks = []
    if drain:
        checks.append(DrainHttpJsonCheck(
            type="drain_http_json", purpose="active_requests",
            url="http://127.0.0.1:8000/metrics", json_paths=("active",),
            wait_timeout_seconds=1, poll_interval_seconds=.1))
    checks.extend((
        NvidiaGpuMemoryCheck(type="nvidia_gpu_memory", gpu_uuid=GPU_UUID,
                             min_free_mib=1024),
        WslPathDiskCheck(type="wsl_path_disk", purpose="model", distro="Ubuntu",
                         path="/models/approved", min_free_mib=1024),
        LoopbackPortAvailableCheck(type="loopback_port_available", port=8000,
                                   owner_environment_id=environment_id),
    ))
    return tuple(checks)


class HealthySafetyProbe:
    def check(self, check):
        return HealthResult(True, "mock safety healthy")


class StatefulRunner:
    def __init__(self, states: dict[str, str], fail_start: str | None = None) -> None:
        self.states = states
        self.fail_start = fail_start
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], timeout: float) -> CommandResult:
        self.calls.append(args)
        unit = args[-1]
        verb = args[-2]
        if verb == "is-active":
            state = self.states[unit]
            return CommandResult(0 if state == "running" else 3,
                                 "active\n" if state == "running" else "inactive\n", "")
        if verb == "start" and unit == self.fail_start:
            return CommandResult(1, "", "Authorization: secret-token")
        if verb in {"start", "restart"}: self.states[unit] = "running"
        if verb == "stop": self.states[unit] = "stopped"
        return CommandResult(0, "", "")


class AdapterTests(unittest.TestCase):
    def test_formal_machine_control_config_enables_only_accepted_targets(self) -> None:
        config = load_control_config(Path(__file__).resolve().parents[1] / "config" / "control.json")
        self.assertTrue(config.control_enabled)
        indexed = {item.id: item for item in config.environments}
        self.assertEqual(indexed["q27_4090"].adapter.type, "wsl_systemd_root")
        self.assertEqual(indexed["ninfer3090_ui"].adapter.type, "wsl_systemd_root")
        self.assertTrue(indexed["ninfer3090_ui"].configured)
        self.assertTrue(indexed["ninfer4090"].configured)
        self.assertTrue(indexed["ninfer4090_ui"].configured)
        self.assertTrue(any(isinstance(check, WslDockerComposeGpuBindingHealthCheck)
                            for check in indexed["ninfer4090"].health_checks))
        self.assertEqual(indexed["ninfer4090"].adapter.timeout_seconds, 630)
        memory = next(check for check in indexed["ninfer4090"].preflight_checks
                      if isinstance(check, NvidiaGpuMemoryCheck))
        self.assertEqual(memory.gpu_uuid,
                         "GPU-24e90667-f02e-1e21-e5fa-b4bd6566ce63")
        self.assertEqual(memory.min_free_mib, 47000)
        self.assertFalse(indexed["video_h3_4090"].configured)
        self.assertTrue(indexed["dev3090_asr"].configured)
        self.assertTrue(indexed["dev3090_tts"].configured)
        self.assertTrue(indexed["dev3090_image"].configured)
        self.assertIsInstance(indexed["dev3090_image"].adapter,
                              WindowsComfyProcessConfig)
        self.assertEqual(indexed["dev3090_image"].adapter.port, 8189)
        self.assertEqual(indexed["dev3090_image"].adapter.cuda_device, 1)
        self.assertEqual(indexed["dev3090_image"].adapter.target_host_gpu_index, 1)
        self.assertEqual(indexed["dev3090_image"].adapter.target_gpu_uuid,
                         "GPU-3b71dd71-0d3f-6f92-8374-2f2b5f23ef8d")
        image_capability = next(
            check for check in indexed["dev3090_image"].health_checks
            if isinstance(check, WindowsComfyCapabilityHealthCheck))
        self.assertEqual(image_capability.system_stats_url,
                         "http://127.0.0.1:8189/system_stats")
        self.assertIn("VHS_VideoCombine", image_capability.required_node_classes)
        self.assertIn("MiniMaxH3AudioConditioningT8",
                      image_capability.required_node_classes)
        self.assertIn("MiniMaxH3AVDecodeT8", image_capability.required_node_classes)
        image_paths = {check.path for check in indexed["dev3090_image"].preflight_checks
                       if isinstance(check, WindowsPathDiskCheck)}
        self.assertIn(
            r"D:\ComfyUI\Models\diffusion_models\krea2_turbo_int8_convrot.safetensors",
            image_paths)
        self.assertIn(
            r"D:\ComfyUI\Models\diffusion_models\minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            image_paths)
        image_gpu_check = next(
            check for check in indexed["dev3090_image"].preflight_checks
            if isinstance(check, NvidiaGpuMemoryCheck))
        self.assertEqual(image_gpu_check.host_gpu_index, 1)
        self.assertTrue(any(isinstance(check, H3VideoProfileCheck)
                            and check.steps == 8
                            for check in indexed["dev3090_image"].preflight_checks))
        self.assertEqual(indexed["dev3090_asr"].adapter.service,
                         "sensevoice-asr-api.service")
        self.assertEqual(indexed["dev3090_tts"].adapter.service,
                         "index-tts-vllm.service")
        self.assertEqual(indexed["dev3090_asr"].adapter.timeout_seconds, 630)
        self.assertEqual(indexed["dev3090_tts"].adapter.timeout_seconds, 630)
        self.assertEqual(indexed["dev3090_asr"].startup_health_timeout_seconds, 600)
        self.assertEqual(indexed["dev3090_tts"].startup_health_timeout_seconds, 600)
        self.assertTrue(any(isinstance(check, WslPortAvailableCheck)
                            and check.port == 18090
                            for check in indexed["dev3090_asr"].preflight_checks))
        self.assertTrue(any(isinstance(check, WslPortAvailableCheck)
                            and check.port == 6006
                            for check in indexed["dev3090_tts"].preflight_checks))
        plane = object.__new__(ControlPlane)
        for environment_id in ("ninfer4090", "ninfer4090_ui", "dev3090_image",
                               "dev3090_asr", "dev3090_tts"):
            for action in ("start", "stop", "restart"):
                self.assertEqual(plane.configuration_blockers(indexed[environment_id], action), [])
        development = next(scene for scene in config.scenes if scene.id == "development")
        self.assertEqual(development.name, "开发/agent场景")
        self.assertIn("dev3090_image", development.desired)
        self.assertIn("dev3090_asr", development.desired)
        self.assertIn("dev3090_tts", development.desired)
        self.assertEqual(development.optional_desired, ())
        compose = (Path(__file__).resolve().parents[1] / "config" / "wsl-compose" /
                   "ninfer4090.compose.yaml").read_text(encoding="utf-8")
        self.assertIn('restart: "no"', compose)
        self.assertIn("stop_grace_period: 600s", compose)
        self.assertIn("cuda-visible-probe:/usr/local/bin/cuda-visible-probe:ro", compose)

        docker = indexed["ninfer4090"]
        wrong_memory = tuple(
            check.model_copy(update={"gpu_uuid": "GPU-deadbeef-dead-beef-dead-beefdeadbeef"})
            if isinstance(check, NvidiaGpuMemoryCheck) else check
            for check in docker.preflight_checks)
        self.assertIn("显存预算", " ".join(plane.configuration_blockers(
            docker.model_copy(update={"preflight_checks": wrong_memory}), "start")))

    def test_wsl_gpu_binding_requires_exact_host_uuid_and_unit_environment(self) -> None:
        calls = []

        def runner(args, timeout):
            calls.append(args)
            if args[0] == "nvidia-smi":
                return CommandResult(0, f"1, {GPU_UUID}\n", "")
            return CommandResult(0, "CUDA_VISIBLE_DEVICES=1 PYTHONUNBUFFERED=1\n", "")

        check = WslSystemdGpuBindingHealthCheck(
            type="wsl_systemd_gpu_binding", distro="Ubuntu-22.04",
            service="index-tts-vllm.service", host_gpu_index=1,
            gpu_uuid=GPU_UUID, cuda_visible_device=1)
        result = DefaultHealthProbe(runner).check(check)
        self.assertTrue(result.healthy)
        self.assertEqual(calls[1], ["wsl.exe", "-d", "Ubuntu-22.04", "--",
                                   "systemctl", "--user", "show",
                                   "index-tts-vllm.service", "--property=Environment",
                                   "--value"])

        wrong = DefaultHealthProbe(lambda args, timeout: CommandResult(
            0, f"1, {GPU_UUID}\n" if args[0] == "nvidia-smi" else
            "CUDA_VISIBLE_DEVICES=0\n", "")).check(check)
        self.assertFalse(wrong.healthy)
        conflicting = DefaultHealthProbe(lambda args, timeout: CommandResult(
            0, f"1, {GPU_UUID}\n" if args[0] == "nvidia-smi" else
            "CUDA_VISIBLE_DEVICES=1 CUDA_VISIBLE_DEVICES=0\n", "")).check(check)
        self.assertFalse(conflicting.healthy)

    def test_wsl_gpu_service_rejects_mismatched_port_check(self) -> None:
        config = load_control_config(Path(__file__).resolve().parents[1] / "config" / "control.json")
        target = next(item for item in config.environments if item.id == "dev3090_asr")
        wrong_checks = tuple(
            WslPortAvailableCheck(type="wsl_port_available", distro="Debian", port=1,
                                  owner_environment_id="dev3090_tts")
            if isinstance(check, WslPortAvailableCheck) else check
            for check in target.preflight_checks)
        wrong = target.model_copy(update={"preflight_checks": wrong_checks})
        plane = object.__new__(ControlPlane)
        self.assertTrue(any("WSL 端口检查" in item
                            for item in plane.configuration_blockers(wrong, "start")))

    def test_startup_health_waits_until_model_endpoint_is_ready(self) -> None:
        config = load_control_config(Path(__file__).resolve().parents[1] / "config" / "control.json")
        target = next(item for item in config.environments if item.id == "dev3090_asr")
        target = target.model_copy(update={"startup_health_timeout_seconds": .2,
                                           "startup_health_poll_interval_seconds": .01})

        class DelayedHealth:
            def __init__(self): self.calls = 0
            async def check_health(self, item, state):
                self.calls += 1
                return {"healthy": self.calls >= 3, "checks": []}

        delayed = DelayedHealth()
        result = asyncio.run(ControlPlane.wait_for_startup_health(delayed, target, "running"))
        self.assertTrue(result["healthy"])
        self.assertEqual(delayed.calls, 3)

    def test_startup_health_cancellation_waits_for_bounded_safe_result(self) -> None:
        config = load_control_config(Path(__file__).resolve().parents[1] / "config" / "control.json")
        target = next(item for item in config.environments if item.id == "dev3090_asr")
        target = target.model_copy(update={"startup_health_timeout_seconds": .2,
                                           "startup_health_poll_interval_seconds": .01})

        class DelayedHealth:
            def __init__(self): self.calls = 0
            async def check_health(self, item, state):
                self.calls += 1
                return {"healthy": self.calls >= 4, "checks": []}

        async def scenario():
            delayed = DelayedHealth()
            task = asyncio.create_task(
                ControlPlane.wait_for_startup_health(delayed, target, "running"))
            await asyncio.sleep(.015)
            task.cancel()
            result = await task
            return delayed.calls, result

        calls, result = asyncio.run(scenario())
        self.assertTrue(result["healthy"])
        self.assertGreaterEqual(calls, 4)

    def test_wsl_port_available_ignores_windows_portproxy_listener(self) -> None:
        calls = []

        def free_runner(args, timeout):
            calls.append(args)
            return CommandResult(0, "LISTEN 0 4096 0.0.0.0:18000 0.0.0.0:*\n", "")

        check = WslPortAvailableCheck(
            type="wsl_port_available", distro="Ubuntu-22.04", port=18090,
            owner_environment_id="dev3090_asr")
        self.assertTrue(DefaultSafetyProbe(free_runner).check(check).healthy)
        self.assertEqual(calls[0], ["wsl.exe", "-d", "Ubuntu-22.04", "--",
                                    "ss", "-ltnH"])

        occupied = DefaultSafetyProbe(lambda args, timeout: CommandResult(
            0, "LISTEN 0 4096 [::]:18090 [::]:*\n", "")).check(check)
        self.assertFalse(occupied.healthy)

    def test_wsl_systemd_root_exact_fixed_arguments(self) -> None:
        calls = []
        runner = lambda args, timeout: calls.append((args, timeout)) or CommandResult(0, "active\n", "")
        adapter = WslSystemdRootAdapter(WslSystemdRootConfig(
            type="wsl_systemd_root", distro="Ubuntu-22.04",
            service="q27-server.service", timeout_seconds=9), runner)
        self.assertEqual(adapter.status(), "running")
        adapter.restart()
        expected = ["wsl.exe", "-d", "Ubuntu-22.04", "-u", "root", "--",
                    "systemctl", "is-active", "q27-server.service"]
        self.assertEqual(calls[0], (expected, 5.0))
        self.assertEqual(calls[1][0][-2:], ["restart", "q27-server.service"])
        with self.assertRaises(ValidationError):
            WslSystemdRootConfig(type="wsl_systemd_root", distro="Ubuntu-22.04",
                                 service="q27-server.service", user="xu")
        with self.assertRaises(ValidationError):
            WslSystemdConfig(type="wsl_systemd", distro="Ubuntu-22.04", scope="user",
                             service="sensevoice-asr-api.service", timeout_seconds=661)

    def test_windows_comfy_adapter_fixed_args_and_exact_pid_only(self) -> None:
        cfg = WindowsComfyProcessConfig(
            type="windows_comfyui_process",
            python_executable=r"C:\Comfy\.venv\Scripts\python.exe",
            main_path=r"C:\Comfy\app\main.py", working_directory=r"C:\Comfy\app",
            base_directory=r"C:\Comfy", user_directory=r"C:\Comfy\user",
            database_path=r"C:\Comfy\user\comfyui.db",
            extra_model_paths_config=r"C:\Comfy\models.yaml",
            input_directory=r"C:\Comfy\input", output_directory=r"C:\Comfy\output",
            host="127.0.0.1", port=8001, target_gpu_uuid=GPU_UUID,
            target_host_gpu_index=1, expected_comfy_device_index=0, cuda_device=1)
        class FakeProcesses:
            def __init__(self): self.state = "stopped"; self.started = None; self.stopped = None
            def status(self, config, command): return self.state, 411 if self.state == "running" else None
            def start(self, config, command): self.started = (command, config.working_directory); self.state = "running"; return 411
            def terminate(self, pid, timeout): self.stopped = (pid, timeout); self.state = "stopped"
        processes = FakeProcesses()
        adapter = WindowsComfyProcessAdapter(cfg, processes)
        adapter.start()
        self.assertEqual(processes.started[0], cfg.command_args())
        self.assertEqual(processes.started[1], r"C:\Comfy\app")
        adapter.stop()
        self.assertEqual(processes.stopped, (411, 30.0))

    def test_default_windows_process_status_cross_checks_exe_commandline_and_port_owner(self) -> None:
        cfg = WindowsComfyProcessConfig(
            type="windows_comfyui_process", python_executable=r"C:\Comfy\python.exe",
            main_path=r"C:\Comfy\main.py", working_directory=r"C:\Comfy",
            base_directory=r"C:\Comfy", user_directory=r"C:\Comfy\user",
            database_path=r"C:\Comfy\x.db", extra_model_paths_config=r"C:\Comfy\x.yaml",
            input_directory=r"C:\Comfy\in", output_directory=r"C:\Comfy\out",
            host="127.0.0.1", port=8000, cuda_device=0, target_gpu_uuid=GPU_UUID,
            target_host_gpu_index=0, expected_comfy_device_index=0)
        process = type("Process", (), {"info": {"pid": 411, "exe": cfg.python_executable,
                                                   "cmdline": cfg.command_args(),
                                                   "create_time": 123.5}})()
        address = type("Address", (), {"ip": "127.0.0.1", "port": 8000})()
        connection = type("Connection", (), {"pid": 411, "status": "LISTEN", "laddr": address})()
        api = DefaultWindowsProcessApi()
        with patch("workstation_manager.control.psutil.process_iter", return_value=[process]), \
             patch("workstation_manager.control.psutil.net_connections", return_value=[connection]), \
             patch("workstation_manager.control.psutil.CONN_LISTEN", "LISTEN"):
            self.assertEqual(api.status(cfg, cfg.command_args()), ("running", 411))
            self.assertFalse(api.owns(411, cfg.command_args()))
            api._owned[tuple(cfg.command_args())] = (411, 123.5)
            self.assertEqual(api.status(cfg, cfg.command_args()), ("running", 411))
            with patch("workstation_manager.control.psutil.Process") as owned_process:
                owned_process.return_value.create_time.return_value = 123.5
                self.assertTrue(api.owns(411, cfg.command_args()))
            connection.pid = 999
            self.assertEqual(api.status(cfg, cfg.command_args()), ("unknown", None))

    def test_windows_paths_reject_unc_device_relative_and_wrong_port_gpu_mapping(self) -> None:
        base = dict(type="windows_comfyui_process", python_executable=r"C:\C\python.exe",
                    main_path=r"C:\C\main.py", working_directory=r"C:\C",
                    base_directory=r"C:\C", user_directory=r"C:\C\user",
                    database_path=r"C:\C\x.db", extra_model_paths_config=r"C:\C\x.yaml",
                    input_directory=r"C:\C\in", output_directory=r"C:\C\out",
                    host="127.0.0.1", port=8000, cuda_device=0,
                    target_gpu_uuid=GPU_UUID, target_host_gpu_index=0,
                    expected_comfy_device_index=0)
        for bad in (r"\\server\share\python.exe", r"\\?\C:\C\python.exe", r"python.exe"):
            with self.assertRaises(ValidationError):
                WindowsComfyProcessConfig(**{**base, "python_executable": bad})
        with self.assertRaises(ValidationError):
            WindowsComfyProcessConfig(**{**base, "cuda_device": 1})
        image_3090 = WindowsComfyProcessConfig(**{
            **base,
            "port": 8189,
            "cuda_device": 1,
            "target_host_gpu_index": 1,
        })
        self.assertEqual(image_3090.port, 8189)
        with self.assertRaises(ValidationError):
            WindowsComfyProcessConfig(**{
                **base,
                "port": 8189,
                "cuda_device": 0,
                "target_host_gpu_index": 0,
            })

    def test_missing_control_file_forces_example_preview_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "control.example.json").write_text(
                '{"version":1,"control_enabled":true,"environments":[],"scenes":[]}',
                encoding="utf-8",
            )
            preview = load_control_config(root / "control.json")
            self.assertFalse(preview.control_enabled)
            self.assertEqual(preview.source, "example_preview")
            (root / "control.json").write_text(
                '{"version":1,"control_enabled":true,"environments":[],"scenes":[]}',
                encoding="utf-8",
            )
            configured = load_control_config(root / "control.json")
            self.assertTrue(configured.control_enabled)
            self.assertEqual(configured.source, "configured")

    def test_health_schema_is_loopback_only_and_gpu_command_is_fixed(self) -> None:
        with self.assertRaises(ValidationError):
            LoopbackHttpHealthCheck(type="loopback_http", url="http://example.com:8000/health")
        gpu = NvidiaGpuProcessHealthCheck(
            type="nvidia_gpu_process", gpu_uuid="GPU-12345678-1234-1234-1234-123456789abc",
            process_name="python.exe",
        )
        calls = []
        probe = DefaultHealthProbe(lambda args, timeout: calls.append((args, timeout)) or CommandResult(
            0, "GPU-12345678-1234-1234-1234-123456789abc, C:\\Python\\python.exe\n", ""))
        result = probe.check(gpu)
        self.assertTrue(result.healthy)
        self.assertEqual(calls[0][0], ["nvidia-smi",
            "--query-compute-apps=gpu_uuid,process_name", "--format=csv,noheader,nounits"])

    def test_wsl_docker_gpu_binding_requires_exact_single_gpu_at_every_layer(self) -> None:
        check = WslDockerComposeGpuBindingHealthCheck(
            type="wsl_docker_compose_gpu_binding", distro="Ubuntu-22.04",
            project_dir="/home/xu/ai_stud/ninfer4090", project="ninfer4090",
            service="ninfer", host_gpu_index=0, gpu_uuid=GPU_UUID,
            cuda_visible_device=GPU_UUID,
        )
        container_id = "a" * 64
        calls = []

        def runner(args, timeout):
            calls.append(args)
            if args[0] == "nvidia-smi":
                return CommandResult(0, f"0, {GPU_UUID}\n1, GPU-deadbeef-dead-beef-dead-beefdeadbeef\n", "")
            if "ps" in args:
                return CommandResult(0, container_id + "\n", "")
            if args[-3:-1] == ["--format", "{{json .HostConfig.DeviceRequests}}"]:
                return CommandResult(0, (
                    '[{"Driver":"nvidia","Count":0,"DeviceIDs":["' + GPU_UUID +
                    '"],"Capabilities":[["gpu"]],"Options":{}}]\n'), "")
            if args[-3:-1] == ["--format", "{{json .Config.Env}}"]:
                return CommandResult(0, (
                    '["NVIDIA_VISIBLE_DEVICES=' + GPU_UUID + '","CUDA_VISIBLE_DEVICES=' +
                    GPU_UUID + '"]\n'), "")
            if "exec" in args:
                return CommandResult(0, "count=1\n" + GPU_UUID + "\n", "")
            return CommandResult(1, "", "unexpected")

        result = DefaultHealthProbe(runner).check(check)
        self.assertTrue(result.healthy, result.message)
        self.assertEqual(calls[1], [
            "wsl.exe", "-d", "Ubuntu-22.04", "--", "docker", "compose",
            "--project-name", "ninfer4090", "--project-directory",
            "/home/xu/ai_stud/ninfer4090", "ps", "-q", "ninfer",
        ])

        def all_gpus_runner(args, timeout):
            result = runner(args, timeout)
            if args[-3:-1] == ["--format", "{{json .HostConfig.DeviceRequests}}"]:
                return CommandResult(0, '[{"Driver":"nvidia","Count":-1,"DeviceIDs":[]}]\n', "")
            return result

        blocked = DefaultHealthProbe(all_gpus_runner).check(check)
        self.assertFalse(blocked.healthy)
        self.assertIn("DeviceRequests", blocked.message)

        def cuda_leak_runner(args, timeout):
            result = runner(args, timeout)
            if "exec" in args:
                return CommandResult(0, "count=2\n" + GPU_UUID +
                                     "\nGPU-deadbeef-dead-beef-dead-beefdeadbeef\n", "")
            return result

        leaked = DefaultHealthProbe(cuda_leak_runner).check(check)
        self.assertFalse(leaked.healthy)
        self.assertIn("CUDA Driver", leaked.message)

        def conflicting_env_runner(args, timeout):
            result = runner(args, timeout)
            if args[-3:-1] == ["--format", "{{json .Config.Env}}"]:
                return CommandResult(0, (
                    '["NVIDIA_VISIBLE_DEVICES=' + GPU_UUID + '","CUDA_VISIBLE_DEVICES=' +
                    GPU_UUID + '","CUDA_VISIBLE_DEVICES=1"]\n'), "")
            return result

        conflict = DefaultHealthProbe(conflicting_env_runner).check(check)
        self.assertFalse(conflict.healthy)
        self.assertIn("容器环境", conflict.message)
    def test_exact_fixed_argument_arrays(self) -> None:
        calls = []
        runner = lambda args, timeout: calls.append((args, timeout)) or CommandResult(0, "active\n", "")
        systemd = WslSystemdAdapter(WslSystemdConfig(
            type="wsl_systemd", distro="Ubuntu-24.04", scope="user",
            service="ninfer-ui.service", timeout_seconds=12), runner)
        self.assertEqual(systemd.status(), "running")
        systemd.restart()
        self.assertEqual(calls[0], (["wsl.exe", "-d", "Ubuntu-24.04", "--", "systemctl",
                                    "--user", "is-active", "ninfer-ui.service"], 5.0))
        self.assertEqual(calls[1][0][-2:], ["restart", "ninfer-ui.service"])
        self.assertEqual(calls[1][1], 12.0)

        calls.clear()
        compose = WslDockerComposeAdapter(WslDockerComposeConfig(
            type="wsl_docker_compose", distro="Ubuntu", project_dir="/home/xu/ninfer",
            project="ninfer", service="api", timeout_seconds=20), runner)
        compose.start()
        self.assertEqual(calls[0][0], ["wsl.exe", "-d", "Ubuntu", "--", "docker", "compose",
                                      "--project-name", "ninfer", "--project-directory",
                                      "/home/xu/ninfer", "up", "-d", "api"])

        calls.clear()
        self.assertEqual(compose.status(), "stopped")
        self.assertEqual(calls[0][1], 5.0)

    def test_schema_rejects_generic_fields_and_unsafe_paths(self) -> None:
        with self.assertRaises(ValidationError):
            WslDockerComposeConfig(type="wsl_docker_compose", distro="Ubuntu",
                                   project_dir="/home/xu/../bad", service="api")
        with self.assertRaises(ValidationError):
            WslSystemdConfig(type="wsl_systemd", distro="Ubuntu", scope="system",
                             service="bad;name.service")
        with self.assertRaises(ValidationError):
            ControlConfig.model_validate({"control_enabled": True, "command": "whoami"})
        with self.assertRaises(ValidationError):
            H3VideoProfileCheck(type="h3_video_profile", steps=4,
                                lora_name="h3_4step.safetensors",
                                shift_video=12, shift_audio=3)
        with self.assertRaises(ValidationError):
            H3VideoProfileCheck(type="h3_video_profile", steps=8,
                                lora_name="unmarked.safetensors",
                                shift_video=12, shift_audio=3)
        with self.assertRaises(ValidationError):
            DrainHttpJsonCheck(type="drain_http_json", purpose="comfy_queue",
                               url="http://127.0.0.1:8000/queue",
                               json_paths=("queue.pending",))

    def test_safety_probe_uses_fixed_gpu_and_wsl_argument_arrays(self) -> None:
        calls = []
        def runner(args, timeout):
            calls.append((args, timeout))
            if args[0] == "nvidia-smi":
                return CommandResult(0, f"{GPU_UUID}, 24576\n", "")
            if "test" in args:
                return CommandResult(0, "", "")
            return CommandResult(0, "Avail\n2097152\n", "")
        probe = DefaultSafetyProbe(runner)
        self.assertTrue(probe.check(NvidiaGpuMemoryCheck(
            type="nvidia_gpu_memory", gpu_uuid=GPU_UUID, min_free_mib=1024)).healthy)
        self.assertTrue(probe.check(WslPathDiskCheck(
            type="wsl_path_disk", purpose="model", distro="Ubuntu",
            path="/models/approved", min_free_mib=1024)).healthy)
        self.assertEqual(calls[0][0], ["nvidia-smi", "--query-gpu=uuid,memory.free",
                                      "--format=csv,noheader,nounits"])
        self.assertEqual(calls[1][0], ["wsl.exe", "-d", "Ubuntu", "--", "test", "-e",
                                      "/models/approved"])
        self.assertEqual(calls[2][0], ["wsl.exe", "-d", "Ubuntu", "--", "df",
                                      "--output=avail", "-k", "/models/approved"])

    def test_gpu_memory_preflight_can_pin_host_index_to_uuid(self) -> None:
        calls = []

        def runner(args, timeout):
            calls.append((args, timeout))
            return CommandResult(0, f"1, {GPU_UUID}, 24576\n", "")

        probe = DefaultSafetyProbe(runner)
        check = NvidiaGpuMemoryCheck(
            type="nvidia_gpu_memory", gpu_uuid=GPU_UUID,
            host_gpu_index=1, min_free_mib=12288)
        self.assertTrue(probe.check(check).healthy)
        self.assertEqual(
            calls[0][0],
            ["nvidia-smi", "--query-gpu=index,uuid,memory.free",
             "--format=csv,noheader,nounits"],
        )

        def wrong_index_runner(args, timeout):
            return CommandResult(0, f"0, {GPU_UUID}, 24576\n", "")

        result = DefaultSafetyProbe(wrong_index_runner).check(check)
        self.assertFalse(result.healthy)
        self.assertIn("index", result.message)

    def test_prometheus_drain_requires_exact_labels_and_all_series(self) -> None:
        check = DrainHttpPrometheusCheck(
            type="drain_http_prometheus", purpose="active_requests",
            url="http://127.0.0.1:8000/metrics", series=(
                PrometheusSeries(metric="vllm:num_requests_running",
                                 labels={"model_name": "qwen3.8-27b-fp8", "engine": "0"}),
                PrometheusSeries(metric="vllm:num_requests_waiting",
                                 labels={"model_name": "qwen3.8-27b-fp8", "engine": "0"}),))
        body = (b'vllm:num_requests_running{model_name="qwen3.8-27b-fp8",engine="0"} 0\n'
                b'vllm:num_requests_waiting{engine="0",model_name="qwen3.8-27b-fp8"} 0\n')
        self.assertTrue(DefaultSafetyProbe._drain_prometheus(check, body).healthy)
        self.assertFalse(DefaultSafetyProbe._drain_prometheus(check, body.splitlines()[0] + b"\n").healthy)
        bad = body.replace(b'engine="0"', b'engine="1"', 1)
        self.assertFalse(DefaultSafetyProbe._drain_prometheus(check, bad).healthy)

    def test_comfy_queue_drain_requires_two_arrays(self) -> None:
        check = DrainHttpJsonArraysCheck(type="drain_http_json_arrays", purpose="comfy_queue",
            url="http://127.0.0.1:8000/queue")
        self.assertTrue(DefaultSafetyProbe._drain_json_arrays(check,
            b'{"queue_running":[],"queue_pending":[]}').healthy)
        self.assertFalse(DefaultSafetyProbe._drain_json_arrays(check,
            b'{"queue_running":[],"queue_pending":0}').healthy)
        self.assertFalse(DefaultSafetyProbe._drain_json_arrays(check,
            b'{"queue_running":[]}').healthy)

    def test_windows_path_disk_probe_and_h3_profile(self) -> None:
        probe = DefaultSafetyProbe(lambda *_: CommandResult(0, "", ""),
            windows_path_probe=healthy_windows_path_probe)
        check = WindowsPathDiskCheck(type="windows_path_disk", purpose="lora",
                                     path=r"D:\Models\h3_8step.safetensors", min_free_gib=10)
        self.assertTrue(probe.check(check).healthy)
        with self.assertRaises(ValidationError):
            WindowsPathDiskCheck(type="windows_path_disk", purpose="model",
                                 path=r"\\server\models", min_free_gib=1)
        self.assertEqual(H3VideoProfileCheck(type="h3_video_profile", steps=8,
            lora_name="h3_turbo_8step.safetensors", shift_video=12, shift_audio=3).steps, 8)

    def test_windows_path_probe_timeout_is_enforced_and_child_is_terminated(self) -> None:
        probe = DefaultSafetyProbe(lambda *_: CommandResult(0, "", ""),
                                   windows_path_probe=slow_windows_path_probe)
        check = WindowsPathDiskCheck(type="windows_path_disk", purpose="model",
                                     path=r"D:\Models\approved", min_free_gib=1,
                                     timeout_seconds=1)
        before_children = {child.pid for child in multiprocessing.active_children()}
        started = time.monotonic()
        result = probe.check(check)
        elapsed = time.monotonic() - started
        self.assertFalse(result.healthy)
        self.assertEqual(result.status, "unknown")
        self.assertIn("超时", result.message)
        self.assertGreaterEqual(elapsed, .8)
        self.assertLess(elapsed, 3)
        self.assertLessEqual({child.pid for child in multiprocessing.active_children()},
                             before_children)

    def test_windows_path_rejects_parent_reparse_ads_and_reserved_devices(self) -> None:
        parent = ntpath.normcase(r"D:\Models\junction")
        def attributes(path):
            return 0x400 if ntpath.normcase(path) == parent else 0x10
        error = _windows_reparse_error(r"D:\Models\junction\model.bin", attributes)
        self.assertIn("reparse", error)
        invalid = _windows_reparse_error(
            r"D:\Models\unknown\model.bin", lambda path: 0xFFFFFFFF)
        self.assertIn("无法读取路径属性", invalid)
        for unsafe in (r"D:\Models\file.bin:stream", r"D:\Models\CON.txt",
                       r"D:\AUX\model.bin", r"D:\Models\com9.log",
                       r"D:\Models\LPT1"):
            with self.subTest(path=unsafe), self.assertRaises(ValidationError):
                WindowsPathDiskCheck(type="windows_path_disk", purpose="model",
                                     path=unsafe, min_free_gib=1)

    def test_windows_path_probe_cleans_worker_that_returned_but_stays_alive(self) -> None:
        probe = DefaultSafetyProbe(
            lambda *_: CommandResult(0, "", ""),
            windows_path_probe=result_then_lingering_thread_windows_path_probe)
        check = WindowsPathDiskCheck(type="windows_path_disk", purpose="model",
                                     path=r"D:\Models\approved", min_free_gib=1,
                                     timeout_seconds=2)
        before_children = {child.pid for child in multiprocessing.active_children()}
        started = time.monotonic()
        result = probe.check(check)
        elapsed = time.monotonic() - started
        self.assertTrue(result.healthy)
        self.assertLess(elapsed, 3)
        self.assertLessEqual({child.pid for child in multiprocessing.active_children()},
                             before_children)

    def test_object_keys_and_comfy_gpu_capability_health(self) -> None:
        key_check = HttpJsonObjectHasKeysCheck(type="http_json_object_has_keys",
            url="http://127.0.0.1:11996/audio/voices", required_keys=("jay_klee",))
        self.assertTrue(DefaultHealthProbe._object_has_keys(key_check, b'{"jay_klee":{}}').healthy)
        self.assertFalse(DefaultHealthProbe._object_has_keys(key_check, b'["jay_klee"]').healthy)
        check = WindowsComfyCapabilityHealthCheck(type="windows_comfy_capability_health",
            system_stats_url="http://127.0.0.1:8001/system_stats",
            queue_url="http://127.0.0.1:8001/queue",
            object_info_url="http://127.0.0.1:8001/object_info",
            target_gpu_uuid=GPU_UUID, target_gpu_name="NVIDIA GeForce RTX 3090",
            target_host_gpu_index=1, expected_comfy_device_index=0,
            required_node_classes=("TextEncodeAceStepAudio1.5",))
        payloads = {
            check.system_stats_url: {"devices":[{"type":"cuda", "index":0,
                "name":"cuda:0 NVIDIA GeForce RTX 3090"}]},
            check.queue_url: {"queue_running":[], "queue_pending":[]},
            check.object_info_url: {"TextEncodeAceStepAudio1.5":{}},
        }
        runner = lambda args, timeout: CommandResult(0,
            f"1, {GPU_UUID}, NVIDIA GeForce RTX 3090\n", "")
        probe = DefaultHealthProbe(runner, json_fetcher=lambda url, limit, timeout: payloads[url])
        self.assertTrue(probe.check(check).healthy)

    def test_subprocess_is_non_shell_hidden_bounded_and_redacted(self) -> None:
        completed = type("Completed", (), {"returncode": 1,
                    "stdout": "x" * 9000, "stderr": "Authorization: bearer-secret\nprefix Set-Cookie: control-cookie-secret"})()
        with patch("workstation_manager.control.subprocess.run", return_value=completed) as run:
            result = SubprocessRunner()(["wsl.exe", "--status"], 3)
        kwargs = run.call_args.kwargs
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 3)
        self.assertLessEqual(len(result.stdout), 8192)
        self.assertNotIn("bearer-secret", result.stderr)
        self.assertNotIn("control-cookie-secret", result.stderr)
        with patch("workstation_manager.control.subprocess.run",
                   side_effect=__import__("subprocess").TimeoutExpired(["wsl.exe"], 3)):
            with self.assertRaisesRegex(Exception, "超时"):
                SubprocessRunner()(["wsl.exe", "--status"], 3)


class ControlPlaneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "manager.db")
        self.a_cfg = WslSystemdConfig(type="wsl_systemd", distro="Ubuntu", scope="system", service="a.service")
        self.b_cfg = WslSystemdConfig(type="wsl_systemd", distro="Ubuntu", scope="system", service="b.service")

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_registered_blocked_adapter_reports_real_status_without_enabling_actions(self) -> None:
        registered = EnvironmentConfig(
            id="registered", name="registered", configured=False,
            missing_reason="安全验收尚未完成", adapter=self.a_cfg,
            health_checks=(HealthCheckConfig(type="adapter_status"),),
            allowed_actions=())
        missing = EnvironmentConfig(
            id="missing", name="missing", configured=False,
            missing_reason="缺少独立入口", adapter=None,
            health_checks=(HealthCheckConfig(type="adapter_status"),),
            allowed_actions=())
        scene = SceneConfig(
            id="development", name="development",
            desired=("registered", "missing"), conflicts=())
        runner = StatefulRunner({"a.service": "running"})
        plane = ControlPlane(ControlConfig(
            control_enabled=True, environments=(registered, missing), scenes=(scene,)),
            self.database, runner=runner)

        environments = await plane.list_environments()
        indexed = {item["id"]: item for item in environments["environments"]}
        self.assertEqual(indexed["registered"]["status"], "running")
        self.assertFalse(indexed["registered"]["configured"])
        self.assertTrue(indexed["registered"]["adapter_configured"])
        self.assertFalse(indexed["registered"]["ready"])
        self.assertTrue(all(
            not capability["ready"]
            for capability in indexed["registered"]["action_capabilities"].values()))
        self.assertEqual(indexed["missing"]["status"], "unconfigured")

        scene_view = await plane.scene_view(scene)
        self.assertEqual(scene_view["statuses"]["registered"], "running")
        self.assertEqual(scene_view["statuses"]["missing"], "unconfigured")
        self.assertEqual(scene_view["current"], "partial")

    def _comfy_restart_environment(self, *, drain: bool = True) -> EnvironmentConfig:
        adapter = WindowsComfyProcessConfig(
            type="windows_comfyui_process", python_executable=r"C:\Comfy\python.exe",
            main_path=r"C:\Comfy\main.py", working_directory=r"C:\Comfy",
            base_directory=r"C:\Comfy", user_directory=r"C:\Comfy\user",
            database_path=r"C:\Comfy\x.db", extra_model_paths_config=r"C:\Comfy\x.yaml",
            input_directory=r"C:\Comfy\in", output_directory=r"C:\Comfy\out",
            host="127.0.0.1", port=8000, cuda_device=0, target_gpu_uuid=GPU_UUID,
            target_host_gpu_index=0, expected_comfy_device_index=0)
        checks = []
        if drain:
            checks.append(DrainHttpJsonArraysCheck(
                type="drain_http_json_arrays", purpose="comfy_queue",
                url="http://127.0.0.1:8000/queue", wait_timeout_seconds=1,
                poll_interval_seconds=.1))
        checks.extend((
            NvidiaGpuMemoryCheck(type="nvidia_gpu_memory", gpu_uuid=GPU_UUID,
                                 min_free_mib=1024),
            WindowsPathDiskCheck(type="windows_path_disk", purpose="model",
                                 path=r"C:\Comfy\models", min_free_gib=1),
            LoopbackPortAvailableCheck(type="loopback_port_available", port=8000,
                                       owner_environment_id="comfy"),
        ))
        return EnvironmentConfig(
            id="comfy", name="comfy", configured=True, adapter=adapter,
            gpu_ai=True, safety_profile="gpu_ai",
            health_checks=(HealthCheckConfig(type="adapter_status"),
                WindowsComfyCapabilityHealthCheck(
                    type="windows_comfy_capability_health",
                    system_stats_url="http://127.0.0.1:8000/system_stats",
                    queue_url="http://127.0.0.1:8000/queue",
                    object_info_url="http://127.0.0.1:8000/object_info",
                    target_gpu_uuid=GPU_UUID,
                    target_gpu_name="NVIDIA GeForce RTX 4090",
                    target_host_gpu_index=0, expected_comfy_device_index=0,
                    required_node_classes=("RequiredNode",)),),
            preflight_checks=tuple(checks), allowed_actions=("restart",))

    async def test_ai_restart_missing_drain_is_statically_blocked_even_when_health_is_healthy(self) -> None:
        target = self._comfy_restart_environment(drain=False)
        class AlwaysHealthy:
            def check(self, check): return HealthResult(True, "mock health")
        plane = ControlPlane(ControlConfig(control_enabled=True, environments=(target,)),
                             self.database, health_probe=AlwaysHealthy(),
                             safety_probe=HealthySafetyProbe())
        with self.assertRaises(ControlError) as raised:
            await plane.submit_environment("comfy", "restart", "restart:comfy", "admin", "local")
        self.assertEqual(raised.exception.code, "environment_blocked")
        self.assertIn("drain_http_json_arrays", " ".join(raised.exception.details))
        self.assertEqual(self.database.list_operations(10), [])

    async def test_restart_of_stopped_environment_runs_start_without_drain(self) -> None:
        target = EnvironmentConfig(
            id="a", name="a", configured=True, adapter=self.a_cfg,
            health_checks=(HealthCheckConfig(type="adapter_status"),),
            preflight_checks=(DrainHttpJsonCheck(
                type="drain_http_json", purpose="active_requests",
                url="http://127.0.0.1:8000/metrics", json_paths=("active",)),),
            allowed_actions=("restart",))
        class NoDrainWhenStopped:
            def check(self, check):
                if isinstance(check, DrainHttpJsonCheck):
                    raise AssertionError("stopped restart 不得读取 drain endpoint")
                return HealthResult(True, "safe")
        runner = StatefulRunner({"a.service": "stopped"})
        plane = ControlPlane(ControlConfig(control_enabled=True, environments=(target,)),
                             self.database, runner=runner,
                             safety_probe=NoDrainWhenStopped())
        operation_id = await plane.submit_environment(
            "a", "restart", "restart:a", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["status"], "succeeded")
        verbs = [args[-2] for args in runner.calls]
        self.assertIn("start", verbs)
        self.assertNotIn("stop", verbs)
        self.assertNotIn("restart", verbs)

    async def test_restart_stop_failure_while_still_running_is_not_reconciled_success(self) -> None:
        target = EnvironmentConfig(
            id="a", name="a", configured=True, adapter=self.a_cfg,
            health_checks=(HealthCheckConfig(type="adapter_status"),),
            preflight_checks=(DrainHttpJsonCheck(
                type="drain_http_json", purpose="active_requests",
                url="http://127.0.0.1:8000/metrics", json_paths=("active",)),),
            allowed_actions=("restart",))
        class StopFails(StatefulRunner):
            def __call__(self, args, timeout):
                if args[-2] == "stop":
                    self.calls.append(args)
                    return CommandResult(1, "", "stop failed")
                return super().__call__(args, timeout)
        runner = StopFails({"a.service": "running"})
        plane = ControlPlane(ControlConfig(control_enabled=True, environments=(target,)),
                             self.database, runner=runner,
                             safety_probe=HealthySafetyProbe())
        operation_id = await plane.submit_environment(
            "a", "restart", "restart:a", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["status"], "failed")
        self.assertEqual(operation["result"], "failed")
        self.assertFalse(any(args[-2] == "start" for args in runner.calls))

    async def test_comfy_restart_nonempty_queue_never_calls_adapter(self) -> None:
        target = self._comfy_restart_environment()
        class AlwaysHealthy:
            def check(self, check): return HealthResult(True, "mock health")
        class BusyQueue:
            def __init__(self): self.calls = []
            def check(self, check):
                self.calls.append(check.type)
                return HealthResult(not isinstance(check, DrainHttpJsonArraysCheck),
                                    "queue busy" if isinstance(check, DrainHttpJsonArraysCheck)
                                    else "startup safe")
        class Processes:
            def __init__(self): self.events = []
            def status(self, config, command): return "running", 411
            def owns(self, pid, command): return True
            def start(self, config, command): self.events.append("start"); return 411
            def terminate(self, pid, timeout): self.events.append("terminate")
        safety = BusyQueue(); processes = Processes()
        plane = ControlPlane(ControlConfig(control_enabled=True, environments=(target,)),
                             self.database, health_probe=AlwaysHealthy(),
                             safety_probe=safety, process_api=processes)
        operation_id = await plane.submit_environment(
            "comfy", "restart", "restart:comfy", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["status"], "failed")
        self.assertEqual(processes.events, [])
        self.assertTrue(safety.calls)
        self.assertTrue(all(item == "drain_http_json_arrays" for item in safety.calls))

    async def test_comfy_restart_waits_for_empty_queue_then_runs_start_preflight(self) -> None:
        target = self._comfy_restart_environment()
        class AlwaysHealthy:
            def check(self, check): return HealthResult(True, "mock health")
        class DrainThenReady:
            def __init__(self, processes):
                self.processes = processes; self.calls = []; self.drain_calls = 0
            def check(self, check):
                self.calls.append(check.type)
                if isinstance(check, DrainHttpJsonArraysCheck):
                    self.drain_calls += 1
                    return HealthResult(self.drain_calls >= 2, "queue state")
                if isinstance(check, NvidiaGpuMemoryCheck):
                    return HealthResult(self.processes.state == "stopped", "GPU release state")
                return HealthResult(True, "startup safe")
        class Processes:
            def __init__(self): self.state = "running"; self.events = []
            def status(self, config, command):
                return (self.state, 411 if self.state == "running" else None)
            def owns(self, pid, command): return True
            def start(self, config, command): self.events.append("start"); self.state = "running"; return 411
            def terminate(self, pid, timeout): self.events.append("terminate"); self.state = "stopped"
        processes = Processes(); safety = DrainThenReady(processes)
        plane = ControlPlane(ControlConfig(control_enabled=True, environments=(target,)),
                             self.database, health_probe=AlwaysHealthy(),
                             safety_probe=safety, process_api=processes)
        operation_id = await plane.submit_environment(
            "comfy", "restart", "restart:comfy", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["status"], "succeeded")
        self.assertEqual(processes.events, ["terminate", "start"])
        self.assertEqual(safety.calls[:2],
                         ["drain_http_json_arrays", "drain_http_json_arrays"])
        self.assertEqual(safety.calls[2:],
                         ["nvidia_gpu_memory", "windows_path_disk",
                          "loopback_port_available"])

    async def test_comfy_failed_start_busy_rollback_queue_never_terminates_and_locks_recovery(self) -> None:
        target = self._comfy_restart_environment().model_copy(
            update={"allowed_actions": ("start", "stop")})
        class UnhealthyAfterStart:
            def check(self, check): return HealthResult(False, "mock capability failure")
        class BusyRollbackQueue:
            def check(self, check):
                return HealthResult(not isinstance(check, DrainHttpJsonArraysCheck),
                                    "new queue busy" if isinstance(check, DrainHttpJsonArraysCheck)
                                    else "startup safe")
        class Processes:
            def __init__(self): self.state = "stopped"; self.events = []
            def status(self, config, command):
                return self.state, 411 if self.state == "running" else None
            def owns(self, pid, command): return True
            def start(self, config, command): self.events.append("start"); self.state = "running"; return 411
            def terminate(self, pid, timeout): self.events.append("terminate"); self.state = "stopped"
        processes = Processes()
        plane = ControlPlane(ControlConfig(control_enabled=True, environments=(target,)),
                             self.database, health_probe=UnhealthyAfterStart(),
                             safety_probe=BusyRollbackQueue(), process_api=processes)
        operation_id = await plane.submit_environment(
            "comfy", "start", "start:comfy", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["result"], "rollback_failed")
        self.assertEqual(processes.events, ["start"])
        recovery = self.database.control_recovery_lock()
        self.assertEqual(recovery["environment_id"], "comfy")
        self.assertEqual(recovery["expected_state"], "stopped")

    async def test_comfy_failed_start_empty_rollback_queue_allows_exact_stop(self) -> None:
        target = self._comfy_restart_environment().model_copy(
            update={"allowed_actions": ("start", "stop")})
        class UnhealthyAfterStart:
            def check(self, check): return HealthResult(False, "mock capability failure")
        class SafeRollback:
            def __init__(self): self.calls = []
            def check(self, check):
                self.calls.append(check.type)
                return HealthResult(True, "safe")
        class Processes:
            def __init__(self): self.state = "stopped"; self.events = []
            def status(self, config, command):
                return self.state, 411 if self.state == "running" else None
            def owns(self, pid, command): return True
            def start(self, config, command): self.events.append("start"); self.state = "running"; return 411
            def terminate(self, pid, timeout): self.events.append("terminate"); self.state = "stopped"
        safety = SafeRollback(); processes = Processes()
        plane = ControlPlane(ControlConfig(control_enabled=True, environments=(target,)),
                             self.database, health_probe=UnhealthyAfterStart(),
                             safety_probe=safety, process_api=processes)
        operation_id = await plane.submit_environment(
            "comfy", "start", "start:comfy", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["result"], "rolled_back")
        self.assertEqual(processes.events, ["start", "terminate"])
        self.assertIn("drain_http_json_arrays", safety.calls)
        self.assertIsNone(self.database.control_recovery_lock())

    async def test_scene_rollback_stop_prometheus_busy_never_calls_stop_and_locks(self) -> None:
        def prom_env(environment_id: str, service: str, port: int) -> EnvironmentConfig:
            adapter = WslSystemdConfig(type="wsl_systemd", distro="Ubuntu",
                                       scope="system", service=service)
            return EnvironmentConfig(
                id=environment_id, name=environment_id, configured=True, adapter=adapter,
                gpu_ai=True, safety_profile="gpu_ai",
                health_checks=(HealthCheckConfig(type="adapter_status"),
                    LoopbackHttpHealthCheck(type="loopback_http",
                        url=f"http://127.0.0.1:{port}/health", json_equals={"status":"ok"}),
                    NvidiaGpuProcessHealthCheck(type="nvidia_gpu_process",
                        gpu_uuid=GPU_UUID, process_name="python.exe")),
                preflight_checks=(DrainHttpPrometheusCheck(
                        type="drain_http_prometheus", purpose="active_requests",
                        url=f"http://127.0.0.1:{port}/metrics",
                        series=(PrometheusSeries(metric="requests_running", labels={}),),
                        wait_timeout_seconds=1, poll_interval_seconds=.1),
                    NvidiaGpuMemoryCheck(type="nvidia_gpu_memory", gpu_uuid=GPU_UUID,
                                         min_free_mib=1024),
                    WslPathDiskCheck(type="wsl_path_disk", purpose="model", distro="Ubuntu",
                                     path=f"/models/{environment_id}", min_free_mib=1024),
                    LoopbackPortAvailableCheck(type="loopback_port_available", port=port,
                                               owner_environment_id=environment_id)),
                allowed_actions=("start", "stop"))
        first = prom_env("first", "first.service", 8100)
        second = prom_env("second", "second.service", 8101)
        class AlwaysHealthy:
            def check(self, check): return HealthResult(True, "healthy")
        class BusyPrometheusOnRollback:
            def check(self, check):
                return HealthResult(not isinstance(check, DrainHttpPrometheusCheck),
                                    "prometheus busy" if isinstance(check, DrainHttpPrometheusCheck)
                                    else "startup safe")
        runner = StatefulRunner({"first.service":"stopped", "second.service":"stopped"},
                                fail_start="second.service")
        config = ControlConfig(control_enabled=True, environments=(first, second),
            scenes=(SceneConfig(id="scene", name="scene", desired=("first", "second"),
                                conflicts=()),))
        plane = ControlPlane(config, self.database, runner, AlwaysHealthy(),
                             BusyPrometheusOnRollback())
        preflight = await plane.scene_preflight("scene")
        self.assertTrue(preflight["ready"])
        operation_id = await plane.submit_scene("scene", "activate:scene", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["result"], "rollback_failed")
        self.assertFalse(any(call[-2:] == ["stop", "first.service"] for call in runner.calls))
        recovery = self.database.control_recovery_lock()
        self.assertIsNotNone(recovery)
        self.assertTrue(any(item["environment_id"] == "first" for item in recovery["items"]))

    async def test_comfy_stop_cannot_bypass_array_queue_drain_with_generic_json(self) -> None:
        adapter = WindowsComfyProcessConfig(
            type="windows_comfyui_process", python_executable=r"C:\Comfy\python.exe",
            main_path=r"C:\Comfy\main.py", working_directory=r"C:\Comfy",
            base_directory=r"C:\Comfy", user_directory=r"C:\Comfy\user",
            database_path=r"C:\Comfy\x.db", extra_model_paths_config=r"C:\Comfy\x.yaml",
            input_directory=r"C:\Comfy\in", output_directory=r"C:\Comfy\out",
            host="127.0.0.1", port=8000, cuda_device=0, target_gpu_uuid=GPU_UUID,
            target_host_gpu_index=0, expected_comfy_device_index=0)
        target = EnvironmentConfig(id="comfy", name="comfy", configured=True,
            adapter=adapter, gpu_ai=True, safety_profile="gpu_ai",
            health_checks=(HealthCheckConfig(type="adapter_status"),),
            preflight_checks=(DrainHttpJsonCheck(type="drain_http_json",
                purpose="active_requests", url="http://127.0.0.1:8000/queue",
                json_paths=("running",)),), allowed_actions=("stop",))
        plane = ControlPlane(ControlConfig(control_enabled=True, environments=(target,)),
                             self.database, StatefulRunner({}))
        blockers = plane.configuration_blockers(target, "stop")
        self.assertTrue(any("drain_http_json_arrays" in blocker for blocker in blockers))

    async def test_disabled_and_noop(self) -> None:
        disabled = ControlPlane(ControlConfig(environments=(env("a", self.a_cfg),)), self.database,
                                StatefulRunner({"a.service": "running"}))
        with self.assertRaisesRegex(Exception, "未启用"):
            await disabled.submit_environment("a", "start", "start:a", "admin", "local")
        runner = StatefulRunner({"a.service": "running"})
        plane = ControlPlane(ControlConfig(control_enabled=True,
                            environments=(env("a", self.a_cfg),)), self.database, runner)
        operation_id = await plane.submit_environment("a", "start", "start:a", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["result"], "noop")
        self.assertFalse(any(call[-2] == "start" for call in runner.calls))

    async def test_scene_only_stops_conflicts_and_rolls_back_changed(self) -> None:
        c_cfg = WslSystemdConfig(type="wsl_systemd", distro="Ubuntu", scope="system", service="c.service")
        runner = StatefulRunner({"a.service": "running", "b.service": "stopped",
                                 "c.service": "running"}, fail_start="b.service")
        config = ControlConfig(control_enabled=True,
            environments=(env("a", self.a_cfg), env("b", self.b_cfg), env("c", c_cfg)),
            scenes=(SceneConfig(id="video", name="video", desired=("b",), conflicts=("a",)),))
        plane = ControlPlane(config, self.database, runner)
        operation_id = await plane.submit_scene("video", "activate:video", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["status"], "failed")
        self.assertEqual(runner.states["a.service"], "running")
        self.assertEqual(runner.states["c.service"], "running")
        self.assertFalse(any(call[-1] == "c.service" and call[-2] != "is-active" for call in runner.calls))
        self.assertTrue(any(step["phase"] == "rollback" and step["target_id"] == "a"
                            for step in operation["steps"]))

    async def test_single_operation_lock(self) -> None:
        entered = asyncio.Event(); release = asyncio.Event()
        class BlockingRunner(StatefulRunner):
            def __call__(self, args, timeout):
                if args[-2] == "start":
                    entered.set()
                    while not release.is_set(): pass
                return super().__call__(args, timeout)
        runner = BlockingRunner({"a.service": "stopped"})
        plane = ControlPlane(ControlConfig(control_enabled=True,
                            environments=(env("a", self.a_cfg),)), self.database, runner)
        await plane.submit_environment("a", "start", "start:a", "admin", "local")
        with self.assertRaisesRegex(Exception, "正在执行"):
            await plane.submit_environment("a", "stop", "stop:a", "admin", "local")
        release.set()
        await plane._active_task

    async def test_two_control_planes_compete_for_cross_instance_lease(self) -> None:
        entered = asyncio.Event(); release = asyncio.Event()
        class BlockingRunner(StatefulRunner):
            def __call__(self, args, timeout):
                if args[-2] == "start":
                    entered.set()
                    while not release.is_set(): pass
                return super().__call__(args, timeout)
        config = ControlConfig(control_enabled=True, environments=(env("a", self.a_cfg),))
        first = ControlPlane(config, self.database, BlockingRunner({"a.service": "stopped"}))
        second_database = Database(self.database.path)
        second = ControlPlane(config, second_database, StatefulRunner({"a.service": "stopped"}))
        await first.submit_environment("a", "start", "start:a", "admin", "one")
        with self.assertRaisesRegex(Exception, "管理器进程|租约"):
            await second.submit_environment("a", "start", "start:a", "admin", "two")
        release.set(); await first._active_task

    async def test_scene_preflight_requires_explicit_rollback_permission(self) -> None:
        runner = StatefulRunner({"a.service": "running", "b.service": "stopped"})
        stop_only = env("a", self.a_cfg, actions=("stop",))
        plane = ControlPlane(ControlConfig(control_enabled=True,
            environments=(stop_only, env("b", self.b_cfg)),
            scenes=(SceneConfig(id="video", name="video", desired=("b",), conflicts=("a",)),)),
            self.database, runner)
        preflight = await plane.scene_preflight("video")
        self.assertFalse(preflight["ready"])
        self.assertIn("回滚", " ".join(preflight["blockers"]))

    async def test_stopped_unconfigured_conflict_does_not_block_scene_activation(self) -> None:
        conflict = EnvironmentConfig(
            id="a", name="inactive conflict", configured=False,
            missing_reason="尚未通过该环境自身的启动验收",
            adapter=self.a_cfg,
            health_checks=(HealthCheckConfig(type="adapter_status"),),
            allowed_actions=(),
        )
        runner = StatefulRunner({"a.service": "stopped", "b.service": "stopped"})
        plane = ControlPlane(ControlConfig(
            control_enabled=True,
            environments=(conflict, env("b", self.b_cfg)),
            scenes=(SceneConfig(id="development", name="development",
                                desired=("b",), conflicts=("a",)),),
        ), self.database, runner)

        preflight = await plane.scene_preflight("development")

        self.assertTrue(preflight["ready"])
        self.assertFalse(any("inactive conflict" in blocker
                             for blocker in preflight["blockers"]))
        self.assertFalse(any(step["target_id"] == "a" and step["action"] in {"stop", "start"}
                             for step in preflight["plan"]))

        operation_id = await plane.submit_scene(
            "development", "activate:development", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["status"], "succeeded")
        self.assertEqual(runner.states, {"a.service": "stopped", "b.service": "running"})

    async def test_failed_conflict_blocks_scene_before_starting_desired(self) -> None:
        runner = StatefulRunner({"a.service": "stopped", "b.service": "stopped"})
        plane = ControlPlane(ControlConfig(
            control_enabled=True,
            environments=(env("a", self.a_cfg), env("b", self.b_cfg)),
            scenes=(SceneConfig(id="development", name="development",
                                desired=("b",), conflicts=("a",)),),
        ), self.database, runner)

        async def failed_conflict(item):
            return "failed" if item.id == "a" else "stopped"

        with patch.object(plane, "status", side_effect=failed_conflict):
            preflight = await plane.scene_preflight("development")

        self.assertFalse(preflight["ready"])
        self.assertIn("状态异常", " ".join(preflight["blockers"]))
        self.assertEqual(runner.states["b.service"], "stopped")

    async def test_shutdown_waits_for_uncancellable_adapter_and_safe_completion(self) -> None:
        entered = threading.Event(); release = threading.Event()
        class SlowRunner(StatefulRunner):
            def __call__(self, args, timeout):
                if args[-2] == "start": entered.set(); release.wait(2)
                return super().__call__(args, timeout)
        plane = ControlPlane(ControlConfig(control_enabled=True,
                            environments=(env("a", self.a_cfg),)), self.database,
                            SlowRunner({"a.service": "stopped"}))
        operation_id = await plane.submit_environment("a", "start", "start:a", "admin", "local")
        while not entered.is_set(): await asyncio.sleep(.005)
        shutdown = asyncio.create_task(plane.shutdown(timeout=.01))
        await asyncio.sleep(.03)
        self.assertFalse(shutdown.done())
        release.set(); await shutdown
        self.assertEqual(self.database.get_operation(operation_id)["status"], "succeeded")

    async def test_rollback_state_mismatch_is_recorded_as_failure(self) -> None:
        class BadRollbackRunner(StatefulRunner):
            def __call__(self, args, timeout):
                if args[-2] == "start" and args[-1] == "a.service":
                    self.calls.append(args); return CommandResult(0, "", "")
                return super().__call__(args, timeout)
        runner = BadRollbackRunner({"a.service": "running", "b.service": "stopped"},
                                   fail_start="b.service")
        plane = ControlPlane(ControlConfig(control_enabled=True,
            environments=(env("a", self.a_cfg), env("b", self.b_cfg)),
            scenes=(SceneConfig(id="video", name="video", desired=("b",), conflicts=("a",)),)),
            self.database, runner)
        operation_id = await plane.submit_scene("video", "activate:video", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["result"], "rollback_failed")
        self.assertTrue(any(step["phase"] == "rollback" and step["status"] == "failed"
                            for step in operation["steps"]))
        recovery = self.database.control_recovery_lock()
        self.assertEqual(recovery["operation_id"], operation_id)
        self.assertEqual([item["environment_id"] for item in recovery["items"]], ["a"])
        with self.assertRaisesRegex(ControlError, "人工处理"):
            await plane.submit_scene("video", "activate:video", "admin", "local")

    async def test_scene_fails_if_conflict_reappears_during_final_verification(self) -> None:
        class ReappearingConflictRunner(StatefulRunner):
            def __init__(self):
                super().__init__({"a.service": "running", "b.service": "stopped"})
                self.a_status_calls = 0
            def __call__(self, args, timeout):
                if args[-2] == "is-active" and args[-1] == "a.service":
                    self.a_status_calls += 1
                    if self.a_status_calls >= 5: self.states["a.service"] = "running"
                return super().__call__(args, timeout)
        runner = ReappearingConflictRunner()
        plane = ControlPlane(ControlConfig(control_enabled=True,
            environments=(env("a", self.a_cfg), env("b", self.b_cfg)),
            scenes=(SceneConfig(id="video", name="video", desired=("b",), conflicts=("a",)),)),
            self.database, runner)
        operation_id = await plane.submit_scene("video", "activate:video", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["status"], "failed")
        self.assertTrue(any(step["phase"] == "verify_conflicts" and step["status"] == "failed"
                            for step in operation["steps"]))

    async def test_scene_two_rollback_failures_create_multi_item_lock(self) -> None:
        c_cfg = WslSystemdConfig(type="wsl_systemd", distro="Ubuntu", scope="system",
                                 service="c.service")
        class TwoBadRollbacks(StatefulRunner):
            def __call__(self, args, timeout):
                if args[-2] == "start" and args[-1] in {"a.service", "b.service"}:
                    self.calls.append(args)
                    return CommandResult(0, "", "")
                return super().__call__(args, timeout)
        runner = TwoBadRollbacks(
            {"a.service": "running", "b.service": "running", "c.service": "stopped"},
            fail_start="c.service")
        plane = ControlPlane(ControlConfig(control_enabled=True,
            environments=(env("a", self.a_cfg), env("b", self.b_cfg), env("c", c_cfg)),
            scenes=(SceneConfig(id="video", name="video", desired=("c",),
                                conflicts=("a", "b")),)), self.database, runner)
        operation_id = await plane.submit_scene("video", "activate:video", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        recovery = self.database.control_recovery_lock()
        self.assertEqual(operation["result"], "rollback_failed")
        self.assertEqual(recovery["operation_id"], operation_id)
        self.assertEqual({item["environment_id"] for item in recovery["items"]}, {"a", "b"})
        with self.assertRaisesRegex(ControlError, "人工处理"):
            await plane.submit_environment("c", "start", "start:c", "admin", "local")

    async def test_h3_scene_plan_has_strict_safety_phase_order(self) -> None:
        health = (
            HealthCheckConfig(type="adapter_status"),
            LoopbackHttpHealthCheck(type="loopback_http", url="http://127.0.0.1:8000/health",
                                    json_equals={"model": "approved-h3"}),
            NvidiaGpuProcessHealthCheck(type="nvidia_gpu_process", gpu_uuid=GPU_UUID,
                                        process_name="python.exe"),
        )
        conflict = EnvironmentConfig(
            id="a", name="source", configured=True, gpu_ai=True, adapter=self.a_cfg,
            health_checks=health, preflight_checks=ai_preflight_checks("a"),
            allowed_actions=("start", "stop"))
        target_checks = (*ai_preflight_checks("b"),
            WslPathDiskCheck(type="wsl_path_disk", purpose="lora", distro="Ubuntu",
                             path="/models/h3/h3_8step.safetensors", min_free_mib=1024),
            RequiredDependencyCheck(type="required_dependency", environment_id="dep"),
            H3VideoProfileCheck(type="h3_video_profile", steps=8,
                                lora_name="h3_turbo_8step.safetensors",
                                shift_video=12, shift_audio=3))
        target = EnvironmentConfig(
            id="b", name="H3", configured=True, gpu_ai=True, safety_profile="h3_video",
            adapter=self.b_cfg, health_checks=health, preflight_checks=target_checks,
            allowed_actions=("start", "stop"))
        dep_cfg = WslSystemdConfig(type="wsl_systemd", distro="Ubuntu", scope="system",
                                   service="dep.service")
        runner = StatefulRunner({"a.service": "running", "b.service": "stopped",
                                 "dep.service": "running"})
        plane = ControlPlane(ControlConfig(control_enabled=True,
            environments=(conflict, target, env("dep", dep_cfg)),
            scenes=(SceneConfig(id="video", name="video", desired=("b",), conflicts=("a",)),)),
            self.database, runner, safety_probe=HealthySafetyProbe())
        plan = (await plane.scene_preflight("video"))["plan"]
        phases = [step["phase"] for step in plan]
        expected = ["drain", "stop_conflicts", "verify_release_ports",
                    "validate_safety", "start_desired", "verify", "verify_conflicts"]
        positions = [phases.index(phase) for phase in expected]
        self.assertEqual(positions, sorted(positions))
        profile = next(step for step in plan
                       if step.get("check", {}).get("type") == "h3_video_profile")
        self.assertEqual(profile["check"]["steps"], 8)
        self.assertEqual(profile["check"]["shift_video"], 12)
        self.assertEqual(profile["check"]["shift_audio"], 3)

    async def test_scene_rollback_running_health_failure_keeps_recovery_lock(self) -> None:
        health = (
            HealthCheckConfig(type="adapter_status"),
            LoopbackHttpHealthCheck(type="loopback_http", url="http://127.0.0.1:8000/health",
                                    json_equals={"model": "approved"}),
            NvidiaGpuProcessHealthCheck(type="nvidia_gpu_process", gpu_uuid=GPU_UUID,
                                        process_name="python.exe"),
        )
        source = EnvironmentConfig(
            id="a", name="source", configured=True, gpu_ai=True, adapter=self.a_cfg,
            health_checks=health, preflight_checks=ai_preflight_checks("a"),
            allowed_actions=("start", "stop"))
        class BaselineThenUnhealthy:
            def __init__(self): self.calls = 0
            def check(self, check):
                self.calls += 1
                return HealthResult(self.calls <= 2, "mock baseline/rollback health")
        runner = StatefulRunner({"a.service": "running", "b.service": "stopped"},
                                fail_start="b.service")
        plane = ControlPlane(ControlConfig(control_enabled=True,
            environments=(source, env("b", self.b_cfg)),
            scenes=(SceneConfig(id="video", name="video", desired=("b",), conflicts=("a",)),)),
            self.database, runner, BaselineThenUnhealthy(), HealthySafetyProbe())
        operation_id = await plane.submit_scene("video", "activate:video", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        recovery = self.database.control_recovery_lock()
        self.assertEqual(operation["result"], "rollback_failed")
        self.assertEqual(recovery["operation_id"], operation_id)
        self.assertEqual(recovery["items"][0]["expected_state"], "running")
        self.assertFalse((await plane.recovery_preflight())["ready"])

    async def test_gpu_ai_requires_endpoint_and_gpu_binding_checks(self) -> None:
        incomplete = EnvironmentConfig(
            id="gpu", name="gpu", configured=True, gpu_ai=True, adapter=self.a_cfg,
            health_checks=(HealthCheckConfig(type="adapter_status"),),
            allowed_actions=("start", "stop"),
        )
        plane = ControlPlane(ControlConfig(control_enabled=True, environments=(incomplete,)),
                             self.database, StatefulRunner({"a.service": "stopped"}))
        preflight = await plane.environment_preflight("gpu")
        self.assertFalse(preflight["ready"])
        self.assertIn("endpoint", " ".join(preflight["blockers"]))
        self.assertIn("GPU UUID", " ".join(preflight["blockers"]))

    async def test_environment_execution_rechecks_static_configuration(self) -> None:
        runner = StatefulRunner({"a.service": "stopped"})
        plane = ControlPlane(ControlConfig(control_enabled=True,
                            environments=(env("a", self.a_cfg),)), self.database, runner)
        with patch.object(plane, "configuration_blockers",
                          side_effect=[[], ["mock execution-time configuration failure"]]) as validate:
            operation_id = await plane.submit_environment(
                "a", "start", "start:a", "admin", "local")
            await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(operation["status"], "failed")
        self.assertIn("configuration failure", operation["error_summary"])
        self.assertFalse(any(call[-2] == "start" for call in runner.calls))
        self.assertTrue(any(step["phase"] == "preflight" and step["status"] == "failed"
                            for step in operation["steps"]))

    async def test_environment_timeout_after_effect_is_reconciled(self) -> None:
        class AppliedThenTimedOut(StatefulRunner):
            def __call__(self, args, timeout):
                if args[-2] == "start":
                    self.calls.append(args); self.states[args[-1]] = "running"
                    raise ControlError(504, "command_timeout", "mock timeout")
                return super().__call__(args, timeout)
        runner = AppliedThenTimedOut({"a.service": "stopped"})
        plane = ControlPlane(ControlConfig(control_enabled=True,
                            environments=(env("a", self.a_cfg),)), self.database, runner)
        operation_id = await plane.submit_environment("a", "start", "start:a", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["status"], "succeeded")
        self.assertEqual(operation["result"], "reconciled")
        self.assertIsNone(self.database.control_recovery_lock())

    async def test_environment_stop_timeout_after_effect_is_reconciled(self) -> None:
        class AppliedStopThenTimedOut(StatefulRunner):
            def __call__(self, args, timeout):
                if args[-2] == "stop":
                    self.calls.append(args); self.states[args[-1]] = "stopped"
                    raise ControlError(504, "command_timeout", "mock timeout")
                return super().__call__(args, timeout)
        runner = AppliedStopThenTimedOut({"a.service": "running"})
        plane = ControlPlane(ControlConfig(control_enabled=True,
                            environments=(env("a", self.a_cfg),)), self.database, runner)
        operation_id = await plane.submit_environment("a", "stop", "stop:a", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["status"], "succeeded")
        self.assertEqual(operation["result"], "reconciled")

    async def test_environment_unhealthy_change_rolls_back(self) -> None:
        checks = (
            HealthCheckConfig(type="adapter_status"),
            LoopbackHttpHealthCheck(type="loopback_http", url="http://127.0.0.1:8000/health",
                                    json_equals={"model": "approved"}),
            NvidiaGpuProcessHealthCheck(type="nvidia_gpu_process",
                gpu_uuid="GPU-12345678-1234-1234-1234-123456789abc", process_name="python.exe"),
        )
        target = EnvironmentConfig(id="gpu", name="gpu", configured=True, gpu_ai=True,
            adapter=self.a_cfg, health_checks=checks,
            preflight_checks=ai_preflight_checks("gpu"),
            allowed_actions=("start", "stop"))
        class UnhealthyProbe:
            def check(self, check): return HealthResult(False, "mock unhealthy")
        runner = StatefulRunner({"a.service": "stopped"})
        plane = ControlPlane(ControlConfig(control_enabled=True, environments=(target,)),
                             self.database, runner, UnhealthyProbe(), HealthySafetyProbe())
        operation_id = await plane.submit_environment(
            "gpu", "start", "start:gpu", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["status"], "failed")
        self.assertEqual(operation["result"], "rolled_back")
        self.assertEqual(runner.states["a.service"], "stopped")
        self.assertIsNone(self.database.control_recovery_lock())

    async def test_environment_rollback_failure_sets_persistent_recovery_lock(self) -> None:
        checks = (
            HealthCheckConfig(type="adapter_status"),
            LoopbackHttpHealthCheck(type="loopback_http", url="http://127.0.0.1:8000/health",
                                    json_equals={"model": "approved"}),
            NvidiaGpuProcessHealthCheck(type="nvidia_gpu_process",
                gpu_uuid="GPU-12345678-1234-1234-1234-123456789abc", process_name="python.exe"),
        )
        target = EnvironmentConfig(id="gpu", name="gpu", configured=True, gpu_ai=True,
            adapter=self.a_cfg, health_checks=checks,
            preflight_checks=ai_preflight_checks("gpu"),
            allowed_actions=("start", "stop"))
        class BadStopRunner(StatefulRunner):
            def __call__(self, args, timeout):
                if args[-2] == "stop":
                    self.calls.append(args); return CommandResult(0, "", "")
                return super().__call__(args, timeout)
        class UnhealthyProbe:
            def check(self, check): return HealthResult(False, "mock unhealthy")
        runner = BadStopRunner({"a.service": "stopped"})
        plane = ControlPlane(ControlConfig(control_enabled=True, environments=(target,)),
                             self.database, runner, UnhealthyProbe(), HealthySafetyProbe())
        operation_id = await plane.submit_environment(
            "gpu", "start", "start:gpu", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["result"], "rollback_failed")
        self.assertEqual(self.database.control_recovery_lock()["operation_id"], operation_id)
        self.assertTrue(any(step["phase"] == "rollback" and step["status"] == "failed"
                            for step in operation["steps"]))

    async def test_unrecoverable_environment_change_persistently_blocks_control(self) -> None:
        checks = (
            HealthCheckConfig(type="adapter_status"),
            LoopbackHttpHealthCheck(type="loopback_http", url="http://127.0.0.1:8000/health",
                                    json_equals={"model": "approved"}),
            NvidiaGpuProcessHealthCheck(type="nvidia_gpu_process",
                gpu_uuid="GPU-12345678-1234-1234-1234-123456789abc", process_name="python.exe"),
        )
        target = EnvironmentConfig(id="gpu", name="gpu", configured=True, gpu_ai=True,
            adapter=self.a_cfg, health_checks=checks,
            preflight_checks=ai_preflight_checks("gpu"), allowed_actions=("start",))
        class ToggleProbe:
            healthy = False
            def check(self, check): return HealthResult(self.healthy, "mock health")
        probe = ToggleProbe(); runner = StatefulRunner({"a.service": "stopped"})
        plane = ControlPlane(ControlConfig(control_enabled=True, environments=(target,)),
                             self.database, runner, probe, HealthySafetyProbe())
        operation_id = await plane.submit_environment(
            "gpu", "start", "start:gpu", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["result"], "recovery_required")
        self.assertEqual(self.database.control_recovery_lock()["environment_id"], "gpu")
        with self.assertRaisesRegex(ControlError, "人工处理"):
            await plane.submit_environment("gpu", "start", "start:gpu", "admin", "local")
        blocked = await plane.list_environments()
        self.assertEqual(blocked["recovery_required"]["environment_id"], "gpu")
        probe.healthy = True
        preflight = await plane.recovery_preflight()
        self.assertFalse(preflight["ready"], "状态尚未人工恢复为 stopped")
        runner.states["a.service"] = "stopped"
        preflight = await plane.recovery_preflight()
        self.assertTrue(preflight["ready"])
        with self.assertRaisesRegex(ControlError, "确认"):
            await plane.resolve_recovery("wrong", "admin", "local")
        await plane.resolve_recovery("resolve-recovery:gpu", "admin", "local")
        self.assertIsNone(self.database.control_recovery_lock())
        self.assertEqual(self.database.list_audit(1)[0]["event"], "control.recovery.resolve")

    async def test_environment_action_capabilities_are_action_specific(self) -> None:
        incomplete = EnvironmentConfig(
            id="gpu", name="gpu", configured=True, gpu_ai=True, adapter=self.a_cfg,
            health_checks=(HealthCheckConfig(type="adapter_status"),),
            preflight_checks=(ai_preflight_checks("gpu")[0],),
            allowed_actions=("start", "stop", "restart"),
        )
        plane = ControlPlane(ControlConfig(control_enabled=True, environments=(incomplete,)),
                             self.database, StatefulRunner({"a.service": "running"}),
                             safety_probe=HealthySafetyProbe())
        listed = (await plane.list_environments())["environments"][0]
        self.assertFalse(listed["action_capabilities"]["start"]["ready"])
        self.assertFalse(listed["action_capabilities"]["restart"]["ready"])
        self.assertTrue(listed["action_capabilities"]["stop"]["ready"])
        self.assertFalse((await plane.environment_preflight("gpu", "start"))["ready"])
        stop = await plane.environment_preflight("gpu", "stop")
        self.assertTrue(stop["ready"])
        self.assertEqual(stop["blockers"], [])

    async def test_stop_capability_remains_ready_when_status_is_unknown(self) -> None:
        runner = lambda args, timeout: CommandResult(1, "unknown\n", "")
        plane = ControlPlane(ControlConfig(control_enabled=True,
                            environments=(env("a", self.a_cfg),)), self.database, runner)
        listed = (await plane.list_environments())["environments"][0]
        self.assertEqual(listed["status"], "unknown")
        self.assertTrue(listed["action_capabilities"]["stop"]["ready"])
        self.assertFalse(listed["action_capabilities"]["start"]["ready"])
        self.assertTrue((await plane.environment_preflight("a", "stop"))["ready"])

    async def test_unknown_before_failed_action_requires_recovery_to_stopped(self) -> None:
        class UnknownBeforeRunner:
            def __init__(self): self.status_calls = 0
            def __call__(self, args, timeout):
                if args[-2] == "is-active":
                    self.status_calls += 1
                    return CommandResult(1 if self.status_calls == 1 else 3,
                                         "unknown\n" if self.status_calls == 1 else "inactive\n", "")
                raise ControlError(504, "command_timeout", "mock timeout")
        plane = ControlPlane(ControlConfig(control_enabled=True,
                            environments=(env("a", self.a_cfg),)), self.database,
                            UnknownBeforeRunner())
        operation_id = await plane.submit_environment("a", "start", "start:a", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["result"], "recovery_required")
        self.assertEqual(self.database.control_recovery_lock()["expected_state"], "stopped")

    async def test_database_finalization_failure_poisoned_and_recovers_on_restart(self) -> None:
        runner = StatefulRunner({"a.service": "stopped"})
        config = ControlConfig(control_enabled=True, environments=(env("a", self.a_cfg),))
        plane = ControlPlane(config, self.database, runner)
        original_finish = self.database.finish_operation_with_audit
        with patch.object(self.database, "finish_operation_with_audit",
                          side_effect=DatabaseError("mock finalization failure")):
            operation_id = await plane.submit_environment(
                "a", "start", "start:a", "admin", "local")
            await plane._active_task
        self.assertEqual(self.database.get_operation(operation_id)["status"], "running")
        self.assertIsNotNone(plane._operation_lock)
        self.assertIsNotNone(self.database.control_lease_owner())
        await plane.shutdown()
        self.assertIsNotNone(plane._operation_lock, "shutdown 不得释放 poisoned 安全锁")
        self.assertIsNotNone(self.database.control_lease_owner())
        with self.assertRaisesRegex(ControlError, "必须重启"):
            await plane.submit_environment("a", "stop", "stop:a", "admin", "local")
        second = ControlPlane(config, Database(self.database.path),
                              StatefulRunner({"a.service": "running"}))
        with self.assertRaisesRegex(ControlError, "管理器进程|租约"):
            await second.submit_environment("a", "stop", "stop:a", "admin", "local")

        # 模拟进程退出：操作系统释放文件句柄，但 SQLite 遗留租约和 running operation 保留。
        plane._operation_lock.release()
        plane._operation_lock = None
        restarted = ControlPlane(config, Database(self.database.path),
                                 StatefulRunner({"a.service": "running"}))
        recovery = restarted.recovery_lock()
        self.assertIsNotNone(recovery)
        self.assertEqual(recovery["operation_id"], operation_id)
        self.assertIn(recovery["expected_state"], {"running", "stopped"})
        with self.assertRaisesRegex(ControlError, "人工处理"):
            await restarted.submit_environment("a", "stop", "stop:a", "admin", "local")

    async def test_invalid_recovery_expected_state_can_never_be_resolved(self) -> None:
        self.database.create_operation("c" * 32, "environment", "a", "start", "admin", "local")
        self.database.finish_operation_with_audit(
            "c" * 32, "failed", "recovery_required", "unknown", "failed", "mock",
            recovery_lock={"environment_id": "a", "expected_state": "failed",
                           "reason": "mock invalid legacy lock"},
        )
        plane = ControlPlane(ControlConfig(control_enabled=True,
                            environments=(env("a", self.a_cfg),)), self.database,
                            StatefulRunner({"a.service": "stopped"}))
        preflight = await plane.recovery_preflight()
        self.assertFalse(preflight["ready"])
        self.assertIn("期望状态无效", " ".join(preflight["blockers"]))
        with self.assertRaisesRegex(ControlError, "预检未通过"):
            await plane.resolve_recovery("resolve-recovery:a", "admin", "local")

    async def test_multi_environment_recovery_requires_every_item_and_exact_confirmation(self) -> None:
        operation_id = "d" * 32
        self.database.create_operation(operation_id, "scene", "video", "activate", "admin", "local")
        self.database.finish_operation_with_audit(
            operation_id, "failed", "rollback_failed", None, None, "mock rollback",
            recovery_items=[
                {"environment_id": "a", "expected_state": "running", "reason": "mock a"},
                {"environment_id": "b", "expected_state": "stopped", "reason": "mock b"},
            ],
        )
        runner = StatefulRunner({"a.service": "running", "b.service": "running"})
        plane = ControlPlane(ControlConfig(control_enabled=True,
            environments=(env("a", self.a_cfg), env("b", self.b_cfg))),
            self.database, runner)
        blocked = await plane.recovery_preflight()
        self.assertFalse(blocked["ready"])
        self.assertEqual(blocked["confirmation"], f"resolve-recovery:{operation_id}")
        runner.states["b.service"] = "stopped"
        ready = await plane.recovery_preflight()
        self.assertTrue(ready["ready"])
        with self.assertRaisesRegex(ControlError, "确认"):
            await plane.resolve_recovery("resolve-recovery:a", "admin", "local")
        await plane.resolve_recovery(f"resolve-recovery:{operation_id}", "admin", "local")
        self.assertIsNone(self.database.control_recovery_lock())

    async def test_ai_stop_drain_timeout_never_invokes_stop(self) -> None:
        checks = (
            HealthCheckConfig(type="adapter_status"),
            LoopbackHttpHealthCheck(type="loopback_http", url="http://127.0.0.1:8000/health",
                                    json_equals={"model": "approved"}),
            NvidiaGpuProcessHealthCheck(type="nvidia_gpu_process", gpu_uuid=GPU_UUID,
                                        process_name="python.exe"),
        )
        target = EnvironmentConfig(
            id="gpu", name="gpu", configured=True, gpu_ai=True, adapter=self.a_cfg,
            health_checks=checks, preflight_checks=ai_preflight_checks("gpu"),
            allowed_actions=("start", "stop"),
        )
        class BusySafety:
            def check(self, check):
                return HealthResult(not isinstance(check, DrainHttpJsonCheck),
                                    "active requests=1" if isinstance(check, DrainHttpJsonCheck)
                                    else "mock safe")
        runner = StatefulRunner({"a.service": "running"})
        plane = ControlPlane(ControlConfig(control_enabled=True, environments=(target,)),
                             self.database, runner, safety_probe=BusySafety())
        self.assertFalse((await plane.environment_preflight("gpu", "stop"))["ready"])
        operation_id = await plane.submit_environment(
            "gpu", "stop", "stop:gpu", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["status"], "failed")
        self.assertTrue(any(step["phase"] == "drain" and step["status"] == "failed"
                            for step in operation["steps"]))
        self.assertFalse(any(call[-2] == "stop" for call in runner.calls))

    async def test_scene_health_failure_rolls_back_new_gpu_target(self) -> None:
        gpu_checks = (
            HealthCheckConfig(type="adapter_status"),
            LoopbackHttpHealthCheck(type="loopback_http", url="http://127.0.0.1:8000/v1/models",
                                    json_equals={"model": "approved-model"}),
            NvidiaGpuProcessHealthCheck(type="nvidia_gpu_process",
                gpu_uuid="GPU-12345678-1234-1234-1234-123456789abc", process_name="python.exe"),
        )
        target = EnvironmentConfig(id="b", name="gpu target", configured=True, gpu_ai=True,
            adapter=self.b_cfg, health_checks=gpu_checks,
            preflight_checks=ai_preflight_checks("b"),
            allowed_actions=("start", "stop"))
        class Probe:
            def check(self, check):
                return HealthResult(False, "mock endpoint/model/GPU mismatch")
        runner = StatefulRunner({"a.service": "running", "b.service": "stopped"})
        plane = ControlPlane(ControlConfig(control_enabled=True,
            environments=(env("a", self.a_cfg), target),
            scenes=(SceneConfig(id="video", name="video", desired=("b",), conflicts=("a",)),)),
            self.database, runner, Probe(), HealthySafetyProbe())
        operation_id = await plane.submit_scene("video", "activate:video", "admin", "local")
        await plane._active_task
        operation = self.database.get_operation(operation_id)
        self.assertEqual(operation["status"], "failed")
        self.assertEqual(runner.states["b.service"], "stopped")
        self.assertTrue(any(step["phase"] == "rollback" and step["target_id"] == "b"
                            for step in operation["steps"]))

    async def test_scene_active_requires_all_mocked_model_and_gpu_health(self) -> None:
        checks = (
            HealthCheckConfig(type="adapter_status"),
            LoopbackHttpHealthCheck(type="loopback_http", url="http://127.0.0.1:8000/health",
                                    json_equals={"model": "approved"}),
            NvidiaGpuProcessHealthCheck(type="nvidia_gpu_process",
                gpu_uuid="GPU-12345678-1234-1234-1234-123456789abc", process_name="python.exe"),
        )
        target = EnvironmentConfig(id="b", name="gpu", configured=True, gpu_ai=True,
            adapter=self.b_cfg, health_checks=checks,
            preflight_checks=ai_preflight_checks("b"),
            allowed_actions=("start", "stop"))
        class Probe:
            def __init__(self, healthy): self.healthy = healthy
            def check(self, check): return HealthResult(self.healthy, "mock health")
        config = ControlConfig(control_enabled=True, environments=(target,),
            scenes=(SceneConfig(id="video", name="video", desired=("b",), conflicts=()),))
        runner = StatefulRunner({"b.service": "running"})
        healthy = await ControlPlane(config, self.database, runner, Probe(True),
                                     HealthySafetyProbe()).list_scenes()
        self.assertEqual(healthy["scenes"][0]["current"], "active")
        unhealthy = await ControlPlane(config, self.database, runner, Probe(False),
                                       HealthySafetyProbe()).list_scenes()
        self.assertNotEqual(unhealthy["scenes"][0]["current"], "active")
        self.assertFalse(unhealthy["scenes"][0]["ready"])

    async def test_scene_active_requires_dependency_environment_health(self) -> None:
        target = EnvironmentConfig(
            id="a", name="target", configured=True, adapter=self.a_cfg,
            health_checks=(HealthCheckConfig(type="adapter_status"),),
            preflight_checks=(RequiredDependencyCheck(
                type="required_dependency", environment_id="b"),),
            allowed_actions=("start", "stop"))
        runner = StatefulRunner({"a.service": "running", "b.service": "stopped"})
        plane = ControlPlane(ControlConfig(control_enabled=True,
            environments=(target, env("b", self.b_cfg)),
            scenes=(SceneConfig(id="development", name="development",
                                desired=("a",), conflicts=()),)), self.database, runner)
        scene = (await plane.list_scenes())["scenes"][0]
        self.assertNotEqual(scene["current"], "active")
        self.assertFalse(scene["ready"])
        self.assertIn("依赖环境 b", " ".join(scene["blockers"]))

    async def test_scene_does_not_rollback_target_that_was_already_running(self) -> None:
        c_cfg = WslSystemdConfig(type="wsl_systemd", distro="Ubuntu", scope="system", service="c.service")
        runner = StatefulRunner({"a.service": "running", "b.service": "running",
                                 "c.service": "stopped"}, fail_start="c.service")
        plane = ControlPlane(ControlConfig(control_enabled=True,
            environments=(env("a", self.a_cfg), env("b", self.b_cfg), env("c", c_cfg)),
            scenes=(SceneConfig(id="video", name="video", desired=("b", "c"), conflicts=("a",)),)),
            self.database, runner)
        operation_id = await plane.submit_scene("video", "activate:video", "admin", "local")
        await plane._active_task
        self.assertEqual(self.database.get_operation(operation_id)["status"], "failed")
        self.assertEqual(runner.states["b.service"], "running")
        self.assertFalse(any(call[-1] == "b.service" and call[-2] == "stop" for call in runner.calls))


class DatabaseAndApiTests(unittest.TestCase):
    def test_audit_retention_preserves_operation_audit_foreign_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Database(Path(temp) / "manager.db", audit_retention_max_events=2)
            database.create_operation("f" * 32, "environment", "a", "start", "admin", "local")
            database.finish_operation_with_audit("f" * 32, "succeeded", "noop", "running", "running")
            for index in range(5):
                database.append_audit("local", "test.event", "success", {"index": index})
            operation = database.get_operation("f" * 32)
            self.assertIsNotNone(operation["audit_event_id"])
            self.assertTrue(any(event["id"] == operation["audit_event_id"]
                                for event in database.list_audit(20)))

    def test_restart_marks_running_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manager.db"
            database = Database(path)
            database.create_operation("a" * 32, "environment", "a", "start", "admin", "local")
            database.update_operation("a" * 32, status="running")
            database.create_operation_step("a" * 32, 1, "action", "a", "start")
            ControlPlane(ControlConfig(), Database(path))
            self.assertEqual(database.get_operation("a" * 32)["status"], "interrupted")
            self.assertEqual(database.get_operation("a" * 32)["steps"][0]["status"], "interrupted")
            self.assertEqual(database.list_audit(1)[0]["event"], "control.recovery")

    def test_restart_rebuilds_multi_environment_scene_recovery_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manager.db"
            database = Database(path)
            operation_id = "e" * 32
            database.create_operation(operation_id, "scene", "video", "activate", "admin", "local")
            database.update_operation(
                operation_id, status="running",
                before_state='{"statuses":{"a":"running","b":"unknown"},"health":{}}')
            ControlPlane(ControlConfig(), Database(path))
            recovery = database.control_recovery_lock()
            self.assertEqual(recovery["operation_id"], operation_id)
            self.assertEqual(
                {item["environment_id"]: item["expected_state"] for item in recovery["items"]},
                {"a": "running", "b": "stopped"},
            )

    def test_default_config_and_api_are_disabled_with_explicit_video_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control_path = root / "control.json"
            control_path.with_name("control.example.json").write_text(
                (Path(__file__).resolve().parents[1] / "config" / "control.example.json").read_text(
                    encoding="utf-8"), encoding="utf-8")
            settings = Settings(database_path=root / "db.sqlite", discovery_scripts_path=root,
                                control_config_path=control_path,
                                scan_scripts_on_startup=False, sample_interval_seconds=60)
            context = TestClient(create_app(settings, Sampler(settings, collector=fake_snapshot)),
                                 client=("127.0.0.1", 50000))
            with context as client:
                setup = client.post("/api/v1/auth/setup", json={"username": "admin",
                                    "password": "correct horse battery staple"})
                csrf = setup.json()["csrf_token"]
                with patch("workstation_manager.control.subprocess.run") as subprocess_run:
                    scenes = client.get("/api/v1/scenes").json()
                subprocess_run.assert_not_called()
                video = next(item for item in scenes["scenes"] if item["id"] == "video")
                development = next(item for item in scenes["scenes"] if item["id"] == "development")
                text = " ".join(video["blockers"])
                development_text = " ".join(development["blockers"])
                self.assertFalse(scenes["control_enabled"])
                self.assertEqual(scenes["source"], "example_preview")
                self.assertIn("H3", text)
                self.assertIn("视频辅助", text)
                self.assertIn("语音识别", development_text)
                self.assertIn("语音合成", development_text)
                rejected = client.post("/api/v1/scenes/video/activate",
                    headers={"X-CSRF-Token": csrf}, json={"confirmation": "activate:video"})
                self.assertEqual(rejected.status_code, 403)
                self.assertEqual(rejected.json()["error"]["code"], "control_disabled")

    def test_enabled_api_requires_csrf_confirmation_and_allowed_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); database = Database(root / "db.sqlite")
            adapter = WslSystemdConfig(type="wsl_systemd", distro="Ubuntu", scope="system",
                                       service="a.service")
            runner = StatefulRunner({"a.service": "stopped"})
            plane = ControlPlane(ControlConfig(control_enabled=True,
                environments=(env("a", adapter, actions=("start",)),)), database, runner)
            settings = Settings(database_path=database.path, discovery_scripts_path=root,
                                scan_scripts_on_startup=False, sample_interval_seconds=60)
            with TestClient(create_app(settings, Sampler(settings, collector=fake_snapshot),
                                       database=database, control_plane=plane),
                            client=("127.0.0.1", 50000)) as client:
                setup = client.post("/api/v1/auth/setup", json={"username": "admin",
                                    "password": "correct horse battery staple"})
                csrf = setup.json()["csrf_token"]
                no_csrf = client.post("/api/v1/environments/a/actions",
                                      json={"action": "start", "confirmation": "start:a"})
                self.assertEqual(no_csrf.status_code, 403)
                wrong = client.post("/api/v1/environments/a/actions",
                    headers={"X-CSRF-Token": csrf},
                    json={"action": "start", "confirmation": "START a"})
                self.assertEqual(wrong.status_code, 422)
                self.assertEqual(wrong.json()["error"]["code"], "confirmation_mismatch")
                forbidden = client.post("/api/v1/environments/a/actions",
                    headers={"X-CSRF-Token": csrf},
                    json={"action": "stop", "confirmation": "stop:a"})
                self.assertEqual(forbidden.status_code, 403)
                self.assertEqual(forbidden.json()["error"]["code"], "action_not_allowed")
                accepted = client.post("/api/v1/environments/a/actions",
                    headers={"X-CSRF-Token": csrf},
                    json={"action": "start", "confirmation": "start:a"})
                self.assertEqual(accepted.status_code, 202)
                operation_id = accepted.json()["operation_id"]
                for _ in range(30):
                    operation = client.get(f"/api/v1/operations/{operation_id}").json()
                    if operation["status"] in {"succeeded", "failed", "interrupted"}: break
                    time.sleep(.01)
                self.assertEqual(operation["status"], "succeeded")
                self.assertEqual(operation["requested_by"], "admin")
                self.assertEqual(operation["source_ip"], "127.0.0.1")
                self.assertTrue(operation["steps"])
                listed = client.get("/api/v1/operations?limit=10").json()["operations"]
                self.assertTrue(listed[0]["steps"])

    def test_direct_environment_action_cannot_bypass_static_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); database = Database(root / "db.sqlite")
            adapter = WslSystemdConfig(type="wsl_systemd", distro="Ubuntu", scope="system",
                                       service="gpu.service")
            incomplete = EnvironmentConfig(
                id="gpu", name="gpu", configured=True, gpu_ai=True, adapter=adapter,
                health_checks=(
                    HealthCheckConfig(type="adapter_status"),
                    LoopbackHttpHealthCheck(type="loopback_http",
                                            url="http://127.0.0.1:8000/health"),
                ),
                preflight_checks=(ai_preflight_checks("gpu")[0],),
                allowed_actions=("start", "stop", "restart"),
            )
            runner = StatefulRunner({"gpu.service": "running"})
            plane = ControlPlane(ControlConfig(control_enabled=True, environments=(incomplete,)),
                                 database, runner, safety_probe=HealthySafetyProbe())
            settings = Settings(database_path=database.path, discovery_scripts_path=root,
                                scan_scripts_on_startup=False, sample_interval_seconds=60)
            with TestClient(create_app(settings, Sampler(settings, collector=fake_snapshot),
                                       database=database, control_plane=plane),
                            client=("127.0.0.1", 50000)) as client:
                setup = client.post("/api/v1/auth/setup", json={"username": "admin",
                                    "password": "correct horse battery staple"})
                csrf = setup.json()["csrf_token"]
                listed_gpu = client.get("/api/v1/environments").json()["environments"][0]
                self.assertFalse(listed_gpu["action_capabilities"]["start"]["ready"])
                self.assertTrue(listed_gpu["action_capabilities"]["stop"]["ready"])
                stop_preflight = client.post(
                    "/api/v1/environments/gpu/preflight?action=stop").json()
                self.assertTrue(stop_preflight["ready"])
                self.assertEqual(stop_preflight["blockers"], [])
                for action in ("start", "restart"):
                    rejected = client.post("/api/v1/environments/gpu/actions",
                        headers={"X-CSRF-Token": csrf},
                        json={"action": action, "confirmation": f"{action}:gpu"})
                    self.assertEqual(rejected.status_code, 409)
                    self.assertEqual(rejected.json()["error"]["code"], "environment_blocked")
                    details = " ".join(rejected.json()["error"]["details"])
                    self.assertIn("HTTP JSON", details)
                    self.assertIn("GPU UUID", details)
                self.assertEqual(database.list_operations(10), [])

                # 缺少启动健康检查不应阻止显式获准的安全释放动作。
                released = client.post("/api/v1/environments/gpu/actions",
                    headers={"X-CSRF-Token": csrf},
                    json={"action": "stop", "confirmation": "stop:gpu"})
                self.assertEqual(released.status_code, 202)
                operation_id = released.json()["operation_id"]
                for _ in range(30):
                    operation = client.get(f"/api/v1/operations/{operation_id}").json()
                    if operation["status"] in {"succeeded", "failed", "interrupted"}: break
                    time.sleep(.01)
                self.assertEqual(operation["status"], "succeeded")
                self.assertEqual(runner.states["gpu.service"], "stopped")

    def test_recovery_resolve_api_requires_csrf_preflight_and_exact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); database = Database(root / "db.sqlite")
            adapter = WslSystemdConfig(type="wsl_systemd", distro="Ubuntu", scope="system",
                                       service="a.service")
            database.create_operation("b" * 32, "environment", "a", "start", "admin", "local")
            database.finish_operation_with_audit(
                "b" * 32, "failed", "recovery_required", "stopped", "unknown", "mock",
                recovery_lock={"environment_id": "a", "expected_state": "stopped",
                               "reason": "mock recovery"},
            )
            plane = ControlPlane(ControlConfig(control_enabled=True,
                environments=(env("a", adapter),)), database,
                StatefulRunner({"a.service": "stopped"}))
            settings = Settings(database_path=database.path, discovery_scripts_path=root,
                                scan_scripts_on_startup=False, sample_interval_seconds=60)
            with TestClient(create_app(settings, Sampler(settings, collector=fake_snapshot),
                                       database=database, control_plane=plane),
                            client=("127.0.0.1", 50000)) as client:
                setup = client.post("/api/v1/auth/setup", json={"username": "admin",
                                    "password": "correct horse battery staple"})
                csrf = setup.json()["csrf_token"]
                preflight = client.post("/api/v1/control/recovery/preflight")
                self.assertEqual(preflight.status_code, 200)
                self.assertTrue(preflight.json()["ready"])
                no_csrf = client.post("/api/v1/control/recovery/resolve",
                                      json={"confirmation": "resolve-recovery:a"})
                self.assertEqual(no_csrf.status_code, 403)
                wrong = client.post("/api/v1/control/recovery/resolve",
                    headers={"X-CSRF-Token": csrf}, json={"confirmation": "wrong"})
                self.assertEqual(wrong.status_code, 422)
                resolved = client.post("/api/v1/control/recovery/resolve",
                    headers={"X-CSRF-Token": csrf},
                    json={"confirmation": "resolve-recovery:a"})
                self.assertEqual(resolved.status_code, 200)
                self.assertIsNone(database.control_recovery_lock())
                self.assertEqual(database.list_audit(1)[0]["event"], "control.recovery.resolve")


if __name__ == "__main__":
    unittest.main()
