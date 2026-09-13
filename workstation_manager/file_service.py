from __future__ import annotations

import mimetypes
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse


class FileServiceError(ValueError):
    """Raised when a file-service path or filesystem operation is invalid."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class FileCatalog:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=False)

    def _resolve(self, relative_path: str, *, expected: str) -> Path:
        if not isinstance(relative_path, str) or "\x00" in relative_path:
            raise FileServiceError("invalid_file_path", "文件路径无效", 400)
        normalized = relative_path.replace("\\", "/").strip("/")
        try:
            candidate = (self.root / normalized).resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise FileServiceError("invalid_file_path", f"文件路径无效: {exc}", 400) from exc
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise FileServiceError("file_path_outside_root", "禁止访问根目录之外的路径", 403) from exc
        if not candidate.exists():
            raise FileServiceError("file_not_found", "文件或目录不存在", 404)
        if expected == "directory" and not candidate.is_dir():
            raise FileServiceError("not_a_directory", "请求路径不是目录", 400)
        if expected == "file" and not candidate.is_file():
            raise FileServiceError("not_a_file", "请求路径不是文件", 400)
        return candidate

    def list_directory(
        self,
        relative_path: str = "",
        *,
        sort_by: str = "modified",
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        if sort_by not in {"modified", "name", "size"}:
            raise FileServiceError("invalid_file_sort", "文件排序字段无效", 400)
        if sort_order not in {"asc", "desc"}:
            raise FileServiceError("invalid_file_sort_order", "文件排序方向无效", 400)
        directory = self._resolve(relative_path, expected="directory")
        entries: list[dict[str, Any]] = []
        try:
            for child in directory.iterdir():
                if re.fullmatch(r"\.axis-upload-[0-9a-f]{32}\.part", child.name):
                    continue
                try:
                    resolved = child.resolve(strict=False)
                    resolved.relative_to(self.root)
                    stat = child.stat()
                except ValueError as exc:
                    raise FileServiceError(
                        "file_path_outside_root",
                        f"目录项目指向根目录之外: {child.name}",
                        403,
                    ) from exc
                except (OSError, RuntimeError) as exc:
                    raise FileServiceError(
                        "file_metadata_failed",
                        f"无法读取目录项目: {child.name}",
                        500,
                    ) from exc
                is_directory = child.is_dir()
                relative = child.relative_to(self.root).as_posix()
                media_type = None if is_directory else mimetypes.guess_type(child.name)[0]
                entries.append({
                    "name": child.name,
                    "path": relative,
                    "type": "directory" if is_directory else "file",
                    "size": None if is_directory else stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    "media_type": media_type,
                    "playable": bool(media_type and media_type.split("/", 1)[0] in {"audio", "video"}),
                })
            sort_key = {
                "modified": lambda item: item["modified_at"],
                "name": lambda item: item["name"].casefold(),
                "size": lambda item: item["size"] or 0,
            }[sort_by]
            entries.sort(key=sort_key, reverse=sort_order == "desc")
            entries.sort(key=lambda item: item["type"] != "directory")
        except FileServiceError:
            raise
        except PermissionError as exc:
            raise FileServiceError("directory_access_denied", "没有权限读取该目录", 403) from exc
        except OSError as exc:
            raise FileServiceError("directory_read_failed", "读取目录失败", 500) from exc
        current_path = "" if directory == self.root else directory.relative_to(self.root).as_posix()
        parent_path = None if directory == self.root else (
            "" if directory.parent == self.root else directory.parent.relative_to(self.root).as_posix()
        )
        return {
            "path": current_path,
            "parent": parent_path,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "entries": entries,
        }

    def _verify_open_file(self, stream: BinaryIO) -> None:
        opened_path: Path | None = None
        if os.name == "nt":
            import ctypes
            import msvcrt

            buffer = ctypes.create_unicode_buffer(32768)
            length = ctypes.windll.kernel32.GetFinalPathNameByHandleW(
                msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0
            )
            if length == 0 or length >= len(buffer):
                raise FileServiceError("file_path_verification_failed", "无法核验已打开文件的路径", 500)
            final_path = buffer.value
            if final_path.startswith("\\\\?\\UNC\\"):
                final_path = "\\\\" + final_path[8:]
            elif final_path.startswith("\\\\?\\"):
                final_path = final_path[4:]
            opened_path = Path(final_path).resolve(strict=False)
        else:
            descriptor_path = Path(f"/proc/self/fd/{stream.fileno()}")
            if descriptor_path.exists():
                opened_path = Path(os.readlink(descriptor_path)).resolve(strict=False)

        if opened_path is None:
            raise FileServiceError("file_path_verification_failed", "当前系统无法核验已打开文件的路径", 500)
        try:
            opened_path.relative_to(self.root)
        except ValueError as exc:
            raise FileServiceError("file_path_outside_root", "禁止访问根目录之外的路径", 403) from exc

    def begin_upload(self, relative_directory: str, filename: str) -> tuple[Path, Path, BinaryIO]:
        if not isinstance(filename, str) or not filename.strip() or "\x00" in filename \
                or filename in {".", ".."} or "/" in filename or "\\" in filename:
            raise FileServiceError("invalid_upload_name", "上传文件名无效", 400)
        directory = self._resolve(relative_directory, expected="directory")
        target = directory / filename
        try:
            target.resolve(strict=False).relative_to(self.root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise FileServiceError("file_path_outside_root", "禁止上传到根目录之外", 403) from exc
        if os.path.lexists(target):
            raise FileServiceError("upload_file_exists", "同名文件已存在，未覆盖原文件", 409)
        temporary = directory / f".axis-upload-{uuid.uuid4().hex}.part"
        try:
            stream = temporary.open("xb")
            self._verify_open_file(stream)
        except FileServiceError:
            if 'stream' in locals():
                stream.close()
            temporary.unlink(missing_ok=True)
            raise
        except PermissionError as exc:
            temporary.unlink(missing_ok=True)
            raise FileServiceError("upload_access_denied", "没有权限写入该目录", 403) from exc
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise FileServiceError("upload_open_failed", f"无法创建上传文件: {exc}", 500) from exc
        return temporary, target, stream

    def rename_entry(self, relative_path: str, new_name: str) -> dict[str, Any]:
        if not isinstance(new_name, str) or not new_name.strip() or "\x00" in new_name \
                or new_name in {".", ".."} or "/" in new_name or "\\" in new_name \
                or re.fullmatch(r"\.axis-upload-[0-9a-f]{32}\.part", new_name):
            raise FileServiceError("invalid_rename_name", "新名称无效", 400)
        normalized = relative_path.replace("\\", "/").strip("/")
        if not normalized:
            raise FileServiceError("invalid_file_path", "根目录不能更名", 400)
        source = self.root / normalized
        try:
            source.parent.resolve(strict=True).relative_to(self.root)
            source.resolve(strict=False).relative_to(self.root)
        except FileNotFoundError as exc:
            raise FileServiceError("file_not_found", "文件或目录不存在", 404) from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise FileServiceError("file_path_outside_root", "禁止更名根目录之外的项目", 403) from exc
        if not os.path.lexists(source):
            raise FileServiceError("file_not_found", "文件或目录不存在", 404)
        if re.fullmatch(r"\.axis-upload-[0-9a-f]{32}\.part", source.name):
            raise FileServiceError("invalid_file_path", "上传中的临时文件不能更名", 400)
        target = source.with_name(new_name)
        try:
            target.resolve(strict=False).relative_to(self.root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise FileServiceError("file_path_outside_root", "禁止更名到根目录之外", 403) from exc
        if source.name == new_name:
            stat = source.stat()
            return {
                "name": source.name,
                "path": source.relative_to(self.root).as_posix(),
                "type": "directory" if source.is_dir() else "file",
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            }
        try:
            target_exists = os.path.lexists(target)
            same_entry = target_exists and os.path.samefile(source, target)
            if target_exists and not same_entry:
                raise FileServiceError("rename_target_exists", "同名文件或目录已存在，未覆盖", 409)
            source.rename(target)
            stat = target.stat()
        except FileServiceError:
            raise
        except FileExistsError as exc:
            raise FileServiceError("rename_target_exists", "同名文件或目录已存在，未覆盖", 409) from exc
        except PermissionError as exc:
            raise FileServiceError("rename_access_denied", "没有权限更名该文件或目录", 403) from exc
        except OSError as exc:
            raise FileServiceError("rename_failed", f"文件或目录更名失败: {exc}", 500) from exc
        return {
            "name": target.name,
            "path": target.relative_to(self.root).as_posix(),
            "type": "directory" if target.is_dir() else "file",
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        }

    def complete_upload(self, temporary: Path, target: Path, stream: BinaryIO) -> dict[str, Any]:
        try:
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            try:
                target.resolve(strict=False).relative_to(self.root)
            except ValueError as exc:
                raise FileServiceError(
                    "file_path_outside_root", "上传目录在写入期间发生变化", 403,
                ) from exc
            if os.path.lexists(target):
                raise FileServiceError("upload_file_exists", "同名文件已存在，未覆盖原文件", 409)
            os.link(temporary, target, follow_symlinks=False)
            temporary.unlink()
            stat = target.stat()
        except FileServiceError:
            if not stream.closed:
                stream.close()
            temporary.unlink(missing_ok=True)
            raise
        except FileExistsError as exc:
            temporary.unlink(missing_ok=True)
            raise FileServiceError("upload_file_exists", "同名文件已存在，未覆盖原文件", 409) from exc
        except PermissionError as exc:
            temporary.unlink(missing_ok=True)
            raise FileServiceError("upload_access_denied", "没有权限完成文件上传", 403) from exc
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise FileServiceError("upload_write_failed", f"写入上传文件失败: {exc}", 500) from exc
        return {
            "name": target.name,
            "path": target.relative_to(self.root).as_posix(),
            "size": stat.st_size,
        }

    @staticmethod
    def discard_upload(temporary: Path, stream: BinaryIO) -> None:
        if not stream.closed:
            stream.close()
        temporary.unlink(missing_ok=True)

    @staticmethod
    def _parse_range(range_header: str | None, size: int) -> tuple[int, int, bool]:
        if not range_header:
            return 0, size - 1, False
        if not range_header.startswith("bytes=") or "," in range_header:
            raise FileServiceError("invalid_file_range", "Range 请求无效", 416)
        value = range_header[6:].strip()
        if "-" not in value:
            raise FileServiceError("invalid_file_range", "Range 请求无效", 416)
        start_text, end_text = value.split("-", 1)
        try:
            if start_text:
                start = int(start_text)
                end = min(int(end_text), size - 1) if end_text else size - 1
            else:
                suffix_length = int(end_text)
                if suffix_length <= 0:
                    raise ValueError
                start = max(size - suffix_length, 0)
                end = size - 1
        except ValueError as exc:
            raise FileServiceError("invalid_file_range", "Range 请求无效", 416) from exc
        if size <= 0 or start < 0 or start >= size or end < start:
            raise FileServiceError("invalid_file_range", "Range 请求超出文件范围", 416)
        return start, end, True

    def file_response(
        self,
        relative_path: str,
        *,
        download: bool = False,
        range_header: str | None = None,
    ) -> StreamingResponse:
        file_path = self._resolve(relative_path, expected="file")
        try:
            stream = file_path.open("rb")
        except PermissionError as exc:
            raise FileServiceError("file_access_denied", "没有权限读取该文件", 403) from exc
        except OSError as exc:
            raise FileServiceError("file_read_failed", "读取文件失败", 500) from exc
        try:
            self._verify_open_file(stream)
            size = os.fstat(stream.fileno()).st_size
            start, end, partial = self._parse_range(range_header, size)
        except FileServiceError:
            stream.close()
            raise
        except OSError as exc:
            stream.close()
            raise FileServiceError(
                "file_path_verification_failed",
                "无法核验已打开文件的路径",
                500,
            ) from exc

        def chunks() -> Iterator[bytes]:
            remaining = end - start + 1
            try:
                stream.seek(start)
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise OSError("文件在读取期间意外结束")
                    remaining -= len(chunk)
                    yield chunk
            finally:
                stream.close()

        media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        disposition = "attachment" if download else "inline"
        length = end - start + 1
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Disposition": f"{disposition}; filename*=utf-8''{quote(file_path.name)}",
        }
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return StreamingResponse(
            chunks(),
            status_code=206 if partial else 200,
            media_type=media_type,
            headers=headers,
        )


def _error_body(exc: FileServiceError) -> dict[str, Any]:
    return {"error": {"code": exc.code, "message": str(exc)}}


def create_file_service_app(root: Path) -> FastAPI:
    catalog = FileCatalog(root)
    app = FastAPI(title="AXIS HTTP 文件服务", version="1")
    app.state.catalog = catalog

    @app.exception_handler(FileServiceError)
    async def file_service_error_handler(_: Request, exc: FileServiceError) -> JSONResponse:
        return JSONResponse(_error_body(exc), status_code=exc.status_code)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok" if catalog.root.is_dir() else "unavailable",
            "root_available": catalog.root.is_dir(),
        }

    @app.get("/api/v1/files")
    async def list_files(
        path: str = Query(default="", max_length=4096),
        sort_by: str = Query(default="modified", max_length=16),
        sort_order: str = Query(default="desc", max_length=4),
    ) -> dict[str, Any]:
        return catalog.list_directory(path, sort_by=sort_by, sort_order=sort_order)

    @app.get("/api/v1/files/content")
    async def read_file(
        request: Request,
        path: str = Query(min_length=1, max_length=4096),
        download: bool = Query(default=False),
    ) -> StreamingResponse:
        return catalog.file_response(
            path,
            download=download,
            range_header=request.headers.get("range"),
        )

    return app


class FileServiceServer:
    def __init__(self, root: Path, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._server = uvicorn.Server(
            uvicorn.Config(
                create_file_service_app(root),
                host=host,
                port=port,
                access_log=False,
                log_level="warning",
            )
        )
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None

    def _run(self) -> None:
        try:
            self._server.run()
        except BaseException as exc:
            self._failure = exc

    def start(self, timeout_seconds: float = 5.0) -> None:
        if self._thread is not None:
            raise RuntimeError("HTTP 文件服务已经启动")
        self._thread = threading.Thread(target=self._run, name="axis-file-service", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._server.started:
                return
            if not self._thread.is_alive():
                cause = f": {self._failure}" if self._failure else ""
                raise RuntimeError(f"无法在 {self.host}:{self.port} 启动 HTTP 文件服务{cause}")
            time.sleep(0.01)
        self.stop()
        raise RuntimeError(f"HTTP 文件服务在 {self.host}:{self.port} 启动超时")

    def stop(self, timeout_seconds: float = 5.0) -> None:
        thread = self._thread
        if thread is None:
            return
        self._server.should_exit = True
        thread.join(timeout_seconds)
        if thread.is_alive():
            raise RuntimeError("HTTP 文件服务未能在超时时间内停止")
        self._thread = None
