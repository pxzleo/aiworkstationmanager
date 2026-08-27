'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const js = fs.readFileSync(path.join(root, 'app.js'), 'utf8');

test('registered service editor exposes the agreed fields and actions', () => {
  for (const value of ['已登记服务', '服务名称', '管理脚本绝对路径', 'GPU 展示标签', '服务端口', 'UI 地址']) {
    assert.ok(html.includes(value), `missing ${value}`);
  }
  for (const action of ['start', 'stop', 'restart']) assert.ok(js.includes(action));
  assert.ok(js.includes("const SERVICE_INTERVAL_MS = 5000;"));
  assert.ok(js.includes("/registered-services"));
});

test('scene editor and management log remain wired', () => {
  assert.ok(html.includes('id="sceneServiceChoices"'));
  assert.ok(html.includes('停止未选服务'));
  assert.ok(js.includes('/scenes'));
  assert.ok(js.includes('/operations?limit=50'));
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
