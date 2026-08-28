from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from workstation_manager.app import create_app
from workstation_manager.collectors import collect_docker, collect_gpus, collect_snapshot
from workstation_manager.config import ConfigError, Settings, load_settings
from workstation_manager.history import HistoryStore, Sampler, parse_window


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

    def test_gpu_csv_is_parsed_per_card(self) -> None:
        output = (
            "0, GPU-a, NVIDIA GeForce RTX 4090, 98, 46359, 49140, 65, 430.5\n"
            "1, GPU-b, NVIDIA GeForce RTX 3090, 2, 100, 24576, 37, 25.0\n"
        )
        gpus = collect_gpus(self.settings, runner=lambda command, timeout: output)
        self.assertEqual([gpu["uuid"] for gpu in gpus], ["GPU-a", "GPU-b"])
        self.assertEqual(gpus[0]["memory_used_mib"], 46359)
        self.assertAlmostEqual(gpus[1]["memory_percent"], 0.41, places=2)

    def test_unsupported_gpu_metric_is_none(self) -> None:
        output = "0, GPU-a, RTX, N/A, 0, 24576, [N/A], N/A\n"
        gpu = collect_gpus(self.settings, runner=lambda command, timeout: output)[0]
        self.assertIsNone(gpu["load_percent"])
        self.assertIsNone(gpu["temperature_c"])
        self.assertIsNone(gpu["power_w"])

    def test_docker_json_lines_are_parsed(self) -> None:
        output = '{"ID":"abc","Names":"ninfer","Image":"image","State":"running","Status":"Up","Ports":"0.0.0.0:8080->8080/tcp","Labels":""}\n'
        containers = collect_docker(self.settings, runner=lambda command, timeout: output)
        self.assertEqual(containers[0]["name"], "ninfer")
        self.assertEqual(containers[0]["state"], "running")
        self.assertNotIn("labels", containers[0])

    def test_docker_non_object_json_is_rejected_explicitly(self) -> None:
        with self.assertRaisesRegex(ValueError, "根节点必须是对象"):
            collect_docker(self.settings, runner=lambda command, timeout: "[]\n")

    @patch("workstation_manager.collectors.collect_ports", return_value=[])
    @patch("workstation_manager.collectors.collect_host", return_value={"cpu": {}, "memory": {}, "disks": []})
    def test_missing_commands_are_structured_and_isolated(self, _host, _ports) -> None:
        def missing(command: list[str], timeout: float) -> str:
            raise FileNotFoundError(command[0])

        snapshot = collect_snapshot(self.settings, runner=missing)
        self.assertEqual(snapshot["gpus"], [])
        self.assertEqual(snapshot["docker"]["containers"], [])
        self.assertEqual({error["collector"] for error in snapshot["collector_errors"]}, {"nvidia", "docker"})
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


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        temporary_root = Path(self.temp.name)
        self.settings = Settings(
            sample_interval_seconds=60,
            database_path=temporary_root / "manager.db",
            manager_log_path=temporary_root / "manager.log",
        )

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
        self.assertEqual(self.client.get("/api/v1/health").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/snapshot").json()["gpus"][0]["uuid"], "GPU-a")
        self.assertEqual(len(self.client.get("/api/v1/history?window=15m").json()["samples"]), 1)
        services = self.client.get("/api/v1/host-services").json()
        self.assertEqual(services["containers"][0]["name"], "example")
        self.assertEqual(services["listening_ports"][0]["port"], 8080)

    def test_setup_accepts_four_character_password(self) -> None:
        response = self.client.post(
            "/api/v1/auth/setup", json={"username": "admin", "password": "1234"}
        )
        self.assertEqual(response.status_code, 201, response.text)

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
            "/auth/status", "/snapshot", "/history?window=15m", "/registered-services",
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
