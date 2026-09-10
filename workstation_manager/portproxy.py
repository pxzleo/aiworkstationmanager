from __future__ import annotations

import ipaddress
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Iterable


PORTPROXY_REGISTRY_PATH = r"SYSTEM\CurrentControlSet\Services\PortProxy\v4tov4\tcp"
WSL_IPV4_RE = re.compile(r"\binet\s+(\d{1,3}(?:\.\d{1,3}){3})/")


class PortProxySyncError(RuntimeError):
    """Raised when an explicitly managed WSL portproxy cannot be synchronized."""


class PortProxyListenerMissing(PortProxySyncError):
    """The port is free but its configured proxy listener is missing."""


@dataclass(frozen=True)
class WslPortProxyMapping:
    service_id: str
    service_name: str
    distro: str
    listen_address: str
    listen_port: int
    connect_port: int
    last_address: str | None = None

    @property
    def registry_name(self) -> str:
        return f"{self.listen_address}/{self.listen_port}"


def mapping_from_service(service: dict[str, Any]) -> WslPortProxyMapping | None:
    if not bool(service.get("wsl_portproxy_enabled")):
        return None
    return WslPortProxyMapping(
        service_id=str(service["id"]),
        service_name=str(service["name"]),
        distro=str(service["wsl_distro"]),
        listen_address=str(service["wsl_listen_address"]),
        listen_port=int(service["wsl_listen_port"]),
        connect_port=int(service["wsl_connect_port"]),
        last_address=str(service["wsl_last_address"]) if service.get("wsl_last_address") else None,
    )


