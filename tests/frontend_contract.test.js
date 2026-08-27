'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const js = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'styles.css'), 'utf8');

test('authentication UI accepts the configured four-character minimum', () => {
  assert.equal((html.match(/minlength="4"/g) || []).length, 2);
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

test('legacy adapter, discovery and service-log UI are absent', () => {
  for (const value of ['脚本导入', '运行适配器', 'logSourceSelect', '现有 WebUI']) {
    assert.ok(!html.includes(value), `legacy UI remains: ${value}`);
  }
  for (const value of ['/discovery/scripts', '/log-sources', '/webuis', '/environments']) {
    assert.ok(!js.includes(value), `legacy endpoint remains: ${value}`);
  }
});
