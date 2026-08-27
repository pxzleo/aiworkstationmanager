from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from workstation_manager.app import ProxyCapabilityStore, create_app
from workstation_manager.config import Settings
from workstation_manager.history import Sampler
from workstation_manager.integrations import (
    BackendProbeConfig, IntegrationConfigError, IntegrationError, IntegrationsConfig, LogService,
    LogSourceConfig, WebUIConfig, WebUIService, load_integrations_config,
    _bounded_subprocess_run, sanitize_log_text, validate_lines, validate_since,
)
from workstation_manager.manager_logging import configure_manager_logging


def write_config(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def base_config(target: str = "http://127.0.0.1:8081") -> dict:
    return {
        "schema_version": 1,
        "webuis": [{
            "id": "ninfer-4090", "name": "NInfer 4090 UI", "kind": "ninfer",
            "configured": True, "target": target, "health_path": "/health",
        }],
        "log_sources": [],
    }


class IntegrationConfigTests(unittest.TestCase):
    def test_missing_formal_file_uses_example_as_forced_disabled_preview(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            formal, example = root / "integrations.json", root / "integrations.example.json"
            write_config(example, base_config())
            config = load_integrations_config(formal, example)
            self.assertEqual(config.source, "example_preview")
            self.assertFalse(config.webuis[0].configured)
            self.assertTrue(config.blockers)

    def test_target_must_be_literal_loopback_with_fixed_port_and_no_url_extras(self) -> None:
        invalid = (
            "http://localhost:8081", "http://192.168.1.2:8081", "http://127.0.0.1",
            "ftp://127.0.0.1:8081", "http://user@127.0.0.1:8081",
            "http://127.0.0.1:8081/?token=secret", "http://127.0.0.1:8081/#x",
        )
        for target in invalid:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / "integrations.json"
                write_config(path, base_config(target))
                with self.assertRaises(IntegrationConfigError):
                    load_integrations_config(path)

    def test_unknown_fields_ids_and_log_types_are_rejected(self) -> None:
        cases = []
        unknown = base_config(); unknown["command"] = "whoami"; cases.append(unknown)
        unknown_id = base_config(); unknown_id["webuis"][0]["id"] = "attacker"; cases.append(unknown_id)
        bad_log = base_config(); bad_log["log_sources"] = [{"id": "x", "name": "x", "type": "file", "configured": True}]; cases.append(bad_log)
        for value in cases:
            with tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / "integrations.json"; write_config(path, value)
                with self.assertRaises(IntegrationConfigError):
                    load_integrations_config(path)

    def test_backend_probe_is_strict_loopback_and_parsed(self) -> None:
        value = base_config(); value["webuis"][0]["backend_probe"] = {"url": "http://127.0.0.1:8080/health", "timeout_seconds": 1.5, "json_equals": {"model.id": "qwen"}}
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "integrations.json"; write_config(path, value)
            probe = load_integrations_config(path).webuis[0].backend_probe
            self.assertEqual(probe.url, "http://127.0.0.1:8080/health")
            self.assertEqual(probe.json_equals, (("model.id", "qwen"),))
        for url in ("http://localhost:8080/health", "http://192.168.1.2:8080/health", "http://127.0.0.1:8080/health?token=x", "http://127.0.0.1:8080/%252e%252e/secret", "http://127.0.0.1:8080/a%5cb"):
            value = base_config(); value["webuis"][0]["backend_probe"] = {"url": url}
            with tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / "integrations.json"; write_config(path, value)
                with self.assertRaises(IntegrationConfigError): load_integrations_config(path)
        value = base_config(); value["webuis"][0]["health_path"] = "/%252e%252e/secret"
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "integrations.json"; write_config(path, value)
            with self.assertRaises(IntegrationConfigError): load_integrations_config(path)
        value = base_config(); value["webuis"][0]["health_path"] = "/a\\b"
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "integrations.json"; write_config(path, value)
            with self.assertRaises(IntegrationConfigError): load_integrations_config(path)


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    def service(self, handler, concurrency: int = 4) -> WebUIService:
        config = IntegrationsConfig(
            source="formal",
            webuis=(WebUIConfig("ninfer-4090", "NInfer", "ninfer", True, "http://127.0.0.1:8081", "/"),),
            blockers=(),
        )
        transport = httpx.MockTransport(handler)
        return WebUIService(
            config,
            client_factory=lambda: httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False),
            concurrency=concurrency,
        )

    async def test_proxy_preserves_safe_path_query_static_content_and_filters_headers(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "http://127.0.0.1:8081/assets/app.js?v=1")
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("cookie", request.headers)
            return httpx.Response(200, content=b"asset", headers={"Content-Type": "text/javascript", "Set-Cookie": "upstream=x", "Connection": "close"})
        status, headers, body = await self.service(handler).proxy(
            "ninfer-4090", "assets/app.js", "GET", b"/proxy/webui/ninfer-4090/assets/app.js",
            b"v=1", {"authorization": "Bearer secret", "cookie": "a=b", "accept": "*/*"}, b"",
        )
        self.assertEqual((status, body), (200, b"asset"))
        names = {name.lower() for name, _ in headers}
        self.assertIn("content-type", names)
        self.assertIn(("cache-control", "no-store"), headers)
        self.assertNotIn("set-cookie", names)
        self.assertNotIn("connection", names)

    async def test_proxy_rejects_traversal_absolute_backslash_control_and_wrong_method(self) -> None:
        async def handler(_: httpx.Request) -> httpx.Response:
            raise AssertionError("unsafe request reached upstream")
        service = self.service(handler)
        cases = (
            ("../secret", b"/proxy/webui/ninfer-4090/%2e%2e/secret", "GET"),
            ("http://evil.test", None, "GET"), ("a\\b", None, "GET"),
            ("safe", None, "PUT"),
        )
        for path, raw_path, method in cases:
            with self.subTest(path=path, method=method), self.assertRaises(IntegrationError):
                await service.proxy("ninfer-4090", path, method, raw_path, b"", {}, b"")

    async def test_same_origin_redirect_is_rewritten_and_external_redirect_is_blocked(self) -> None:
        service = self.service(lambda _: httpx.Response(302, headers={"Location": "http://127.0.0.1:8081/login?next=%2F"}))
        _, headers, _ = await service.proxy("ninfer-4090", "", "GET", None, b"", {}, b"")
        self.assertIn(("location", "/proxy/webui/ninfer-4090/login?next=%2F"), headers)
        external = self.service(lambda _: httpx.Response(302, headers={"Location": "https://evil.example/"}))
        with self.assertRaisesRegex(IntegrationError, "外部跳转"):
            await external.proxy("ninfer-4090", "", "GET", None, b"", {}, b"")

    async def test_relative_and_query_only_redirects_use_current_upstream_url(self) -> None:
        relative = self.service(lambda _: httpx.Response(302, headers={"Location": "next?step=2#done"}))
        _, headers, _ = await relative.proxy(
            "ninfer-4090", "nested/page", "GET", None, b"", {}, b"",
        )
        self.assertIn(
            ("location", "/proxy/webui/ninfer-4090/nested/next?step=2#done"), headers,
        )
        query_only = self.service(lambda _: httpx.Response(302, headers={"Location": "?step=3"}))
        _, headers, _ = await query_only.proxy(
            "ninfer-4090", "nested/page", "GET", None, b"step=1", {}, b"",
        )
        self.assertIn(
            ("location", "/proxy/webui/ninfer-4090/nested/page?step=3"), headers,
        )

    async def test_ui_and_backend_status_are_independent(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.port == 8081:
                return httpx.Response(200, text="ui")
            return httpx.Response(503, json={"status": "offline"})
        transport = httpx.MockTransport(handler)
        config = IntegrationsConfig(source="formal", blockers=(), webuis=(
            WebUIConfig("ninfer-4090", "NInfer", "ninfer", True, "http://127.0.0.1:8081", "/", BackendProbeConfig("http://127.0.0.1:8080/health", 1.0)),
        ))
        result = await WebUIService(config, client_factory=lambda: httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)).list_status()
        item = result["webuis"][0]
        self.assertEqual(item["ui_status"], "online")
        self.assertEqual(item["backend_status"], "offline")
        self.assertIsNotNone(item["backend_checked_at"])
        self.assertTrue(item["backend_blocker"])
        self.assertEqual(item["proxy_url"], "/proxy/webui/ninfer-4090/")

    async def test_backend_probe_stream_limit_and_scalar_types_are_exact(self) -> None:
        async def check(expected, body: bytes) -> dict:
            transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"ui" if request.url.port == 8081 else body))
            config = IntegrationsConfig(source="formal", blockers=(), webuis=(
                WebUIConfig("ninfer-4090", "NInfer", "ninfer", True, "http://127.0.0.1:8081", "/", BackendProbeConfig("http://127.0.0.1:8080/health", 1.0, (("ready", expected),))),
            ))
            result = await WebUIService(config, client_factory=lambda: httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)).list_status()
            return result["webuis"][0]
        type_mismatch = await check(1, b'{"ready":true}')
        self.assertEqual(type_mismatch["backend_status"], "offline")
        self.assertIn("字段不匹配", type_mismatch["backend_blocker"])
        oversized = await check(True, b'{"ready":true,"padding":"' + b"x" * (64 * 1024) + b'"}')
        self.assertEqual(oversized["backend_status"], "offline")
        self.assertIn("超过限制", oversized["backend_blocker"])

    async def test_read_only_method_response_timeout_and_concurrency_are_bounded(self) -> None:
        service = self.service(lambda _: httpx.Response(200, content=b"ok"))
        with self.assertRaisesRegex(IntegrationError, "方法"):
            await service.proxy("ninfer-4090", "", "POST", None, b"", {}, b"x" * (1024 * 1024 + 1))
        huge = self.service(lambda _: httpx.Response(200, content=b"x" * (16 * 1024 * 1024 + 1)))
        with self.assertRaisesRegex(IntegrationError, "响应"):
            await huge.proxy("ninfer-4090", "", "GET", None, b"", {}, b"")
        timeout = self.service(lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("timeout", request=request)))
        with self.assertRaisesRegex(IntegrationError, "上游不可用"):
            await timeout.proxy("ninfer-4090", "", "GET", None, b"", {}, b"")

        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(_: httpx.Request) -> httpx.Response:
            entered.set(); await release.wait(); return httpx.Response(200, content=b"ok")
        limited = self.service(slow, concurrency=1)
        first = asyncio.create_task(limited.proxy("ninfer-4090", "", "GET", None, b"", {}, b""))
        await entered.wait()
        with self.assertRaisesRegex(IntegrationError, "并发"):
            await limited.proxy("ninfer-4090", "", "GET", None, b"", {}, b"")
        release.set(); await first


