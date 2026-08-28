'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const read = (relative) => fs.readFileSync(path.join(root, relative), 'utf8');

test('local documentation links resolve to repository files', () => {
  for (const relative of ['README.md', 'README.en.md', '脚本要求.md', 'SCRIPT_REQUIREMENTS.en.md']) {
    const source = read(relative);
    const links = [...source.matchAll(/\[[^\]]+\]\(([^)]+)\)/g)].map((match) => match[1]);
    for (const link of links) {
      if (/^(?:https?:|#)/.test(link)) continue;
      assert.ok(fs.existsSync(path.resolve(root, path.dirname(relative), decodeURIComponent(link))), `${relative} has a broken link: ${link}`);
    }
  }
});

test('Chinese and English API documentation cover every implemented API route', () => {
  const app = read('workstation_manager/app.py');
  const documents = [['README.md', read('README.md')], ['README.en.md', read('README.en.md')]];
  const routes = [...app.matchAll(/@app\.(?:get|post|put|delete)\("(\/api\/v1\/[^"?]+)"/g)]
    .map((match) => match[1].replace(/\{[^}]+\}/g, '{id}'));
  for (const [name, source] of documents) {
    for (const route of new Set(routes)) assert.ok(source.includes(route), `${name} is missing API route ${route}`);
  }
});

test('example configuration documents every Settings field', () => {
  const configSource = read('workstation_manager/config.py');
  const settingsBlock = configSource.slice(configSource.indexOf('class Settings:'), configSource.indexOf('    @property'));
  const fields = [...settingsBlock.matchAll(/^    ([a-z][a-z0-9_]+):/gm)].map((match) => match[1]);
  const example = JSON.parse(read('config/settings.example.json'));
  assert.deepEqual(Object.keys(example).sort(), fields.sort());
});

test('release contains runtime language assets and both documentation languages', () => {
  const release = read('Build-Release.ps1');
  for (const file of ['i18n.js', 'README.md', 'README.en.md', '脚本要求.md', 'SCRIPT_REQUIREMENTS.en.md']) {
    assert.ok(release.includes(`"${file}"`), `release is missing ${file}`);
  }
  const schema = /SCHEMA_VERSION = (\d+)/.exec(read('workstation_manager/database.py'))[1];
  assert.ok(read('README.md').includes(`schema 为 ${schema}`));
  assert.ok(read('README.en.md').includes(`schema is ${schema}`));
});

test('distributable documentation avoids machine-specific and source-only claims', () => {
  const chinese = read('README.md'); const english = read('README.en.md');
  assert.ok(!chinese.includes('当前工作站已'));
  assert.ok(!english.includes('This workstation currently has'));
  assert.ok(chinese.includes('源码检出中运行测试（发布包不包含测试目录）'));
  assert.ok(english.includes('Release packages do not include the test suite'));
  assert.ok(chinese.includes('名称不区分大小写，再按 ID'));
  assert.ok(english.includes('case-insensitive service name, then ID'));
});
