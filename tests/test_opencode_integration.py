from __future__ import annotations

import json
import hashlib
import base64
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "integrations" / "opencode" / "skills" / "h3-ref2v-video-pipeline"
AXIS_SKILL = ROOT / "integrations" / "opencode" / "skills" / "axis-video" / "SKILL.md"
BUILDER = SKILL / "scripts" / "build_api.py"
SHARED_INPUT_RESOLVER = SKILL / "scripts" / "resolve_shared_input.py"
BASELINE = SKILL / "assets" / "h3-ref2v-8step-api.json"
FOUR_STEP_BASELINE = SKILL / "assets" / "h3-ref2v-4step-api.json"
PLUGIN = ROOT / "integrations" / "opencode" / "plugins" / "axis-video.ts"
AUTOMATIC_TASK_PLUGIN = ROOT / "integrations" / "opencode" / "plugins" / "axis-automatic-tasks.ts"
AUTOMATIC_TASK_SKILL = ROOT / "integrations" / "opencode" / "skills" / "axis-automatic-tasks" / "SKILL.md"
AUTOMATIC_TASK_INSTALLER = ROOT / "integrations" / "opencode" / "Install-AxisAutomaticTasks.ps1"


class H3WorkflowBuilderTests(unittest.TestCase):
    def test_automatic_task_skill_claims_finishes_and_repeats_serially(self) -> None:
        plugin = AUTOMATIC_TASK_PLUGIN.read_text(encoding="utf-8")
        skill = AUTOMATIC_TASK_SKILL.read_text(encoding="utf-8")
        installer = AUTOMATIC_TASK_INSTALLER.read_text(encoding="utf-8")
        self.assertIn("axis_automatic_task_claim", plugin)
        self.assertIn("axis_automatic_task_finish", plugin)
        self.assertIn("axis_automatic_task_heartbeat", plugin)
        self.assertIn("execution_token", plugin)
        self.assertIn("context.sessionID", plugin)
        self.assertIn("/api/v1/automatic-tasks/claim", plugin)
        self.assertIn("直到队列为空", skill)
        self.assertIn("不得并行领取或执行下一项", skill)
        self.assertIn("回写完成状态后再调用", skill)
        self.assertIn("每分钟在后台自动续期", skill)
        self.assertIn("failed", skill)
        self.assertIn("axis-automatic-tasks.ts", installer)
        self.assertIn("skills\\axis-automatic-tasks", installer)

    def test_automatic_task_plugin_heartbeats_during_a_long_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = Path(directory) / "automatic-heartbeat.ts"
            task_id = "a" * 32
            runner.write_text(
                f'''import {{ AxisAutomaticTasksPlugin }} from {json.dumps(AUTOMATIC_TASK_PLUGIN.as_uri())}
const requests: string[] = []
globalThis.fetch = async (url) => {{
  const path = String(url)
  requests.push(path)
  if (path.endsWith("/claim")) return new Response(JSON.stringify({{task: {{id: {json.dumps(task_id)}, execution_token: "token-a"}}}}), {{status: 200}})
  return new Response(JSON.stringify({{task: {{id: {json.dumps(task_id)}}}}}), {{status: 200}})
}}
const hooks = await AxisAutomaticTasksPlugin({{}} as never, {{heartbeatIntervalMs: 10}})
const context = {{sessionID: "session-a"}} as never
await hooks.tool?.axis_automatic_task_claim.execute({{}}, context)
await Bun.sleep(45)
const beforeFinish = requests.filter((path) => path.endsWith("/heartbeat")).length
if (beforeFinish < 2) throw new Error(`background heartbeat count was ${{beforeFinish}}`)
await hooks.tool?.axis_automatic_task_finish.execute(
  {{task_id: {json.dumps(task_id)}, execution_token: "token-a", status: "succeeded", summary: "done"}},
  context,
)
await Bun.sleep(30)
const afterFinish = requests.filter((path) => path.endsWith("/heartbeat")).length
if (afterFinish !== beforeFinish) throw new Error("heartbeat continued after finish")
await hooks.tool?.axis_automatic_task_claim.execute({{}}, {{sessionID: "session-b"}} as never)
await Bun.sleep(25)
await hooks.event?.({{event: {{type: "session.idle", properties: {{sessionID: "session-b"}}}} as never}})
const beforeIdleWait = requests.filter((path) => path.endsWith("/heartbeat")).length
await Bun.sleep(30)
const afterIdleWait = requests.filter((path) => path.endsWith("/heartbeat")).length
if (afterIdleWait !== beforeIdleWait) throw new Error("heartbeat continued after session.idle")
await hooks.dispose?.()
''',
                encoding="utf-8",
            )
            bun = ["bun"] if os.name != "nt" else [
                "powershell", "-NoProfile", "-File",
                str(Path(os.environ["APPDATA"]) / "npm" / "bun.ps1"),
            ]
            result = subprocess.run(
                [*bun, str(runner)], cwd=ROOT,
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    @staticmethod
    def sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def write_test_png(path: Path) -> None:
        path.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
        ))

    def test_cancelled_job_requires_a_new_explicit_user_request_before_resubmission(self) -> None:
        for path in (AXIS_SKILL, SKILL / "SKILL.md"):
            skill = path.read_text(encoding="utf-8")
            self.assertIn("取消是终态", skill)
            self.assertIn("新的明确生成要求", skill)
            self.assertIn("不得重新生成", skill)

    def test_shared_input_resolver_accepts_media_names_and_rejects_unsafe_or_ambiguous_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_directory = root / "输入"
            input_directory.mkdir()
            video = input_directory / "中文视频.mp4"
            image = input_directory / "封面.png"
            video.write_bytes(b"video")
            image.write_bytes(b"image")
            resolved = subprocess.run(
                [sys.executable, str(SHARED_INPUT_RESOLVER), "中文视频", "--root", str(root), "--kind", "video"],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            self.assertEqual(Path(resolved.stdout.strip()).resolve(), video.resolve())
            rejected = subprocess.run(
                [sys.executable, str(SHARED_INPUT_RESOLVER), "../封面.png", "--root", str(root)],
                check=False, capture_output=True, text=True,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("without directory components", rejected.stderr)
            (input_directory / "中文视频.mov").write_bytes(b"video-2")
            ambiguous = subprocess.run(
                [sys.executable, str(SHARED_INPUT_RESOLVER), "中文视频", "--root", str(root)],
                check=False, capture_output=True, text=True,
            )
            self.assertNotEqual(ambiguous.returncode, 0)
            self.assertIn("ambiguous", ambiguous.stderr)

            nested = root / "nested"
            nested.mkdir()
            (nested / "local.mp4").write_bytes(b"must-not-be-used")
            bypass = subprocess.run(
                [
                    sys.executable, str(BUILDER), "--baseline", str(BASELINE),
                    "--source", str(Path("nested") / "local.mp4"), "--shared-root", str(root),
                    "--detection-report", str(root / "missing.json"), "--task-id", "test-task",
                    "--width", "768", "--height", "1344", "--length", "107",
                    "--prompt-file", str(root / "missing.txt"), "--prefix", "test/source",
                    "--out", str(root / "workflow.json"),
                ],
                cwd=root, check=False, capture_output=True, text=True,
            )
            self.assertNotEqual(bypass.returncode, 0)
            self.assertIn("without directory components", bypass.stderr)

            outside = root / "outside"
            outside.mkdir()
            (outside / "escaped.mp4").write_bytes(b"outside")
            linked_root = root / "linked-root"
            linked_root.mkdir()
            try:
                os.symlink(outside, linked_root / "输入", target_is_directory=True)
            except OSError:
                pass
            else:
                escaped = subprocess.run(
                    [sys.executable, str(SHARED_INPUT_RESOLVER), "escaped.mp4", "--root", str(linked_root)],
                    check=False, capture_output=True, text=True,
                )
                self.assertNotEqual(escaped.returncode, 0)
                self.assertIn("outside the shared root", escaped.stderr)

        axis_skill = AXIS_SKILL.read_text(encoding="utf-8")
        pipeline_skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        for content in (axis_skill, pipeline_skill):
            self.assertIn("resolve_shared_input.py", content)
            self.assertIn("输入/", content)

    def test_skill_requires_coherent_anatomy_and_natural_skin_completion(self) -> None:
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        for requirement in (
            "源片中不可见的身体区域", "合理补全", "骨骼标志", "软组织拉伸与压缩",
            "真实的局部差异", "连续受光", "闪烁",
            "subject_definitions", "summary", "retention_analysis", "partially_preserved",
            "fully_preserved", "detailed_description", "只描述声音",
            "最高优先级硬约束", "局部颜色、材质、比例和受光基准",
            "分色", "色漂", "色斑", "肤色衣物状色块",
        ):
            self.assertIn(requirement, skill)
        ordered_sections = (
            "`subject_definitions`：", "`summary`：", "`retention_analysis`：",
            "`detailed_description`：", "`overall_soundscape` 和 `non_diegetic_music`",
        )
        positions = [skill.index(section) for section in ordered_sections]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("不得假装引用源片未展示的细节", skill)
        self.assertIn("Ref2VA 六段规则是提示词写作的唯一规范", skill)
        self.assertIn("不得为了“对齐写法”搜索、读取或复用既往任务", skill)
        self.assertIn("历史 API JSON 只可作为节点图基线", skill)
        self.assertIn("必须用本次依据源片和用户要求新写的提示词覆盖", skill)
        self.assertIn("The only permitted visual change is", skill)
        self.assertIn("变化区域之外完全保留", skill)
        self.assertIn("所有视觉描述只针对变化区域", skill)
        self.assertIn("所有换装或移除遮挡物任务都必须", skill)
        self.assertIn("不以是否露出源片中不可见区域为条件", skill)
        self.assertIn("只通过 `<Video 1>` 全局引用锁定", skill)
        self.assertNotIn("必须保留的身份、动作、镜头和时间线", skill)
        self.assertIn("1600–2200 个 UTF-8 字节", skill)
        self.assertIn("软目标", skill)
        self.assertIn("允许超过 2200", skill)
        self.assertIn("每个真实镜头一段", skill)
        self.assertIn("不得重述脸、发型、表情、手势、道具、背景或镜头", skill)
        self.assertIn("负面约束通常保留", skill)
        self.assertIn("已确认失败项不得因数量或长度删除", skill)
        self.assertIn("不得为了满足长度删除", skill)
        self.assertIn("源片实际衣物或遮挡物的名称、材质、轮廓和覆盖范围", skill)
        self.assertIn("每个相关镜头中的目标状态", skill)
        self.assertIn("残留衣物轮廓", skill)
        self.assertIn("肤色衣物状色块", skill)
        self.assertIn("衣物塑造的外轮廓", skill)
        self.assertIn("三个字段各写自身职责", skill)
        self.assertIn("不存在、被遮挡或无法判断的项目不得猜测", skill)

    def test_skill_locks_everything_outside_the_requested_edit_region(self) -> None:
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        requirements = (ROOT / "REQUIREMENTS.md").read_text(encoding="utf-8")
        for content in (skill, requirements):
            self.assertIn("最高优先级硬约束", content)
            self.assertIn("只允许用户指定的变装或遮挡区域发生变化", content)
            self.assertIn("变化区域之外的所有源片可见内容不得发生任何变化", content)
            self.assertIn("所有视觉描述只针对变化区域", content)
            self.assertIn("不得逐时间段重复描述未变化区域", content)
        self.assertIn("<Video 1>", skill)
        self.assertIn("partially_preserved -", skill)
        self.assertIn("The only permitted visual change is", skill)
        self.assertIn("outside the edited region must remain unchanged from <Video 1>", skill)

    def run_builder(
        self, root: Path, *, length: int = 107, baseline: Path = BASELINE,
    ) -> subprocess.CompletedProcess[str]:
        source = root / "source.mp4"
        screenshot = root / "source-frame-01.png"
        detection_report = root / "source_detection_report.json"
        prompt = root / "prompt.txt"
        source.write_bytes(b"placeholder")
        self.write_test_png(screenshot)
        prompt.write_text("Preserve motion and camera continuity.", encoding="utf-8")
        detection_report.write_text(json.dumps({
            "task_id": "test-task",
            "created_at": "2026-09-12T12:00:00+08:00",
            "source": {
                "path": str(source.resolve()),
                "sha256": self.sha256(source),
            },
            "prompt_sha256": self.sha256(prompt),
            "frames": [{
                "path": str(screenshot.resolve()),
                "timestamp_seconds": 0.0,
                "agent_id": "visual-agent-01",
                "result": "A source subject is visible in a continuous shot.",
                "sha256": self.sha256(screenshot),
                "image_type": "png",
                "width": 1,
                "height": 1,
            }],
        }), encoding="utf-8")
        return subprocess.run(
            [
                sys.executable, str(BUILDER),
                "--baseline", str(baseline),
                "--source", str(source),
                "--detection-report", str(detection_report),
                "--task-id", "test-task",
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
            self.assertEqual(details["detected_frames"], 1)
            evidence = json.loads(
                (root / "workflow.json.evidence.json").read_text(encoding="utf-8"),
            )
            self.assertEqual(evidence["task_id"], "test-task")
            self.assertEqual(evidence["workflow"]["sha256"], self.sha256(root / "workflow.json"))
            self.assertEqual(evidence["source"]["sha256"], self.sha256(root / "source.mp4"))
            self.assertEqual(evidence["prompt"]["sha256"], self.sha256(root / "prompt.txt"))
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
            self.assertNotIn("152", graph)
            self.assertEqual(graph["151"]["inputs"]["model"], ["145", 0])

    def test_builder_requires_a_current_isolated_detection_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            screenshot = root / "source-frame-02.png"
            second_screenshot = root / "source-frame-03.png"
            self.write_test_png(screenshot)
            self.write_test_png(second_screenshot)
            source = root / "source.mp4"
            prompt = root / "prompt.txt"
            source.write_bytes(b"placeholder")
            prompt.write_text(
                "Preserve motion and camera continuity.", encoding="utf-8",
            )
            valid_frame = {
                "path": str(screenshot.resolve()),
                "timestamp_seconds": 0.0,
                "agent_id": "visual-agent-01",
                "result": "Visible facts.",
                "sha256": self.sha256(screenshot),
                "image_type": "png",
                "width": 1,
                "height": 1,
            }
            cases = (
                ({
                    "task_id": "test-task",
                    "created_at": "2026-09-12T12:00:00+08:00",
                    "source": {
                        "path": str((root / "different.mp4").resolve()),
                        "sha256": self.sha256(source),
                    },
                    "prompt_sha256": self.sha256(prompt),
                    "frames": [valid_frame],
                }, "source must match --source"),
                ({
                    "task_id": "test-task",
                    "created_at": "2026-09-12T12:00:00+08:00",
                    "source": {
                        "path": str(source.resolve()),
                        "sha256": self.sha256(source),
                    },
                    "prompt_sha256": self.sha256(prompt),
                    "frames": [
                        {**valid_frame, "agent_id": "same-agent"},
                        {
                            **valid_frame,
                            "path": str(second_screenshot.resolve()),
                            "timestamp_seconds": 1.0,
                            "agent_id": "same-agent",
                            "result": "Second frame facts.",
                            "sha256": self.sha256(second_screenshot),
                        },
                    ],
                }, "one unique agent_id per frame"),
            )
            for report, expected_error in cases:
                with self.subTest(report=report):
                    report_path = root / "source_detection_report.json"
                    report_path.write_text(json.dumps(report), encoding="utf-8")
                    result = subprocess.run(
                        [
                            sys.executable, str(BUILDER),
                            "--baseline", str(BASELINE),
                            "--source", str(root / "source.mp4"),
                            "--detection-report", str(report_path),
                            "--task-id", "test-task",
                            "--width", "768", "--height", "1344", "--length", "107",
                            "--prompt-file", str(root / "prompt.txt"),
                            "--prefix", "test/segment-01",
                            "--out", str(root / "workflow.json"),
                        ],
                        check=False, capture_output=True, text=True,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(expected_error, result.stderr)

    def test_builder_rejects_changed_source_prompt_or_invalid_screenshot(self) -> None:
        for mutation, expected_error in (
            ("source", "source sha256 does not match"),
            ("prompt", "prompt_sha256 does not match"),
            ("screenshot", "must be a decodable PNG, JPEG, or WebP"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.assertEqual(self.run_builder(root).returncode, 0)
                if mutation == "source":
                    (root / "source.mp4").write_bytes(b"changed source")
                elif mutation == "prompt":
                    (root / "prompt.txt").write_text("Changed prompt.", encoding="utf-8")
                else:
                    (root / "source-frame-01.png").write_bytes(b"not an image")
                result = subprocess.run(
                    [
                        sys.executable, str(BUILDER),
                        "--baseline", str(BASELINE),
                        "--source", str(root / "source.mp4"),
                        "--detection-report", str(root / "source_detection_report.json"),
                        "--task-id", "test-task",
                        "--width", "768", "--height", "1344", "--length", "107",
                        "--prompt-file", str(root / "prompt.txt"),
                        "--prefix", "test/segment-01",
                        "--out", str(root / "workflow.json"),
                    ],
                    check=False, capture_output=True, text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)

    def test_axis_plugin_rechecks_evidence_before_h3_submission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_builder(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            workflow = root / "workflow.json"
            command = (
                f'import {{ verifyWorkflowEvidence }} from {json.dumps(PLUGIN.as_uri())}; '
                f'await verifyWorkflowEvidence({json.dumps(str(workflow))});'
            )
            runner = root / "verify-evidence.ts"
            runner.write_text(command, encoding="utf-8")
            bun = ["bun"]
            if os.name == "nt":
                bun = [
                    "powershell", "-NoProfile", "-File",
                    str(Path(os.environ["APPDATA"]) / "npm" / "bun.ps1"),
                ]
            verified = subprocess.run(
                [*bun, str(runner)], cwd=ROOT,
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            screenshot = root / "source-frame-01.png"
            screenshot_bytes = screenshot.read_bytes()
            screenshot.write_bytes(b"changed screenshot")
            rejected_screenshot = subprocess.run(
                [*bun, str(runner)], cwd=ROOT,
                check=False, capture_output=True, text=True,
            )
            self.assertNotEqual(rejected_screenshot.returncode, 0)
            self.assertIn("截图 1 证据 SHA-256 不匹配", rejected_screenshot.stderr)
            screenshot.write_bytes(screenshot_bytes)
            workflow.write_text(
                workflow.read_text(encoding="utf-8") + "\n", encoding="utf-8",
            )
            rejected = subprocess.run(
                [*bun, str(runner)], cwd=ROOT,
                check=False, capture_output=True, text=True,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("workflow 证据 SHA-256 不匹配", rejected.stderr)

    def test_axis_waits_for_the_submitting_session_before_stopping_ninfer(self) -> None:
        plugin = PLUGIN.read_text(encoding="utf-8")
        manager = (ROOT / "workstation_manager" / "video_jobs.py").read_text(
            encoding="utf-8",
        )
        axis_skill = AXIS_SKILL.read_text(encoding="utf-8")
        pipeline_skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn('event.type === "session.idle"', plugin)
        self.assertIn("/handoff_ready", plugin)
        self.assertIn("handoff_ready", manager)
        self.assertLess(
            manager.index("_wait_for_opencode_idle(job_id"),
            manager.index("await self._wait_for_ninfer_idle(job_id)"),
        )
        self.assertIn("提交成功后不得再运行其他工具", axis_skill)
        self.assertIn("GET /api/v1/files", pipeline_skill)
        self.assertIn("不得读取、复制或解密浏览器 Cookie 数据库", pipeline_skill)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = root / "workflow.json"
            workflow.write_text('{"1":{"class_type":"Test"}}', encoding="utf-8")
            runner = root / "handoff.ts"
            runner.write_text(
                f'''import {{ AxisVideoPlugin }} from {json.dumps(PLUGIN.as_uri())}
const nativeFetch = globalThis.fetch.bind(globalThis)
const payloads = []
globalThis.fetch = async (_url, init) => {{
  payloads.push(JSON.parse(String(init?.body || "null")))
  return new Response(JSON.stringify({{job: {{id: `job-${{payloads.length}}`, status: "queued"}}, created: true}}), {{status: 202}})
}}
let prompts = 0
let rejectPrompt = true
let deferPrompt = false
let resolvePrompt = null
const hooks = await AxisVideoPlugin({{client: {{session: {{promptAsync: async () => {{
  prompts++
  if (deferPrompt) return await new Promise((resolve) => {{ resolvePrompt = resolve }})
  return rejectPrompt ? {{error: {{message: "temporary"}}}} : {{}}
}} }} }} }} as never)
try {{
  await hooks.tool?.axis_video_submit.execute(
    {{workflow_path: {json.dumps(str(workflow))}}},
    {{sessionID: "ses_test", messageID: "msg_test", agent: "build", abort: new AbortController().signal, directory: {json.dumps(str(root))}}} as never,
  )
  await hooks.tool?.axis_video_submit.execute(
    {{workflow_path: {json.dumps(str(workflow))}}},
    {{sessionID: "ses_test", messageID: "msg_test_2", agent: "build", abort: new AbortController().signal, directory: {json.dumps(str(root))}}} as never,
  )
  const base = payloads[0].callback_url + "/session/ses_test/handoff_ready/"
  if ((await nativeFetch(base + "job-1")).status !== 425) throw new Error("handoff became ready before session.idle")
  await hooks.event?.({{event: {{type: "session.idle", properties: {{sessionID: "ses_test"}}}} as never}})
  if ((await nativeFetch(base + "job-1")).status !== 204 || (await nativeFetch(base + "job-2")).status !== 204) {{
    throw new Error("handoffs did not become ready after session.idle")
  }}
  await hooks.event?.({{event: {{type: "session.status", properties: {{sessionID: "ses_test", status: {{type: "busy"}}}}}} as never}})
  if ((await nativeFetch(base + "job-1")).status !== 425 || (await nativeFetch(base + "job-2")).status !== 425) {{
    throw new Error("a new regular response did not block pending handoffs")
  }}
  await hooks.event?.({{event: {{type: "session.status", properties: {{sessionID: "ses_test", status: {{type: "idle"}}}}}} as never}})
  if ((await nativeFetch(base + "job-1")).status !== 204 || (await nativeFetch(base + "job-2")).status !== 204) {{
    throw new Error("session.status idle did not release pending handoffs")
  }}
  const callbackUrl = payloads[0].callback_url + "/session/ses_test/prompt_async?handoff_owner=job-1"
  deferPrompt = true
  const interleavedPromise = nativeFetch(callbackUrl, {{
    method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{parts: [{{type: "text", text: "done"}}]}}),
  }})
  while (!resolvePrompt) await Bun.sleep(1)
  await hooks.event?.({{event: {{type: "session.status", properties: {{sessionID: "ses_test", status: {{type: "busy"}}}}}} as never}})
  resolvePrompt({{error: {{message: "temporary"}}}})
  const interleavedCallback = await interleavedPromise
  if (interleavedCallback.status !== 502) throw new Error("interleaved callback failure was not exposed")
  if ((await nativeFetch(base + "job-1")).status !== 425 || (await nativeFetch(base + "job-2")).status !== 425) {{
    throw new Error("failed callback overwrote a newer busy status")
  }}
  await hooks.event?.({{event: {{type: "session.status", properties: {{sessionID: "ses_test", status: {{type: "idle"}}}}}} as never}})
  deferPrompt = false
  const failedCallback = await nativeFetch(callbackUrl, {{
    method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{parts: [{{type: "text", text: "done"}}]}}),
  }})
  if (failedCallback.status !== 502) throw new Error("callback failure was not exposed")
  if ((await nativeFetch(base + "job-1")).status !== 204 || (await nativeFetch(base + "job-2")).status !== 204) {{
    throw new Error("failed callback did not restore handoff readiness")
  }}
  rejectPrompt = false
  const callback = await nativeFetch(callbackUrl, {{
    method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{parts: [{{type: "text", text: "done"}}]}}),
  }})
  if (callback.status !== 204 || prompts !== 3) throw new Error("callback was not delivered")
  if ((await nativeFetch(base + "job-1")).status !== 425) throw new Error("completed handoff state was not released")
  if ((await nativeFetch(base + "job-2")).status !== 425) throw new Error("callback response did not block the second handoff")
  await hooks.event?.({{event: {{type: "session.idle", properties: {{sessionID: "ses_test"}}}} as never}})
  if ((await nativeFetch(base + "job-2")).status !== 204) throw new Error("second handoff did not recover after the callback response")
}} finally {{
  await hooks.dispose?.()
}}
''',
                encoding="utf-8",
            )
            bun = ["bun"] if os.name != "nt" else [
                "powershell", "-NoProfile", "-File",
                str(Path(os.environ["APPDATA"]) / "npm" / "bun.ps1"),
            ]
            result = subprocess.run(
                [*bun, str(runner)], cwd=ROOT,
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_skills_apply_one_fresh_agent_per_screenshot_to_every_stage(self) -> None:
        for path in (AXIS_SKILL, SKILL / "SKILL.md"):
            skill = path.read_text(encoding="utf-8")
            self.assertIn("任何", skill)
            self.assertIn("主会话禁止直接", skill)
            self.assertIn("每张截图", skill)
            self.assertIn("全新独立", skill)
            self.assertIn("最多读取一张", skill)
            self.assertIn("源片分析", skill)
            self.assertIn("排障", skill)
            self.assertIn("历史结果对比", skill)
            self.assertIn("source_detection_report.json", skill)
            self.assertIn("免检快速模式", skill)

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

    def test_builder_rejects_sage_attention_or_disconnected_sigma_shift(self) -> None:
        mutations = (
            lambda graph: graph.update({"152": {
                "class_type": "PathchSageAttentionKJ",
                "inputs": {"model": ["145", 0], "sage_attention": "auto"},
            }}),
            lambda graph: graph["151"]["inputs"].update({"model": ["127", 0]}),
            lambda graph: graph["124"]["inputs"].update({"model": ["145", 0]}),
            lambda graph: graph["126"]["inputs"].update({"model": ["145", 0]}),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                graph = json.loads(BASELINE.read_text(encoding="utf-8"))
                mutate(graph)
                baseline = root / "invalid-eight-step-routing.json"
                baseline.write_text(json.dumps(graph), encoding="utf-8")
                result = self.run_builder(root, baseline=baseline)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("without SageAttention", result.stderr)

    def test_builder_keeps_the_four_step_baseline_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_builder(root, baseline=FOUR_STEP_BASELINE)
            self.assertEqual(result.returncode, 0, result.stderr)
            graph = json.loads((root / "workflow.json").read_text(encoding="utf-8"))
            self.assertEqual(graph["124"]["inputs"]["steps"], 4)
            self.assertNotIn("152", graph)


if __name__ == "__main__":
    unittest.main()
