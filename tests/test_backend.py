from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import psutil

from workstation_manager import __version__
from workstation_manager.app import create_app
from workstation_manager.auth import REMEMBER_SESSION_TTL_SECONDS
from workstation_manager.collectors import (
    _cached, _slow_cache, collect_docker, collect_docker_resources,
    collect_gpu_processes, collect_gpus, collect_host_power_sensors, collect_snapshot,
    summarize_power,
    collect_wsl_resources,
)
from workstation_manager import collectors
from workstation_manager.config import ConfigError, Settings, load_settings
from workstation_manager.power_model import (
    PowerModel,
    PowerModelError,
    solve_calibration,
)
from workstation_manager.database import Database, DatabaseError
from workstation_manager.history import (
    CollectionTimeoutError,
    HistoryStore,
    ResourceCollectionWorker,
    Sampler,
    SamplerStopError,
    parse_window,
)


def _hang_once_collection(settings: Settings) -> dict:
    marker = settings.database_path.with_suffix(".collector-started")
    if not marker.exists():
        marker.write_text("started", encoding="utf-8")
        time.sleep(10)
    return {
        "sampled_at": datetime.now(timezone.utc).isoformat(),
        "host": {"cpu": {}, "memory": {}},
        "gpus": [{"uuid": "GPU-recovered", "index": 0, "name": "RTX"}],
        "collector_errors": [],
    }


def _hang_with_child_process(settings: Settings) -> dict:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    settings.database_path.with_suffix(".child-pid").write_text(
        str(child.pid), encoding="utf-8"
    )
    time.sleep(60)
    raise RuntimeError("阻塞采集意外返回")


class ConfigTests(unittest.TestCase):
    def test_environment_overrides_defaults(self) -> None:
        settings = load_settings(
            {
                "WM_PORT": "19101",
                "WM_SAMPLE_INTERVAL_SECONDS": "2.5",
                "WM_CRITICAL_PORTS": "8080,18030",
            }
        )
        self.assertEqual(settings.port, 19101)
        self.assertEqual(settings.sample_interval_seconds, 2.5)
        self.assertEqual(settings.critical_ports, (8080, 18030))

    def test_default_history_uses_disk_retention_and_bounded_realtime_memory(self) -> None:
        settings = Settings()
        self.assertEqual(settings.history_minutes, 1440)
        self.assertEqual(settings.realtime_history_capacity, 181)

    def test_security_and_storage_configuration(self) -> None:
        settings = load_settings(
            {
                "WM_DATABASE_PATH": "D:/temp/manager.db",
                "WM_SESSION_TTL_SECONDS": "600",
                "WM_COOKIE_SECURE": "true",
                "WM_REQUEST_BODY_MAX_BYTES": "8192",
                "WM_AUTH_CONCURRENCY_LIMIT": "3",
                "WM_SESSION_MAX_ACTIVE": "8",
                "WM_AUDIT_RETENTION_MAX_EVENTS": "500",
                "WM_AUDIT_RETENTION_DAYS": "30",
                "WM_LOGIN_FAILURE_MAX_ROWS": "600",
                "WM_SCRIPT_STATUS_TIMEOUT_SECONDS": "3",
                "WM_SCRIPT_ACTION_TIMEOUT_SECONDS": "600",
            }
        )
        self.assertEqual(settings.database_path, Path("D:/temp/manager.db"))
        self.assertEqual(settings.session_ttl_seconds, 600)
        self.assertTrue(settings.cookie_secure)
        self.assertEqual(settings.request_body_max_bytes, 8192)
        self.assertEqual(settings.auth_concurrency_limit, 3)
        self.assertEqual(settings.session_max_active, 8)
        self.assertEqual(settings.audit_retention_max_events, 500)
        self.assertEqual(settings.audit_retention_days, 30)
        self.assertEqual(settings.login_failure_max_rows, 600)
        self.assertEqual(settings.script_status_timeout_seconds, 3)
        self.assertEqual(settings.script_action_timeout_seconds, 600)

    def test_invalid_port_is_explicit(self) -> None:
        with self.assertRaisesRegex(ConfigError, "1..65535"):
            load_settings({"WM_PORT": "70000"})
        with self.assertRaisesRegex(ConfigError, "不能与管理器 port 相同"):
            load_settings({"WM_PORT": "18765"})

    def test_configuration_rejects_non_finite_and_fractional_integers(self) -> None:
        invalid_cases = (
            ({"WM_SAMPLE_INTERVAL_SECONDS": "nan"}, "有限数字"),
            ({"WM_COMMAND_TIMEOUT_SECONDS": "inf"}, "有限数字"),
            ({"WM_HISTORY_MINUTES": "0.5"}, "正整数"),
            ({"WM_HISTORY_MINUTES": 15.0}, "正整数"),
            ({"WM_PORT": "19100.5"}, "整数端口"),
            ({"WM_PORT": 19100.0}, "整数端口"),
            ({"WM_CRITICAL_PORTS": "8080,8000.5"}, "整数端口"),
            ({"WM_SAMPLE_INTERVAL_SECONDS": True}, "有限数字"),
            ({"WM_PORT": "9" * 10000}, "整数端口"),
            ({"WM_HISTORY_MINUTES": "9" * 10000}, "正整数"),
        )
        for environment, message in invalid_cases:
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(ConfigError, message):
                    load_settings(environment)

    def test_configuration_enforces_resource_bounds(self) -> None:
        invalid_cases = (
            ({"WM_SAMPLE_INTERVAL_SECONDS": "0.49"}, "0.5..3600"),
            ({"WM_SAMPLE_INTERVAL_SECONDS": "3600.1"}, "0.5..3600"),
            ({"WM_HISTORY_MINUTES": "0"}, "1..1440"),
            ({"WM_HISTORY_MINUTES": "1441"}, "1..1440"),
            ({"WM_COMMAND_TIMEOUT_SECONDS": "0.09"}, "0.1..120"),
            ({"WM_COMMAND_TIMEOUT_SECONDS": "120.1"}, "0.1..120"),
        )
        for environment, message in invalid_cases:
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(ConfigError, message):
                    load_settings(environment)

        settings = load_settings(
            {
                "WM_SAMPLE_INTERVAL_SECONDS": "0.5",
                "WM_HISTORY_MINUTES": "1440",
                "WM_COMMAND_TIMEOUT_SECONDS": "120",
            }
        )
        self.assertEqual(settings.history_capacity, 172801)

    def test_history_capacity_rejects_unbounded_direct_settings(self) -> None:
        invalid_settings = (
            Settings(sample_interval_seconds=0.001, history_minutes=1440),
            Settings(sample_interval_seconds=float("inf")),
            Settings(sample_interval_seconds=float("-inf")),
            Settings(history_minutes=float("inf")),
        )
        for settings in invalid_settings:
            with self.subTest(settings=settings):
                with self.assertRaisesRegex(ConfigError, "容量"):
                    _ = settings.history_capacity

    @patch(
        "workstation_manager.config.Path.read_text",
        side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
    )
    def test_config_file_decode_error_is_wrapped(self, _read_text) -> None:
        with self.assertRaisesRegex(ConfigError, "无法读取配置文件"):
            load_settings({"WM_CONFIG_FILE": "broken.json"})

    @patch("workstation_manager.config.json.loads", side_effect=ValueError("invalid value"))
    @patch("workstation_manager.config.Path.read_text", return_value="{}")
    def test_config_file_value_error_is_wrapped(self, _read_text, _loads) -> None:
        with self.assertRaisesRegex(ConfigError, "不是有效 JSON"):
            load_settings({"WM_CONFIG_FILE": "broken.json"})

    def test_config_file_huge_numeric_values_are_wrapped(self) -> None:
        for field in ("sample_interval_seconds", "command_timeout_seconds"):
            with self.subTest(field=field):
                config_text = '{"' + field + '":' + "9" * 1000 + "}"
                with patch("workstation_manager.config.Path.read_text", return_value=config_text):
                    with self.assertRaises(ConfigError):
                        load_settings({"WM_CONFIG_FILE": "huge-number.json"})


class CollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings()
        _slow_cache.clear()

    def test_gpu_csv_is_parsed_per_card(self) -> None:
        output = (
            "0, GPU-a, NVIDIA GeForce RTX 4090, 98, 46359, 49140, 65, 430.5, 2715, 45, P0, 72, 8, 4, 4, 16, 0x4\n"
            "1, GPU-b, NVIDIA GeForce RTX 3090, 2, 100, 24576, 37, 25.0, 210, 0, P8, 1, 0, 0, 1, 4, 0x1\n"
        )
        gpus = collect_gpus(self.settings, runner=lambda command, timeout: output)
        self.assertEqual([gpu["uuid"] for gpu in gpus], ["GPU-a", "GPU-b"])
        self.assertEqual(gpus[0]["memory_used_mib"], 46359)
        self.assertEqual(gpus[0]["graphics_clock_mhz"], 2715)
        self.assertEqual(gpus[0]["performance_state"], "P0")
        self.assertEqual(gpus[0]["pcie_width"], 16)
        self.assertAlmostEqual(gpus[1]["memory_percent"], 0.41, places=2)

    def test_unsupported_gpu_metric_is_none(self) -> None:
        output = "0, GPU-a, RTX, N/A, 0, 24576, [N/A], N/A, N/A, N/A, P8, N/A, N/A, N/A, N/A, N/A, N/A\n"
        gpu = collect_gpus(self.settings, runner=lambda command, timeout: output)[0]
        self.assertIsNone(gpu["load_percent"])
        self.assertIsNone(gpu["temperature_c"])
        self.assertIsNone(gpu["power_w"])
        self.assertIsNone(gpu["graphics_clock_mhz"])

    def test_host_power_sensors_parse_non_gpu_watts(self) -> None:
        output = (
            '[{"source":"root\\\\LibreHardwareMonitor","name":"CPU Package",'
            '"hardware_name":"Ryzen","hardware_type":"Cpu","value_w":125.5},'
            '{"source":"root\\\\LibreHardwareMonitor","name":"GPU Power",'
            '"hardware_name":"NVIDIA RTX","hardware_type":"GpuNvidia","value_w":300}]'
        )
        with patch("workstation_manager.collectors.os.name", "nt"):
            sensors = collect_host_power_sensors(
                self.settings, runner=lambda command, timeout: output
            )

        self.assertEqual(len(sensors), 1)
        self.assertEqual(sensors[0]["value_w"], 125.5)

    def test_snapshot_total_power_sums_gpu_and_readable_sensors(self) -> None:
        def runner(command: list[str], timeout: float) -> str:
            if command[0] == "nvidia-smi" and "--query-gpu=" in command[1]:
                return (
                    "0, GPU-a, RTX 4090, 10, 100, 1000, 40, 430, 2000, 40, "
                    "P0, 5, 0, 0, 4, 16, 0x0\n"
                )
            if command[0] == "nvidia-smi":
                return ""
            if command[0] == "docker":
                return ""
            if command[0] == "powershell.exe":
                return '[{"name":"CPU Package","value_w":120}]'
            raise AssertionError(command)

        with patch("workstation_manager.collectors.os.name", "nt"), \
                patch("workstation_manager.collectors.collect_host",
                      return_value={"cpu": {}, "memory": {}, "disks": []}), \
                patch("workstation_manager.collectors.collect_ports", return_value=[]):
            snapshot = collect_snapshot(self.settings, runner=runner)

        power = snapshot["host"]["power"]
        # 采集进程只输出实测值，估算由主进程里的 Sampler 补上。
        self.assertEqual(power["measured_w"], 550)
        self.assertEqual(power["total_w"], 550)
        self.assertIsNone(power["estimated_w"])
        self.assertEqual(power["gpu_w"], 430)
        self.assertEqual(power["cpu_package_w"], 120)
        self.assertIsNone(power["sensor_w"])
        self.assertEqual(power["sensors"][0]["role"], "cpu_package")
        self.assertEqual(power["sensors"][0]["method"], "instant")

    def test_gpu_processes_preserve_wddm_unavailable_memory(self) -> None:
        output = "GPU-a, 1234, C:\\AI\\server.exe, N/A\n"
        processes = collect_gpu_processes(
            self.settings, runner=lambda command, timeout: output
        )
        self.assertEqual(processes[0]["pid"], 1234)
        self.assertEqual(processes[0]["name"], "C:\\AI\\server.exe")
        self.assertIsNone(processes[0]["memory_used_mib"])

    def test_gpu_process_csv_preserves_quoted_commas_in_path(self) -> None:
        output = 'GPU-a, 1234, "C:\\AI,Tools\\server.exe", 2048\n'
        process = collect_gpu_processes(
            self.settings, runner=lambda command, timeout: output
        )[0]
        self.assertEqual(process["name"], "C:\\AI,Tools\\server.exe")
        self.assertEqual(process["memory_used_mib"], 2048)

    def test_slow_cache_reuses_success_and_failure_until_backoff_expires(self) -> None:
        calls = {"success": 0, "failure": 0}

        def success() -> str:
            calls["success"] += 1
            return "ready"

        def failure() -> str:
            calls["failure"] += 1
            raise RuntimeError("runtime unavailable")

        self.assertEqual(_cached("success", 30, success), "ready")
        self.assertEqual(_cached("success", 30, success), "ready")
        with self.assertRaisesRegex(RuntimeError, "runtime unavailable"):
            _cached("failure", 30, failure)
        with self.assertRaisesRegex(RuntimeError, "runtime unavailable"):
            _cached("failure", 30, failure)
        self.assertEqual(calls, {"success": 1, "failure": 1})

    def test_docker_json_lines_are_parsed(self) -> None:
        output = '{"ID":"abc","Names":"ninfer","Image":"image","State":"running","Status":"Up","Ports":"0.0.0.0:8080->8080/tcp","Labels":""}\n'
        containers = collect_docker(self.settings, runner=lambda command, timeout: output)
        self.assertEqual(containers[0]["name"], "ninfer")
        self.assertEqual(containers[0]["state"], "running")
        self.assertNotIn("labels", containers[0])

    def test_docker_non_object_json_is_rejected_explicitly(self) -> None:
        with self.assertRaisesRegex(ValueError, "根节点必须是对象"):
            collect_docker(self.settings, runner=lambda command, timeout: "[]\n")

    def test_docker_resource_json_is_parsed(self) -> None:
        output = '{"Name":"ninfer","CPUPerc":"2.5%","MemUsage":"4GiB / 8GiB","NetIO":"1MB / 2MB","BlockIO":"3MB / 4MB","PIDs":"12"}\n'
        resources = collect_docker_resources(
            self.settings, runner=lambda command, timeout: output
        )
        self.assertEqual(resources["ninfer"]["memory_usage"], "4GiB / 8GiB")
        self.assertEqual(resources["ninfer"]["pids"], "12")

    def test_wsl_memory_and_swap_are_parsed_without_starting_another_distro(self) -> None:
        outputs = iter([
            "Ubuntu-22.04\n",
            "MemTotal:       49152000 kB\nMemAvailable:   30000000 kB\n"
            "SwapTotal:      33554432 kB\nSwapFree:       32505856 kB\n",
        ])
        resources = collect_wsl_resources(
            self.settings, runner=lambda command, timeout: next(outputs)
        )
        self.assertEqual(resources["distributions"], ["Ubuntu-22.04"])
        self.assertEqual(resources["swap_used_bytes"], 1024 * 1024 * 1024)

    @patch("workstation_manager.collectors.collect_ports", return_value=[])
    @patch("workstation_manager.collectors.collect_host", return_value={"cpu": {}, "memory": {}, "disks": []})
    def test_missing_commands_are_structured_and_isolated(self, _host, _ports) -> None:
        def missing(command: list[str], timeout: float) -> str:
            raise FileNotFoundError(command[0])

        snapshot = collect_snapshot(self.settings, runner=missing)
        self.assertEqual(snapshot["gpus"], [])
        self.assertEqual(snapshot["docker"]["containers"], [])
        expected = {"nvidia", "docker"}
        if os.name == "nt":
            expected.add("host_power")
        self.assertEqual({error["collector"] for error in snapshot["collector_errors"]}, expected)
        self.assertTrue(all(error["cause"] for error in snapshot["collector_errors"]))

    @patch("workstation_manager.collectors.collect_ports", return_value=[{"port": 8080, "listening": True}])
    @patch(
        "workstation_manager.collectors.collect_docker",
        return_value=[{"name": "example", "state": "running"}],
    )
    @patch("workstation_manager.collectors.collect_gpus", side_effect=RuntimeError("driver query failed"))
    @patch(
        "workstation_manager.collectors.collect_host",
        return_value={"cpu": {"load_percent": 10}, "memory": {"percent": 20}, "disks": []},
    )
    def test_runtime_error_is_isolated_and_other_collectors_return(
        self, _host, _gpus, _docker, _ports
    ) -> None:
        snapshot = collect_snapshot(self.settings)
        self.assertEqual(snapshot["gpus"], [])
        self.assertEqual(snapshot["docker"]["containers"][0]["name"], "example")
        self.assertTrue(snapshot["ports"][0]["listening"])
        self.assertEqual(snapshot["collector_errors"][0]["collector"], "nvidia")
        self.assertEqual(snapshot["collector_errors"][0]["error_type"], "RuntimeError")
        self.assertEqual(snapshot["collector_errors"][0]["cause"], "driver query failed")


