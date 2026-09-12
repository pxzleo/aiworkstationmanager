---
name: h3-ref2v-video-pipeline
description: Build and finish local MiniMax H3 reference-to-video workflows for AXIS-managed ComfyUI generation. Use for reference-video editing that must preserve timing, camera motion, scene continuity, and optional source audio; supports multi-segment jobs, normal visual QA, and an explicit no-preview/no-verification fast path. Not for text-only video, hosted provider APIs, or direct service lifecycle control.
---

# H3 Ref2V 视频流水线

本 Skill 负责探测素材、构建单分支 ComfyUI API workflow、规划多段任务和完成成片收尾。场景切换、RTX 4090 独占、任务排队、ComfyUI 提交、进度监控、恢复原场景和回调必须交给 `axis-video` Skill 提供的工具；不要直接启停 NInfer、ComfyUI 或 AXIS 管理的其他服务。

## 执行模式

- 正常模式：允许先做短预览，正式生成后检查媒体流并抽帧验收。
- 免检快速模式：用户明确说“跳过检验”“跳过验收”“不要样片”或“直接正式生成”时立即启用。只做源片参数探测、安全分段、正式工作流构建、正式生成和必要的拼接、格式转换、音频回填；禁止生成 preview/sample，禁止抽帧、视觉验收、成片 `ffprobe` 检查和音频哈希检查，也不得后台补做。交付时标注“按用户要求未检验”，不得声称 PASS。
- 免检只省略样片和验收，不放宽单分支、帧数、资源门槛、AXIS 串行调度或不覆盖原文件等规则。

## 工作流基线

基线随 Skill 一起安装，始终从 Skill 目录解析，不依赖历史任务目录：

- 4 步快速版：`assets/h3-ref2v-4step-api.json`
- 8 步质量版：`assets/h3-ref2v-8step-api.json`

默认使用 8 步。用户明确要求快速时使用 4 步；只有用户明确要求比较时才分别制作对比预览，不要擅自生成两个正式版本。基线中的源片、提示词和输出前缀都是占位值，每次必须通过 `scripts/build_api.py` 覆盖。

## 参数与分段

1. 用 `ffprobe` 记录源片分辨率、帧率、视频流时长、帧数和音频流。
2. 生成帧率固定 24 fps。`length` 必须满足 `17n+5`；使用不小于目标窗口帧数的最小网格值。
3. 宽高必须为正数且是 16 的倍数。8 步短边默认 768，长边按原比例取最接近的 16 倍数；4 步短边默认 448。
4. 未经新的容量验证，RTX 4090 单段最多 200 帧。更长的视频必须拆成多个独立 workflow，优先在真实硬切点处分段；没有硬切点时按帧数均分。
5. 每个 workflow 只能有一个 `MiniMaxH3ReferenceToVideo` 和一个生成采样分支。多段任务保持同一 seed，并按时间顺序一次调用 `axis_video_submit_batch`，禁止逐段预提交或把多段合并为一个多分支图。
6. 网格输出可能长于目标窗口。拼接前按各段窗口帧数裁剪，拼接后再按源片时长收尾。

构建单段工作流：

```powershell
python scripts\build_api.py `
  --baseline assets\h3-ref2v-8step-api.json `
  --source <source.mp4> `
  --width 768 --height 1344 --length 107 `
  --prompt-file <prompt.txt> `
  --prefix <task-tag>/segment-01 `
  --out <segment-01.json>
```

`--skip-frames` 可从源片指定的 24 fps 帧序号开始读取，用于分段而不预切视频。构建脚本会在写文件前验证基线、源片、提示词、尺寸、网格帧数、跳帧值和必要节点，并输出 LoRA、采样步数、尺寸、帧数、seed、源片、跳帧值和输出前缀供核对。

## 提示词

提示词使用英文，并明确：主体与场景定义、必须保留的身份、动作、镜头和时间线，需要改变的内容，逐时间窗描述，声音处理和不应新增的元素。多段视频的时间戳相对各段起点重算；同一人物跨段保持一致描述和 seed。