class LogTests(unittest.TestCase):
    def test_arguments_are_fixed_non_shell_bounded_and_redacted(self) -> None:
        calls = []
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, "\x1b[31mAuthorization: Bearer top-secret\x1b[0m\n2026-01-01 INFO Cookie: wm_session=session-secret; second=second-secret\nprefix Set-Cookie: upstream=set-cookie-secret; Path=/\n{\"cookie\":\"json-cookie-secret\"}\nRuntimeError Cookie=exception-cookie-secret\npostgres://user:dsn-secret@127.0.0.1/db\neyJabcdefghijk.abcdefghijk.abcdefghijk\nok", "")
        config = IntegrationsConfig(
            source="formal", blockers=(),
            log_sources=(LogSourceConfig("ninfer", "NInfer", "docker_logs", True, container="ninfer-4090"),),
        )
        result = LogService(config, Path("unused"), runner=runner).entries("ninfer", 20, "1h")
        args, kwargs = calls[0]
        self.assertEqual(args, ["docker", "logs", "--tail", "20", "--since", "1h", "ninfer-4090"])
        self.assertFalse(kwargs["shell"])
        self.assertIn("<redacted>", "\n".join(result["lines"]))
        self.assertNotIn("top-secret", "\n".join(result["lines"]))
        for secret in ("session-secret", "second-secret", "set-cookie-secret", "json-cookie-secret", "exception-cookie-secret"):
            self.assertNotIn(secret, "\n".join(result["lines"]))
        self.assertNotIn("dsn-secret", "\n".join(result["lines"]))
        self.assertNotIn("eyJabcdefghijk", "\n".join(result["lines"]))
        self.assertNotIn("\x1b", "\n".join(result["lines"]))

    def test_wsl_journal_arguments_and_limits_are_strict(self) -> None:
        calls = []
        def runner(args, **kwargs):
            calls.append(args); return subprocess.CompletedProcess(args, 0, "line", "")
        config = IntegrationsConfig(source="formal", blockers=(), log_sources=(
            LogSourceConfig("ui", "UI", "wsl_journal", True, distro="Ubuntu", scope="user", unit="ninfer-ui.service"),
        ))
        LogService(config, Path("unused"), runner=runner).entries("ui", 1000, "7d")
        self.assertEqual(calls[0], ["wsl.exe", "-d", "Ubuntu", "--exec", "journalctl", "--no-pager", "--output=short-iso", "--lines=1000", "--since=-7d", "--user", "--unit=ninfer-ui.service"])
        for value in (0, 1001, True):
            with self.assertRaises(IntegrationError): validate_lines(value)
        for value in ("0m", "8d", "169h", "yesterday", "1h;whoami"):
            with self.assertRaises(IntegrationError): validate_since(value)

    def test_manager_log_is_tail_only_and_rotation_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "manager.log"
            logger = configure_manager_logging(path, "INFO", max_bytes=256, backup_count=2)
            for index in range(80): logger.info("line %03d prefix Cookie: wm_session=manager-cookie-secret", index)
            try:
                raise RuntimeError('{"Set-Cookie":"manager-exception-secret"}')
            except RuntimeError:
                logger.exception("manager exception")
            for handler in logger.handlers: handler.flush()
            self.assertTrue(path.exists())
            self.assertLessEqual(len(list(path.parent.glob("manager.log*"))), 3)
            result = LogService(IntegrationsConfig(source="formal", blockers=()), path).entries("manager", 5, "1h")
            text = "\n".join(result["lines"])
            self.assertLessEqual(len(result["lines"]), 5)
            persisted = "\n".join(candidate.read_text(encoding="utf-8") for candidate in path.parent.glob("manager.log*"))
            self.assertNotIn("manager-cookie-secret", persisted)
            self.assertNotIn("manager-exception-secret", persisted)
            self.assertIn("<redacted>", text)
            for handler in list(logger.handlers):
                logger.removeHandler(handler); handler.close()

    def test_manager_log_honors_cancel_and_per_source_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "manager.log"; path.write_text("line", encoding="utf-8")
            service = LogService(IntegrationsConfig(source="formal", blockers=()), path)
            cancelled = threading.Event(); cancelled.set()
            with self.assertRaisesRegex(IntegrationError, "取消"):
                service.entries("manager", 10, "1h", cancelled)
            entered, release = threading.Event(), threading.Event()
            def slow_tail(_path): entered.set(); release.wait(2); return "line", False
            results = []
            with patch("workstation_manager.integrations._read_file_tail", side_effect=slow_tail):
                first = threading.Thread(target=lambda: results.append(service.entries("manager", 10, "1h")))
                first.start(); self.assertTrue(entered.wait(1))
                with self.assertRaisesRegex(IntegrationError, "已有读取任务"):
                    service.entries("manager", 10, "1h")
                release.set(); first.join(2)
            self.assertEqual(results[0]["lines"], ["line"])

    def test_manager_tail_reports_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "manager.log"; path.write_bytes(b"line\n" * 60_000)
            result = LogService(IntegrationsConfig(source="formal", blockers=()), path).entries("manager", 10, "1h")
            self.assertTrue(result["truncated"])

    def test_bounded_process_kills_and_waits_on_limit_and_cancel(self) -> None:
        class Output:
            def __init__(self, chunks): self.chunks = iter(chunks)
            def read(self, _size): return next(self.chunks, b"")
        class Process:
            def __init__(self, chunks):
                self.stdout = Output(chunks); self.returncode = None; self.killed = False; self.waited = False
            def poll(self): return self.returncode
            def kill(self): self.killed = True; self.returncode = -9
            def wait(self, timeout=None): self.waited = True; return self.returncode
        limited = Process([b"x" * (256 * 1024 + 1)])
        with patch("workstation_manager.integrations.subprocess.Popen", return_value=limited):
            result = _bounded_subprocess_run(["fixed"], 1, None, 0)
        self.assertTrue(limited.killed)
        self.assertTrue(limited.waited)
        self.assertIn("truncated", result.stdout)
        cancelled = Process([b""])
        cancel_event = threading.Event()
        def start_then_cancel(*args, **kwargs): cancel_event.set(); return cancelled
        with patch("workstation_manager.integrations.subprocess.Popen", side_effect=start_then_cancel), self.assertRaises(subprocess.SubprocessError):
            _bounded_subprocess_run(["fixed"], 1, None, 0, cancel_event)
        self.assertTrue(cancelled.killed)
        self.assertTrue(cancelled.waited)
        pre_cancelled = threading.Event(); pre_cancelled.set()
        with patch("workstation_manager.integrations.subprocess.Popen") as popen, self.assertRaises(subprocess.SubprocessError):
            _bounded_subprocess_run(["fixed"], 1, None, 0, pre_cancelled)
        popen.assert_not_called()
        timed_out = Process([b""])
        with patch("workstation_manager.integrations.subprocess.Popen", return_value=timed_out), self.assertRaises(subprocess.TimeoutExpired):
            _bounded_subprocess_run(["fixed"], 0.001, None, 0)
        self.assertTrue(timed_out.killed)
        self.assertTrue(timed_out.waited)
        failed_cleanup = Process([b""])
        def failed_kill(): raise OSError("mock kill failure")
        def failed_wait(timeout=None): raise subprocess.TimeoutExpired(["fixed"], timeout or 1)
        failed_cleanup.kill = failed_kill
        failed_cleanup.wait = failed_wait
        cancel_event.clear()
        def start_failed(*args, **kwargs): cancel_event.set(); return failed_cleanup
        with patch("workstation_manager.integrations.subprocess.Popen", side_effect=start_failed), self.assertRaisesRegex(subprocess.SubprocessError, "无法确认终止"):
            _bounded_subprocess_run(["fixed"], 1, None, 0, cancel_event)
        failed_service_process = Process([b""])
        failed_service_process.kill = failed_kill
        failed_service_process.wait = failed_wait
        config = IntegrationsConfig(source="formal", blockers=(), log_sources=(LogSourceConfig("source", "Source", "docker_logs", True, container="fixed-container"),))
        cancel_event.clear()
        def start_failed_service(*args, **kwargs): cancel_event.set(); return failed_service_process
        with patch("workstation_manager.integrations.subprocess.Popen", side_effect=start_failed_service), self.assertRaises(IntegrationError) as caught:
            LogService(config, Path("unused")).entries("source", 10, "1h", cancel_event)
        self.assertEqual(caught.exception.code, "log_cancel_cleanup_failed")

    def test_global_log_concurrency_is_bounded_across_sources(self) -> None:
        entered = threading.Semaphore(0); release = threading.Event()
        def runner(args, **kwargs):
            entered.release(); release.wait(2); return subprocess.CompletedProcess(args, 0, "line", "")
        sources = tuple(LogSourceConfig(f"source-{index}", f"Source {index}", "docker_logs", True, container=f"container-{index}") for index in range(5))
        service = LogService(IntegrationsConfig(source="formal", blockers=(), log_sources=sources), Path("unused"), runner=runner)
        threads = [threading.Thread(target=service.entries, args=(f"source-{index}", 10, "1h")) for index in range(4)]
        for thread in threads: thread.start()
        for _ in range(4): self.assertTrue(entered.acquire(timeout=1))
        with self.assertRaisesRegex(IntegrationError, "并发"):
            service.entries("source-4", 10, "1h")
        release.set()
        for thread in threads: thread.join(2)