class PowerModelTests(unittest.TestCase):
    def _sensor(self, name: str, value_w: float, role: str = "cpu_package") -> dict:
        return {"name": name, "value_w": value_w, "role": role}

    def test_disabled_model_reports_measured_only(self) -> None:
        summary = summarize_power(
            [{"power_w": 430.0}], [self._sensor("RAPL_Package0_PKG", 120.0)],
            PowerModel(enabled=False),
        )
        self.assertEqual(summary["measured_w"], 550)
        self.assertEqual(summary["total_w"], 550)
        self.assertIsNone(summary["estimated_w"])
        self.assertIsNone(summary["estimate_breakdown"])

    def test_estimate_adds_vrm_baseline_and_psu_loss(self) -> None:
        model = PowerModel(
            enabled=True, cpu_vrm_efficiency=0.89, baseline_w=75.0, psu_efficiency=0.90
        )
        summary = summarize_power(
            [{"power_w": 400.0}, {"power_w": 330.0}],
            [self._sensor("RAPL_Package0_PKG", 200.0)], model,
        )
        self.assertEqual(summary["measured_w"], 930)
        breakdown = summary["estimate_breakdown"]
        # 200 W 封装功耗在 89% 的 VRM 效率下要额外消耗约 24.7 W。
        self.assertAlmostEqual(breakdown["cpu_vrm_loss_w"], 24.72, places=1)
        self.assertEqual(breakdown["baseline_w"], 75)
        expected_total = (930 + breakdown["cpu_vrm_loss_w"] + 75) / 0.90
        self.assertAlmostEqual(summary["total_w"], round(expected_total, 2), places=1)
        self.assertAlmostEqual(
            summary["measured_w"] + summary["estimated_w"], summary["total_w"], places=1
        )

    def test_estimate_never_invents_a_reading_without_sensors(self) -> None:
        summary = summarize_power([], [], PowerModel(enabled=True))
        self.assertIsNone(summary["measured_w"])
        self.assertIsNone(summary["total_w"])
        self.assertIsNone(summary["estimated_w"])

    def test_other_sensors_skip_the_vrm_correction(self) -> None:
        model = PowerModel(
            enabled=True, cpu_vrm_efficiency=0.89, baseline_w=0.0, psu_efficiency=1.0
        )
        summary = summarize_power(
            [], [self._sensor("Mainboard", 100.0, role="other")], model
        )
        self.assertEqual(summary["sensor_w"], 100)
        self.assertIsNone(summary["cpu_package_w"])
        self.assertEqual(summary["estimate_breakdown"]["cpu_vrm_loss_w"], 0)
        self.assertEqual(summary["total_w"], 100)

    def test_two_point_calibration_solves_baseline_and_efficiency(self) -> None:
        # 构造一组自洽的数据：底噪 80 W，电源效率 0.88。
        baseline, efficiency = 80.0, 0.88
        idle_accounted, load_accounted = 220.0, 1100.0
        baseline_w, psu_efficiency = solve_calibration(
            idle_accounted_w=idle_accounted,
            idle_wall_w=(idle_accounted + baseline) / efficiency,
            load_accounted_w=load_accounted,
            load_wall_w=(load_accounted + baseline) / efficiency,
            default_psu_efficiency=0.90,
        )
        self.assertAlmostEqual(psu_efficiency, efficiency, places=3)
        self.assertAlmostEqual(baseline_w, baseline, places=1)

    def test_single_point_calibration_keeps_default_efficiency(self) -> None:
        baseline_w, psu_efficiency = solve_calibration(
            idle_accounted_w=220.0, idle_wall_w=340.0,
            load_accounted_w=None, load_wall_w=None,
            default_psu_efficiency=0.90,
        )
        self.assertEqual(psu_efficiency, 0.90)
        self.assertAlmostEqual(baseline_w, 340.0 * 0.90 - 220.0, places=2)

    def test_calibration_rejects_implausible_readings(self) -> None:
        with self.assertRaises(PowerModelError):
            solve_calibration(
                idle_accounted_w=220.0, idle_wall_w=200.0,
                load_accounted_w=None, load_wall_w=None,
                default_psu_efficiency=0.90,
            )
        with self.assertRaises(PowerModelError):
            # 满载墙插功率反而更低，无法解出有意义的效率。
            solve_calibration(
                idle_accounted_w=220.0, idle_wall_w=340.0,
                load_accounted_w=1100.0, load_wall_w=330.0,
                default_psu_efficiency=0.90,
            )

    def test_energy_counter_delta_replaces_instant_reading(self) -> None:
        collectors._energy_state.clear()
        collectors._energy_scale.clear()
        # 每瓦对应 2e8 个原始计数，计时器频率 1e7。
        scale, frequency = 2e8, 1e7
        energy, timestamp = 0.0, 0.0
        watts_over_time = [100.0, 100.0, 100.0, 100.0]
        methods: list[str] = []
        for watts in watts_over_time:
            energy += watts * scale * 3.0
            timestamp += 3.0 * frequency
            item = {
                "energy_raw": energy, "energy_timestamp": timestamp,
                "energy_frequency": frequency,
            }
            value, method = collectors._energy_derived_watts("RAPL_Package0_PKG", item, watts)
            methods.append(method)
        self.assertEqual(methods[0], "instant")
        self.assertEqual(methods[-1], "energy_delta")
        self.assertAlmostEqual(value, 100.0, places=1)

    def test_energy_counter_uses_sys100ns_timestamps(self) -> None:
        # 实测机器（Z690 / 13700K）只填 Timestamp_Sys100NS，PerfTime 恒为 0。
        collectors._energy_state.clear()
        collectors._energy_scale.clear()
        scale, frequency = 2.85e8, 1e7
        energy, timestamp = 393087662185833.0, 134342857924850295.0
        watts, method = 0.0, "instant"
        for _ in range(4):
            energy += 90.0 * scale * 5.0
            timestamp += 5.0 * frequency
            watts, method = collectors._energy_derived_watts(
                "RAPL_Package0_PKG",
                {
                    "energy_raw": energy, "energy_timestamp": timestamp,
                    "energy_frequency": frequency,
                },
                90.0,
            )
        self.assertEqual(method, "energy_delta")
        self.assertAlmostEqual(watts, 90.0, places=1)

    def test_energy_counter_falls_back_to_the_monotonic_clock(self) -> None:
        # 时间戳字段全为 0 时仍要能差分，只是改用本地单调时钟计时。
        collectors._energy_state.clear()
        collectors._energy_scale.clear()
        scale = 2.85e8
        clock = [1000.0]
        energy = [0.0]

        def sample() -> tuple[float, str]:
            clock[0] += 5.0
            energy[0] += 90.0 * scale * 5.0
            return collectors._energy_derived_watts(
                "RAPL_Package0_PKG",
                {"energy_raw": energy[0], "energy_timestamp": 0, "energy_frequency": 0},
                90.0,
            )

        with patch("workstation_manager.collectors.time.monotonic", side_effect=lambda: clock[0]):
            for _ in range(4):
                watts, method = sample()
        self.assertEqual(method, "energy_delta")
        self.assertAlmostEqual(watts, 90.0, places=1)

    def test_energy_clock_change_discards_the_previous_sample(self) -> None:
        collectors._energy_state.clear()
        collectors._energy_scale.clear()
        counter = {"energy_raw": 1e12, "energy_timestamp": 1e7, "energy_frequency": 1e7}
        collectors._energy_derived_watts("PKG", counter, 90.0)
        # 计数器时间戳突然消失，两次读数来自不同的时钟，不能相减。
        watts, method = collectors._energy_derived_watts(
            "PKG", {"energy_raw": 2e12, "energy_timestamp": 0, "energy_frequency": 0}, 90.0
        )
        self.assertEqual(method, "instant")
        self.assertEqual(watts, 90.0)

    def test_energy_counter_reset_falls_back_to_instant(self) -> None:
        collectors._energy_state.clear()
        collectors._energy_scale.clear()
        base = {"energy_timestamp": 1e7, "energy_frequency": 1e7}
        collectors._energy_derived_watts("PKG", {"energy_raw": 1e12, **base}, 90.0)
        value, method = collectors._energy_derived_watts(
            "PKG", {"energy_raw": 0.0, "energy_timestamp": 2e7, "energy_frequency": 1e7}, 90.0
        )
        self.assertEqual(method, "instant")
        self.assertEqual(value, 90.0)


class HistoryTests(unittest.TestCase):
    def test_history_is_bounded_and_filtered(self) -> None:
        store = HistoryStore(capacity=2)
        for second in range(3):
            store.append(
                {
                    "sampled_at": f"2099-01-01T00:00:0{second}+00:00",
                    "host": {"cpu": {"load_percent": second}, "memory": {"percent": second}},
                    "gpus": [],
                }
            )
        self.assertEqual(len(store.query(999999999)), 2)

    def test_window_validation(self) -> None:
        self.assertEqual(parse_window("15m"), 15)
        self.assertEqual(parse_window("1440m"), 1440)
        with self.assertRaisesRegex(ValueError, "分钟格式"):
            parse_window("1h")
        with self.assertRaisesRegex(ValueError, "不能超过"):
            parse_window("1441m")
        with self.assertRaisesRegex(ValueError, "不能超过"):
            parse_window("9" * 10000 + "m")

    def test_history_excludes_expired_samples(self) -> None:
        store = HistoryStore(capacity=3)
        now = datetime.now(timezone.utc)
        for sampled_at, load in ((now - timedelta(minutes=2), 1), (now, 2)):
            store.append(
                {
                    "sampled_at": sampled_at.isoformat(),
                    "host": {"cpu": {"load_percent": load}, "memory": {"percent": load}},
                    "gpus": [],
                }
            )
        samples = store.query(1)
        self.assertEqual([sample["cpu_load_percent"] for sample in samples], [2])

    def test_history_includes_sample_exactly_at_cutoff(self) -> None:
        store = HistoryStore(capacity=1)
        now = datetime.now(timezone.utc)
        store.append(
            {
                "sampled_at": (now - timedelta(minutes=15)).isoformat(),
                "host": {"cpu": {"load_percent": 7}, "memory": {"percent": 8}},
                "gpus": [],
            }
        )
        self.assertEqual(len(store.query(15, now=now)), 1)


