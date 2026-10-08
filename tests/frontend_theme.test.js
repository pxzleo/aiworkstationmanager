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
  const colorScheme = { value: '', setAttribute(name, value) { if (name === 'content') this.value = value; } };
  const handlers = {};
  const select = { value: '', addEventListener(name, handler) { this[name] = handler; } };
  const options = ['shadcn-dark', 'shadcn-light', 'matrix', 'aurora', 'obsidian'].map((themeOption) => ({
    dataset: { themeOption }, selected: false, pressed: '',
    classList: { toggle(name, value) { options.find((option) => option.dataset.themeOption === themeOption).selected = value; } },
    setAttribute(name, value) { if (name === 'aria-pressed') this.pressed = value; },
    addEventListener(name, handler) { this[name] = handler; },
  }));
  const document = {
    documentElement: { dataset: {} },
    addEventListener(name, handler) { handlers[name] = handler; }, dispatchEvent() {},
    querySelector: (selector) => selector.includes('color-scheme') ? colorScheme : themeColor,
    querySelectorAll: (selector) => selector === '[data-theme-select]' ? [select] : options,
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
  handlers.DOMContentLoaded();
  return { theme: context.window.axisTheme, document, saved, themeColor, colorScheme, options, select };
}

test('theme defaults to Shadcn Dark and rejects unknown saved values', () => {
  assert.equal(loadTheme().theme.theme, 'shadcn-dark');
  assert.equal(loadTheme('unknown').theme.theme, 'shadcn-dark');
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
  assert.equal(loaded.theme.theme, 'shadcn-dark');
  assert.equal(loaded.document.documentElement.dataset.theme, 'shadcn-dark');
});

test('settings and topbar stay synchronized and restore the light native color scheme', () => {
  const loaded = loadTheme();
  assert.equal(loaded.select.value, 'shadcn-dark');
  const light = loaded.options.find((option) => option.dataset.themeOption === 'shadcn-light');
  light.click();
  assert.equal(loaded.select.value, 'shadcn-light');
  assert.equal(light.selected, true);
  assert.equal(light.pressed, 'true');
  assert.equal(loaded.colorScheme.value, 'light');
  assert.equal(loaded.themeColor.value, '#ffffff');
  const restored = loadTheme(loaded.saved.get('axis_manager_theme'));
  assert.equal(restored.select.value, 'shadcn-light');
  assert.equal(restored.colorScheme.value, 'light');
  restored.select.value = 'shadcn-dark';
  restored.select.change();
  assert.equal(restored.colorScheme.value, 'dark');
  assert.equal(restored.options.find((option) => option.dataset.themeOption === 'shadcn-light').pressed, 'false');
});

test('all existing saved themes remain valid', () => {
  for (const theme of ['matrix', 'aurora', 'obsidian']) assert.equal(loadTheme(theme).theme.theme, theme);
});

test('theme and accessibility state still update when browser storage writes are blocked', () => {
  const loaded = loadTheme(null, { setError: new Error('SecurityError') });
  assert.doesNotThrow(() => loaded.theme.applyTheme('aurora'));
  assert.equal(loaded.theme.theme, 'aurora');
  assert.equal(loaded.document.documentElement.dataset.theme, 'aurora');
});
