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
  for (const value of ['已登记服务', '服务名称', '管理脚本绝对路径', 'GPU 展示标签', '服务端口', 'UI 地址', '健康检查地址', '响应必须包含']) {
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
  for (const label of ['处理器负载', '系统内存', '提交内存', '页面文件', '核心负载', '核心频率', '功率', '温度', '显存占用', '存储容量', '磁盘读取', '网络下载', 'WSL 内存', 'Docker 容器', '平均', '峰值', '最低']) assert.ok(js.includes(label), `missing monitor label ${label}`);
  for (const selector of ['.monitor-group', '.monitor-chart-grid', '.chart-y-axis', '.chart-x-axis', '.chart-statistics']) assert.ok(css.includes(selector), `missing ${selector}`);
  assert.ok(js.includes('function chartAxisValues'));
  assert.ok(html.includes('id="historyRangeSelect"'));
  assert.ok(html.includes('id="monitorTabbar"'));
  for (const view of ['summary', 'gpu', 'host', 'system']) assert.ok(html.includes(`data-monitor-view="${view}"`));
  for (const minutes of ['15', '60', '1440']) assert.ok(html.includes(`data-history-minutes="${minutes}"`));
  assert.ok(js.includes('monitorChart.axisLabels(state.historyWindowMinutes)'));
  assert.ok(js.includes('monitorChart.windowMilliseconds(state.historyWindowMinutes)'));
  assert.ok(js.includes("`/history?window=${state.historyWindowMinutes}m`"));
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
  assert.ok(js.includes("'未检测到 NVIDIA GPU。'"));
  assert.ok(!js.includes('bindGpuSlots'));
  assert.ok(!js.includes('slots: [null, null]'));
  assert.ok(css.includes('repeat(auto-fit, minmax(min(100%, 440px), 1fr))'));
  assert.ok(css.includes('@keyframes gpuCardIn'));
  assert.ok(css.includes('@media (prefers-reduced-motion: reduce)'));
  assert.ok(i18n.includes("'未检测到 NVIDIA GPU。': 'No NVIDIA GPU detected.'"));
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
  assert.ok(js.includes("auditRequest.status === 'fulfilled' ? auditRequest.value.events || [] : []"));
  assert.ok(js.includes("operation.requested_by || ''"));
  assert.ok(js.includes("'operation-actor'"));
  assert.ok(js.includes("操作账号"));
  assert.ok(css.includes('.operation-actor'));
  assert.equal((js.match(/operationActor\(/g) || []).length, 3);
  assert.ok(css.includes('.operation-row > div { min-width: 0; }'));
  assert.ok(css.includes('.timeline li > div { min-width: 0; }'));
  assert.ok(css.includes('overflow-wrap: anywhere'));
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
  assert.ok(html.includes('styles.css?v=20260830-11'));
  assert.ok(html.includes('i18n.js?v=20260830-10'));
  assert.ok(html.includes('app.js?v=20260830-11'));
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
