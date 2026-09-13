from __future__ import annotations

import argparse
import os
from pathlib import Path


IMAGE_EXTENSIONS = {".avif", ".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
VIDEO_EXTENSIONS = {".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".webm", ".wmv"}


def resolve_shared_input(name: str, root: Path | None = None, kind: str = "any") -> Path:
    if not name or name in {".", ".."} or "\x00" in name or "/" in name or "\\" in name:
        raise ValueError("shared input must be a file name without directory components")
    shared_root = (root or Path(os.environ.get("WM_FILE_SERVICE_ROOT", "D:/共享/"))).expanduser().resolve(strict=True)
    if not shared_root.is_dir():
        raise NotADirectoryError(f"shared root is not a directory: {shared_root}")
    input_directory = (shared_root / "输入").resolve(strict=True)
    try:
        input_directory.relative_to(shared_root)
    except ValueError as exc:
        raise ValueError("shared input directory resolves outside the shared root") from exc
    if not input_directory.is_dir():
        raise NotADirectoryError(f"shared input directory is not a directory: {input_directory}")
    extensions = VIDEO_EXTENSIONS if kind == "video" else IMAGE_EXTENSIONS if kind == "image" \
        else VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
    requested = Path(name)
    candidates = [
        item for item in input_directory.iterdir()
        if item.is_file() and item.suffix.casefold() in extensions
        and (item.name.casefold() == name.casefold()
             or (not requested.suffix and item.stem.casefold() == name.casefold()))
    ]
    if not candidates:
        raise FileNotFoundError(f"media file was not found in shared input directory: {name}")
    if len(candidates) > 1:
        matches = ", ".join(sorted(item.name for item in candidates))
        raise ValueError(f"shared input name is ambiguous: {name}: {matches}")
    resolved = candidates[0].resolve(strict=True)
    try:
        resolved.relative_to(input_directory)
    except ValueError as exc:
        raise ValueError("shared input file resolves outside the input directory") from exc
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve a named image or video in the AXIS shared input directory")
    parser.add_argument("name", help="file name, or a unique stem when the extension is omitted")
    parser.add_argument("--root", type=Path, help="AXIS file_service_root; defaults to WM_FILE_SERVICE_ROOT or D:/共享/")
    parser.add_argument("--kind", choices=("any", "video", "image"), default="any")
    args = parser.parse_args()
    print(resolve_shared_input(args.name, args.root, args.kind))


if __name__ == "__main__":
    main()