class PackagingContractTests(unittest.TestCase):
    def test_start_and_task_scripts_are_location_independent_and_exactly_scoped(self) -> None:
        root = Path(__file__).resolve().parent.parent
        start = (root / "Start-Manager.ps1").read_text(encoding="utf-8")
        install = (root / "Install-ManagerTask.ps1").read_text(encoding="utf-8")
        uninstall = (root / "Uninstall-ManagerTask.ps1").read_text(encoding="utf-8")
        self.assertIn("$MyInvocation.MyCommand.Path", start)
        self.assertIn("WM_CONFIG_FILE", start)
        self.assertIn("$hadConfigEnvironment", start)
        self.assertIn("Remove-Item -LiteralPath Env:WM_CONFIG_FILE", start)
        self.assertIn("-WorkingDirectory $projectRoot", install)
        self.assertIn("Logon", install); self.assertIn("Startup", install)
        self.assertIn("LogonType S4U", install)
        self.assertIn("$python.Source", install)
        self.assertIn('$taskName = "AXIS-AI-Workstation-Manager"', uninstall)
        self.assertIn("Unregister-ScheduledTask -TaskName $taskName", uninstall)
        self.assertNotIn("Get-ScheduledTask |", uninstall)

    def test_settings_example_is_complete_and_unknown_fields_fail(self) -> None:
        root = Path(__file__).resolve().parent.parent
        example = json.loads((root / "config" / "settings.example.json").read_text(encoding="utf-8"))
        self.assertEqual(set(example), set(Settings.__dataclass_fields__))
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "settings.json"; write_config(path, {**example, "cookie_secur": True})
            with self.assertRaisesRegex(Exception, "未知字段"):
                from workstation_manager.config import load_settings
                load_settings({"WM_CONFIG_FILE": str(path)})
        from workstation_manager.config import ConfigError, load_settings
        parsed = load_settings({"WM_ALLOWED_PUBLIC_ORIGINS": "https://manager.example.lan,https://manager.example.lan:8443", "WM_TRUSTED_PROXY_IPS": "127.0.0.1,192.168.1.10"})
        self.assertEqual(parsed.allowed_public_origins, ("https://manager.example.lan", "https://manager.example.lan:8443"))
        self.assertEqual(parsed.trusted_proxy_ips, ("127.0.0.1", "192.168.1.10"))
        with self.assertRaises(ConfigError):
            load_settings({"WM_ALLOWED_PUBLIC_ORIGINS": "https://*.example.lan"})
        with self.assertRaises(ConfigError):
            load_settings({"WM_MANAGER_LOG_PATH": r"\\server\share\manager.log"})

    def test_start_script_restores_config_environment_in_same_session(self) -> None:
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            fake_python = temporary / "fake-python.cmd"
            fake_python.write_text("@echo off\r\nexit /b 0\r\n", encoding="ascii")
            config = temporary / "settings.json"; config.write_text("{}", encoding="utf-8")
            quote = lambda value: str(value).replace("'", "''")
            command = (
                f"$start='{quote(root / 'Start-Manager.ps1')}'; $config='{quote(config)}'; $fake='{quote(fake_python)}'; "
                "$env:WM_CONFIG_FILE='before-value'; & $start -ConfigFile $config -PythonPath $fake; "
                "if ($env:WM_CONFIG_FILE -ne 'before-value') { throw 'existing WM_CONFIG_FILE was not restored' }; "
                "Remove-Item Env:WM_CONFIG_FILE; & $start -ConfigFile $config -PythonPath $fake; "
                "if (Test-Path Env:WM_CONFIG_FILE) { throw 'WM_CONFIG_FILE leaked into caller session' }"
            )
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_start_script_restores_config_environment_after_failures(self) -> None:
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            failing_python = temporary / "failing-python.cmd"
            failing_python.write_text("@echo off\r\nexit /b 7\r\n", encoding="ascii")
            config = temporary / "settings.json"; config.write_text("{}", encoding="utf-8")
            missing = temporary / "missing-settings.json"
            quote = lambda value: str(value).replace("'", "''")
            command = (
                f"$start='{quote(root / 'Start-Manager.ps1')}'; $config='{quote(config)}'; $missing='{quote(missing)}'; $fake='{quote(failing_python)}'; "
                "$env:WM_CONFIG_FILE='before-value'; try { & $start -ConfigFile $config -PythonPath $fake } catch { }; "
                "if ($env:WM_CONFIG_FILE -ne 'before-value') { throw 'nonzero Python exit did not restore WM_CONFIG_FILE' }; "
                "try { & $start -ConfigFile $missing -PythonPath $fake } catch { }; "
                "if ($env:WM_CONFIG_FILE -ne 'before-value') { throw 'config resolution failure did not preserve WM_CONFIG_FILE' }; exit 0"
            )
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_release_builder_creates_clean_allowlist_staging(self) -> None:
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "release"
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / "Build-Release.ps1"), "-Destination", str(destination)],
                shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((destination / "workstation_manager" / "app.py").is_file())
            self.assertTrue((destination / "config" / "integrations.example.json").is_file())
            for forbidden in ("data", "logs", "output", ".playwright-cli"):
                self.assertFalse((destination / forbidden).exists())
            self.assertFalse((destination / "config" / "integrations.json").exists())
            self.assertFalse((destination / "config" / "control.json").exists())
            self.assertFalse((destination / "REQUIREMENTS.md").exists())
            self.assertFalse(any(destination.rglob("*.db")))
            self.assertFalse(any(destination.rglob("*.png")))
            packaged = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in destination.rglob("*") if path.is_file())
            for sensitive_baseline in (r"C:\Users\xu", r"D:\AIWork", "GPU-00000000", "XU", "XUPC", "192.168.100.190", "192.168.100.0/24"):
                self.assertNotIn(sensitive_baseline, packaged)
            readme = (destination / "README.md").read_text(encoding="utf-8")
            self.assertIn("access_log off", readme)
            self.assertIn("log_skip @proxyAssets", readme)


