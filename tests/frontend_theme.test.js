'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.resolve(__dirname, '..', 'theme.js'), 'utf8');

function loadTheme(savedTheme = null, { getError = null, setError = null } = {}) {
  const saved = new Map();
  if (savedTheme !== null) saved.set('axis_manager_theme', savedTheme);
  const themeColor = { value: '', setAttribute(name, value) { if (name === 'content') this.value = value; } };
  const document = {
    documentElement: { dataset: {} },
    addEventListener() {}, dispatchEvent() {},
    querySelector: () => themeColor,
    querySelectorAll: () => [],
  };
  const context = {
    window: {}, document,
    localStorage: {
      getItem(key) { if (getError) throw getError; return saved.get(key) ?? null; },
      setItem(key, value) { if (setError) throw setError; saved.set(key, value); },
    },
    CustomEvent: class CustomEvent {},
  };
  vm.runInNewContext(source, context);
  return { theme: context.window.axisTheme, document, saved, themeColor };
}

test('theme defaults to Matrix Green and rejects unknown saved values', () => {
  assert.equal(loadTheme().theme.theme, 'matrix');
  assert.equal(loadTheme('unknown').theme.theme, 'matrix');
});

test('saved theme is applied before the page renders', () => {
  const loaded = loadTheme('aurora');
  assert.equal(loaded.theme.theme, 'aurora');
  assert.equal(loaded.document.documentElement.dataset.theme, 'aurora');
  assert.equal(loaded.themeColor.value, '#080b16');
});

test('manual theme changes are applied and persisted', () => {
  const loaded = loadTheme();
  assert.equal(loaded.theme.applyTheme('obsidian'), 'obsidian');
  assert.equal(loaded.document.documentElement.dataset.theme, 'obsidian');
  assert.equal(loaded.saved.get('axis_manager_theme'), 'obsidian');
});

test('theme remains usable when browser storage reads are blocked', () => {
  let loaded;
  assert.doesNotThrow(() => { loaded = loadTheme(null, { getError: new Error('SecurityError') }); });
  assert.equal(loaded.theme.theme, 'matrix');
  assert.equal(loaded.document.documentElement.dataset.theme, 'matrix');
});

test('theme and accessibility state still update when browser storage writes are blocked', () => {
  const loaded = loadTheme(null, { setError: new Error('SecurityError') });
  assert.doesNotThrow(() => loaded.theme.applyTheme('aurora'));
  assert.equal(loaded.theme.theme, 'aurora');
  assert.equal(loaded.document.documentElement.dataset.theme, 'aurora');
});
