import argparse
import json
from pathlib import Path
from typing import Any


REQUIRED_NODES = {
    "92": "SaveVideo",
    "124": "BasicScheduler",
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
    return graph


def main() -> None:
    args = parse_args()
    validate_args(args)
    baseline = require_file(args.baseline, "baseline")
    source = require_file(args.source, "source")
    prompt_file = require_file(args.prompt_file, "prompt-file")
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

    details = {
        "written": str(out),
        "lora": graph["145"]["inputs"].get("lora_name"),
        "steps": graph["124"]["inputs"].get("steps"),
        "width": args.width,
        "height": args.height,
        "length": args.length,
        "seed": graph["129"]["inputs"].get("noise_seed"),
        "video": str(source),
        "frame_load_cap": video_inputs.get("frame_load_cap"),
        "skip_first_frames": video_inputs.get("skip_first_frames"),
        "prefix": args.prefix,
    }
    print(json.dumps(details, ensure_ascii=False))


if __name__ == "__main__":
    main()
