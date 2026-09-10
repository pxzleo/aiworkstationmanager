from __future__ import annotations

import subprocess
import unittest

from workstation_manager.portproxy import (
    PortProxySyncError,
    WslPortProxyMapping,
    WslPortProxySynchronizer,
)


class FakeWindowsPortProxy:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.calls: list[list[str]] = []

    def read(self, name: str) -> str | None:
        return self.values.get(name)

    def run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        if arguments[0] == "wsl.exe":
            return subprocess.CompletedProcess(
                arguments, 0, "2: eth0    inet 172.29.43.201/20 scope global eth0\n", ""
            )
        if arguments[0] == "powershell.exe":
            return subprocess.CompletedProcess(arguments, 0, "", "")
        operation = arguments[3]
        listen_address = arguments[5].split("=", 1)[1]
        listen_port = arguments[6].split("=", 1)[1]
        if operation == "delete":
            self.values.pop(f"{listen_address}/{listen_port}", None)
            return subprocess.CompletedProcess(arguments, 0, "", "")
        connect_address = arguments[7].split("=", 1)[1]
        connect_port = arguments[8].split("=", 1)[1]
        self.values[f"{listen_address}/{listen_port}"] = f"{connect_address}/{connect_port}"
        return subprocess.CompletedProcess(arguments, 0, "", "")


class PortProxySynchronizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.windows = FakeWindowsPortProxy()
        self.synchronizer = WslPortProxySynchronizer(self.windows.run, self.windows.read)
        self.mapping = WslPortProxyMapping(
            "service", "小智核心服务", "Ubuntu-22.04", "0.0.0.0", 18000, 18000
        )

    def test_adds_missing_mapping_to_current_wsl_address(self) -> None:
        self.synchronizer.sync(self.mapping)

        self.assertEqual(self.windows.values["0.0.0.0/18000"], "172.29.43.201/18000")
        netsh_call = next(call for call in self.windows.calls if call[0] == "netsh.exe")
        self.assertEqual(netsh_call[3], "add")

    def test_updates_stale_private_mapping_without_delete_gap(self) -> None:
        self.windows.values["0.0.0.0/18000"] = "192.168.236.243/18000"
        mapping = WslPortProxyMapping(
            "service", "小智核心服务", "Ubuntu-22.04", "0.0.0.0", 18000, 18000,
            "192.168.236.243",
        )

        self.synchronizer.sync(mapping)

        self.assertEqual(self.windows.values["0.0.0.0/18000"], "172.29.43.201/18000")
        netsh_call = next(call for call in self.windows.calls if call[0] == "netsh.exe")
        self.assertEqual(netsh_call[3], "set")

    def test_refuses_unowned_private_mapping_even_when_target_port_matches(self) -> None:
        self.windows.values["0.0.0.0/18000"] = "192.168.236.243/18000"

        with self.assertRaisesRegex(PortProxySyncError, "拒绝覆盖"):
            self.synchronizer.sync(self.mapping)

        self.assertEqual(self.windows.values["0.0.0.0/18000"], "192.168.236.243/18000")

    def test_refuses_to_overwrite_mapping_with_different_target_port(self) -> None:
        self.windows.values["0.0.0.0/18000"] = "192.168.236.243/22"

        with self.assertRaisesRegex(PortProxySyncError, "拒绝覆盖"):
            self.synchronizer.sync(self.mapping)

        self.assertEqual(self.windows.values["0.0.0.0/18000"], "192.168.236.243/22")

    def test_sync_services_reads_each_distro_address_once(self) -> None:
        services = [
            {
                "id": "first", "name": "ASR", "wsl_portproxy_enabled": 1,
                "wsl_distro": "Ubuntu-22.04", "wsl_listen_address": "0.0.0.0",
                "wsl_listen_port": 18090, "wsl_connect_port": 18090,
            },
            {
                "id": "second", "name": "TTS", "wsl_portproxy_enabled": 1,
                "wsl_distro": "Ubuntu-22.04", "wsl_listen_address": "0.0.0.0",
                "wsl_listen_port": 6006, "wsl_connect_port": 6006,
            },
        ]

        errors, synced = self.synchronizer.sync_services(services)
        self.assertEqual(errors, {})
        self.assertEqual(synced, {"first": "172.29.43.201", "second": "172.29.43.201"})
        self.assertEqual(sum(call[0] == "wsl.exe" for call in self.windows.calls), 1)

    def test_conflicting_registrations_fail_before_any_mapping_is_changed(self) -> None:
        services = [
            {
                "id": "first", "name": "A", "wsl_portproxy_enabled": 1,
                "wsl_distro": "Ubuntu-22.04", "wsl_listen_address": "0.0.0.0",
                "wsl_listen_port": 8080, "wsl_connect_port": 8080,
            },
            {
                "id": "second", "name": "B", "wsl_portproxy_enabled": 1,
                "wsl_distro": "Ubuntu-22.04", "wsl_listen_address": "0.0.0.0",
                "wsl_listen_port": 8080, "wsl_connect_port": 8081,
            },
        ]

        errors, synced = self.synchronizer.sync_services(services)

        self.assertEqual(set(errors), {"first", "second"})
        self.assertEqual(synced, {})
        self.assertEqual(self.windows.calls, [])

    def test_fails_when_ip_helper_does_not_own_the_listener(self) -> None:
        original_run = self.windows.run

        def fail_listener(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            if arguments[0] == "powershell.exe" and "Get-NetTCPConnection" in arguments[-1]:
                return subprocess.CompletedProcess(arguments, 3, "", "")
            return original_run(arguments)

        synchronizer = WslPortProxySynchronizer(fail_listener, self.windows.read)

        with self.assertRaisesRegex(PortProxySyncError, "未实际监听"):
            synchronizer.sync(self.mapping)

    def test_recreates_owned_current_mapping_with_missing_listener(self) -> None:
        mapping = WslPortProxyMapping(
            "service", "NInfer", "Ubuntu-22.04", "192.168.100.190", 8081, 8081,
            "172.29.43.201",
        )
        self.windows.values[mapping.registry_name] = "172.29.43.201/8081"
        original = self.windows.run
        repaired = False

        def run(arguments):
            nonlocal repaired
            if arguments[0] == "powershell.exe" and "Get-NetTCPConnection" in arguments[-1]:
                absent = "if($listener){exit 3}" in arguments[-1]
                return subprocess.CompletedProcess(arguments, 0 if absent or repaired else 3, "", "")
            if arguments[0] == "netsh.exe" and arguments[3] == "add":
                repaired = True
            return original(arguments)

        sync = WslPortProxySynchronizer(run, self.windows.read)
        self.assertEqual(sync.sync(mapping), "172.29.43.201")
        self.assertEqual([c[3] for c in self.windows.calls if c[0] == "netsh.exe"], ["delete", "add"])
        self.assertEqual(self.windows.values[mapping.registry_name], "172.29.43.201/8081")

    def test_current_mapping_with_missing_listener_requires_ownership(self) -> None:
        self.windows.values[self.mapping.registry_name] = "172.29.43.201/18000"
        original = self.windows.run
        def run(arguments):
            if arguments[0] == "powershell.exe" and "Get-NetTCPConnection" in arguments[-1]:
                return subprocess.CompletedProcess(arguments, 3, "", "")
            return original(arguments)
        with self.assertRaisesRegex(PortProxySyncError, "未知转发"):
            WslPortProxySynchronizer(run, self.windows.read).sync(self.mapping)
        self.assertFalse(any(c[0] == "netsh.exe" for c in self.windows.calls))

    def test_unknown_listener_is_not_repaired(self) -> None:
        self.windows.values[self.mapping.registry_name] = "172.29.43.201/18000"
        original = self.windows.run
        def run(arguments):
            if arguments[0] == "powershell.exe" and "Get-NetTCPConnection" in arguments[-1]:
                return subprocess.CompletedProcess(arguments, 4, "", "")
            return original(arguments)
        with self.assertRaisesRegex(PortProxySyncError, "未知进程"):
            WslPortProxySynchronizer(run, self.windows.read).sync(self.mapping)
        self.assertFalse(any(c[0] == "netsh.exe" for c in self.windows.calls))

    def test_healthy_current_mapping_is_not_rewritten(self) -> None:
        self.windows.values[self.mapping.registry_name] = "172.29.43.201/18000"
        self.synchronizer.sync(self.mapping)
        self.assertFalse(any(c[0] == "netsh.exe" for c in self.windows.calls))

    def test_unknown_listener_blocks_missing_or_stale_mapping_before_write(self) -> None:
        for actual in (None, "172.29.43.200/8081"):
            with self.subTest(actual=actual):
                windows = FakeWindowsPortProxy()
                mapping = WslPortProxyMapping(
                    "service", "NInfer", "Ubuntu-22.04", "192.168.100.190", 8081, 8081,
                    "172.29.43.200",
                )
                if actual:
                    windows.values[mapping.registry_name] = actual
                def run(arguments):
                    if arguments[0] == "powershell.exe" and "Get-NetTCPConnection" in arguments[-1]:
                        return subprocess.CompletedProcess(arguments, 4, "", "")
                    return windows.run(arguments)
                with self.assertRaisesRegex(PortProxySyncError, "未知进程"):
                    WslPortProxySynchronizer(run, windows.read).sync(mapping)
                self.assertFalse(any(c[0] == "netsh.exe" for c in windows.calls))

    def test_owned_listener_still_missing_after_repair_fails_without_retry_loop(self) -> None:
        mapping = WslPortProxyMapping(
            "service", "NInfer", "Ubuntu-22.04", "192.168.100.190", 8081, 8081,
            "172.29.43.201",
        )
        self.windows.values[mapping.registry_name] = "172.29.43.201/8081"
        original = self.windows.run
        def run(arguments):
            if arguments[0] == "powershell.exe" and "Get-NetTCPConnection" in arguments[-1]:
                absent = "if($listener){exit 3}" in arguments[-1]
                return subprocess.CompletedProcess(arguments, 0 if absent else 3, "", "")
            return original(arguments)
        with self.assertRaisesRegex(PortProxySyncError, "未实际监听"):
            WslPortProxySynchronizer(run, self.windows.read).sync(mapping)
        self.assertEqual([c[3] for c in self.windows.calls if c[0] == "netsh.exe"], ["delete", "add"])

    def test_wildcard_and_specific_address_conflict_before_any_change(self) -> None:
        services = [
            {
                "id": "wildcard", "name": "A", "wsl_portproxy_enabled": 1,
                "wsl_distro": "Ubuntu-22.04", "wsl_listen_address": "0.0.0.0",
                "wsl_listen_port": 8080, "wsl_connect_port": 8080,
            },
            {
                "id": "specific", "name": "B", "wsl_portproxy_enabled": 1,
                "wsl_distro": "Ubuntu-22.04", "wsl_listen_address": "192.168.100.190",
                "wsl_listen_port": 8080, "wsl_connect_port": 8080,
            },
        ]

        errors, synced = self.synchronizer.sync_services(services)

        self.assertEqual(set(errors), {"wildcard", "specific"})
        self.assertEqual(synced, {})
        self.assertEqual(self.windows.calls, [])

    def test_removes_only_mapping_matching_recorded_owned_target(self) -> None:
        self.windows.values["0.0.0.0/18000"] = "192.168.236.243/18000"
        mapping = WslPortProxyMapping(
            "service", "小智核心服务", "Ubuntu-22.04", "0.0.0.0", 18000, 18000,
            "192.168.236.243",
        )

        self.synchronizer.remove_owned(mapping)

        self.assertNotIn("0.0.0.0/18000", self.windows.values)
        self.assertTrue(any(call[:4] == ["netsh.exe", "interface", "portproxy", "delete"]
                            for call in self.windows.calls))

    def test_refuses_to_remove_unknown_mapping(self) -> None:
        self.windows.values["0.0.0.0/18000"] = "192.168.236.244/18000"
        mapping = WslPortProxyMapping(
            "service", "小智核心服务", "Ubuntu-22.04", "0.0.0.0", 18000, 18000,
            "192.168.236.243",
        )

        with self.assertRaisesRegex(PortProxySyncError, "拒绝覆盖"):
            self.synchronizer.remove_owned(mapping)

        self.assertEqual(self.windows.values["0.0.0.0/18000"], "192.168.236.244/18000")

    def test_restores_mapping_when_listener_disappearance_check_fails(self) -> None:
        self.windows.values["0.0.0.0/18000"] = "192.168.236.243/18000"
        mapping = WslPortProxyMapping(
            "service", "小智核心服务", "Ubuntu-22.04", "0.0.0.0", 18000, 18000,
            "192.168.236.243",
        )
        original_run = self.windows.run

        def fail_absence_check(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            if (arguments[0] == "powershell.exe" and "Get-NetTCPConnection" in arguments[-1]
                    and "if($listener){exit 3}" in arguments[-1]):
                return subprocess.CompletedProcess(arguments, 3, "", "")
            return original_run(arguments)

        synchronizer = WslPortProxySynchronizer(fail_absence_check, self.windows.read)

        with self.assertRaisesRegex(PortProxySyncError, "已恢复旧映射"):
            synchronizer.remove_owned(mapping)

        self.assertEqual(self.windows.values["0.0.0.0/18000"], "192.168.236.243/18000")


if __name__ == "__main__":
    unittest.main()
