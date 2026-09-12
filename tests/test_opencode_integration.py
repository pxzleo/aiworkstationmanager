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
    def test_skill_requires_coherent_anatomy_and_natural_skin_completion(self) -> None:
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        for requirement in (
            "源片中不可见的身体区域", "合理补全", "骨骼标志", "软组织拉伸与压缩",
            "毛孔", "皮下散射", "真实差异", "跨帧跳变",
            "subject_definitions", "summary", "retention_analysis", "partially_preserved",
            "fully_preserved", "detailed_description", "只描述声音",
            "脸部是第一身份锚点", "感知上不可区分", "局部颜色与材质基准",
            "分色", "区域性色漂", "云状色斑", "不得让身体变得更黄",
        ):
            self.assertIn(requirement, skill)
        ordered_sections = (
            "`subject_definitions`：", "`summary`：", "`retention_analysis`：",
            "`detailed_description`：", "`overall_soundscape` 和 `non_diegetic_music`",
        )
        positions = [skill.index(section) for section in ordered_sections]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("不得在这里定义或假装引用源片未展示的身体细节", skill)
        self.assertIn("把新露出区域作为目标画面需要合理生成", skill)
        self.assertIn("Ref2VA 六段规则是提示词写作的唯一规范", skill)
        self.assertIn("不得为了“对齐写法”搜索、读取或复用既往任务", skill)
        self.assertIn("历史 API JSON 只可作为节点图基线", skill)
        self.assertIn("必须用本次依据源片和用户要求新写的提示词覆盖", skill)
        self.assertIn("The only permitted visual change is the requested clothing or occlusion edit", skill)
        self.assertIn("所有源片可见身体特征必须保持感知一致", skill)
        self.assertIn("先写脸部与所有源片可见区域不得改变", skill)

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
            self.assertEqual(
                graph["136"]["inputs"]["prompt"],
                "Preserve motion and camera continuity.",
            )
            baseline_graph = json.loads(BASELINE.read_text(encoding="utf-8"))
            self.assertNotEqual(
                graph["136"]["inputs"]["prompt"],
                baseline_graph["136"]["inputs"]["prompt"],
            )
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
