import argparse
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
import subprocess
from typing import Any

from resolve_shared_input import resolve_shared_input


REQUIRED_NODES = {
    "92": "SaveVideo",
    "124": "BasicScheduler",
    "126": "BasicGuider",
    "129": "RandomNoise",
    "136": "MiniMaxH3ReferenceToVideo",
    "145": "LoraLoaderModelOnly",
    "150": "VHS_LoadVideoPath",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Derive one H3 ref2v API graph from a bundled 4-step or 8-step baseline",
    )
    parser.add_argument("--baseline", required=True, help="baseline API graph JSON")
    parser.add_argument("--out", required=True, help="output API graph JSON")
    parser.add_argument("--source", required=True, help="existing source video path")
    parser.add_argument(
        "--shared-root", type=Path,
        help="AXIS file_service_root used when --source contains only a file name",
    )
    parser.add_argument(
        "--detection-report", required=True,
        help="current source_detection_report.json produced from isolated visual agents",
    )
    parser.add_argument("--task-id", required=True, help="unique current video task ID")
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument(
        "--length", type=int, required=True,
        help="generation frame count at 24 fps; must satisfy 17n+5",
    )
    parser.add_argument("--prompt-file", required=True, help="existing UTF-8 prompt text file")
    parser.add_argument("--prefix", required=True, help="SaveVideo filename_prefix")
    parser.add_argument("--seed", type=int, default=None, help="noise_seed; omit to keep baseline")
    parser.add_argument(
        "--skip-frames", type=int, default=0,
        help="source frame index at 24 fps; use with --length for one segment",
    )
    return parser.parse_args()


def require_file(value: str, label: str) -> Path:
    path = Path(value)
    if not path.is_file():
        raise FileNotFoundError(f"{label} does not exist or is not a file: {path}")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def detect_image(path: Path) -> tuple[str, int, int]:
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height", "-of", "json", str(path),
        ],
        check=False, capture_output=True, text=True,
    )
    if probe.returncode:
        raise ValueError(f"screenshot cannot be decoded by ffprobe: {path}: {probe.stderr.strip()}")
    streams = json.loads(probe.stdout).get("streams", [])
    if len(streams) != 1:
        raise ValueError(f"screenshot must contain exactly one image stream: {path}")
    stream = streams[0]
    codec = stream.get("codec_name")
    image_type = {"mjpeg": "jpeg", "png": "png", "webp": "webp"}.get(codec)
    width, height = stream.get("width"), stream.get("height")
    if not image_type or not isinstance(width, int) or not isinstance(height, int) \
            or width <= 0 or height <= 0:
        raise ValueError(f"screenshot must be a decodable PNG, JPEG, or WebP image: {path}")
    decode = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-f", "null", "-"],
        check=False, capture_output=True, text=True,
    )
    if decode.returncode:
        raise ValueError(f"screenshot full decode failed: {path}: {decode.stderr.strip()}")
    return image_type, width, height


def validate_args(args: argparse.Namespace) -> None:
    if args.width <= 0 or args.height <= 0:
        raise ValueError("width and height must be positive")
    if args.width % 16 or args.height % 16:
        raise ValueError("width and height must be multiples of 16")
    if args.length < 5 or (args.length - 5) % 17:
        raise ValueError("length must satisfy the H3 17n+5 frame grid")
    if args.skip_frames < 0:
        raise ValueError("skip-frames must be zero or greater")
    if not args.prefix.strip():
        raise ValueError("prefix must not be empty")


def load_graph(path: Path) -> dict[str, Any]:
    graph = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(graph, dict):
        raise ValueError("baseline root must be a JSON object")
    for node_id, expected_class in REQUIRED_NODES.items():
        node = graph.get(node_id)
        if not isinstance(node, dict) or node.get("class_type") != expected_class \
                or not isinstance(node.get("inputs"), dict):
            raise ValueError(
                f"baseline node {node_id} must be {expected_class} with an inputs object",
            )
    class_types = [
        str(node.get("class_type") or "")
        for node in graph.values() if isinstance(node, dict)
    ]
    h3_conditioning = sum(
        class_type.startswith("MiniMaxH3") and class_type.endswith("ToVideo")
        for class_type in class_types
    )
    h3_native_samplers = sum(
        class_type.startswith("MiniMaxH3") and "Sampler" in class_type
        for class_type in class_types
    )
    advanced_samplers = class_types.count("SamplerCustomAdvanced")
    if h3_conditioning != 1 or max(h3_native_samplers, advanced_samplers) > 1:
        raise ValueError("baseline must contain exactly one H3 generation branch")
    if graph["124"]["inputs"].get("steps") == 8:
        sigma_shift = graph.get("151")
        if not isinstance(sigma_shift, dict) \
                or sigma_shift.get("class_type") != "MiniMaxH3SigmaShift" \
                or not isinstance(sigma_shift.get("inputs"), dict):
            raise ValueError(
                "8-step baseline node 151 must be MiniMaxH3SigmaShift "
                "with an inputs object",
            )
        if "152" in graph \
                or sigma_shift["inputs"].get("model") != ["145", 0] \
                or graph["124"]["inputs"].get("model") != ["151", 0] \
                or graph["126"]["inputs"].get("model") != ["151", 0]:
            raise ValueError(
                "8-step baseline must connect LoRA directly to Sigma Shift, "
                "then to scheduler and guider, without SageAttention",
            )
    return graph