class WslPortProxySynchronizer:
    def __init__(
        self,
        command_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
        mapping_reader: Callable[[str], str | None] | None = None,
    ) -> None:
        self._command_runner = command_runner or self._run_command
        self._mapping_reader = mapping_reader or self._read_mapping

    @staticmethod
    def _run_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    @staticmethod
    def _read_mapping(name: str) -> str | None:
        if os.name != "nt":
            raise PortProxySyncError("WSL 端口转发同步只支持 Windows")
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, PORTPROXY_REGISTRY_PATH) as key:
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    return None
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PortProxySyncError(f"无法读取 Windows 端口转发 {name}: {exc}") from exc
        return str(value)

    def _wsl_ipv4(self, distro: str) -> str:
        try:
            result = self._command_runner([
                "wsl.exe", "-d", distro, "--", "bash", "-lc",
                "ip -4 -o addr show dev eth0 scope global",
            ])
        except (OSError, subprocess.SubprocessError) as exc:
            raise PortProxySyncError(f"无法读取 {distro} 当前 IPv4 地址: {exc}") from exc
        match = WSL_IPV4_RE.search(result.stdout)
        if result.returncode != 0 or match is None:
            detail = (result.stderr or result.stdout).strip()
            suffix = f": {detail}" if detail else ""
            raise PortProxySyncError(f"无法读取 {distro} 当前 IPv4 地址{suffix}")
        address = match.group(1)
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise PortProxySyncError(f"{distro} 返回了无效 IPv4 地址") from exc
        if parsed.version != 4 or not parsed.is_private:
            raise PortProxySyncError(f"{distro} 返回的 IPv4 地址不属于私有网络")
        return address

    @staticmethod
    def _validate_existing_target(mapping: WslPortProxyMapping, actual: str) -> None:
        owned_target = (
            f"{mapping.last_address}/{mapping.connect_port}"
            if mapping.last_address else None
        )
        if actual != owned_target:
            raise PortProxySyncError(
                f"{mapping.service_name} 的监听端口已有未知转发，拒绝覆盖: "
                f"{mapping.registry_name} -> {actual}"
            )

    def _ensure_ip_helper(self) -> None:
        script = (
            "$ErrorActionPreference='Stop';"
            "$service=Get-Service -Name iphlpsvc;"
            "if($service.Status -ne 'Running'){Start-Service -Name iphlpsvc;"
            "$service.WaitForStatus('Running',[TimeSpan]::FromSeconds(5))}"
        )
        try:
            result = self._command_runner([
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script,
            ])
        except (OSError, subprocess.SubprocessError) as exc:
            raise PortProxySyncError(f"无法启动 Windows IP Helper 服务: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            suffix = f": {detail}" if detail else ""
            raise PortProxySyncError(f"Windows IP Helper 服务不可用{suffix}")

    def _verify_listener(self, mapping: WslPortProxyMapping, should_exist: bool = True) -> None:
        script = (
            "$ErrorActionPreference='Stop';"
            "$service=Get-CimInstance Win32_Service -Filter \"Name='iphlpsvc'\";"
            "if($service.State -ne 'Running' -or $service.ProcessId -le 0){exit 2};"
            "$deadline=(Get-Date).AddSeconds(5);"
            "if(Get-NetTCPConnection -State Listen "
            f"-LocalPort {mapping.listen_port} -ErrorAction SilentlyContinue|Where-Object {{"
            f"$_.LocalAddress -ne '{mapping.listen_address}' -and ("
            f"'{mapping.listen_address}' -eq '0.0.0.0' -or $_.LocalAddress -in @('0.0.0.0','::'))"
            "}){exit 4};"
            "do{$listener=Get-NetTCPConnection -State Listen "
            f"-LocalAddress '{mapping.listen_address}' -LocalPort {mapping.listen_port} "
            "-ErrorAction SilentlyContinue;"
            "if($listener|Where-Object OwningProcess -ne $service.ProcessId){exit 4};"
            f"if($listener){{exit {0 if should_exist else 3}}};"
            "Start-Sleep -Milliseconds 200}while((Get-Date)-lt $deadline);"
            f"exit {3 if should_exist else 0}"
        )
        try:
            result = self._command_runner([
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script,
            ])
        except (OSError, subprocess.SubprocessError) as exc:
            raise PortProxySyncError(
                f"无法校验 {mapping.service_name} 的 Windows 监听端口: {exc}"
            ) from exc
        if result.returncode != 0:
            if result.returncode == 4:
                raise PortProxySyncError(
                    f"{mapping.service_name} 的监听端口被未知进程占用，拒绝修改"
                )
            state = "未实际监听" if should_exist else "仍在监听"
            error_type = (PortProxyListenerMissing
                          if should_exist and result.returncode == 3 else PortProxySyncError)
            raise error_type(
                f"{mapping.service_name} 的 Windows 端口转发 IP Helper {state} "
                f"{mapping.listen_address}:{mapping.listen_port}"
            )

    def remove_owned(self, mapping: WslPortProxyMapping) -> bool:
        self._ensure_ip_helper()
        actual = self._mapping_reader(mapping.registry_name)
        if actual is None:
            self._verify_listener(mapping, should_exist=False)
            return False
        self._validate_existing_target(mapping, actual)
        arguments = [
            "netsh.exe", "interface", "portproxy", "delete", "v4tov4",
            f"listenaddress={mapping.listen_address}",
            f"listenport={mapping.listen_port}",
        ]
        try:
            result = self._command_runner(arguments)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PortProxySyncError(
                f"无法清理 {mapping.service_name} 的 Windows 端口转发: {exc}"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            suffix = f": {detail}" if detail else ""
            raise PortProxySyncError(
                f"清理 {mapping.service_name} 的 Windows 端口转发失败{suffix}"
            )
        if self._mapping_reader(mapping.registry_name) is not None:
            raise PortProxySyncError(f"{mapping.service_name} 的 Windows 端口转发清理校验失败")
        try:
            self._verify_listener(mapping, should_exist=False)
        except PortProxySyncError as verify_exc:
            try:
                self.restore_owned(mapping)
            except PortProxySyncError as restore_exc:
                raise PortProxySyncError(
                    f"{verify_exc}; 旧映射恢复失败: {restore_exc}"
                ) from verify_exc
            raise PortProxySyncError(f"{verify_exc}; 已恢复旧映射") from verify_exc
        return True

    def restore_owned(self, mapping: WslPortProxyMapping) -> None:
        if not mapping.last_address:
            raise PortProxySyncError(f"{mapping.service_name} 没有可恢复的旧映射目标")
        self._ensure_ip_helper()
        expected = f"{mapping.last_address}/{mapping.connect_port}"
        actual = self._mapping_reader(mapping.registry_name)
        if actual == expected:
            self.sync(mapping, mapping.last_address)
            return
        if actual is not None:
            raise PortProxySyncError(
                f"{mapping.service_name} 的监听端口已被其他映射占用，无法恢复"
            )
        arguments = [
            "netsh.exe", "interface", "portproxy", "add", "v4tov4",
            f"listenaddress={mapping.listen_address}",
            f"listenport={mapping.listen_port}",
            f"connectaddress={mapping.last_address}",
            f"connectport={mapping.connect_port}",
        ]
        try:
            result = self._command_runner(arguments)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PortProxySyncError(
                f"恢复 {mapping.service_name} 的旧端口转发失败: {exc}"
            ) from exc
        if result.returncode != 0 or self._mapping_reader(mapping.registry_name) != expected:
            detail = (result.stderr or result.stdout).strip()
            suffix = f": {detail}" if detail else ""
            raise PortProxySyncError(f"恢复 {mapping.service_name} 的旧端口转发失败{suffix}")
        self._verify_listener(mapping)

    def sync(self, mapping: WslPortProxyMapping, wsl_address: str | None = None) -> str:
        address = wsl_address or self._wsl_ipv4(mapping.distro)
        expected = f"{address}/{mapping.connect_port}"
        self._ensure_ip_helper()
        actual = self._mapping_reader(mapping.registry_name)
        if actual == expected:
            try:
                self._verify_listener(mapping)
                return address
            except PortProxyListenerMissing:
                self._validate_existing_target(mapping, actual)
                # Recreate only this owned, non-listening rule; never restart IP Helper.
                self.remove_owned(mapping)
                actual = None
        operation = "add"
        if actual is not None:
            self._validate_existing_target(mapping, actual)
            operation = "set"
        try:
            self._verify_listener(mapping)
        except PortProxyListenerMissing:
            pass  # An unused port is expected before creation; conflicts still raise.
        arguments = [
            "netsh.exe", "interface", "portproxy", operation, "v4tov4",
            f"listenaddress={mapping.listen_address}",
            f"listenport={mapping.listen_port}",
            f"connectaddress={address}",
            f"connectport={mapping.connect_port}",
        ]
        try:
            result = self._command_runner(arguments)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PortProxySyncError(
                f"无法同步 {mapping.service_name} 的 Windows 端口转发: {exc}"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            suffix = f": {detail}" if detail else ""
            raise PortProxySyncError(
                f"同步 {mapping.service_name} 的 Windows 端口转发失败{suffix}"
            )
        verified = self._mapping_reader(mapping.registry_name)
        if verified != expected:
            raise PortProxySyncError(
                f"{mapping.service_name} 的 Windows 端口转发校验失败: "
                f"期望 {mapping.registry_name} -> {expected}，实际 {verified or '不存在'}"
            )
        self._verify_listener(mapping)
        return address

    def sync_services(
        self, services: Iterable[dict[str, Any]]
    ) -> tuple[dict[str, str], dict[str, str]]:
        mappings = [mapping for service in services if (mapping := mapping_from_service(service))]
        errors: dict[str, str] = {}
        addresses: dict[str, str] = {}
        synced_addresses: dict[str, str] = {}
        owners: dict[tuple[str, int], list[WslPortProxyMapping]] = {}
        for mapping in mappings:
            key = (mapping.listen_address, mapping.listen_port)
            owners.setdefault(key, []).append(mapping)
        by_port: dict[int, list[WslPortProxyMapping]] = {}
        for mapping in mappings:
            by_port.setdefault(mapping.listen_port, []).append(mapping)
        for port_mappings in by_port.values():
            listen_addresses = {item.listen_address for item in port_mappings}
            if "0.0.0.0" in listen_addresses and len(listen_addresses) > 1:
                message = f"局域网监听端口 {port_mappings[0].listen_port} 的通配地址与具体地址冲突"
                for item in port_mappings:
                    errors[item.service_id] = message
        for group in owners.values():
            mapping = group[0]
            if mapping.service_id in errors:
                continue
            if len(group) > 1:
                message = (
                    f"局域网监听 {mapping.listen_address}:{mapping.listen_port} "
                    f"被多个服务重复登记"
                )
                for item in group:
                    errors[item.service_id] = message
                continue
            try:
                address = addresses.get(mapping.distro)
                if address is None:
                    address = self._wsl_ipv4(mapping.distro)
                    addresses[mapping.distro] = address
                synced_address = self.sync(mapping, address)
                for item in group:
                    synced_addresses[item.service_id] = synced_address
            except PortProxySyncError as exc:
                for item in group:
                    errors[item.service_id] = str(exc)
        return errors, synced_addresses
