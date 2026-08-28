'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.resolve(__dirname, '..', 'i18n.js'), 'utf8');

function loadI18n({ browserLanguage = 'en-US', savedLanguage = null } = {}) {
  const saved = new Map();
  if (savedLanguage) saved.set('axis_manager_language', savedLanguage);
  const document = {
    documentElement: { lang: '', nodeType: 9 },
    addEventListener() {},
    dispatchEvent() {},
    createTreeWalker: () => ({ nextNode: () => null }),
    querySelectorAll: () => [],
  };
  const context = {
    window: {}, document,
    navigator: { languages: [browserLanguage], language: browserLanguage },
    localStorage: { getItem: (key) => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value) },
    CustomEvent: class CustomEvent {},
    Node: { TEXT_NODE: 3, ELEMENT_NODE: 1, DOCUMENT_NODE: 9 },
    NodeFilter: { SHOW_ELEMENT: 1, SHOW_TEXT: 4 },
  };
  vm.runInNewContext(source, context);
  return { i18n: context.window.axisI18n, saved, document };
}

test('browser language is detected and a saved choice takes precedence', () => {
  assert.equal(loadI18n({ browserLanguage: 'zh-CN' }).i18n.language, 'zh');
  assert.equal(loadI18n({ browserLanguage: 'fr-FR' }).i18n.language, 'en');
  assert.equal(loadI18n({ browserLanguage: 'zh-CN', savedLanguage: 'en' }).i18n.language, 'en');
});

test('translation works in both directions and preserves dynamic causes', () => {
  const { i18n, saved, document } = loadI18n();
  assert.equal(i18n.translate('已登记服务'), 'Registered Services');
  assert.equal(i18n.translate('脚本动作 start 执行超时'), 'Script action start timed out');
  assert.equal(
    i18n.translate('无法启动管理脚本: access denied'),
    'Unable to start the management script: access denied',
  );
  i18n.setLanguage('zh');
  assert.equal(i18n.translate('Registered Services'), '已登记服务');
  assert.equal(saved.get('axis_manager_language'), 'zh');
  assert.equal(document.documentElement.lang, 'zh-CN');
});