def validate_detection_report(
    path: Path, source: Path, prompt_file: Path, task_id: str,
) -> dict[str, Any]:
    if path.name != "source_detection_report.json":
        raise ValueError("detection-report filename must be source_detection_report.json")
    report = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(report, dict):
        raise ValueError("detection-report root must be a JSON object")
    if report.get("task_id") != task_id:
        raise ValueError("detection-report task_id must match --task-id")
    created_at = report.get("created_at")
    if not isinstance(created_at, str):
        raise ValueError("detection-report created_at must be an ISO 8601 timestamp")
    try:
        parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            "detection-report created_at must be an ISO 8601 timestamp",
        ) from error
    if parsed_created_at.tzinfo is None:
        raise ValueError("detection-report created_at must include a timezone")
    report_source = report.get("source")
    if not isinstance(report_source, dict):
        raise ValueError("detection-report source must be an object")
    source_path = report_source.get("path")
    if not isinstance(source_path, str) or not Path(source_path).is_absolute():
        raise ValueError("detection-report source.path must be an absolute path")
    if Path(source_path).resolve() != source.resolve():
        raise ValueError("detection-report source must match --source")
    if report_source.get("sha256") != sha256_file(source):
        raise ValueError("detection-report source sha256 does not match --source")
    if report.get("prompt_sha256") != sha256_file(prompt_file):
        raise ValueError("detection-report prompt_sha256 does not match --prompt-file")
    frames = report.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("detection-report frames must be a non-empty array")
    agent_ids: set[str] = set()
    frame_paths: set[Path] = set()
    for index, frame in enumerate(frames, start=1):
        if not isinstance(frame, dict):
            raise ValueError(f"detection-report frame {index} must be an object")
        frame_path = frame.get("path")
        if not isinstance(frame_path, str) or not Path(frame_path).is_absolute() \
                or not Path(frame_path).is_file():
            raise ValueError(
                f"detection-report frame {index} path must be an existing absolute file",
            )
        resolved_frame_path = Path(frame_path).resolve()
        if resolved_frame_path in frame_paths:
            raise ValueError("detection-report must list each screenshot exactly once")
        frame_paths.add(resolved_frame_path)
        timestamp = frame.get("timestamp_seconds")
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool) \
                or not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError(
                f"detection-report frame {index} timestamp_seconds must be non-negative",
            )
        agent_id = frame.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError(
                f"detection-report frame {index} agent_id must be non-empty",
            )
        agent_id = agent_id.strip()
        if agent_id in agent_ids:
            raise ValueError("detection-report requires one unique agent_id per frame")
        agent_ids.add(agent_id)
        result = frame.get("result")
        if not isinstance(result, str) or not result.strip():
            raise ValueError(
                f"detection-report frame {index} result must be non-empty text",
            )
        image_type, width, height = detect_image(resolved_frame_path)
        if frame.get("sha256") != sha256_file(resolved_frame_path):
            raise ValueError(
                f"detection-report frame {index} sha256 does not match screenshot",
            )
        if frame.get("image_type") != image_type \
                or frame.get("width") != width or frame.get("height") != height:
            raise ValueError(
                f"detection-report frame {index} image metadata does not match screenshot",
            )
    return report


def main() -> None:
    args = parse_args()
    validate_args(args)
    task_id = args.task_id.strip()
    if not task_id:
        raise ValueError("task-id must not be empty")
    baseline = require_file(args.baseline, "baseline")
    source_value = Path(args.source)
    source = require_file(args.source, "source") if source_value.is_absolute() \
        else resolve_shared_input(args.source, args.shared_root, "video")
    detection_report = require_file(args.detection_report, "detection-report")
    prompt_file = require_file(args.prompt_file, "prompt-file")
    report = validate_detection_report(detection_report, source, prompt_file, task_id)
    prompt = prompt_file.read_text(encoding="utf-8-sig").replace("\r\n", "\n").rstrip("\n")
    if not prompt.strip():
        raise ValueError("prompt-file must not be empty")

    graph = load_graph(baseline)
    video_inputs = graph["150"]["inputs"]
    video_inputs.update({
        "video": str(source),
        "custom_width": args.width,
        "custom_height": args.height,
        "frame_load_cap": args.length,
        "skip_first_frames": args.skip_frames,
    })
    h3_inputs = graph["136"]["inputs"]
    h3_inputs.update({
        "prompt": prompt,
        "width": args.width,
        "height": args.height,
        "length": args.length,
    })
    graph["92"]["inputs"]["filename_prefix"] = args.prefix
    if args.seed is not None:
        graph["129"]["inputs"]["noise_seed"] = args.seed

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")

    evidence_path = Path(f"{out}.evidence.json")
    evidence = {
        "version": 1,
        "task_id": task_id,
        "workflow": {"path": str(out.resolve()), "sha256": sha256_file(out)},
        "source": {"path": str(source.resolve()), "sha256": sha256_file(source)},
        "detection_report": {
            "path": str(detection_report.resolve()),
            "sha256": sha256_file(detection_report),
        },
        "prompt": {
            "path": str(prompt_file.resolve()),
            "sha256": sha256_file(prompt_file),
        },
    }
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    details = {
        "written": str(out),
        "evidence": str(evidence_path),
        "task_id": task_id,
        "lora": graph["145"]["inputs"].get("lora_name"),
        "steps": graph["124"]["inputs"].get("steps"),
        "width": args.width,
        "height": args.height,
        "length": args.length,
        "seed": graph["129"]["inputs"].get("noise_seed"),
        "video": str(source),
        "detection_report": str(detection_report),
        "detected_frames": len(report["frames"]),
        "frame_load_cap": video_inputs.get("frame_load_cap"),
        "skip_first_frames": video_inputs.get("skip_first_frames"),
        "prefix": args.prefix,
    }
    print(json.dumps(details, ensure_ascii=False))


if __name__ == "__main__":
    main()