class SamplerTests(unittest.IsolatedAsyncioTestCase):
    async def test_nvidia_failure_keeps_last_successful_gpu_until_recovery(self) -> None:
        snapshots = iter(
            [
                {
                    "sampled_at": "2026-09-05T02:14:38+00:00",
                    "host": {"cpu": {}, "memory": {}},
                    "gpus": [
                        {
                            "uuid": "GPU-a",
                            "index": 0,
                            "name": "RTX 4090",
                            "load_percent": 80,
                        }
                    ],
                    "collector_errors": [],
                },
                {
                    "sampled_at": "2026-09-05T02:14:44+00:00",
                    "host": {"cpu": {}, "memory": {}},
                    "gpus": [],
                    "collector_errors": [
                        {
                            "collector": "nvidia",
                            "error_type": "CommandError",
                            "message": "nvidia 采集失败",
                            "cause": "nvidia-smi 返回退出码 255",
                        }
                    ],
                },
                {
                    "sampled_at": "2026-09-05T02:14:50+00:00",
                    "host": {"cpu": {}, "memory": {}},
                    "gpus": [
                        {
                            "uuid": "GPU-a",
                            "index": 0,
                            "name": "RTX 4090",
                            "load_percent": 20,
                        }
                    ],
                    "collector_errors": [],
                },
            ]
        )
        sampler = Sampler(Settings(), collector=lambda _: next(snapshots))

        first = await sampler.sample_once()
        failed = await sampler.sample_once()
        recovered = await sampler.sample_once()

        self.assertEqual(failed["gpus"], first["gpus"])
        self.assertEqual(
            failed["stale_collectors"]["nvidia"]["last_success_at"],
            first["sampled_at"],
        )
        history = sampler.history.query(
            15, now=datetime.fromisoformat(recovered["sampled_at"])
        )
        self.assertEqual(history[1]["gpus"], [])
        self.assertEqual(recovered["gpus"][0]["load_percent"], 20)
        self.assertNotIn("stale_collectors", recovered)

    def test_collection_worker_restarts_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings(database_path=Path(temporary) / "manager.db")
            worker = ResourceCollectionWorker(settings, _hang_once_collection)
            self.addCleanup(worker.close)

            with self.assertRaises(CollectionTimeoutError):
                worker.collect(timeout=2)

            recovered = worker.collect(timeout=5)

        self.assertEqual(recovered["gpus"][0]["uuid"], "GPU-recovered")

    @unittest.skipUnless(os.name == "nt", "Windows Job Object 进程树回收测试")
    def test_collection_timeout_terminates_external_child_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings(database_path=Path(temporary) / "manager.db")
            worker = ResourceCollectionWorker(settings, _hang_with_child_process)
            self.addCleanup(worker.close)

            with self.assertRaises(CollectionTimeoutError):
                worker.collect(timeout=2)

            child_pid = int(
                settings.database_path.with_suffix(".child-pid").read_text(encoding="utf-8")
            )
            for _ in range(100):
                if not psutil.pid_exists(child_pid):
                    break
                time.sleep(0.02)

        self.assertFalse(psutil.pid_exists(child_pid))

    async def test_stop_closes_worker_after_inflight_collection_failure(self) -> None:
        class FakeWorker:
            closed = False

            def close(self) -> None:
                self.closed = True

        async def fail_collection() -> dict:
            raise RuntimeError("collector failed")

        sampler = Sampler(Settings())
        worker = FakeWorker()
        sampler._collection_worker = worker  # type: ignore[assignment]
        sampler._collection_task = asyncio.create_task(fail_collection())
        await asyncio.sleep(0)

        with self.assertRaises(SamplerStopError):
            await sampler.stop()

        self.assertTrue(worker.closed)

    async def test_collection_timeout_marks_current_snapshot_stale_until_recovery(self) -> None:
        snapshots = iter(
            [
                {
                    "sampled_at": "2026-09-05T02:14:38+00:00",
                    "host": {"cpu": {}, "memory": {}},
                    "gpus": [{"uuid": "GPU-a", "index": 0, "name": "RTX"}],
                    "collector_errors": [],
                },
                {
                    "sampled_at": "2026-09-05T02:15:00+00:00",
                    "host": {"cpu": {}, "memory": {}},
                    "gpus": [{"uuid": "GPU-a", "index": 0, "name": "RTX"}],
                    "collector_errors": [],
                },
            ]
        )
        sampler = Sampler(Settings(), collector=lambda _: next(snapshots))
        first = await sampler.sample_once()

        sampler.record_error(
            CollectionTimeoutError("timed out"),
            "后台采样失败，将在下个周期重试",
        )

        self.assertEqual(
            sampler.current["stale_collectors"]["snapshot"]["last_success_at"],
            first["sampled_at"],
        )
        self.assertEqual(
            sampler.current["stale_collectors"]["nvidia"]["last_success_at"],
            first["sampled_at"],
        )
        self.assertEqual(sampler.current["collector_errors"][-1]["collector"], "sampler")

        recovered = await sampler.sample_once()
        self.assertNotIn("stale_collectors", recovered)
        self.assertIsNone(sampler.last_error)

    async def test_stop_reports_collection_and_worker_close_failures_together(self) -> None:
        class FailingWorker:
            def close(self) -> None:
                raise RuntimeError("close failed")

        async def fail_collection() -> dict:
            raise RuntimeError("collector failed")

        sampler = Sampler(Settings())
        sampler._collection_worker = FailingWorker()  # type: ignore[assignment]
        sampler._collection_task = asyncio.create_task(fail_collection())
        await asyncio.sleep(0)

        with self.assertRaisesRegex(
            SamplerStopError, "collector failed.*close failed"
        ):
            await sampler.stop()

        self.assertIn("close failed", sampler.last_error["cause"])

    async def test_history_sink_failure_degrades_and_recovers_without_losing_snapshot(self) -> None:
        calls = 0

        def sink(_: dict) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("disk full")

        snapshot = {
            "sampled_at": datetime.now(timezone.utc).isoformat(),
            "host": {"cpu": {}, "memory": {}},
            "gpus": [],
        }
        sampler = Sampler(Settings(), collector=lambda _: snapshot, sample_sink=sink)

        self.assertIs(await sampler.sample_once(), snapshot)
        self.assertEqual(sampler.current, snapshot)
        self.assertEqual(len(sampler.history.query(15)), 1)
        self.assertEqual(sampler.history_persistence_error["error_type"], "RuntimeError")
        self.assertEqual(sampler.history_persistence_error["cause"], "disk full")

        await sampler.sample_once()
        self.assertIsNone(sampler.history_persistence_error)

    async def test_sample_sink_receives_reduced_history_record(self) -> None:
        records = []
        settings = Settings(sample_interval_seconds=5)
        snapshot = {
            "sampled_at": datetime.now(timezone.utc).isoformat(),
            "host": {"cpu": {"load_percent": 12}, "memory": {"percent": 34}, "disks": [1],
                     "power": {"total_w": 550}},
            "gpus": [{"uuid": "GPU-a", "index": 0, "name": "RTX", "load_percent": 56,
                      "graphics_clock_mhz": 2715, "power_w": 430, "temperature_c": 70}],
            "docker": {"containers": [1]},
        }
        sampler = Sampler(settings, collector=lambda _: snapshot, sample_sink=records.append)

        await sampler.sample_once()

        self.assertEqual(records[0]["cpu_load_percent"], 12)
        self.assertEqual(records[0]["total_power_w"], 550)
        self.assertEqual(records[0]["gpus"][0]["uuid"], "GPU-a")
        self.assertEqual(records[0]["gpus"][0]["graphics_clock_mhz"], 2715)
        self.assertEqual(records[0]["gpus"][0]["power_w"], 430)
        self.assertNotIn("docker", records[0])
        self.assertEqual(records[0]["disks"], [])

    async def test_stop_does_not_overwrite_scheduler_started_during_cleanup(self) -> None:
        settings = Settings(sample_interval_seconds=60)

        def fake_collector(_: Settings) -> dict:
            return {
                "sampled_at": datetime.now(timezone.utc).isoformat(),
                "host": {"cpu": {}, "memory": {}},
                "gpus": [],
            }

        sampler = Sampler(settings, collector=fake_collector)

        class CompletedTask:
            def cancel(self) -> None:
                return None

            def done(self) -> bool:
                return True

            def __await__(self):
                async def start_replacement() -> None:
                    sampler.start()

                return start_replacement().__await__()

        old_task = CompletedTask()
        sampler._task = old_task  # type: ignore[assignment]
        await sampler.stop()
        self.assertIsNotNone(sampler._task)
        self.assertIsNot(sampler._task, old_task)
        await sampler.stop()

    async def test_start_does_not_duplicate_task(self) -> None:
        settings = Settings(sample_interval_seconds=60)

        def fake_collector(_: Settings) -> dict:
            return {
                "sampled_at": "2099-01-01T00:00:00+00:00",
                "host": {"cpu": {}, "memory": {}},
                "gpus": [],
            }

        sampler = Sampler(settings, collector=fake_collector)
        await sampler.sample_once()
        sampler.start()
        first_task = sampler._task
        sampler.start()
        self.assertIs(sampler._task, first_task)
        await sampler.stop()

    async def test_background_error_does_not_end_sampler(self) -> None:
        settings = Settings(sample_interval_seconds=0.01)
        calls = 0

        def sometimes_fails(_: Settings) -> dict:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("bad sample")
            return {
                "sampled_at": "2099-01-01T00:00:00+00:00",
                "host": {"cpu": {}, "memory": {}},
                "gpus": [],
            }

        sampler = Sampler(settings, collector=sometimes_fails)
        await sampler.sample_once()
        sampler.start()
        for _ in range(100):
            if calls >= 3:
                break
            await asyncio.sleep(0.005)
        self.assertIsNotNone(sampler._task)
        self.assertFalse(sampler._task.done())
        self.assertGreaterEqual(calls, 3)
        await sampler.stop()

    async def test_stop_waits_for_inflight_thread_collection(self) -> None:
        settings = Settings(sample_interval_seconds=0.01, command_timeout_seconds=0.1)
        started = threading.Event()
        release = threading.Event()

        def blocking_collector(_: Settings) -> dict:
            started.set()
            release.wait(timeout=1)
            return {
                "sampled_at": datetime.now(timezone.utc).isoformat(),
                "host": {"cpu": {}, "memory": {}},
                "gpus": [],
            }

        sampler = Sampler(settings, collector=blocking_collector)
        sampler.start()
        self.assertTrue(await asyncio.to_thread(started.wait, 0.5))
        stop_task = asyncio.create_task(sampler.stop())
        await asyncio.sleep(0.02)
        self.assertFalse(stop_task.done())
        release.set()
        await asyncio.wait_for(stop_task, timeout=0.5)
        self.assertIsNone(sampler._task)
        self.assertIsNone(sampler._collection_task)

    async def test_restart_during_stop_does_not_overlap_collection(self) -> None:
        settings = Settings(sample_interval_seconds=0.01, command_timeout_seconds=0.01)
        started = threading.Event()
        release = threading.Event()
        state_lock = threading.Lock()
        active = 0
        maximum_active = 0
        calls = 0

        def blocking_collector(_: Settings) -> dict:
            nonlocal active, maximum_active, calls
            with state_lock:
                active += 1
                calls += 1
                maximum_active = max(maximum_active, active)
            started.set()
            try:
                release.wait(timeout=1)
                return {
                    "sampled_at": datetime.now(timezone.utc).isoformat(),
                    "host": {"cpu": {}, "memory": {}},
                    "gpus": [],
                }
            finally:
                with state_lock:
                    active -= 1

        sampler = Sampler(settings, collector=blocking_collector)
        sampler.start()
        self.assertTrue(await asyncio.to_thread(started.wait, 0.5))
        stop_task = asyncio.create_task(sampler.stop())
        await asyncio.sleep(0.02)
        sampler.start()
        await asyncio.sleep(0.03)
        self.assertFalse(stop_task.done())
        self.assertEqual(calls, 1)
        self.assertEqual(maximum_active, 1)
        release.set()
        await asyncio.wait_for(stop_task, timeout=0.5)
        for _ in range(100):
            if calls >= 2:
                break
            await asyncio.sleep(0.005)
        self.assertGreaterEqual(calls, 2)
        self.assertEqual(maximum_active, 1)
        await sampler.stop()


class PowerModelApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        temporary_root = Path(self.temp.name)
        self.settings = Settings(
            sample_interval_seconds=60,
            database_path=temporary_root / "manager.db",
            manager_log_path=temporary_root / "manager.log",
            file_service_root=temporary_root / "共享",
            power_baseline_w=75.0,
            power_psu_efficiency=0.90,
            power_cpu_vrm_efficiency=0.89,
        )
        self.settings.file_service_root.mkdir()
        self.gpu_power = 400.0
        self.cpu_power = 100.0

        def fake_collector(_: Settings) -> dict:
            sensors = [{
                "name": "RAPL_Package0_PKG", "hardware_type": "CpuPackage",
                "role": "cpu_package", "value_w": self.cpu_power, "method": "energy_delta",
            }]
            gpus = [{"uuid": "GPU-a", "index": 0, "name": "RTX", "power_w": self.gpu_power}]
            return {
                "sampled_at": "2099-01-01T00:00:00+00:00",
                "host": {
                    "cpu": {"load_percent": 12.5},
                    "memory": {"percent": 50.0},
                    "power": summarize_power(gpus, sensors, PowerModel(enabled=False)),
                },
                "gpus": gpus,
                "docker": {"containers": []},
                "ports": [],
                "collector_errors": [],
            }

        sampler = Sampler(self.settings, collector=fake_collector)
        self.client_context = TestClient(
            create_app(self.settings, sampler), client=("127.0.0.1", 50000)
        )
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def _login(self) -> dict[str, str]:
        setup = self.client.post(
            "/api/v1/auth/setup", json={"username": "admin", "password": "1234"}
        )
        return {"X-CSRF-Token": setup.json()["csrf_token"]}

    def test_snapshot_reports_measured_and_estimated_power(self) -> None:
        self._login()
        power = self.client.get("/api/v1/snapshot").json()["host"]["power"]
        self.assertEqual(power["measured_w"], 500)
        self.assertEqual(power["gpu_w"], 400)
        self.assertEqual(power["cpu_package_w"], 100)
        # 估算必须把合计推高，并且和实测值分开列出。
        self.assertGreater(power["total_w"], power["measured_w"])
        self.assertAlmostEqual(
            power["measured_w"] + power["estimated_w"], power["total_w"], places=1
        )
        self.assertEqual(power["model"]["source"], "default")
        self.assertGreater(power["estimate_breakdown"]["psu_loss_w"], 0)

    def test_history_stores_measured_and_estimated_columns(self) -> None:
        self._login()
        history = self.client.get("/api/v1/history?window=15m").json()
        sample = history["samples"][0]
        self.assertEqual(sample["measured_power_w"], 500)
        self.assertIsNotNone(sample["estimated_power_w"])
        self.assertAlmostEqual(
            sample["measured_power_w"] + sample["estimated_power_w"],
            sample["total_power_w"], places=1,
        )

    def test_calibration_round_trip_changes_the_model(self) -> None:
        headers = self._login()
        before = self.client.get("/api/v1/power-model").json()
        self.assertFalse(before["model"]["calibrated"])
        accounted = before["current"]["accounted_dc_w"]
        self.assertIsNotNone(accounted)

        idle = self.client.post(
            "/api/v1/power-model/calibration",
            json={"point": "idle", "wall_w": 700.0}, headers=headers,
        )
        self.assertEqual(idle.status_code, 200)
        calibrated = idle.json()
        self.assertTrue(calibrated["model"]["calibrated"])
        self.assertEqual(calibrated["model"]["source"], "calibrated")
        # 单点校准保留默认电源效率，只把底噪解出来。
        self.assertEqual(calibrated["model"]["psu_efficiency"], 0.90)
        self.assertAlmostEqual(
            calibrated["model"]["baseline_w"], 700.0 * 0.90 - accounted, places=1
        )
        self.assertEqual(calibrated["calibration"]["updated_by"], "admin")

        cleared = self.client.delete(
            "/api/v1/power-model/calibration", headers=headers
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertFalse(cleared.json()["model"]["calibrated"])
        self.assertIsNone(cleared.json()["calibration"])

    def test_calibration_rejects_a_wall_reading_below_measured_power(self) -> None:
        headers = self._login()
        response = self.client.post(
            "/api/v1/power-model/calibration",
            json={"point": "idle", "wall_w": 100.0}, headers=headers,
        )
        self.assertEqual(response.status_code, 422)

    def test_calibration_requires_csrf(self) -> None:
        self._login()
        response = self.client.post(
            "/api/v1/power-model/calibration", json={"point": "idle", "wall_w": 700.0}
        )
        self.assertEqual(response.status_code, 403)


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        temporary_root = Path(self.temp.name)
        self.settings = Settings(
            sample_interval_seconds=60,
            database_path=temporary_root / "manager.db",
            manager_log_path=temporary_root / "manager.log",
            file_service_root=temporary_root / "共享",
        )
        self.settings.file_service_root.mkdir()
        (self.settings.file_service_root / "测试.txt").write_text("hello", encoding="utf-8")

        def fake_collector(_: Settings) -> dict:
            return {
                "sampled_at": "2099-01-01T00:00:00+00:00",
                "host": {
                    "cpu": {"load_percent": 12.5, "temperature_c": None},
                    "memory": {"percent": 50.0},
                },
                "gpus": [
                    {
                        "uuid": "GPU-a",
                        "index": 0,
                        "name": "RTX",
                        "load_percent": 25.0,
                        "memory_used_mib": 100,
                        "memory_total_mib": 1000,
                        "memory_percent": 10.0,
                        "temperature_c": 40.0,
                    }
                ],
                "docker": {"containers": [{"name": "example", "state": "running"}]},
                "ports": [{"port": 8080, "listening": True, "listeners": []}],
                "collector_errors": [],
            }

        sampler = Sampler(self.settings, collector=fake_collector)
        self.client_context = TestClient(
            create_app(self.settings, sampler), client=("127.0.0.1", 50000)
        )
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def test_health_snapshot_history_and_services(self) -> None:
        health = self.client.get("/api/v1/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["version"], __version__)
        self.assertEqual(self.client.get("/api/v1/snapshot").json()["gpus"][0]["uuid"], "GPU-a")
        history = self.client.get("/api/v1/history?window=15m").json()
        self.assertEqual(len(history["samples"]), 1)
        self.assertEqual(history["bucket_seconds"], 0)
        self.assertEqual(history["retention_minutes"], 1440)
        self.assertEqual(history["stored_sample_count"], 1)
        services = self.client.get("/api/v1/host-services").json()
        self.assertEqual(services["containers"][0]["name"], "example")
        self.assertEqual(services["listening_ports"][0]["port"], 8080)

    def test_setup_accepts_four_character_password(self) -> None:
        response = self.client.post(
            "/api/v1/auth/setup", json={"username": "admin", "password": "1234"}
        )
        self.assertEqual(response.status_code, 201, response.text)

    def test_authenticated_file_service_browses_downloads_and_streams_ranges(self) -> None:
        unauthenticated = self.client.get("/api/v1/file-service/files")
        self.assertEqual(unauthenticated.status_code, 401)
        setup = self.client.post(
            "/api/v1/auth/setup", json={"username": "admin", "password": "1234"}
        )
        self.assertEqual(setup.status_code, 201, setup.text)

        info = self.client.get("/api/v1/file-service")
        self.assertEqual(info.status_code, 200)
        self.assertEqual(info.json()["port"], 18765)
        self.assertTrue(info.json()["root_available"])

        listing = self.client.get("/api/v1/file-service/files")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["entries"][0]["name"], "测试.txt")
        partial = self.client.get(
            "/api/v1/file-service/content",
            params={"path": "测试.txt"},
            headers={"Range": "bytes=1-3"},
        )
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, b"ell")

    def test_authenticated_file_service_streams_upload_without_overwriting(self) -> None:
        payload = b"x" * (2 * 1024 * 1024 + 37)
        upload_directory = self.settings.file_service_root / "输入"
        upload_directory.mkdir()
        unauthenticated = self.client.post(
            "/api/v1/file-service/upload",
            params={"path": "输入", "name": "未登录.bin"},
            content=payload,
        )
        self.assertEqual(unauthenticated.status_code, 401)
        setup = self.client.post(
            "/api/v1/auth/setup", json={"username": "admin", "password": "1234"}
        )
        csrf = setup.json()["csrf_token"]
        missing_csrf = self.client.post(
            "/api/v1/file-service/upload",
            params={"path": "输入", "name": "无令牌.bin"},
            content=payload,
        )
        self.assertEqual(missing_csrf.status_code, 403)

        uploaded = self.client.post(
            "/api/v1/file-service/upload",
            params={"path": "输入", "name": "中文上传.bin"},
            content=payload,
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        self.assertEqual(uploaded.json()["file"]["path"], "输入/中文上传.bin")
        self.assertEqual(uploaded.json()["file"]["size"], len(payload))
        self.assertEqual(
            (upload_directory / "中文上传.bin").read_bytes(), payload,
        )
        duplicate = self.client.post(
            "/api/v1/file-service/upload",
            params={"path": "输入", "name": "中文上传.bin"},
            content=b"changed",
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["error"]["code"], "upload_file_exists")
        self.assertEqual(
            (upload_directory / "中文上传.bin").read_bytes(), payload,
        )
        traversal = self.client.post(
            "/api/v1/file-service/upload",
            params={"path": "输入", "name": "../outside.bin"},
            content=b"blocked",
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(traversal.status_code, 400)
        self.assertFalse((self.settings.file_service_root.parent / "outside.bin").exists())

    def test_authenticated_file_service_renames_files_and_directories(self) -> None:
        directory = self.settings.file_service_root / "旧目录"
        directory.mkdir()
        (directory / "内容.txt").write_text("内容", encoding="utf-8")
        (self.settings.file_service_root / "已存在.txt").write_text("保留", encoding="utf-8")
        self.assertEqual(
            self.client.post(
                "/api/v1/file-service/rename",
                json={"path": "旧目录", "new_name": "新目录"},
            ).status_code,
            401,
        )
        setup = self.client.post(
            "/api/v1/auth/setup", json={"username": "admin", "password": "1234"}
        )
        csrf = setup.json()["csrf_token"]
        self.assertEqual(
            self.client.post(
                "/api/v1/file-service/rename",
                json={"path": "旧目录", "new_name": "新目录"},
            ).status_code,
            403,
        )
        renamed = self.client.post(
            "/api/v1/file-service/rename",
            json={"path": "旧目录", "new_name": "新目录"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["entry"]["path"], "新目录")
        self.assertEqual(
            (self.settings.file_service_root / "新目录" / "内容.txt").read_text(encoding="utf-8"),
            "内容",
        )
        conflict = self.client.post(
            "/api/v1/file-service/rename",
            json={"path": "测试.txt", "new_name": "已存在.txt"},
            headers={"X-CSRF-Token": csrf, "Accept-Language": "en"},
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["error"]["code"], "rename_target_exists")
        self.assertEqual(
            conflict.json()["error"]["message"],
            "A file or folder with that name already exists and was not overwritten.",
        )
        traversal = self.client.post(
            "/api/v1/file-service/rename",
            json={"path": "测试.txt", "new_name": "../越界.txt"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(traversal.status_code, 400, traversal.text)
        audit = self.client.get("/api/v1/audit?limit=10").json()["events"]
        self.assertIn("management.file.rename", [event["event"] for event in audit])

        audit_source = self.settings.file_service_root / "审计前.txt"
        audit_source.write_text("审计", encoding="utf-8")
        original_append_audit = Database.append_audit

        def fail_final_audit(
            database: Database, source_ip: str, event: str, result: str, summary: dict,
        ) -> None:
            if event == "management.file.rename" and result == "success":
                raise DatabaseError("模拟最终审计失败")
            original_append_audit(database, source_ip, event, result, summary)

        with patch.object(Database, "append_audit", new=fail_final_audit):
            audit_failure = self.client.post(
                "/api/v1/file-service/rename",
                json={"path": "审计前.txt", "new_name": "审计后.txt"},
                headers={"X-CSRF-Token": csrf},
            )
        self.assertEqual(audit_failure.status_code, 500, audit_failure.text)
        self.assertTrue((self.settings.file_service_root / "审计前.txt").is_file())
        self.assertFalse((self.settings.file_service_root / "审计后.txt").exists())
        audit = self.client.get("/api/v1/audit?limit=20").json()["events"]
        requested = [
            event for event in audit
            if event["event"] == "management.file.rename.requested"
            and event["summary"].get("source_path") == "审计前.txt"
        ]
        self.assertEqual(len(requested), 1)
        self.assertTrue(any(
            event["event"] == "management.file.rename"
            and event["result"] == "failure"
            and event["summary"].get("reason") == "final_audit_failed_rolled_back"
            for event in audit
        ))

    def test_authenticated_file_service_moves_items_to_recycle_bin(self) -> None:
        directory = self.settings.file_service_root / "待删除目录"
        directory.mkdir()
        (directory / "内容.txt").write_text("内容", encoding="utf-8")
        recycled: list[str] = []
        recycle_store = self.settings.file_service_root.parent / "模拟回收站"
        recycle_store.mkdir()

        def fake_recycle(value: str) -> None:
            path = Path(value)
            recycled.append(path.name)
            path.rename(recycle_store / path.name)

        self.client.app.state.file_catalog._recycle_entry = fake_recycle
        self.assertEqual(
            self.client.delete(
                "/api/v1/file-service/entry", params={"path": "待删除目录"},
            ).status_code,
            401,
        )
        setup = self.client.post(
            "/api/v1/auth/setup", json={"username": "admin", "password": "1234"}
        )
        csrf = setup.json()["csrf_token"]
        self.assertEqual(
            self.client.delete(
                "/api/v1/file-service/entry", params={"path": "待删除目录"},
            ).status_code,
            403,
        )
        deleted = self.client.delete(
            "/api/v1/file-service/entry",
            params={"path": "待删除目录"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertTrue(deleted.json()["recycled"])
        self.assertEqual(recycled, ["待删除目录"])
        self.assertFalse(directory.exists())
        self.assertEqual(
            (recycle_store / "待删除目录" / "内容.txt").read_text(encoding="utf-8"),
            "内容",
        )
        root_delete = self.client.delete(
            "/api/v1/file-service/entry",
            params={"path": "/"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(root_delete.status_code, 400, root_delete.text)
        (self.settings.file_service_root / "点段目录").mkdir()
        encoded_root_delete = self.client.delete(
            "/api/v1/file-service/entry?path=%E7%82%B9%E6%AE%B5%E7%9B%AE%E5%BD%95%2F..",
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(encoded_root_delete.status_code, 400, encoded_root_delete.text)
        self.assertTrue(self.settings.file_service_root.exists())
        audit = self.client.get("/api/v1/audit?limit=10").json()["events"]
        self.assertTrue(any(
            event["event"] == "management.file.recycle"
            and event["result"] == "success"
            and event["summary"].get("path") == "待删除目录"
            for event in audit
        ))

    def test_automatic_tasks_crud_and_opencode_serial_execution(self) -> None:
        self.assertEqual(self.client.get("/api/v1/automatic-tasks").status_code, 401)
        setup = self.client.post(
            "/api/v1/auth/setup", json={"username": "admin", "password": "1234"}
        )
        csrf = setup.json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}

        first = self.client.post(
            "/api/v1/automatic-tasks",
            json={"content": "# 检查工作站健康状态。\n输出异常服务。"},
            headers=headers,
        )
        second = self.client.post(
            "/api/v1/automatic-tasks",
            json={"content": "整理共享目录中的视频文件并报告结果"},
            headers=headers,
        )
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["task"]["title"], "检查工作站健康状态。")
        self.assertEqual(second.status_code, 201, second.text)
        first_id = first.json()["task"]["id"]
        second_id = second.json()["task"]["id"]

        listing = self.client.get("/api/v1/automatic-tasks").json()
        self.assertEqual(listing["summary"], {"pending": 2, "running": 0, "total": 2})
        self.assertNotIn("execution_session_id", listing["tasks"][0])

        claimed = self.client.post(
            "/api/v1/automatic-tasks/claim", json={"session_id": "session-a"}
        )
        self.assertEqual(claimed.status_code, 200, claimed.text)
        self.assertEqual(claimed.json()["task"]["id"], first_id)
        self.assertEqual(claimed.json()["task"]["status"], "running")
        first_token = claimed.json()["task"]["execution_token"]
        self.assertNotIn("execution_token", listing["tasks"][0])
        busy = self.client.post(
            "/api/v1/automatic-tasks/claim", json={"session_id": "session-b"},
            headers={"Accept-Language": "en"},
        )
        self.assertEqual(busy.status_code, 409)
        self.assertEqual(busy.json()["error"]["code"], "automatic_tasks_busy")
        self.assertEqual(
            busy.json()["error"]["message"],
            "Another OpenCode session is running the automatic-task queue.",
        )
        self.assertEqual(
            self.client.put(
                f"/api/v1/automatic-tasks/{first_id}",
                json={"content": "不能覆盖运行任务"}, headers=headers,
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.delete(
                f"/api/v1/automatic-tasks/{first_id}", headers=headers,
            ).status_code,
            409,
        )
        mismatch = self.client.post(
            f"/api/v1/automatic-tasks/{first_id}/finish",
            json={"session_id": "session-b", "execution_token": first_token,
                  "status": "succeeded", "summary": "错误会话"},
        )
        self.assertEqual(mismatch.status_code, 409)
        finished = self.client.post(
            f"/api/v1/automatic-tasks/{first_id}/finish",
            json={"session_id": "session-a", "execution_token": first_token,
                  "status": "succeeded", "summary": "检查完成"},
        )
        self.assertEqual(finished.status_code, 200, finished.text)
        self.assertEqual(finished.json()["task"]["result_summary"], "检查完成")
        repeated = self.client.post(
            f"/api/v1/automatic-tasks/{first_id}/finish",
            json={"session_id": "session-a", "execution_token": first_token,
                  "status": "succeeded", "summary": "响应丢失后的重试"},
        )
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["task"]["result_summary"], "检查完成")

        next_task = self.client.post(
            "/api/v1/automatic-tasks/claim", json={"session_id": "session-a"}
        ).json()["task"]
        self.assertEqual(next_task["id"], second_id)
        second_token = next_task["execution_token"]
        heartbeat = self.client.post(
            f"/api/v1/automatic-tasks/{second_id}/heartbeat",
            json={"session_id": "session-a", "execution_token": second_token},
        )
        self.assertEqual(heartbeat.status_code, 200, heartbeat.text)
        self.assertEqual(
            self.client.post(
                f"/api/v1/automatic-tasks/{second_id}/finish",
                json={"session_id": "session-a", "execution_token": second_token,
                      "status": "failed", "summary": "   "},
            ).status_code,
            422,
        )
        failed = self.client.post(
            f"/api/v1/automatic-tasks/{second_id}/finish",
            json={"session_id": "session-a", "execution_token": second_token,
                  "status": "failed", "summary": "输入文件不存在"},
        )
        self.assertEqual(failed.status_code, 200, failed.text)
        self.assertEqual(failed.json()["task"]["error_summary"], "输入文件不存在")
        self.assertIsNone(
            self.client.post(
                "/api/v1/automatic-tasks/claim", json={"session_id": "session-a"}
            ).json()["task"]
        )

        rerun = self.client.post(
            f"/api/v1/automatic-tasks/{first_id}/reset", headers=headers,
        )
        self.assertEqual(rerun.status_code, 200, rerun.text)
        self.assertEqual(rerun.json()["task"]["status"], "pending")
        self.assertEqual(rerun.json()["task"]["attempts"], 1)
        self.assertIsNone(rerun.json()["task"]["result_summary"])
        rejected_old_result = self.client.post(
            f"/api/v1/automatic-tasks/{first_id}/finish",
            json={"session_id": "session-a", "execution_token": first_token,
                  "status": "succeeded", "summary": "旧执行结果"},
        )
        self.assertEqual(rejected_old_result.status_code, 409, rejected_old_result.text)
        rerun_claim = self.client.post(
            "/api/v1/automatic-tasks/claim", json={"session_id": "session-a"}
        )
        self.assertEqual(rerun_claim.status_code, 200, rerun_claim.text)
        self.assertEqual(rerun_claim.json()["task"]["id"], first_id)
        self.assertEqual(rerun_claim.json()["task"]["attempts"], 2)
        rerun_token = rerun_claim.json()["task"]["execution_token"]
        rerun_finished = self.client.post(
            f"/api/v1/automatic-tasks/{first_id}/finish",
            json={"session_id": "session-a", "execution_token": rerun_token,
                  "status": "succeeded", "summary": "再次检查完成"},
        )
        self.assertEqual(rerun_finished.status_code, 200, rerun_finished.text)
        self.assertEqual(rerun_finished.json()["task"]["attempts"], 2)
        failed_rerun = self.client.post(
            f"/api/v1/automatic-tasks/{second_id}/reset", headers=headers,
        )
        self.assertEqual(failed_rerun.status_code, 200, failed_rerun.text)
        self.assertEqual(failed_rerun.json()["task"]["status"], "pending")
        self.assertEqual(failed_rerun.json()["task"]["attempts"], 1)
        self.assertIsNone(failed_rerun.json()["task"]["error_summary"])

        updated = self.client.put(
            f"/api/v1/automatic-tasks/{second_id}",
            json={"content": "重新整理共享目录"}, headers=headers,
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["task"]["status"], "pending")
        self.assertIsNone(updated.json()["task"]["error_summary"])
        deleted = self.client.delete(
            f"/api/v1/automatic-tasks/{first_id}", headers=headers,
        )
        self.assertEqual(deleted.status_code, 204, deleted.text)
        self.assertEqual(
            self.client.delete(
                f"/api/v1/automatic-tasks/{second_id}", headers=headers,
            ).status_code,
            204,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/automatic-tasks", json={"content": "   "}, headers=headers,
            ).status_code,
            422,
        )

        expiring = self.client.post(
            "/api/v1/automatic-tasks", json={"content": "验证租约恢复"}, headers=headers,
        ).json()["task"]
        old_claim = self.client.post(
            "/api/v1/automatic-tasks/claim", json={"session_id": "session-old"},
        ).json()["task"]
        old_token = old_claim["execution_token"]
        with closing(sqlite3.connect(self.settings.database_path)) as connection:
            connection.execute(
                "UPDATE automatic_tasks SET lease_expires_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", expiring["id"]),
            )
            connection.commit()
        reclaimed = self.client.post(
            "/api/v1/automatic-tasks/claim", json={"session_id": "session-old"},
        )
        self.assertEqual(reclaimed.status_code, 200, reclaimed.text)
        self.assertNotEqual(reclaimed.json()["task"]["execution_token"], old_token)
        self.assertEqual(reclaimed.json()["task"]["attempts"], 2)
        self.assertEqual(
            self.client.post(
                "/api/v1/automatic-tasks/claim", json={"session_id": "session-new"},
            ).status_code,
            409,
        )
        late = self.client.post(
            f"/api/v1/automatic-tasks/{expiring['id']}/finish",
            json={"session_id": "session-old", "execution_token": old_token,
                  "status": "succeeded", "summary": "迟到结果"},
        )
        self.assertEqual(late.status_code, 409, late.text)
        reset = self.client.post(
            f"/api/v1/automatic-tasks/{expiring['id']}/reset", headers=headers,
        )
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(reset.json()["task"]["status"], "pending")

        with closing(sqlite3.connect(self.settings.database_path)) as connection:
            now = datetime.now(timezone.utc).isoformat()
            connection.executemany(
                """INSERT INTO automatic_tasks(id,title,content,status,created_at,updated_at)
                   VALUES (?,?,?,'pending',?,?)""",
                [(f"{index:032x}", f"分页 {index}", "分页任务", now, now)
                 for index in range(1, 502)],
            )
            connection.commit()
        page_one = self.client.get("/api/v1/automatic-tasks?limit=500&offset=0").json()
        page_two = self.client.get("/api/v1/automatic-tasks?limit=500&offset=500").json()
        self.assertTrue(page_one["has_more"])
        self.assertEqual(len(page_one["tasks"]), 500)
        self.assertGreaterEqual(len(page_two["tasks"]), 1)

    def test_automatic_tasks_reorder_changes_serial_claim_order(self) -> None:
        setup = self.client.post(
            "/api/v1/auth/setup", json={"username": "admin", "password": "1234"}
        )
        csrf = setup.json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}
        task_ids = [
            self.client.post(
                "/api/v1/automatic-tasks",
                json={"content": f"顺序任务 {index}"},
                headers=headers,
            ).json()["task"]["id"]
            for index in range(1, 4)
        ]
        reordered_ids = [task_ids[2], task_ids[0], task_ids[1]]

        self.assertEqual(
            self.client.post(
                "/api/v1/automatic-tasks/reorder",
                json={"previous_task_ids": task_ids, "task_ids": reordered_ids},
            ).status_code,
            403,
        )
        invalid = self.client.post(
            "/api/v1/automatic-tasks/reorder",
            json={"previous_task_ids": task_ids, "task_ids": reordered_ids[:-1]},
            headers=headers,
        )
        self.assertEqual(invalid.status_code, 422, invalid.text)
        reordered = self.client.post(
            "/api/v1/automatic-tasks/reorder",
            json={"previous_task_ids": task_ids, "task_ids": reordered_ids},
            headers=headers,
        )
        self.assertEqual(reordered.status_code, 200, reordered.text)
        self.assertEqual(reordered.json()["task_ids"], reordered_ids)
        stale = self.client.post(
            "/api/v1/automatic-tasks/reorder",
            json={"previous_task_ids": task_ids, "task_ids": [task_ids[1], task_ids[2], task_ids[0]]},
            headers=headers,
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        current_ids = reordered_ids
        added = self.client.post(
            "/api/v1/automatic-tasks",
            json={"content": "并发新增的任务"},
            headers=headers,
        ).json()["task"]["id"]
        changed_set = self.client.post(
            "/api/v1/automatic-tasks/reorder",
            json={"previous_task_ids": current_ids, "task_ids": current_ids},
            headers=headers,
        )
        self.assertEqual(changed_set.status_code, 409, changed_set.text)
        self.assertEqual(
            self.client.delete(
                f"/api/v1/automatic-tasks/{added}", headers=headers,
            ).status_code,
            204,
        )
        claimed = self.client.post(
            "/api/v1/automatic-tasks/claim", json={"session_id": "session-order"}
        )
        self.assertEqual(claimed.status_code, 200, claimed.text)
        self.assertEqual(claimed.json()["task"]["id"], reordered_ids[0])
        reset = self.client.post(
            f"/api/v1/automatic-tasks/{reordered_ids[0]}/reset", headers=headers,
        )
        self.assertEqual(reset.status_code, 200, reset.text)
        claimed_after_reset = self.client.post(
            "/api/v1/automatic-tasks/claim", json={"session_id": "session-order"}
        )
        self.assertEqual(claimed_after_reset.status_code, 200, claimed_after_reset.text)
        self.assertEqual(claimed_after_reset.json()["task"]["id"], reordered_ids[1])

    def test_remember_login_extends_server_session_and_cookie_to_thirty_days(self) -> None:
        setup = self.client.post(
            "/api/v1/auth/setup", json={"username": "admin", "password": "1234"}
        )
        self.assertEqual(setup.status_code, 201, setup.text)
        self.client.cookies.clear()

        regular = self.client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "1234"}
        )
        self.assertEqual(regular.status_code, 200, regular.text)
        self.assertIn(f"Max-Age={self.settings.session_ttl_seconds}", regular.headers["set-cookie"])
        regular_expiry = datetime.fromisoformat(regular.json()["expires_at"])
        self.client.cookies.clear()

        remembered = self.client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "1234", "remember": True},
        )
        self.assertEqual(remembered.status_code, 200, remembered.text)
        self.assertIn(
            f"Max-Age={REMEMBER_SESSION_TTL_SECONDS}", remembered.headers["set-cookie"]
        )
        remembered_expiry = datetime.fromisoformat(remembered.json()["expires_at"])
        self.assertGreater(
            remembered_expiry - regular_expiry,
            timedelta(days=29),
        )
        logout = self.client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": remembered.json()["csrf_token"]},
        )
        self.assertEqual(logout.status_code, 200, logout.text)
        self.assertIn("Max-Age=0", logout.headers["set-cookie"])
        self.assertFalse(self.client.get("/api/v1/auth/status").json()["authenticated"])

    def test_health_is_degraded_when_snapshot_has_collector_errors(self) -> None:
        self.client.app.state.sampler.current["collector_errors"] = [
            {
                "collector": "docker",
                "error_type": "RuntimeError",
                "message": "docker 采集失败",
                "cause": "daemon unavailable",
            }
        ]
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "degraded")
        self.assertEqual(response.json()["collector_errors"][0]["collector"], "docker")

    def test_health_reports_sampler_failure_without_exposing_cause(self) -> None:
        self.client.app.state.sampler.last_error = {
            "collector": "sampler",
            "error_type": "CollectionTimeoutError",
            "message": "后台采样超时，将在下个周期重试",
            "cause": "secret process detail",
        }

        body = self.client.get("/api/v1/health").json()

        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["readiness"]["sampler"], "degraded")
        self.assertEqual(body["sampler_error"]["error_type"], "CollectionTimeoutError")
        self.assertNotIn("cause", body["sampler_error"])

    def test_health_is_degraded_when_resource_history_persistence_fails(self) -> None:
        self.client.app.state.sampler.history_persistence_error = {
            "error_type": "DatabaseError",
            "message": "资源历史写入失败",
            "cause": "disk full at secret path",
        }

        body = self.client.get("/api/v1/health").json()

        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["readiness"]["resource_history"], "degraded")
        self.assertEqual(body["history_persistence_error"]["error_type"], "DatabaseError")
        self.assertNotIn("cause", body["history_persistence_error"])

    def test_invalid_history_window_returns_structured_error(self) -> None:
        response = self.client.get("/api/v1/history?window=1h")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "ValueError")

    def test_oversized_history_window_returns_structured_error(self) -> None:
        response = self.client.get("/api/v1/history?window=" + "9" * 10000 + "m")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "ValueError")

    def test_root_serves_existing_prototype(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("AXIS", response.text)
        self.assertEqual(self.client.get("/styles.css").status_code, 200)
        self.assertEqual(self.client.get("/app.js").status_code, 200)
        self.assertEqual(self.client.get("/i18n.js").status_code, 200)
        self.assertEqual(self.client.get("/request-guard.js").status_code, 200)
        self.assertEqual(self.client.get("/gpu-layout.js").status_code, 200)
        self.assertEqual(self.client.get("/monitor-chart.js").status_code, 200)
        self.assertEqual(self.client.get("/theme.js").status_code, 200)
        self.assertEqual(self.client.get("/REQUIREMENTS.md").status_code, 404)

    def test_frontend_assets_are_real_api_driven_and_javascript_is_valid(self) -> None:
        source = (Path(__file__).resolve().parent.parent / "app.js").read_text(encoding="utf-8")
        html = (Path(__file__).resolve().parent.parent / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("Math.random", source)
        self.assertNotIn("|| snapshot.gpus?.[", source)
        self.assertIn("function syncGpuCards", source)
        self.assertIn("gpuLayout.prepareGpus", source)
        self.assertIn("sidebar.inert", source)
        self.assertIn("mainContent.inert", source)
        self.assertIn("requestGuard.reset()", source)
        self.assertIn("!requestGuard.isCurrent(ticket)", source)
        self.assertIn("invalid_response", source)
        self.assertIn("const SERVICE_INTERVAL_MS = 5000;", source)
        self.assertIn("/registered-services", source)
        self.assertIn("openServiceDialog", source)
        self.assertIn('id="gpuStage"', html)
        self.assertNotIn('id="gpu0Name"', html)
        self.assertNotIn('id="gpu1Name"', html)
        self.assertIn('id="addSceneButton"', html)
        self.assertIn('id="addServiceButton"', html)
        self.assertIn('管理脚本绝对路径', html)
        self.assertNotIn('运行适配器', html)
        self.assertNotIn("setInterval(", source)
        self.assertNotIn("state.failures", source)
        self.assertIn("aria-expanded", html)
        for forbidden_demo in (
            'id="gpu0Util">1<', 'id="gpu1Util">0<', 'id="gpu0MemoryValue">46.3<',
            'id="gpu1MemoryValue">0.4<', '<polyline id="gpu0Sparkline" points="0,',
            '<polyline id="gpu1Sparkline" points="0,', '22:53:58.142',
            'NInfer 健康检查通过', 'id="primaryServiceName">ninfer-4090<',
        ):
            self.assertNotIn(forbidden_demo, html)
        for endpoint in (
            "/auth/status", "/snapshot", "/history?window=", "/registered-services",
            "/scenes", "/operations?limit=50",
        ):
            self.assertIn(endpoint, source)
        node = shutil.which("node")
        if node is None:
            self.skipTest("node 不可用，跳过 JavaScript 语法检查")
        completed = subprocess.run(
            [node, "--check", str(Path(__file__).resolve().parent.parent / "app.js")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        guard_test = subprocess.run(
            [node, str(Path(__file__).resolve().parent / "frontend_request_guard.test.js")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        self.assertEqual(guard_test.returncode, 0, guard_test.stderr)
        i18n_test = subprocess.run(
            [node, "--test", str(Path(__file__).resolve().parent / "frontend_i18n.test.js")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        self.assertEqual(i18n_test.returncode, 0, i18n_test.stderr)

    def test_api_errors_follow_requested_language_without_changing_error_code(self) -> None:
        setup = self.client.post(
            "/api/v1/auth/setup", json={"username": "admin", "password": "1234"}
        )
        self.assertEqual(setup.status_code, 201, setup.text)
        self.client.cookies.clear()

        english = self.client.get("/api/v1/users", headers={"Accept-Language": "en-US,en;q=0.9"})
        self.assertEqual(english.status_code, 401)
        self.assertEqual(english.json()["error"]["code"], "authentication_required")
        self.assertEqual(english.json()["error"]["message"], "Authentication is required.")
        self.assertEqual(english.headers["content-language"], "en")

        chinese = self.client.get("/api/v1/users", headers={"Accept-Language": "zh-CN"})
        self.assertEqual(chinese.status_code, 401)
        self.assertEqual(chinese.json()["error"]["code"], "authentication_required")
        self.assertEqual(chinese.json()["error"]["message"], "需要登录")
        self.assertEqual(chinese.headers["content-language"], "zh")

        fallback = self.client.get("/api/v1/users", headers={"Accept-Language": "fr-FR"})
        self.assertEqual(fallback.json()["error"]["message"], "Authentication is required.")

    def test_standard_http_errors_use_the_declared_language(self) -> None:
        missing = self.client.get("/api/v1/not-a-real-route")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "http_error")
        self.assertEqual(missing.json()["error"]["message"], "请求的资源不存在。")
        self.assertEqual(missing.headers["content-language"], "zh")

        missing_english = self.client.get(
            "/api/v1/not-a-real-route", headers={"Accept-Language": "en"}
        )
        self.assertEqual(
            missing_english.json()["error"]["message"],
            "The requested resource was not found.",
        )
        self.assertEqual(missing_english.headers["content-language"], "en")

        method = self.client.post("/api/v1/health", headers={"Accept-Language": "zh-CN"})
        self.assertEqual(method.status_code, 405)
        self.assertEqual(method.json()["error"]["message"], "请求方法不允许。")
        self.assertEqual(method.headers["content-language"], "zh")

    @patch("workstation_manager.collectors.collect_ports", return_value=[])
    @patch("workstation_manager.collectors.collect_docker", return_value=[])
    @patch("workstation_manager.collectors.collect_gpus", side_effect=RuntimeError("gpu unavailable"))
    @patch(
        "workstation_manager.collectors.collect_host",
        return_value={"cpu": {"load_percent": 1}, "memory": {"percent": 2}, "disks": []},
    )
    def test_application_starts_when_one_collector_raises_runtime_error(
        self, _host, _gpus, _docker, _ports
    ) -> None:
        isolated_settings = replace(
            self.settings,
            database_path=Path(self.temp.name) / "collector-error.db",
            manager_log_path=Path(self.temp.name) / "collector-error.log",
        )
        sampler = Sampler(isolated_settings, collector=collect_snapshot)
        with TestClient(
            create_app(isolated_settings, sampler), client=("127.0.0.1", 50000)
        ) as client:
            response = client.get("/api/v1/snapshot")
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["host"]["cpu"]["load_percent"], 1)
            self.assertEqual(body["gpus"], [])
            self.assertEqual(body["collector_errors"][0]["error_type"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
