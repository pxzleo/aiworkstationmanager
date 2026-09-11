from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "integrations" / "opencode" / "skills" / "h3-ref2v-video-pipeline"
BUILDER = SKILL / "scripts" / "build_api.py"
BASELINE = SKILL / "assets" / "h3-ref2v-8step-api.json"


class H3WorkflowBuilderTests(unittest.TestCase):
    def run_builder(
        self, root: Path, *, length: int = 107, baseline: Path = BASELINE,
    ) -> subprocess.CompletedProcess[str]:
        source = root / "source.mp4"
        prompt = root / "prompt.txt"
        source.write_bytes(b"placeholder")
        prompt.write_text("Preserve motion and camera continuity.", encoding="utf-8")
        return subprocess.run(
            [
                sys.executable, str(BUILDER),
                "--baseline", str(baseline),
                "--source", str(source),
                "--width", "768", "--height", "1344", "--length", str(length),
                "--prompt-file", str(prompt),
                "--prefix", "test/segment-01",
                "--out", str(root / "workflow.json"),
            ],
            check=False, capture_output=True, text=True,
        )

    def test_builder_writes_single_branch_graph_and_reports_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_builder(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            details = json.loads(result.stdout)
            graph = json.loads((root / "workflow.json").read_text(encoding="utf-8"))
            self.assertEqual(details["steps"], 8)
            self.assertEqual(details["length"], 107)
            self.assertEqual(details["prefix"], "test/segment-01")
            self.assertEqual(graph["136"]["inputs"]["width"], 768)
            self.assertEqual(graph["150"]["inputs"]["frame_load_cap"], 107)
            self.assertEqual(sum(
                node.get("class_type") == "MiniMaxH3ReferenceToVideo"
                for node in graph.values()
            ), 1)

    def test_builder_rejects_frames_outside_the_h3_grid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_builder(Path(directory), length=108)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("17n+5", result.stderr)

    def test_builder_rejects_multiple_h3_sampling_branches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = json.loads(BASELINE.read_text(encoding="utf-8"))
            graph["999"] = {
                "class_type": "SamplerCustomAdvanced",
                "inputs": dict(graph["125"]["inputs"]),
            }
            baseline = root / "multi-branch.json"
            baseline.write_text(json.dumps(graph), encoding="utf-8")
            result = self.run_builder(root, baseline=baseline)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exactly one H3 generation branch", result.stderr)


if __name__ == "__main__":
    unittest.main()
