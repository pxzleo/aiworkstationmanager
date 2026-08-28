'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

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
});

test('registered service editor exposes the agreed fields and actions', () => {
  for (const value of ['已登记服务', '服务名称', '管理脚本绝对路径', 'GPU 展示标签', '服务端口', 'UI 地址']) {
    assert.ok(html.includes(value), `missing ${value}`);
  }
  for (const action of ['start', 'stop', 'restart']) assert.ok(js.includes(action));
  assert.ok(js.includes("const SERVICE_INTERVAL_MS = 5000;"));
  assert.ok(js.includes("/registered-services"));
  assert.ok(js.includes("service.busy ? '操作中'"));
  assert.ok(js.includes("service.operation_pending || actionGuard.pending"));
  assert.ok(js.includes("检查状态"));
  assert.ok(js.includes("/status"));
  assert.ok(html.includes('id="stopAllServicesButton"'));
  assert.ok(js.includes('/registered-services/actions/stop-all'));
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
  assert.ok(js.includes('state.gpus.forEach((gpu, position)'));
  assert.ok(js.includes('function createMonitorChart'));
  assert.ok(js.includes('function createMonitorGroup'));
  assert.ok(js.includes('function renderMonitorDetails'));
  for (const label of ['已累计', '图表点', '最近更新', '监控设备', '处理器负载', '系统内存', '核心负载', '核心频率', '功率', '温度', '显存占用', '平均', '峰值', '最低']) assert.ok(js.includes(label), `missing monitor label ${label}`);
  for (const selector of ['.monitor-group', '.monitor-chart-grid', '.chart-y-axis', '.chart-x-axis', '.chart-statistics']) assert.ok(css.includes(selector), `missing ${selector}`);
  assert.ok(js.includes('function chartAxisValues'));
  assert.ok(html.includes('id="historyRangeSelect"'));
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
  assert.ok(html.includes('停止未选服务'));
  assert.ok(js.includes('/scenes'));
  assert.ok(js.includes('/operations?limit=50'));
  assert.ok(js.includes('/scenes/reorder'));
  assert.ok(js.includes("panel.draggable"));
  assert.ok(js.includes("dragstart"));
  assert.ok(js.includes("上移"));
  assert.ok(html.includes('id="sceneProgressDialog"'));
  assert.ok(html.includes('id="cancelSceneSwitchButton"'));
  assert.ok(js.includes('/cancel'));
  assert.ok(js.includes("const progress = terminal ? 100"));
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
  assert.ok(css.includes('.scene-selector { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }'));
  assert.ok(css.includes('border-radius: 12px'));
  assert.ok(css.includes('.scene-panel.selected:hover'));
  assert.ok(css.includes('transform: translateY(-2px)'));
  assert.ok(html.includes('id="overviewSceneSelect"'));
  assert.ok(html.includes('<option value="">切换场景</option>'));
  assert.ok(js.includes('handleOverviewSceneChange'));
  assert.ok(css.includes('width: 8em'));
  assert.ok(!html.includes('id="switchSceneButton"'));
  assert.ok(!js.includes("switchButton.onclick = () => navigate('scenes')"));
  assert.ok(html.includes('只记录服务启停与场景切换'));
  assert.ok(!html.includes('管理审计'));
  assert.ok(!js.includes('/audit?limit=100'));
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

test('legacy adapter, discovery and service-log UI are absent', () => {
  for (const value of ['脚本导入', '运行适配器', 'logSourceSelect', '现有 WebUI']) {
    assert.ok(!html.includes(value), `legacy UI remains: ${value}`);
  }
  for (const value of ['/discovery/scripts', '/log-sources', '/webuis', '/environments']) {
    assert.ok(!js.includes(value), `legacy endpoint remains: ${value}`);
  }
});