本节的 Ref2VA 六段规则是提示词写作的唯一规范。不得调用面向 I2V/I2VA 的 `minimax-h3-prompt` Skill 来决定格式，也不得为了“对齐写法”搜索、读取或复用既往任务的提示词、成片说明或工作流内嵌提示词。历史 API JSON 只可作为节点图基线，并且必须用本次依据源片和用户要求新写的提示词覆盖其中旧值。

换装或移除遮挡物会露出源片中不可见的身体区域时，提示词必须在靠前位置明确以下约束：

- 最高优先级不是重新美化人物，而是让目标人物除服装或遮挡变化外与源片在感知上不可区分。脸部是第一身份锚点；源片可见的脸型、五官几何、表情、妆容、发际线、肤色、曝光、白平衡、纹理、清晰度和受光不得重绘、提亮、磨皮、改色或重新打光。源片可见的颈部、四肢、躯干及轮廓同样不得改变体型、比例、肤色或材质。
- `summary` 只写一句具体编辑边界：`Change only the requested clothing or occlusion; keep the source face, hair, exposed skin, body outline, pose, hands, camera, lighting, and background unchanged.` 不要用 `perceptually indistinguishable` 代替具体属性，也不要把任务描述成重新设计人物。
- 源片中可见的身份、体型比例、姿态、动作节奏和镜头透视作为连续性锚点；原先被遮挡的区域只能描述为依据这些锚点进行的合理补全，不得声称从源片精确保留了不可见细节。
- 新露出的区域必须与相邻可见区域组成同一个连续身体，并随源片动作同步；骨骼标志、关节活动、重力、软组织拉伸与压缩、接触形变和自身遮挡符合真实解剖及当前视角，不能出现重复、融合、断裂或滑动的结构。
- 源片可见的脸部和各身体部位必须分别保持自身已有的肤色、亮度、纹理、粗糙度和受光差异，不得强行统一成同一数值或同一涂层。新露出区域以其相邻可见皮肤为局部颜色与材质基准，并与同一人物的整体底色、白平衡和材质体系协调连续；保留由身体部位、解剖、血色、受力、曲率、朝向、遮挡和原片光照造成的真实差异，同时禁止脸与身体出现非原片已有的分色、局部重新曝光、独立补光、区域性色漂、云状色斑或跨帧跳变。
- 皮肤保持源片级照片质感而非额外美化：保留源片已有的毛孔、细纹、清晰度、轻微皮下散射及连续阴影和高光，不得把脸变得更白、更粉、更冷、更亮或更光滑，也不得让身体变得更黄、橙、红褐、灰暗或更饱和；禁止瓷感、蜡质、塑料感、磨皮、显著痣/疤痕/纹身增删和闪烁。
- 严格按官方 Full-Reference Mode 的六段顺序放置这些信息：
  - `subject_definitions`：把源片中的同一人物定义为 `<Subject 1>`，只声明引用来源及源片实际可观察的身份、比例和外观锚点，并明确列出实际可见的脸部皮肤和其他裸露皮肤作为颜色与材质基准；不得在这里定义或假装引用源片未展示的身体细节。
  - `summary`：只用一句简短的 `[video editing ...]` 概括换装关系，不在这里堆叠质量词。
  - `retention_analysis`：换装并生成原先不可见区域时，将 `<Subject 1>` 标为 `partially_preserved`，逐项声明脸部和所有源片可见身体特征必须保持感知一致，仅服装/遮挡和原先不可见区域发生变化；不得把源片未展示的细节标成 `fully_preserved`。
  - `detailed_description`：先写脸部与所有源片可见区域不得改变，再把新露出区域作为目标画面需要合理生成的连续身体部分；在对应时间窗内具体描述与原片一致的颜色、材质、曝光、受光、姿态、关节运动和软组织响应，且必须与源片动作同步。不要只堆叠 `realistic`、`natural`、`anatomically correct` 等抽象形容词。
  - `overall_soundscape` 和 `non_diegetic_music` 只描述声音，不混入解剖或画质要求。
- 提示词较长时优先保留 `subject_definitions`、`retention_analysis` 和逐时间窗的关键约束，不要把它们放在容易被忽略的末尾。

