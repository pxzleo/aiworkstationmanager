from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import threading

import uvicorn

from workstation_manager.app import create_app
from workstation_manager.config import Settings
from workstation_manager.history import Sampler
from workstation_manager.integrations import (
    BackendProbeConfig, IntegrationsConfig, LogService, LogSourceConfig, WebUIConfig, WebUIService,
)


class MockWebUIHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/health-ui"):
            body, content_type = b"ok", "text/plain"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/backend-health"):
            body, content_type = b'{"status":"offline"}', "application/json"
            self.send_response(503)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/":
            self.send_response(302)
            self.send_header("Location", "/nested/page/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/assets/relative.css"):
            body, content_type = b"body{font-family:sans-serif;background:rgb(16,20,21);color:rgb(238,244,242)}", "text/css"
        elif self.path.startswith("/assets/root.css"):
            body, content_type = b"h1{color:rgb(238,244,242)}", "text/css"
        elif self.path.startswith("/assets/mock.js"):
            body = b"window.mockStaticLoaded=true;"
            content_type = "application/javascript"
        else:
            body = b"<!doctype html><html><head><base href='/'><link rel='stylesheet' href='assets/relative.css?v=1'><link rel='stylesheet' href='/assets/root.css?v=1'><script src='/assets/mock.js' defer></script></head><body><h1>Mock NInfer WebUI</h1><p>isolated read-only proxy accepted</p></body></html>"
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def snapshot(_settings: Settings) -> dict:
    return {
        "sampled_at": "2026-08-27T12:00:00+00:00",
        "host": {"hostname": "acceptance-mock", "cpu": {"load_percent": 12}, "memory": {"percent": 34}, "disks": []},
        "gpus": [], "docker": {"containers": []}, "ports": [], "collector_errors": [],
    }


def mock_runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args, 0,
        "2026-08-27T12:00:00Z manager mock log ready\nAuthorization: Bearer acceptance-secret\n",
        "",
    )


def main() -> None:
    root = Path(os.environ["WM_ACCEPTANCE_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    upstream = ThreadingHTTPServer(("127.0.0.1", 19101), MockWebUIHandler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    config = IntegrationsConfig(
        source="formal", blockers=(),
        webuis=(WebUIConfig("ninfer-4090", "NInfer 4090 Mock", "ninfer", True, "http://127.0.0.1:19101", "/health-ui", BackendProbeConfig("http://127.0.0.1:19101/backend-health", 1.0)),),
        log_sources=(LogSourceConfig("ninfer-mock", "NInfer Mock 日志", "docker_logs", True, container="never-executed"),),
    )
    settings = Settings(
        host="127.0.0.1", port=19100, database_path=root / "acceptance.db",
        discovery_scripts_path=root / "scripts", scan_scripts_on_startup=False,
        control_config_path=root / "missing-control.json",
        integrations_config_path=root / "mock-integrations.json",
        manager_log_path=root / "manager.log", sample_interval_seconds=60,
    )
    webuis = WebUIService(config)
    logs = LogService(config, settings.manager_log_path, runner=mock_runner)
    try:
        uvicorn.run(
            create_app(settings, Sampler(settings, collector=snapshot), webui_service=webuis, log_service=logs),
            host=settings.host, port=settings.port, access_log=False,
        )
    finally:
        upstream.shutdown()
        upstream.server_close()


if __name__ == "__main__":
    main()
