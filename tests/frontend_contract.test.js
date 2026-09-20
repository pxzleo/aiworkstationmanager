'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const js = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const gpuLayout = fs.readFileSync(path.join(root, 'gpu-layout.js'), 'utf8');
const theme = fs.readFileSync(path.join(root, 'theme.js'), 'utf8');
const i18n = fs.readFileSync(path.join(root, 'i18n.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'styles.css'), 'utf8');

test('authentication UI accepts the configured four-character minimum', () => {
  for (const id of ['passwordInput', 'confirmPasswordInput']) {
    assert.match(html, new RegExp(`id="${id}"[^>]*minlength="4"`));
  }
  assert.ok(js.includes('密码至少 4 个字符'));
  assert.ok(!html.includes('minlength="12"'));
  assert.ok(html.includes('id="rememberLoginInput"'));
  assert.ok(html.includes('在该电脑自动登录'));
  assert.ok(html.includes('仅用于私人设备，保持登录 30 天'));
  assert.ok(js.includes("byId('rememberLoginLabel').hidden = setup"));
  assert.ok(js.includes("body.remember = byId('rememberLoginInput').checked"));
  assert.ok(css.includes('.auth-remember'));
  assert.ok(i18n.includes("'在该电脑自动登录': 'Sign in automatically on this device'"));
  assert.ok(i18n.includes("'仅用于私人设备，保持登录 30 天': 'Private devices only. Stay signed in for 30 days.'"));
});

test('registered service editor exposes the agreed fields and actions', () => {
  for (const value of ['已登记服务', '服务名称', '管理脚本绝对路径', 'GPU 展示标签', '服务端口', 'UI 地址', '健康检查地址', '响应必须包含', '由管理器维护 WSL 局域网映射', 'WSL 发行版', 'Windows 监听地址', 'Windows 监听端口', 'WSL 目标端口']) {
    assert.ok(html.includes(value), `missing ${value}`);
  }
  for (const action of ['start', 'stop', 'restart']) assert.ok(js.includes(action));
  assert.ok(js.includes("const SERVICE_INTERVAL_MS = 5000;"));
  assert.ok(js.includes("/registered-services"));
  assert.ok(js.includes("service.busy ? '操作中'"));
  assert.ok(js.includes("service.operation_pending || actionGuard.pending"));
  assert.ok(js.includes("深度检查"));
  assert.ok(js.includes("/status"));
  assert.ok(js.includes("serviceStatusLabel"));
  assert.ok(js.includes("health_url"));
  assert.ok(js.includes("health_expect"));
  assert.ok(js.includes("wsl_portproxy_enabled"));
  assert.ok(js.includes("serviceWslPortproxyEnabled"));
  assert.ok(js.includes("service.wsl_portproxy_error"));
  assert.ok(js.includes("映射异常"));
  assert.ok(html.includes('id="stopAllServicesButton"'));
  assert.ok(js.includes('openStopAllProgress(result.operation_id)'));
  assert.ok(js.includes('renderStopAllProgress(item)'));
  assert.ok(js.includes('renderOperationProgress(operation, operation.total_steps'));
  assert.ok(!js.includes('stopAllProgressTotal'));
  assert.ok(!js.includes("state.services.filter((service) => service.status.state !== 'stopped').length"));
  assert.ok(js.includes("全部服务已停止"));
  assert.ok(js.includes("返回服务列表"));
  for (const copy of ['管理器正在确认需要停止的服务并按顺序执行。', '正在确认需要停止的服务', '无需执行服务步骤，正在确认最终状态。']) {
    assert.ok(i18n.includes(`'${copy}':`), `missing stop-all translation: ${copy}`);
  }
  assert.ok(js.includes('/registered-services/actions/stop-all'));
  assert.ok(js.includes('filtered.forEach((service, index)'));
  assert.ok(js.includes("userElement('i', 'env-logo', String(index + 1))"));
  assert.ok(!js.includes('service.name.slice(0, 1).toUpperCase()'));
});

test('overview only presents services with an observed running state', () => {
  assert.ok(html.includes('<h2>已启动服务</h2>'));
  assert.ok(html.includes('仅显示健康检查确认为运行中的服务'));
  assert.ok(js.includes("function runningServices() { return state.services.filter((service) => service.status.state === 'running'); }"));
  assert.ok(js.includes('const running = runningServices();'));
  assert.ok(js.includes("'当前没有已启动服务。'"));
  assert.ok(js.includes("'GPU 标签下没有已启动服务'"));
});

test('overview cards and monitor charts follow the detected GPU count', () => {
  assert.ok(html.includes('id="gpuStage"'));
  assert.ok(!html.includes('id="gpu0Name"'));
  assert.ok(!html.includes('id="gpu1Name"'));
  assert.ok(js.includes('gpuLayout.prepareGpus'));
  assert.ok(js.includes('gpuLayout.gpuSetSignature'));
  assert.ok(gpuLayout.includes('function metricForGpu'));
  assert.ok(gpuLayout.includes('function serviceGpuKey'));
  assert.ok(gpuLayout.includes('function serviceGpuKeys'));
  assert.ok(js.includes('function createGpuCard'));
  assert.ok(js.includes('function syncGpuCards'));
  assert.ok(js.includes('ordered.forEach((gpu, position)'));
  assert.ok(js.includes('function buildGpuMonitor'));
  assert.ok(js.includes('function createMonitorChart'));
  assert.ok(js.includes('function createMonitorGroup'));
  assert.ok(js.includes('function renderMonitorDetails'));
  for (const label of ['整机功耗', '处理器负载', '系统内存', '提交内存', '页面文件', '核心负载', '核心频率', '功率', '温度', '显存占用', '存储容量', '磁盘读取', '网络下载', 'WSL 内存', 'Docker 容器', '平均', '峰值', '最低']) assert.ok(js.includes(label), `missing monitor label ${label}`);
  for (const selector of ['.monitor-group', '.monitor-chart-grid', '.chart-y-axis', '.chart-x-axis', '.chart-statistics']) assert.ok(css.includes(selector), `missing ${selector}`);
  assert.ok(js.includes('function chartAxisValues'));
  assert.ok(html.includes('id="historyRangeSelect"'));
  assert.ok(html.includes('id="monitorTabbar"'));
  for (const view of ['summary', 'gpu', 'host', 'system']) assert.ok(html.includes(`data-monitor-view="${view}"`));
  for (const minutes of ['15', '60', '1440', '10080', '43200']) assert.ok(html.includes(`data-history-minutes="${minutes}"`));
  for (const id of ['historyPrevButton', 'historyPeriodLabel', 'historyNextButton']) assert.ok(html.includes(`id="${id}"`));
  assert.ok(js.includes('monitorChart.axisLabels(minutes)'));
  assert.ok(js.includes('historyPeriodForRange(state.historyWindowMinutes'));
  assert.ok(js.includes('const windowMs = period.endMs - period.startMs'));
  assert.ok(js.includes('`/history?window=${minutes}m${end}`'));
  assert.ok(js.indexOf("title: '整机功耗'") < js.indexOf("title: '处理器负载'"));
  assert.ok(js.includes("geometry.isolatedPoints.map((point) => svgElement('circle'"));
  assert.ok(css.includes('.chart-isolated-point'));
  assert.ok(css.includes('.gpu-correlation-stack'));
  assert.ok(css.includes('.chart-cursor'));
  assert.ok(css.includes('.monitor-gpu-layout { grid-template-columns: 1fr; }'));
  assert.ok(js.includes('body.append(context, correlation)'));
  assert.ok(js.includes('bindCorrelationCursor'));
  assert.ok(js.includes('setPointerCapture'));
  assert.ok(js.includes("plot.addEventListener('pointerup'"));
  assert.ok(js.includes("plot.addEventListener('pointercancel'"));
  assert.ok(css.includes('touch-action: pan-y'));
  assert.ok(!js.includes("range.type = 'range'"));
  assert.ok(!css.includes('.correlation-control input'));
  assert.ok(js.includes("'同步时间'"));
  assert.ok(css.includes('.correlation-time'));
  assert.ok(css.includes('.range-select button { min-width: 52px; min-height: 42px;'));
  assert.ok(js.includes("announcement.setAttribute('aria-live', 'polite')"));
  assert.ok(js.includes('monitorChart.nearestSample'));
  assert.ok(js.includes("getter: metricGib('memory_used_mib')"));
  assert.ok(js.includes("unit: 'GB', decimals: 1"));
  assert.ok(js.includes('showMaximumInCurrent: true'));
  assert.ok(js.includes('statisticsIncludeUnit: true'));
  assert.ok(js.includes('spec.statisticsIncludeUnit === true'));
  assert.ok(js.includes("metricGib('memory_total_mib')"));
  assert.ok(js.includes("getter: (sample) => gib(sample.memory_used_bytes), unit: 'GB', decimals: 1"));
  assert.ok(js.includes("gib(sample.memory_total_bytes)"));
  assert.ok(js.includes("statisticsIncludeUnit: true, compact: true"));
  assert.ok(js.includes('network_received_bytes_per_second'));
  assert.ok(js.includes('wsl_swap_used_bytes'));
  assert.ok(js.includes('memory_utilization_percent'));
  assert.ok(css.includes('.monitor-summary-list'));
  assert.ok(css.includes('.monitor-device-selector'));
  assert.ok(css.includes('.monitor-runtime-section'));
  for (const removed of ['已累计', '图表点', '最近更新', 'monitor-overview']) {
    assert.ok(!js.includes(removed), `removed monitor metadata remains in app.js: ${removed}`);
    assert.ok(!css.includes(removed), `removed monitor metadata remains in styles.css: ${removed}`);
  }
  assert.ok(js.includes('key: gpu._uiKey'));
  assert.ok(js.includes('state.selectedMonitorGpuKey = gpuKey'));
  assert.ok(js.includes("details.summary.get(gpu._uiKey)"));
  assert.ok(!js.includes("getter: (sample) => sample.memory_percent }"));
  assert.ok(!js.includes("getter: metric('memory_percent'), unit: '%', maximum: 100"));
  assert.ok(i18n.includes("'处理器负载': 'Processor load'"));
  assert.ok(i18n.includes("'整机功耗': 'Total system power'"));
  assert.ok(js.includes("'未检测到 NVIDIA GPU。'"));
  assert.ok(!js.includes('bindGpuSlots'));
  assert.ok(!js.includes('slots: [null, null]'));
  assert.ok(css.includes('repeat(auto-fit, minmax(min(100%, 440px), 1fr))'));
  assert.ok(css.includes('@keyframes gpuCardIn'));
  assert.ok(css.includes('@media (prefers-reduced-motion: reduce)'));
  assert.ok(i18n.includes("'未检测到 NVIDIA GPU。': 'No NVIDIA GPU detected.'"));
  assert.ok(js.includes('snapshot.stale_collectors?.nvidia'));
  assert.ok(js.includes("staleGpu ? [] : snapshot.gpus || []"));
  assert.ok(js.includes("gpu._stale ? `${ui('上次数据')}"));
  assert.ok(js.includes('gpuLayout.sparklinePath'));
  assert.ok(!js.includes("metricForGpu(sample, gpu, 'load_percent')).filter(finite)"));
  assert.ok(css.includes('.gpu-lane.stale'));
  assert.ok(i18n.includes("'GPU 数据延迟': 'GPU data delayed'"));
  assert.ok(i18n.includes("'采样状态': 'Sampling status'"));
});

test('scene editor and management log remain wired', () => {
  assert.ok(html.includes('id="sceneServiceChoices"'));
  assert.ok(html.includes('id="sceneDescription" maxlength="1000"'));
  assert.ok(html.includes('id="sceneDetailedDescription"'));
  assert.ok(html.includes('maxlength="8000"'));
  assert.ok(html.includes('id="sceneDetailDialog"'));
  assert.ok(html.includes('id="sceneDetailBody"'));
  assert.ok(js.includes("iconButton('查看详细说明', 'info')"));
  assert.ok(js.includes("iconButton('上移场景', 'arrow-up')"));
  assert.ok(js.includes("iconButton('下移场景', 'arrow-down')"));
  assert.ok(js.includes("iconButton('编辑场景', 'edit')"));
  assert.ok(js.includes("iconButton('删除场景', 'trash', 'danger')"));
  assert.ok(js.includes("labeledIconButton(activateLabel, 'switch'"));
  assert.ok(js.includes("iconButton('上移服务', 'arrow-up')"));
  assert.ok(js.includes("button.setAttribute('aria-label', ui(label))"));
  for (const icon of ['arrow-up', 'arrow-down', 'info', 'edit', 'trash', 'star', 'switch', 'save']) {
    assert.ok(html.includes(`id="i-${icon}"`), `missing ${icon} icon`);
  }
  assert.ok(js.includes('openSceneDetails(scene)'));
  assert.ok(js.includes('scene.detailed_description'));
  assert.ok(js.includes("detailed_description: byId('sceneDetailedDescription').value.trim()"));
  assert.ok(css.includes('.scene-detail-body'));
  assert.ok(css.includes('white-space: pre-wrap'));
  assert.ok(i18n.includes("'详细使用说明': 'Detailed Usage Instructions'"));
  assert.ok(html.includes('停止未选服务'));
  assert.ok(js.includes('/scenes'));
  assert.ok(js.includes('/operations?limit=50'));
  assert.ok(js.includes('/scenes/reorder'));
  assert.ok(js.includes("panel.draggable"));
  assert.ok(js.includes("function scenesForDisplay()"));
  assert.ok(js.includes("scene.state !== 'active' && !scene.busy"));
  assert.ok(js.includes("displayedScenes[index - 1]?.state === 'active'"));
  assert.ok(js.includes("dragstart"));
  assert.ok(js.includes("scene.is_default ? '取消默认场景' : '设为默认场景'"));
  assert.ok(js.includes("`/scenes/${scene.id}/default`"));
  assert.ok(js.includes("AXIS 下次启动时会自动切换到该场景"));
  assert.ok(js.includes("scene.is_default ? ' scene-default' : ''"));
  assert.ok(js.includes('`scene-default-banner${combined'));
  assert.ok(js.includes("'默认启动场景'"));
  assert.ok(js.includes("'AXIS 启动时自动切换'"));
  assert.ok(js.includes("'scene-state-banners'"));
  assert.ok(js.includes("'scene-active-banner'"));
  assert.ok(js.includes("'当前已激活场景'"));
  assert.ok(js.includes("'服务组合正在生效'"));
  assert.ok(js.includes("const combined = scene.is_default && scene.state === 'active'"));
  assert.ok(js.includes("' scene-combined-banner'"));
  assert.ok(js.includes("'默认场景 · 已激活'"));
  assert.ok(js.includes('let cardHeader = top'));
  assert.ok(js.includes('cardHeader = banners'));
  assert.ok(js.includes('panel.append(cardHeader'));
  assert.ok(js.includes("'scene-banner-meta'"));
  assert.ok(!js.includes('panel.append(banners)'));
  assert.ok(css.includes('.scene-panel.scene-default'));
  assert.ok(css.includes('.scene-default-banner'));
  assert.ok(css.includes('.scene-panel.selected.scene-default'));
  assert.ok(css.includes('.scene-active-banner'));
  assert.ok(css.includes('.scene-combined-banner'));
  assert.ok(css.includes('.scene-state-banners { margin: -28px -28px 0;'));
  assert.ok(css.includes('.scene-default-banner, .scene-active-banner { min-height: 55px;'));
  assert.ok(css.includes('.scene-state-banners .scene-drag-handle'));
  assert.ok(css.includes('.scene-panel > p { height: 3.1em; min-height: 3.1em; max-height: 3.1em;'));
  assert.ok(css.includes('margin: 0; overflow: hidden; overflow-wrap: anywhere; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2;'));
  assert.ok(css.includes('-webkit-line-clamp: 2'));
  assert.ok(css.includes('font-size: 12px; line-height: 1.55;'));
  assert.ok(css.includes('.scene-panel.scene-default { border-color: rgba(var(--accent-rgb),.68)'));
  assert.ok(css.includes('.scene-default-banner { border-bottom: 1px solid rgba(var(--accent-rgb),.42)'));
  assert.ok(!css.includes('.scene-panel.scene-default { border-color: rgba(231,184,106'));
  assert.ok(!css.includes('.scene-default-banner { border-bottom: 1px solid rgba(231,184,106'));
  assert.ok(i18n.includes("'默认启动场景': 'Default Startup Scene'"));
  assert.ok(i18n.includes("'AXIS 启动时自动切换': 'AXIS switches here on startup'"));
  assert.ok(i18n.includes("'当前已激活场景': 'Currently Active Scene'"));
  assert.ok(i18n.includes("'服务组合正在生效': 'Service combination is active'"));
  assert.ok(i18n.includes("'默认场景 · 已激活': 'Default · Active'"));
  assert.ok(html.includes('id="sceneProgressDialog"'));
  assert.ok(html.includes('id="cancelSceneSwitchButton"'));
  assert.ok(js.includes('/cancel'));
  assert.ok(js.includes("const progress = terminal ? 100"));
  assert.ok(js.includes('let sceneProgressExpectedTotal = null'));
  assert.ok(js.includes('function renderSceneProgress(scene, operation) { const total = Number.isInteger(operation.total_steps) ? operation.total_steps : sceneProgressExpectedTotal'));
  assert.ok(js.includes('function openSceneProgress(scene, operationId) { sceneProgressExpectedTotal = state.services.filter'));
  assert.ok(js.includes('finally { sceneProgressOperationId = null; sceneProgressExpectedTotal = null'));
  assert.ok(js.includes("terminal ? '没有需要执行的服务步骤。'"));
  assert.ok(js.includes("interrupted: '已终止'"));
  assert.ok(js.includes("queued: '等待执行', running: '执行中'"));
  assert.ok(js.includes('operationStatusLabel(operation.status)'));
  assert.ok(js.includes('scene.services'));
  assert.ok(js.includes('service.ui_url'));
  assert.ok(js.includes("'scene-ui-link', '打开 UI ↗'"));
  assert.ok(js.includes("partial: '部分启动'"));
  assert.ok(js.includes("inactive: '未激活'"));
  assert.ok(js.includes("'scene-inactive'"));
  assert.ok(css.includes('.scene-panel-top .scene-inactive'));
  assert.ok(css.includes('grid-column: 3; grid-row: 1 / span 2'));
  assert.ok(css.includes('.scene-ui-link:hover'));
  assert.ok(css.includes('.scene-selector { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 560px), 1fr)); gap: 16px; }'));
  assert.ok(css.includes('.scene-panel { min-height: 405px; padding: 28px; display: flex; flex-direction: column;'));
  assert.ok(css.includes('.scene-card-actions { display: flex; align-items: center; justify-content: space-between;'));
  assert.ok(css.includes('.icon-button.danger:hover:not(:disabled)'));
  assert.ok(css.includes('.scene-activate-button { min-width: 154px;'));
  assert.ok(html.includes('class="dialog-shell" id="sceneForm"'));
  assert.ok(html.includes('<footer class="dialog-footer"><p class="auth-error" id="sceneFormError" role="alert"></p><div class="dialog-footer-actions">'));
  assert.ok(css.includes('grid-template-rows: auto minmax(0, 1fr) auto'));
  assert.ok(css.includes('.dialog-scroll { min-height: 0; overflow-y: auto;'));
  assert.ok(css.includes('.dialog-footer { min-height: 76px;'));
  assert.ok(css.includes('.dialog-footer > .auth-error'));
  assert.ok(css.includes('.scene-editor-dialog { width: min(720px'));
  assert.ok(css.includes('border-radius: 12px'));
  assert.ok(css.includes('.scene-panel.selected:hover'));
  assert.ok(css.includes('transform: translateY(-2px)'));
  assert.ok(html.includes('id="overviewSceneSelect"'));
  assert.ok(html.includes('<option value="">切换场景</option>'));
  assert.ok(js.includes('handleOverviewSceneChange'));
  assert.ok(css.includes('width: 8em'));
  assert.ok(!html.includes('id="switchSceneButton"'));
  assert.ok(!js.includes("switchButton.onclick = () => navigate('scenes')"));
  assert.ok(html.includes('记录服务启停、场景切换与默认场景更改'));
  assert.ok(!html.includes('管理审计'));
  assert.ok(js.includes("api('/audit?limit=100'"));
  assert.ok(js.includes("management.scene.default.set"));
  assert.ok(js.includes("management.scene.default.clear"));
  assert.ok(js.includes('Promise.allSettled'));
  assert.ok(js.includes('const auditEvents = auditData?.events || []'));
  assert.ok(js.includes("operation.requested_by || ''"));
  assert.ok(js.includes("'operation-actor'"));
  assert.ok(js.includes("操作账号"));
  assert.ok(css.includes('.operation-actor'));
  assert.equal((js.match(/operationActor\(/g) || []).length, 3);
  assert.ok(css.includes('.operation-row > div { min-width: 0; }'));
  assert.ok(css.includes('.timeline li > div { min-width: 0; }'));
  assert.ok(css.includes('overflow-wrap: anywhere'));
});

test('active scene is displayed first without changing the relative order of other scenes', () => {
  const source = js.match(/function scenesForDisplay\(\) \{[^\n]+\}/)?.[0];
  assert.ok(source);
  const sandbox = { state: { scenes: [
    { id: 'first', state: 'inactive' },
    { id: 'active', state: 'active' },
    { id: 'last', state: 'partial' },
  ] } };
  vm.runInNewContext(source, sandbox);
  assert.deepEqual(Array.from(sandbox.scenesForDisplay(), (scene) => scene.id), ['active', 'first', 'last']);
});

test('authenticated refresh keeps the login panel hidden while the session is checked', () => {
  assert.ok(html.includes('<body class="auth-pending app-initializing">'));
  assert.ok(html.includes('id="startupScreen"'));
  assert.ok(css.includes('.app-initializing .auth-gate { display: none; }'));
  assert.ok(css.includes('body:not(.app-initializing) .startup-screen { display: none; }'));
  assert.ok(js.includes("document.body.classList.remove('app-initializing')"));
  assert.ok(js.includes("document.body.classList.remove('auth-pending', 'app-initializing')"));
  assert.ok(i18n.includes("'正在载入 AXIS': 'Loading AXIS'"));
});

test('scene generation controls use the current frontend asset cache key', () => {
  assert.ok(html.includes('styles.css?v=20260920-8'));
  assert.ok(html.includes('i18n.js?v=20260920-5'));
  assert.ok(html.includes('app.js?v=20260920-8'));
});

test('read polling tolerates transient network failures without retrying writes', () => {
  assert.ok(js.includes('const READ_RETRY_DELAYS_MS = [400, 1200];'));
  assert.ok(js.includes("['GET', 'HEAD'].includes(method)"));
  assert.ok(js.includes("showPollingError('历史数据读取失败', error)"));
  assert.ok(js.includes('NETWORK_NOTICE_COOLDOWN_MS'));
});

test('video job page monitors every scheduler stage and exposes cancellation', () => {
  assert.ok(html.includes('data-page="video-jobs"'));
  assert.ok(html.includes('id="videoJobList"'));
  assert.ok(html.includes('id="sceneDefaultGeneration"'));
  assert.ok(html.includes('默认生成场景'));
  assert.ok(!html.includes('id="scenePurpose"'));
  assert.ok(js.includes("api('/video-jobs?limit=100'"));
  assert.ok(js.includes('function renderVideoJobs'));
  assert.ok(js.includes('function videoJobTitle'));
  assert.ok(js.includes('function videoJobOutputPath'));
  assert.ok(js.includes('function videoJobTiming'));
  assert.ok(js.includes('function formatDuration'));
  assert.ok(js.includes('job.video_spec?.title'));
  assert.ok(!js.includes("'任务号'"));
  assert.ok(!js.includes('job.session_id'));
  assert.ok(!js.includes('job.prompt_id'));
  assert.ok(!js.includes('NInfer processing'));
  assert.ok(!js.includes('realtime?.node_name || realtime?.node_id'));
  assert.ok(js.includes('nodeName !== nodeId'));
  assert.ok(js.includes("'输出文件'"));
  assert.ok(js.includes("'开始时间'"));
  assert.ok(js.includes("'持续时间'"));
  assert.ok(js.includes("return String(job.output_path || '').trim()"));
  assert.ok(js.includes("job.shared_output_path"));
  assert.ok(js.includes("outputLink.href = fileContentUrl(job.shared_output_path)"));
  assert.ok(js.includes("outputLink.target = '_blank'"));
  assert.ok(js.includes("publishing_output: '复制到共享目录'"));
  assert.ok(!js.includes('job.output_path || job.requested_output_path'));
  assert.ok(!js.includes('activity || job.workflow_path'));
  assert.ok(html.includes('id="videoQueuedSegments"'));
  assert.ok(js.includes('result.queue_summary'));
  assert.ok(js.includes('job.batch_index'));
  assert.ok(js.includes('job.video_spec'));
  assert.ok(js.includes('function videoJobProgress'));
  assert.ok(js.includes('function appendVideoJobRealtime'));
  assert.ok(js.includes('实时进度暂不可用'));
  assert.ok(css.includes('.video-job-specs'));
  assert.ok(css.includes('.video-job-progress'));
  assert.ok(css.includes('.video-job-realtime'));
  assert.ok(css.includes('.video-job-time'));
  assert.ok(js.includes('function cancelVideoJob'));
  assert.ok(js.includes("'callback_pending'"));
  assert.ok(js.includes("!['callback_pending', 'callback_delivered'].includes(job.phase)"));
  assert.ok(js.includes("!['callback_pending', 'callback_delivered', 'succeeded', 'failed', 'cancelled'].includes(job.status)"));
  for (const stage of ['等待 OpenCode 当前响应结束', '等待 NInfer 空闲', '切换生成场景', '检查 ComfyUI', '提交工作流', 'ComfyUI 生成中', '收集输出', '恢复原场景', '回调 OpenCode']) {
    assert.ok(js.includes(stage), `missing video stage ${stage}`);
  }
});

test('automatic task page manages a serial OpenCode queue', () => {
  assert.ok(html.includes('data-page="automatic-tasks"'));
  assert.ok(html.includes('id="automaticTaskList"'));
  assert.ok(html.includes('id="automaticTaskDialog"'));
  assert.ok(html.includes('id="automaticTaskContent"'));
  assert.ok(js.includes('`/automatic-tasks?limit=200&offset=${offset}`'));
  assert.ok(js.includes('while (result.has_more)'));
  assert.ok(js.includes("method: id ? 'PUT' : 'POST'"));
  assert.ok(js.includes("api(`/automatic-tasks/${task.id}`, { method: 'DELETE' })"));
  assert.ok(js.includes("api(`/automatic-tasks/${task.id}/reset`, { method: 'POST' })"));
  assert.ok(js.includes("api('/automatic-tasks/reorder', { method: 'POST'"));
  assert.ok(js.includes('previous_task_ids: previousTaskIds'));
  assert.ok(js.includes("resource: 'automatic-tasks'"));
  assert.ok(js.includes('automaticTaskOrderSaving'));
  assert.ok(js.includes("iconButton('上移任务', 'arrow-up')"));
  assert.ok(js.includes("iconButton('下移任务', 'arrow-down')"));
  assert.ok(js.includes("edit.disabled = runningTask"));
  assert.ok(js.includes("remove.disabled = runningTask"));
  assert.ok(js.includes("if (task.status !== 'pending')"));
  assert.ok(js.includes('再次排队并重新执行'));
  assert.ok(js.includes('旧外部操作仍可能继续，重新执行可能重复产生副作用'));
  assert.ok(js.includes('Existing external work may continue, and running the task again may repeat side effects'));
  assert.ok(js.includes('已有结果将被清除'));
  assert.ok(js.includes('The previous result will be cleared'));
  assert.ok(css.includes('.automatic-task-row'));
  assert.ok(css.includes('.automatic-task-running'));
  assert.ok(i18n.includes("'自动任务': 'Automatic Tasks'"));
});

test('refresh restores the current management page', () => {
  assert.ok(js.includes("const PAGE_STORAGE_KEY = 'axis-active-page';"));
  assert.ok(js.includes('sessionStorage.setItem(PAGE_STORAGE_KEY, page)'));
  assert.ok(js.includes('sessionStorage.getItem(PAGE_STORAGE_KEY)'));
  assert.ok(js.includes('navigate(rememberedPage())'));
});

test('file service page browses folders, downloads files and plays media', () => {
  assert.ok(html.includes('data-page="files"'));
  assert.ok(html.includes('id="page-files"'));
  assert.ok(html.includes('id="fileBreadcrumbs"'));
  assert.ok(html.includes('id="fileRows"'));
  assert.ok(html.includes('id="uploadFilesButton"'));
  assert.ok(html.includes('id="fileRenameDialog"'));
  assert.ok(html.includes('id="fileRenameForm"'));
  assert.ok(html.includes('id="fileUploadInput" type="file" multiple hidden'));
  assert.ok(html.includes('id="fileSortSelect"'));
  assert.ok(html.includes('data-file-view="list"'));
  assert.ok(html.includes('data-file-view="thumbnail"'));
  assert.ok(html.includes('data-file-view="thumbnail" aria-pressed="true"'));
  assert.ok(html.includes('id="mediaDialog"'));
  assert.ok(html.includes('id="mediaStage"'));
  assert.ok(js.includes("api('/file-service'"));
  assert.ok(js.includes('`/file-service/files?${params}`'));
  assert.ok(js.includes("fileSort: 'modified-desc'"));
  assert.ok(js.includes('async function uploadSelectedFiles(files)'));
  assert.ok(js.includes('async function renameFileEntry(event)'));
  assert.ok(js.includes("api('/file-service/rename'"));
  assert.ok(js.includes("openFileRenameDialog(entry)"));
  assert.ok(js.includes('const uploadPath = state.filePath'));
  assert.ok(js.includes("rawBody: file, timeout: null"));
  assert.ok(js.includes("new URLSearchParams({ path: uploadPath, name: file.name })"));
  assert.ok(js.includes("sort_by: sortBy, sort_order: sortOrder"));
  assert.ok(js.includes("function fileThumbnail(entry)"));
  assert.ok(js.includes("const preview = element('button', 'file-thumbnail')"));
  assert.ok(js.includes("preview.addEventListener('click'"));
  assert.ok(js.includes("state.fileView === 'thumbnail'"));
  assert.ok(js.includes("fileView: 'thumbnail'"));
  assert.ok(js.includes('new IntersectionObserver'));
  assert.ok(js.includes('video.dataset.src = source'));
  assert.ok(js.includes('releaseFileThumbnailVideos(rows)'));
  assert.ok(js.includes("video.removeAttribute('src'); video.load()"));
  assert.ok(js.includes('fileContentUrl(entry.path, true)'));
  assert.ok(js.includes("entry.media_type?.startsWith('audio/') ? 'audio' : 'video'"));
  assert.ok(js.includes('function requestMediaFullscreen(player)'));
  assert.ok(js.includes('if (player.requestFullscreen)'));
  assert.ok(!js.includes("const target = byId('mediaDialog').querySelector('.media-dialog-shell')"));
  assert.ok(js.includes('player.webkitEnterFullscreen'));
  assert.ok(js.includes('function requestNativeVideoFullscreen(player, cause)'));
  assert.ok(js.includes('.catch((error) => requestNativeVideoFullscreen(player, error))'));
  assert.ok(js.includes('shell.contains(document.fullscreenElement)'));
  assert.ok(js.includes('await document.exitFullscreen()'));
  assert.ok(js.includes("showMediaSwipeNotice(`${ui('无法退出全屏')}：${error.message}`)"));
  assert.ok(js.includes('function adjacentMediaVideo(offset)'));
  assert.ok(js.includes("state.files.filter((item) => item.media_type?.startsWith('video/'))"));
  assert.ok(js.includes("player.addEventListener('touchstart'"));
  assert.ok(js.includes("player.addEventListener('touchmove'"));
  assert.ok(js.includes("player.addEventListener('touchend'"));
  assert.ok(js.includes('deltaY < 0 ? 1 : -1'));
  assert.ok(js.includes('Math.abs(deltaY) < MEDIA_SWIPE_MIN_DISTANCE_PX'));
  assert.ok(js.includes("showMediaSwipeNotice(ui(offset < 0 ? '已经是第一个视频' : '已经是最后一个视频'))"));
  assert.ok(html.includes('id="mediaSwipeNotice" role="status" aria-live="polite"'));
  assert.ok(html.includes('<span>操作</span>'));
  assert.ok(!js.includes("fileActionLabel(entry), 'play'"));
  assert.ok(js.includes("function fileActionLabel(entry)"));
  assert.ok(js.includes("name.setAttribute('aria-label', fileActionLabel(entry))"));
  assert.ok(js.includes("preview.setAttribute('aria-label', fileActionLabel(entry))"));
  assert.ok(js.includes("player.preload = 'metadata'"));
  assert.ok(js.includes("player.removeAttribute('src')"));
  assert.ok(css.includes('.file-browser > header, .file-row'));
  assert.ok(css.includes('.file-actions'));
  assert.ok(js.includes("iconButton('更名', 'edit', 'file-rename-button')"));
  assert.ok(js.includes("iconButton('删除', 'trash', 'file-delete-button')"));
  assert.ok(js.includes("api(`/file-service/entry?path=${encodeURIComponent(entry.path)}`"));
  assert.ok(js.includes("method: 'DELETE'"));
  assert.ok(js.includes("之后可从 Windows 回收站恢复"));
  assert.ok(css.includes('.file-delete-button:hover'));
  assert.ok(js.includes("const meta = element('span', 'file-card-meta')"));
  assert.ok(js.includes('meta.append(type, actions)'));
  assert.ok(css.includes('.file-browser.thumbnail-view .file-card-meta { grid-column: 1; grid-row: 3;'));
  assert.ok(css.includes('.file-browser.thumbnail-view .file-name strong { display: block; overflow: hidden; white-space: nowrap;'));
  assert.ok(css.includes('.file-browser.thumbnail-view .file-rename-button, .file-browser.thumbnail-view .file-delete-button { width: 30px; height: 30px; min-height: 30px; }'));
  assert.ok(css.includes('.file-browser.thumbnail-view .file-size { display: none; }'));
  assert.ok(i18n.includes("'更名': 'Rename'"));
  assert.ok(i18n.includes("'已经是第一个视频': 'This is the first video'"));
  assert.ok(i18n.includes("'已经是最后一个视频': 'This is the last video'"));
  assert.ok(i18n.includes("'浏览器不支持全屏播放': 'Fullscreen playback is not supported by this browser'"));
  assert.ok(css.includes('.file-browser.thumbnail-view #fileRows'));
  assert.ok(css.includes('aspect-ratio: 9 / 16'));
  assert.ok(css.includes('.file-browser.thumbnail-view #fileRows { grid-template-columns: repeat(2, minmax(0, 1fr));'));
  assert.ok(css.includes('.file-thumbnail img, .file-thumbnail video'));
  assert.ok(css.includes('.file-thumbnail video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; }'));
  assert.ok(css.includes('.media-stage video'));
  assert.ok(css.includes('touch-action: pan-x pinch-zoom'));
  assert.ok(css.includes('.media-dialog-shell:fullscreen'));
  assert.ok(css.includes('.media-swipe-notice.show'));
  assert.ok(i18n.includes("'文件服务': 'File Service'"));
});

test('scene operation progress keeps the fixed backend denominator', () => {
  const start = js.indexOf('function renderOperationProgress');
  const end = js.indexOf('\nfunction renderSceneProgress', start);
  assert.ok(start >= 0 && end > start);
  const values = {};
  const bar = { style: {} };
  const log = { replaceChildren() {}, append() {}, scrollTop: 0, scrollHeight: 0 };
  const sandbox = {
    text: (id, value) => { values[id] = value; },
    byId: (id) => id === 'sceneProgressBar' ? bar : log,
    targetName: () => '测试服务',
    element: () => ({ append() {} }),
    formatDate: () => 'now',
    ui: (value) => value,
  };
  vm.runInNewContext(js.slice(start, end), sandbox);
  const operation = {
    status: 'running',
    steps: [
      { status: 'succeeded', action: 'stop' },
      { status: 'succeeded', action: 'stop' },
      { status: 'succeeded', action: 'stop' },
      { status: 'succeeded', action: 'stop' },
      { status: 'running', action: 'start' },
    ],
  };
  sandbox.renderOperationProgress(operation, 5, {});
  assert.equal(values.sceneProgressPercent, '80%');
  assert.equal(bar.style.width, '80%');
  operation.steps = [
    { status: 'succeeded', action: 'stop' },
    { status: 'running', action: 'start' },
  ];
  sandbox.renderOperationProgress(operation, 1, {});
  assert.equal(values.sceneProgressPercent, '50%');
  assert.equal(bar.style.width, '50%');
});

test('scene progress freezes the legacy fallback and prefers backend total steps', () => {
  const openStart = js.indexOf('function openSceneProgress');
  const openEnd = js.indexOf('\nfunction renderOperationProgress', openStart);
  const renderStart = js.indexOf('function renderSceneProgress');
  const renderEnd = js.indexOf('\nfunction renderStopAllProgress', renderStart);
  assert.ok(openStart >= 0 && openEnd > openStart);
  assert.ok(renderStart >= 0 && renderEnd > renderStart);
  const totals = [];
  const sandbox = {
    state: {
      services: [
        { id: 'target-running', status: { state: 'running' } },
        { id: 'target-stopped', status: { state: 'stopped' } },
        { id: 'target-unknown', status: { state: 'unknown' } },
        { id: 'outside-running', status: { state: 'running' } },
        { id: 'outside-stopped', status: { state: 'stopped' } },
      ],
    },
    openOperationProgress: () => {},
    renderOperationProgress: (_operation, total) => totals.push(total),
  };
  const source = `let sceneProgressExpectedTotal = null;\n${js.slice(openStart, openEnd)}\n${js.slice(renderStart, renderEnd)}`;
  vm.runInNewContext(source, sandbox);
  const scene = { name: '测试场景', service_ids: ['target-running', 'target-stopped', 'target-unknown'] };
  sandbox.openSceneProgress(scene, 'operation-id');
  sandbox.state.services.forEach((service) => { service.status.state = 'stopped'; });
  sandbox.renderSceneProgress(scene, { status: 'running', steps: [] });
  sandbox.renderSceneProgress(scene, { status: 'running', steps: [], total_steps: 7 });
  assert.deepEqual(totals, [3, 7]);
});

test('user management lists accounts and wires account actions', () => {
  assert.ok(html.includes('data-page="users"'));
  assert.ok(html.includes('id="userRows"'));
  assert.ok(html.includes('id="addUserButton"'));
  assert.ok(html.includes('id="userDialog"'));
  assert.ok(html.includes('id="passwordDialog"'));
  for (const id of ['newUserPassword', 'newUserPasswordConfirm', 'changedPassword', 'changedPasswordConfirm']) {
    assert.match(html, new RegExp(`id="${id}"[^>]*minlength="4"`));
  }
  assert.ok(js.includes("api('/users'"));
  assert.ok(js.includes("method: 'PUT'"));
  assert.ok(js.includes("current_session_invalidated"));
  assert.ok(js.includes('不能删除当前登录用户'));
  assert.ok(css.includes('.user-table .table-head'));
  assert.ok(css.includes('.user-avatar'));
});

test('Chinese and English UI supports automatic detection and a remembered manual switch', () => {
  assert.equal((html.match(/data-language-select/g) || []).length, 2);
  assert.ok(html.indexOf('gpu-layout.js') < html.indexOf('app.js'));
  assert.ok(html.indexOf('monitor-chart.js') < html.indexOf('app.js'));
  assert.ok(html.indexOf('i18n.js') < html.indexOf('app.js'));
  assert.ok(html.includes('styles.css?v=20260920-8'));
  assert.ok(html.includes('i18n.js?v=20260920-5'));
  assert.ok(html.includes('app.js?v=20260920-8'));
  assert.ok(i18n.includes("navigator.languages"));
  assert.ok(i18n.includes("localStorage.getItem(STORAGE_KEY)"));
  assert.ok(i18n.includes("localStorage.setItem(STORAGE_KEY, next)"));
  assert.ok(i18n.includes("startsWith('zh') ? 'zh' : 'en'"));
  assert.ok(i18n.includes("'已登记服务': 'Registered Services'"));
  assert.ok(i18n.includes("'部分启动': 'Partially Started'"));
  assert.ok(i18n.includes("'用户管理': 'User Management'"));
  assert.ok(js.includes("headers.set('Accept-Language', window.axisI18n.language)"));
  assert.ok(js.includes("document.addEventListener('languagechange'"));
  assert.ok(js.includes("userElement('option'"));
  assert.ok(js.includes("window.axisI18n.language, ...state.scenes"));
  assert.ok(js.includes("ui(step.action === 'start' ? '启动' : '停止')"));
  assert.ok(i18n.includes("'服务脚本执行失败': 'The service script failed.'"));
  assert.ok(i18n.includes("'管理脚本不存在': 'The management script was not found.'"));
  assert.ok(i18n.includes("'Unable to start the management script: $1'"));
  assert.ok(css.includes('.language-select'));
});

test('mobile navigation closes when the user taps outside the sidebar', () => {
  assert.ok(html.includes('id="sidebarBackdrop"'));
  assert.ok(js.includes("sidebarBackdrop.addEventListener('click'"));
  assert.ok(js.includes("event.key === 'Escape'"));
  assert.ok(css.includes('.sidebar-backdrop.open'));
  assert.ok(i18n.includes("'关闭导航': 'Close navigation'"));
});

test('system settings provides three persistent display styles', () => {
  assert.ok(html.includes('data-page="settings"'));
  assert.ok(html.includes('id="page-settings"'));
  for (const value of ['matrix', 'aurora', 'obsidian']) assert.ok(html.includes(`data-theme-option="${value}"`));
  assert.equal((html.match(/data-theme-option=/g) || []).length, 3);
  assert.ok(html.indexOf('theme.js') < html.indexOf('styles.css'));
  assert.ok(theme.includes("const STORAGE_KEY = 'axis_manager_theme'"));
  assert.ok(theme.includes("document.documentElement.dataset.theme = theme"));
  assert.ok(css.includes(':root[data-theme="aurora"]'));
  assert.ok(css.includes(':root[data-theme="obsidian"]'));
  assert.ok(css.includes('.theme-option.selected'));
  for (const staleColor of ['background: #101516', 'background: #090d0e', 'background: #111617', 'background: #080a0b', 'color: #bac3c1']) {
    assert.ok(!css.includes(staleColor), `fixed Matrix Green surface remains: ${staleColor}`);
  }
  for (const label of ['矩阵绿', '极光蓝', '曜石金']) assert.ok(i18n.includes(`'${label}':`));
});

test('total system power separates measured sensors from the estimate', () => {
  // 曲线画的是合计值，但界面必须同时说明其中多少是实测、多少是估算，
  // 不能把估算伪装成传感器读数。
  assert.ok(js.includes("monitorDetail('实测 / 估算')"));
  assert.ok(js.includes('function powerBreakdownText(power)'));
  assert.ok(js.includes('powerBreakdownText(host.power)'));
  assert.ok(js.includes("description: '实测传感器加上主板、内存、供电与电源损耗的估算'"));
  assert.ok(!js.includes("description: 'GPU 与系统已暴露功耗传感器总和'"));
  assert.ok(js.includes('measured_power_w: staleSnapshot ? null : host?.power?.measured_w'));
  assert.ok(js.includes('estimated_power_w: staleSnapshot ? null : host?.power?.estimated_w'));
  assert.ok(i18n.includes("'实测 / 估算': 'Measured / estimated'"));
});

test('host power stacks 3090, 4090 and CPU below total, with other as a value only', () => {
  for (const label of ['3090 功率', '4090 功率', 'CPU 功率', '其他功率']) {
    assert.ok(js.includes(`label: '${label}'`));
    assert.ok(i18n.includes(`'${label}':`));
  }
  assert.ok(js.includes('powerTotal: true, powerSeries'));
  assert.ok(js.includes('bindCorrelationCursor([charts[0]], announcement)'));
  assert.ok(js.includes("plotGetter: stackedPower('gpu3090')"));
  assert.ok(js.includes("plotGetter: stackedPower('gpu3090', 'gpu4090')"));
  assert.ok(js.includes("plotGetter: stackedPower('gpu3090', 'gpu4090', 'cpu')"));
  assert.ok(js.includes("getter: powerPart('other'), valueOnly: true"));
  assert.ok(js.includes('spec.powerSeries?.filter((series) => series.plotGetter).forEach'));
  assert.ok(js.includes('monitorChart.holdPowerReadings(samples'));
  assert.ok(js.includes('monitorChart.stackedPower(sample, keys)'));
  assert.ok(js.includes('maximum: powerMaximum'));
  assert.ok(js.includes('cpu_power_w: staleSnapshot ? null : host?.power?.cpu_package_w'));
  assert.ok(js.includes("element('small', '', '窗口耗电量')"));
  assert.ok(js.includes("element('small', '', '窗口电费')"));
  assert.ok(js.includes('monitorChart.energyKWh(samples, (sample) => sample?.total_power_w'));
  assert.ok(js.includes('(kWh * rate).toFixed(3)'));
  assert.ok(css.includes('.monitor-host-group .power-chart-total { grid-column: 1 / -1;'));
});

test('electricity rate settings use the persisted power model', () => {
  assert.ok(html.includes('id="electricityRateForm"'));
  assert.ok(html.includes('id="electricityRate"'));
  assert.ok(js.includes("api('/power-model/electricity-rate', { method: 'PUT'"));
  assert.ok(js.includes('electricity_rate_yuan_per_kwh'));
});

test('system settings offers a wall-meter calibration for total system power', () => {
  assert.ok(html.includes('id="powerCalibrationForm"'));
  assert.ok(html.includes('id="powerCalibrationPoint"'));
  assert.ok(html.includes('id="powerCalibrationWall"'));
  assert.ok(html.includes('id="clearPowerCalibrationButton"'));
  for (const id of ['powerModelSource', 'powerModelSplit', 'powerModelBaseline', 'powerModelEfficiency']) {
    assert.ok(html.includes(`id="${id}"`), `settings page is missing ${id}`);
  }
  assert.match(html, /id="powerCalibrationWall"[^>]*max="5000"/);
  assert.ok(js.includes("api('/power-model'"));
  assert.ok(js.includes("api('/power-model/calibration', { method: 'POST'"));
  assert.ok(js.includes("api('/power-model/calibration', { method: 'DELETE' })"));
  assert.ok(js.includes('refreshPowerModel()'));
  assert.ok(css.includes('.power-calibration-form'));
  assert.ok(i18n.includes("'整机功耗校准': 'Total system power calibration'"));
});

test('system settings shows the runtime version and GitHub project link', () => {
  assert.ok(html.includes('id="systemVersion"'));
  assert.ok(html.includes('https://github.com/pxzleo/aiworkstationmanager'));
  assert.ok(html.includes('rel="noopener noreferrer"'));
  assert.ok(js.includes("api('/health', { resource: 'system-info' })"));
  assert.ok(js.includes("dataText('systemVersion', health.version, '版本未知')"));
  assert.ok(css.includes('.system-information-list'));
  assert.ok(i18n.includes("'系统信息': 'System information'"));
  assert.ok(i18n.includes("'系统版本': 'System version'"));
});

test('legacy adapter, discovery and service-log UI are absent', () => {
  for (const value of ['脚本导入', '运行适配器', 'logSourceSelect', '现有 WebUI']) {
    assert.ok(!html.includes(value), `legacy UI remains: ${value}`);
  }
  for (const value of ['/discovery/scripts', '/log-sources', '/webuis', '/environments']) {
    assert.ok(!js.includes(value), `legacy endpoint remains: ${value}`);
  }
});