提示词必须短而具体，通常控制在 1600–2200 个 UTF-8 字节：

- `subject_definitions` 每个引用一行，只写源片实际可见事实，不写 `must never be redrawn` 等命令。
- `summary` 用一句话概括编辑边界；`retention_analysis` 每个引用一行说明保留/变化关系；`detailed_description` 在镜头中落实可见事实。三个字段各写自身职责，但不得逐字复制整段约束或完整负面词清单。
- `detailed_description` 每个真实镜头一段，只写该镜头实际可见且可判定的项目：眼睛大小与视线、嘴部开合与表情、脸型和妆容、发型轮廓、手臂/手/道具位置、可见身体轮廓和运动模糊；不存在、被遮挡或无法判断的项目不得猜测。再写需要改变的遮挡区域及其局部匹配；禁止只写 `same actions`、`exactly unchanged` 或 `perceptually identical`。
- 负面约束只保留与源片和既往失败直接相关的 4–8 项，例如眼睛放大、下巴变尖、表情改变、手或道具移位、脸身分色、蜡质皮肤、色斑和锐度不匹配；不要堆叠通用质量词。
- 在写入 workflow 前计算字节数；超过 2200 字节先删除重复句、抽象形容词和无关负面词，不得删除逐镜头可观察事实、唯一编辑边界或局部补全约束。

需要保留源片音频时，生成 workflow 可以只输出视频，完成后用 `finish_video.py` 回填。需要模型原生音效或音乐时，工作流必须使用能同时解码视频和音频 latent 的节点，并把生成音频接到最终视频节点；不得把无声输出误报为含音频。

## 提交与收尾

- 单段正式任务调用 `axis_video_submit`。
- 多段正式任务把全部单分支 JSON 按顺序一次传给 `axis_video_submit_batch`。AXIS 会逐段释放生成模型，批内不恢复场景、不回调，批尾统一恢复并回调一次。
- 失败后只重建、重提失败片段；保留已完成片段，不得重复生成。
- 取消是终态。收到取消结果后不得重新生成或重新提交已取消任务，也不得继续同批后续片段；只有用户在取消之后提出新的明确生成要求时，才可创建新任务。取消不得套用失败重试规则。
- AXIS 输出可直接传给 `finish_video.py --generated <video>`，回填源片原音频并裁到目标时长。输出已存在时脚本必须拒绝覆盖。

正常模式下，先用 `ffprobe` 确认用户要求的音视频流和时长，再做视觉验收。抽帧后主会话禁止直接读取图片；每张截图必须交给一个全新独立子代理，每个子代理只能查看一张图，并只返回 `PASS/FAIL + 识别事实 + 理由`。主会话只汇总文字。子代理不可用时，请用户通过局域网链接确认或明确报告无法完成视觉验收，不得由主会话降级读图。

需要局域网交付时，使用用户或项目 `AGENTS.md` 指定的固定文件服务根目录和端口。当前部署约定端口为 `18765`，不得占用业务端口或小智音频桥端口。启动前验证监听进程归属；未知监听不得停止或接管。交付前验证真实局域网 URL 返回 HTTP 200。

## 自带资源

- `assets/h3-ref2v-4step-api.json`：去敏的 4 步单分支 API 基线。
- `assets/h3-ref2v-8step-api.json`：去敏的 8 步单分支 API 基线。
- `scripts/build_api.py`：从基线构建一个单分支 workflow。
- `scripts/build_api_batch.py`：仅用于读取或迁移旧的多分支图；不得将其输出提交给 AXIS。
- `scripts/submit_wait.ps1`：仅在明确要求绕过 AXIS、直接调试独立 ComfyUI 时提交并等待单个 prompt；不能用于场景切换任务。
- `scripts/submit_multi.ps1`：仅在明确要求绕过 AXIS、直接调试独立 ComfyUI 时提交多个独立 prompt；不能用于场景切换任务。
- `scripts/finish_video.py`：按源片参数收尾并在正常模式下验证原音频一致性。

所有脚本错误必须直接报告根因。AXIS 工具报错时原样说明，不要绕过调度器继续切换服务或重复提交。