class IntegrationApiTests(unittest.TestCase):
    def test_proxy_capability_expires_and_is_target_scoped(self) -> None:
        store = ProxyCapabilityStore(ttl_seconds=0)
        expired = store.issue("ninfer-4090", "session-hash")
        with self.assertRaises(IntegrationError):
            store.validate(expired, "ninfer-4090")
        active = ProxyCapabilityStore(ttl_seconds=120)
        token = active.issue("ninfer-4090", "session-hash")
        active.validate(token, "ninfer-4090")
        with self.assertRaises(IntegrationError):
            active.validate(token, "ninfer-3090")
        with self.assertRaises(IntegrationError):
            active.validate(token + "x", "ninfer-4090")
        active.revoke_session("session-hash")
        with self.assertRaises(IntegrationError):
            active.validate(token, "ninfer-4090")
        session_limited = ProxyCapabilityStore(ttl_seconds=120)
        already_expired = session_limited.issue(
            "ninfer-4090", "expired-session",
            (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        )
        with self.assertRaises(IntegrationError):
            session_limited.validate(already_expired, "ninfer-4090")

    def test_routes_require_login_and_proxy_and_logs_use_injected_mocks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = IntegrationsConfig(
                source="formal", blockers=(),
                webuis=(WebUIConfig("ninfer-4090", "NInfer", "ninfer", True, "http://127.0.0.1:8081", "/"),),
                log_sources=(LogSourceConfig("ninfer", "NInfer", "docker_logs", True, container="ninfer-4090"),),
            )
            def upstream(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/assets/app.js"):
                    return httpx.Response(200, text="window.mockStaticLoaded = true;", headers={"content-type": "application/javascript"})
                return httpx.Response(200, text='<html><head><base href="/"></head><script src="assets/app.js"></script></html>', headers={"content-type": "text/html"})
            transport = httpx.MockTransport(upstream)
            webuis = WebUIService(config, client_factory=lambda: httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False))
            calls = []
            def runner(args, **kwargs):
                calls.append(args); return subprocess.CompletedProcess(args, 0, "prefix Cookie: api-return-secret", "")
            logs = LogService(config, root / "manager.log", runner=runner)
            settings = Settings(
                database_path=root / "db.sqlite", discovery_scripts_path=root,
                scan_scripts_on_startup=False, manager_log_path=root / "manager.log",
                integrations_config_path=root / "missing.json",
                allowed_public_origins=("https://manager.example.lan",),
                trusted_proxy_ips=("127.0.0.1",),
                session_max_active=1,
            )
            def snapshot(_settings):
                return {"sampled_at": "2026-08-27T00:00:00+00:00", "host": {}, "gpus": [], "docker": {"containers": []}, "ports": [], "collector_errors": []}
            app = create_app(settings, Sampler(settings, collector=snapshot), webui_service=webuis, log_service=logs)
            with TestClient(app, client=("127.0.0.1", 50100)) as client:
                self.assertEqual(client.get("/api/v1/webuis").status_code, 401)
                blocked_setup = client.post("/api/v1/auth/setup", headers={"X-Forwarded-For": "192.168.1.8"}, json={"username": "admin", "password": "correct horse battery staple"})
                self.assertEqual(blocked_setup.status_code, 403)
                setup = client.post("/api/v1/auth/setup", json={"username": "admin", "password": "correct horse battery staple"})
                self.assertEqual(setup.status_code, 201)
                status = client.get("/api/v1/webuis")
                self.assertEqual(status.status_code, 200)
                self.assertEqual(status.json()["webuis"][0]["status"], "online")
                proxied = client.get("/proxy/webui/ninfer-4090/assets/app.js?v=1")
                self.assertIn("mockStaticLoaded", proxied.text)
                self.assertIn("application/javascript", proxied.headers["content-type"])
                proxy_root = client.get("/proxy/webui/ninfer-4090/")
                match = re.search(r'<base href="(/proxy-asset/([^/]+)/ninfer-4090/)">', proxy_root.text)
                self.assertIsNotNone(match)
                asset_root, capability = match.group(1), match.group(2)
                session_cookie = client.cookies.get("wm_session")
                owner_session_hash = app.state.auth.authenticate(session_cookie).token_hash
                client.cookies.clear()
                anonymous_asset = client.get(asset_root + "assets/app.js?v=1")
                self.assertEqual(anonymous_asset.status_code, 200)
                self.assertIn("mockStaticLoaded", anonymous_asset.text)
                self.assertEqual(client.get(f"/proxy-asset/{capability}x/ninfer-4090/assets/app.js").status_code, 403)
                self.assertEqual(client.get(f"/proxy-asset/{capability}/ninfer-3090/assets/app.js").status_code, 403)
                self.assertEqual(client.get("/proxy/webui/ninfer-4090/").status_code, 401)
                client.cookies.set("wm_session", session_cookie)
                csp = proxied.headers["content-security-policy"]
                self.assertIn("sandbox allow-scripts", csp)
                self.assertIn("default-src 'none'", csp)
                self.assertIn("script-src http://testserver", csp)
                self.assertIn("base-uri http://testserver", csp)
                self.assertIn("form-action 'none'", csp)
                for forbidden in ("allow-same-origin", "allow-top-navigation", "allow-forms", "script-src https:", "connect-src https:"):
                    self.assertNotIn(forbidden, csp)
                self.assertEqual(proxied.headers["x-content-type-options"], "nosniff")
                self.assertEqual(client.post("/proxy/webui/ninfer-4090/", content=b"x").status_code, 405)
                https_proxy = client.get("/proxy/webui/ninfer-4090/", headers={"Host": "manager.example.lan", "Origin": "https://manager.example.lan"})
                self.assertEqual(https_proxy.status_code, 200)
                self.assertIn("script-src https://manager.example.lan", https_proxy.headers["content-security-policy"])
                self.assertNotIn("script-src https:;", https_proxy.headers["content-security-policy"])
                blocked_host = client.get("/proxy/webui/ninfer-4090/", headers={"Host": "unlisted.example.lan"})
                self.assertEqual(blocked_host.status_code, 400)
                self.assertEqual(client.get("/api/v1/log-sources").status_code, 200)
                entries = client.get("/api/v1/log-sources/ninfer/entries?lines=20&since=1h")
                self.assertNotIn("api-return-secret", entries.text)
                self.assertIn("<redacted>", entries.text)
                self.assertEqual(calls[0], ["docker", "logs", "--tail", "20", "--since", "1h", "ninfer-4090"])
                events = client.get("/api/v1/audit?limit=20").json()["events"]
                self.assertTrue(any(item["event"] == "logs.read" for item in events))
                audit_text = client.get("/api/v1/audit?limit=200").text
                self.assertNotIn(capability, audit_text)
                client.cookies.clear()
                second_login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct horse battery staple"})
                self.assertEqual(second_login.status_code, 200)
                session_cookie = client.cookies.get("wm_session")
                owner_session_hash = app.state.auth.authenticate(session_cookie).token_hash
                second_root = client.get("/proxy/webui/ninfer-4090/")
                second_match = re.search(r'<base href="(/proxy-asset/([^/]+)/ninfer-4090/)">', second_root.text)
                self.assertIsNotNone(second_match)
                second_asset_root, second_capability = second_match.group(1), second_match.group(2)
                self.assertNotIn(second_capability, client.get("/api/v1/audit?limit=200").text)
            untrusted_app = create_app(settings, Sampler(settings, collector=snapshot), webui_service=webuis, log_service=logs)
            with TestClient(untrusted_app, client=("192.168.1.50", 50101), base_url="http://192.168.1.10:19100") as untrusted:
                untrusted.cookies.set("wm_session", session_cookie)
                forged = untrusted.get("/proxy/webui/ninfer-4090/", headers={"Host": "manager.example.lan", "Origin": "https://manager.example.lan"})
                self.assertEqual(forged.status_code, 400)
            with TestClient(app, client=("127.0.0.1", 50109)) as local_assets:
                self.assertEqual(local_assets.get(asset_root + "assets/app.js?v=1").status_code, 403)
            with app.state.database.connect() as connection:
                with connection:
                    connection.execute(
                        "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), owner_session_hash),
                    )
            with TestClient(app, client=("127.0.0.1", 50110)) as local_assets:
                self.assertEqual(local_assets.get(second_asset_root + "assets/app.js?v=1").status_code, 403)


class LogCancellationApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_log_request_kills_reader_and_writes_failure_audit(self) -> None:
        class SlowLogs:
            def __init__(self) -> None:
                self.entered = threading.Event(); self.cancelled = threading.Event()
            def list_sources(self): return {"source": "formal", "sources": [], "blockers": []}
            def entries(self, source_id, lines, since, cancel_event):
                self.entered.set()
                while not cancel_event.is_set(): time.sleep(0.01)
                self.cancelled.set()
                raise IntegrationError(499, "log_read_cancelled", "日志读取已取消")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); slow = SlowLogs()
            settings = Settings(database_path=root / "db.sqlite", discovery_scripts_path=root, scan_scripts_on_startup=False, manager_log_path=root / "manager.log", integrations_config_path=root / "missing.json")
            def snapshot(_settings): return {"sampled_at": "2026-08-27T00:00:00+00:00", "host": {}, "gpus": [], "docker": {"containers": []}, "ports": [], "collector_errors": []}
            app = create_app(settings, Sampler(settings, collector=snapshot), log_service=slow)
            with TestClient(app, client=("127.0.0.1", 50101), base_url="http://127.0.0.1:19100") as client:
                setup = client.post("/api/v1/auth/setup", json={"username": "admin", "password": "correct horse battery staple"})
                self.assertEqual(setup.status_code, 201)
                cookie = client.cookies.get("wm_session")
                messages = []
                scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http", "path": "/api/v1/log-sources/slow/entries", "raw_path": b"/api/v1/log-sources/slow/entries", "query_string": b"lines=20&since=1h", "root_path": "", "headers": [(b"host", b"127.0.0.1:19100"), (b"cookie", f"wm_session={cookie}".encode())], "client": ("127.0.0.1", 50102), "server": ("127.0.0.1", 19100), "extensions": {}}
                sent_request = False
                async def receive():
                    nonlocal sent_request
                    if not sent_request:
                        sent_request = True; return {"type": "http.request", "body": b"", "more_body": False}
                    await asyncio.sleep(60); return {"type": "http.disconnect"}
                async def send(message): messages.append(message)
                request_task = asyncio.create_task(app(scope, receive, send))
                await asyncio.to_thread(slow.entered.wait, 2)
                request_task.cancel()
                with self.assertRaises(asyncio.CancelledError): await request_task
                self.assertTrue(slow.cancelled.wait(2))
                events = app.state.database.list_audit(20)
                self.assertTrue(any(item["event"] == "logs.read" and item["result"] == "failure" and item["summary"].get("reason") == "cancelled" for item in events))

    async def test_http_disconnect_cancels_reader_and_audits_cleanup(self) -> None:
        class SlowLogs:
            def __init__(self) -> None:
                self.cancelled = threading.Event()
            def list_sources(self): return {"source": "formal", "sources": [], "blockers": []}
            def entries(self, source_id, lines, since, cancel_event):
                while not cancel_event.is_set(): time.sleep(0.01)
                self.cancelled.set()
                raise IntegrationError(499, "log_read_cancelled", "日志读取已取消")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); slow = SlowLogs()
            settings = Settings(database_path=root / "db.sqlite", discovery_scripts_path=root, scan_scripts_on_startup=False, manager_log_path=root / "manager.log", integrations_config_path=root / "missing.json")
            def snapshot(_settings): return {"sampled_at": "2026-08-27T00:00:00+00:00", "host": {}, "gpus": [], "docker": {"containers": []}, "ports": [], "collector_errors": []}
            app = create_app(settings, Sampler(settings, collector=snapshot), log_service=slow)
            with TestClient(app, client=("127.0.0.1", 50103), base_url="http://127.0.0.1:19100") as client:
                setup = client.post("/api/v1/auth/setup", json={"username": "admin", "password": "correct horse battery staple"})
                cookie = client.cookies.get("wm_session")
                receive_count = 0
                async def receive():
                    nonlocal receive_count
                    receive_count += 1
                    if receive_count == 1:
                        return {"type": "http.request", "body": b"", "more_body": False}
                    return {"type": "http.disconnect"}
                async def send(_message): return None
                scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http", "path": "/api/v1/log-sources/slow/entries", "raw_path": b"/api/v1/log-sources/slow/entries", "query_string": b"lines=20&since=1h", "root_path": "", "headers": [(b"host", b"127.0.0.1:19100"), (b"cookie", f"wm_session={cookie}".encode())], "client": ("127.0.0.1", 50104), "server": ("127.0.0.1", 19100), "extensions": {}}
                with self.assertRaises(asyncio.CancelledError):
                    await app(scope, receive, send)
                self.assertTrue(slow.cancelled.wait(2))
                events = app.state.database.list_audit(20)
                matching = [item for item in events if item["event"] == "logs.read" and item["result"] == "failure"]
                self.assertTrue(any(item["summary"].get("reason") == "client_disconnected" and item["summary"].get("cleanup") == "complete" for item in matching))

    async def test_cancel_cleanup_failure_is_explicitly_audited(self) -> None:
        class FailedCleanupLogs:
            def __init__(self) -> None: self.entered = threading.Event()
            def list_sources(self): return {"source": "formal", "sources": [], "blockers": []}
            def entries(self, source_id, lines, since, cancel_event):
                self.entered.set()
                while not cancel_event.is_set(): time.sleep(0.01)
                raise IntegrationError(502, "log_read_failed", "日志采集进程无法确认终止")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); failed = FailedCleanupLogs()
            settings = Settings(database_path=root / "db.sqlite", discovery_scripts_path=root, scan_scripts_on_startup=False, manager_log_path=root / "manager.log", integrations_config_path=root / "missing.json")
            def snapshot(_settings): return {"sampled_at": "2026-08-27T00:00:00+00:00", "host": {}, "gpus": [], "docker": {"containers": []}, "ports": [], "collector_errors": []}
            app = create_app(settings, Sampler(settings, collector=snapshot), log_service=failed)
            with TestClient(app, client=("127.0.0.1", 50105), base_url="http://127.0.0.1:19100") as client:
                client.post("/api/v1/auth/setup", json={"username": "admin", "password": "correct horse battery staple"})
                cookie = client.cookies.get("wm_session")
                sent = False
                async def receive():
                    nonlocal sent
                    if not sent:
                        sent = True; return {"type": "http.request", "body": b"", "more_body": False}
                    await asyncio.sleep(60); return {"type": "http.disconnect"}
                async def send(_message): return None
                scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http", "path": "/api/v1/log-sources/failed/entries", "raw_path": b"/api/v1/log-sources/failed/entries", "query_string": b"lines=20&since=1h", "root_path": "", "headers": [(b"host", b"127.0.0.1:19100"), (b"cookie", f"wm_session={cookie}".encode())], "client": ("127.0.0.1", 50106), "server": ("127.0.0.1", 19100), "extensions": {}}
                request_task = asyncio.create_task(app(scope, receive, send))
                await asyncio.to_thread(failed.entered.wait, 2)
                request_task.cancel()
                with self.assertRaises(asyncio.CancelledError): await request_task
                events = app.state.database.list_audit(20)
                matching = [item for item in events if item["event"] == "logs.read" and item["result"] == "failure"]
                self.assertTrue(any(item["summary"].get("reason") == "cancelled_cleanup_failed" and item["summary"].get("cleanup") == "error" and item["summary"].get("cleanup_error_code") == "log_read_failed" for item in matching))

    async def test_cancel_cleanup_timeout_logs_only_timeout(self) -> None:
        class TimeoutLogs:
            def __init__(self) -> None: self.entered = threading.Event(); self.finished = threading.Event()
            def list_sources(self): return {"source": "formal", "sources": [], "blockers": []}
            def entries(self, source_id, lines, since, cancel_event):
                self.entered.set()
                while not cancel_event.is_set(): time.sleep(0.01)
                self.finished.set()
                raise IntegrationError(499, "log_read_cancelled", "日志读取已取消")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); logs = TimeoutLogs()
            settings = Settings(database_path=root / "db.sqlite", discovery_scripts_path=root, scan_scripts_on_startup=False, manager_log_path=root / "manager.log", integrations_config_path=root / "missing.json")
            def snapshot(_settings): return {"sampled_at": "2026-08-27T00:00:00+00:00", "host": {}, "gpus": [], "docker": {"containers": []}, "ports": [], "collector_errors": []}
            app = create_app(settings, Sampler(settings, collector=snapshot), log_service=logs)
            with TestClient(app, client=("127.0.0.1", 50107), base_url="http://127.0.0.1:19100") as client:
                client.post("/api/v1/auth/setup", json={"username": "admin", "password": "correct horse battery staple"})
                cookie = client.cookies.get("wm_session")
                sent = False
                async def receive():
                    nonlocal sent
                    if not sent:
                        sent = True; return {"type": "http.request", "body": b"", "more_body": False}
                    await asyncio.sleep(60); return {"type": "http.disconnect"}
                async def send(_message): return None
                async def immediate_timeout(worker_task):
                    def consume_result(done):
                        try:
                            done.exception()
                        except asyncio.CancelledError:
                            return
                    worker_task.add_done_callback(consume_result)
                    raise asyncio.TimeoutError
                scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http", "path": "/api/v1/log-sources/timeout/entries", "raw_path": b"/api/v1/log-sources/timeout/entries", "query_string": b"lines=20&since=1h", "root_path": "", "headers": [(b"host", b"127.0.0.1:19100"), (b"cookie", f"wm_session={cookie}".encode())], "client": ("127.0.0.1", 50108), "server": ("127.0.0.1", 19100), "extensions": {}}
                request_task = asyncio.create_task(app(scope, receive, send))
                await asyncio.to_thread(logs.entered.wait, 2)
                with patch("workstation_manager.app._await_log_cleanup", new=immediate_timeout), self.assertLogs(app.state.manager_logger.name, level="WARNING") as captured:
                    request_task.cancel()
                    with self.assertRaises(asyncio.CancelledError): await request_task
                joined = "\n".join(captured.output)
                self.assertIn("cleanup timed out", joined)
                self.assertNotIn("cleaned up", joined)
                self.assertTrue(logs.finished.wait(2))
                events = app.state.database.list_audit(20)
                self.assertTrue(any(item["event"] == "logs.read" and item["summary"].get("cleanup") == "timeout" and item["summary"].get("reason") == "cancelled_cleanup_failed" for item in events))


if __name__ == "__main__":
    unittest.main()
