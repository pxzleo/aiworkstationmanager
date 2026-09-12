import uvicorn

from .app import create_app
from .config import load_settings
from .file_service import FileServiceServer


def main() -> None:
    settings = load_settings()
    app = create_app(settings)
    file_service = FileServiceServer(
        settings.file_service_root, settings.host, settings.file_service_port
    )
    file_service.start()
    try:
        # 关闭默认访问日志，避免完整 query（可能包含上游敏感参数）落盘/终端。
        uvicorn.run(app, host=settings.host, port=settings.port, access_log=False)
    finally:
        file_service.stop()


if __name__ == "__main__":
    main()
