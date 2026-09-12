from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from workstation_manager.file_service import FileCatalog, FileServiceError, create_file_service_app


class FileCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "共享目录"
        self.root.mkdir()
        (self.root / "子目录").mkdir()
        (self.root / "子目录" / "说明.txt").write_text("中文内容", encoding="utf-8")
        (self.root / "影片.mp4").write_bytes(b"0123456789")
        (self.root / "资料.bin").write_bytes(b"binary")
        self.catalog = FileCatalog(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_lists_unicode_root_and_subdirectory_with_media_metadata(self) -> None:
        root = self.catalog.list_directory()
        self.assertEqual(root["path"], "")
        self.assertIsNone(root["parent"])
        self.assertEqual([entry["name"] for entry in root["entries"]], ["子目录", "影片.mp4", "资料.bin"])
        video = root["entries"][1]
        self.assertEqual(video["media_type"], "video/mp4")
        self.assertTrue(video["playable"])
        self.assertEqual(video["size"], 10)

        child = self.catalog.list_directory("子目录")
        self.assertEqual(child["path"], "子目录")
        self.assertEqual(child["parent"], "")
        self.assertEqual(child["entries"][0]["path"], "子目录/说明.txt")

    def test_rejects_path_traversal_and_non_matching_types(self) -> None:
        outside = self.root.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        with self.assertRaisesRegex(FileServiceError, "根目录之外"):
            self.catalog.file_response("../outside.txt")
        with self.assertRaisesRegex(FileServiceError, "不是目录"):
            self.catalog.list_directory("影片.mp4")
        with self.assertRaisesRegex(FileServiceError, "不是文件"):
            self.catalog.file_response("子目录")


class StandaloneFileServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "共享"
        self.root.mkdir()
        (self.root / "中文视频.mp4").write_bytes(b"0123456789")
        self.client = TestClient(create_file_service_app(self.root))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_directory_download_inline_and_range_requests(self) -> None:
        listing = self.client.get("/api/v1/files")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["entries"][0]["name"], "中文视频.mp4")

        inline = self.client.get("/api/v1/files/content", params={"path": "中文视频.mp4"})
        self.assertEqual(inline.status_code, 200)
        self.assertEqual(inline.content, b"0123456789")
        self.assertIn("inline", inline.headers["content-disposition"])

        partial = self.client.get(
            "/api/v1/files/content",
            params={"path": "中文视频.mp4"},
            headers={"Range": "bytes=2-5"},
        )
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, b"2345")
        self.assertEqual(partial.headers["content-range"], "bytes 2-5/10")
        self.assertEqual(partial.headers["accept-ranges"], "bytes")

        suffix = self.client.get(
            "/api/v1/files/content",
            params={"path": "中文视频.mp4"},
            headers={"Range": "bytes=-3"},
        )
        self.assertEqual(suffix.status_code, 206)
        self.assertEqual(suffix.content, b"789")

        invalid_range = self.client.get(
            "/api/v1/files/content",
            params={"path": "中文视频.mp4"},
            headers={"Range": "bytes=20-30"},
        )
        self.assertEqual(invalid_range.status_code, 416)
        self.assertEqual(invalid_range.json()["error"]["code"], "invalid_file_range")

        download = self.client.get(
            "/api/v1/files/content", params={"path": "中文视频.mp4", "download": "true"}
        )
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["content-disposition"])
        self.assertIn("filename*=utf-8", download.headers["content-disposition"].lower())

    def test_missing_root_and_path_traversal_return_structured_errors(self) -> None:
        outside = self.root.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        traversal = self.client.get("/api/v1/files/content", params={"path": "../outside.txt"})
        self.assertEqual(traversal.status_code, 403)
        self.assertEqual(traversal.json()["error"]["code"], "file_path_outside_root")

        missing_client = TestClient(create_file_service_app(self.root / "missing"))
        health = missing_client.get("/health")
        self.assertEqual(health.json(), {"status": "unavailable", "root_available": False})
        listing = missing_client.get("/api/v1/files")
        self.assertEqual(listing.status_code, 404)
        self.assertEqual(listing.json()["error"]["code"], "file_not_found")

    def test_does_not_enable_cross_origin_file_reads(self) -> None:
        response = self.client.get(
            "/api/v1/files",
            headers={"Origin": "https://untrusted.example"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_unreadable_file_returns_structured_error(self) -> None:
        with patch.object(Path, "open", side_effect=PermissionError("locked")):
            response = self.client.get(
                "/api/v1/files/content",
                params={"path": "中文视频.mp4"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "file_access_denied")


if __name__ == "__main__":
    unittest.main()
