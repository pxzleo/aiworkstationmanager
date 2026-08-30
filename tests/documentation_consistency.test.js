'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const read = (relative) => fs.readFileSync(path.join(root, relative), 'utf8');

test('local documentation links resolve to repository files', () => {
  for (const relative of ['README.md', 'README.zh-CN.md', 'DEVELOPMENT.md', 'DEVELOPMENT.en.md', 'scriptspec.md', 'SCRIPT_REQUIREMENTS.en.md']) {
    const source = read(relative);
    const links = [...source.matchAll(/\[[^\]]+\]\(([^)]+)\)/g)].map((match) => match[1]);
    for (const link of links) {
      if (/^(?:https?:|#)/.test(link)) continue;
      assert.ok(fs.existsSync(path.resolve(root, path.dirname(relative), decodeURIComponent(link))), `${relative} has a broken link: ${link}`);
    }
  }
});

test('Chinese and English development guides cover every implemented API route', () => {
  const app = read('workstation_manager/app.py');
  const documents = [['DEVELOPMENT.md', read('DEVELOPMENT.md')], ['DEVELOPMENT.en.md', read('DEVELOPMENT.en.md')]];
  const routes = [...app.matchAll(/@app\.(get|post|put|delete)\("(\/api\/v1\/[^"?]+)"/g)]
    .map((match) => [match[1].toUpperCase(), match[2].replace(/\{[^}]+\}/g, '{id}')]);
  for (const [name, source] of documents) {
    for (const [method, route] of routes) {
      assert.ok(source.includes(`| ${method} | \`${route}\``), `${name} is missing ${method} ${route}`);
    }
  }
});

test('development guides cover API request fields and query bounds', () => {
  const app = read('workstation_manager/app.py');
  const payloadBlock = app.slice(app.indexOf('class Credentials'), app.indexOf('class RequestBodyLimitMiddleware'));
  const fields = [...payloadBlock.matchAll(/^    ([a-z][a-z0-9_]+):/gm)].map((match) => match[1]);
  for (const relative of ['DEVELOPMENT.md', 'DEVELOPMENT.en.md']) {
    const source = read(relative);
    for (const field of new Set(fields)) assert.ok(source.includes(`\`${field}\``), `${relative} is missing payload field ${field}`);
    assert.ok(source.includes('1m..1440m'), `${relative} is missing the history window range`);
    assert.ok(source.includes('1..500'), `${relative} is missing the list limit range`);
  }
});

test('example configuration documents every Settings field', () => {
  const configSource = read('workstation_manager/config.py');
  const settingsBlock = configSource.slice(configSource.indexOf('class Settings:'), configSource.indexOf('    @property'));
  const fields = [...settingsBlock.matchAll(/^    ([a-z][a-z0-9_]+):/gm)].map((match) => match[1]);
  const example = JSON.parse(read('config/settings.example.json'));
  assert.deepEqual(Object.keys(example).sort(), fields.sort());
});

test('development guides document every supported environment variable', () => {
  const configSource = read('workstation_manager/config.py');
  const variables = [...configSource.matchAll(/"(WM_[A-Z_]+)"/g)].map((match) => match[1]);
  for (const relative of ['DEVELOPMENT.md', 'DEVELOPMENT.en.md']) {
    const source = read(relative);
    for (const variable of new Set(variables)) assert.ok(source.includes(variable), `${relative} is missing ${variable}`);
  }
  assert.ok(read('DEVELOPMENT.md').includes('当前仅进行格式解析并保存'));
  assert.ok(read('DEVELOPMENT.en.md').includes('currently parsed and stored only'));
});

test('release contains runtime language assets and both documentation languages', () => {
  const release = read('Build-Release.ps1');
  for (const file of ['i18n.js', 'gpu-layout.js', 'monitor-chart.js', 'theme.js', 'README.md', 'README.zh-CN.md', 'DEVELOPMENT.md', 'DEVELOPMENT.en.md', 'scriptspec.md', 'SCRIPT_REQUIREMENTS.en.md']) {
    assert.ok(release.includes(`"${file}"`), `release is missing ${file}`);
  }
  const schema = /SCHEMA_VERSION = (\d+)/.exec(read('workstation_manager/database.py'))[1];
  assert.ok(read('DEVELOPMENT.md').includes(`schema 为 ${schema}`));
  assert.ok(read('DEVELOPMENT.en.md').includes(`schema is ${schema}`));
});

test('application version is semantic and documented as the single UI source', () => {
  const packageSource = read('workstation_manager/__init__.py');
  const version = /__version__ = "(\d+\.\d+\.\d+)"/.exec(packageSource)?.[1];
  assert.ok(version, 'application version must use semantic major.minor.patch format');
  assert.ok(read('DEVELOPMENT.md').includes('workstation_manager/__init__.py'));
  assert.ok(read('DEVELOPMENT.en.md').includes('workstation_manager/__init__.py'));
});

test('distributable documentation avoids machine-specific and source-only claims', () => {
  const chinese = read('README.zh-CN.md'); const english = read('README.md');
  assert.ok(!chinese.includes('当前工作站已'));
  assert.ok(!english.includes('This workstation currently has'));
  assert.ok(!chinese.includes('## API'));
  assert.ok(!english.includes('## API'));
  assert.ok(chinese.includes('[开发文档、完整 API 与高级配置](DEVELOPMENT.md)'));
  assert.ok(english.includes('[Development, complete API, and advanced configuration](DEVELOPMENT.en.md)'));
});

test('English main README and Chinese README keep previews and script specification links', () => {
  const chinese = read('README.zh-CN.md'); const english = read('README.md');
  for (const image of ['docs/1.png', 'docs/2.png', 'docs/3.png', 'docs/4.png', 'docs/5.png']) {
    assert.ok(chinese.includes(`(${image})`), `Chinese README is missing ${image}`);
    assert.ok(english.includes(`(${image})`), `English README is missing ${image}`);
  }
  assert.ok(chinese.includes('[scriptspec.md](scriptspec.md)'));
  assert.ok(english.includes('[scriptspec.md](scriptspec.md)'));
  assert.ok(english.includes('[简体中文](README.zh-CN.md)'));
});
