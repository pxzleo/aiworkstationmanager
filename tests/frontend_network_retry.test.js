'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.resolve(__dirname, '..', 'app.js'), 'utf8');
const start = source.indexOf('class ApiError');
const end = source.indexOf('const pages =');
const requestSource = source.slice(start, end);
const refreshLogsStart = source.indexOf('async function refreshLogs()');
const refreshLogsEnd = source.indexOf('async function refreshVideoJobs()', refreshLogsStart);
const refreshLogsSource = source.slice(refreshLogsStart, refreshLogsEnd);

function sandboxWith(fetchImplementation) {
  const lifecycle = new AbortController();
  const sandbox = {
    AbortController,
    Headers,
    URLSearchParams,
    setTimeout,
    clearTimeout,
    API_PREFIX: '/api/v1',
    REQUEST_TIMEOUT_MS: 5,
    READ_RETRY_DELAYS_MS: [1, 1],
    NETWORK_NOTICE_COOLDOWN_MS: 15000,
    requestGuard: {
      begin: () => ({ signal: lifecycle.signal }),
      isCurrent: (ticket) => !ticket.signal.aborted,
    },
    state: { csrfToken: null },
    window: { axisI18n: { language: 'zh' } },
    fetch: fetchImplementation,
    showAuth: () => {},
  };
  vm.runInNewContext(`${requestSource}\nthis.apiUnderTest = api;`, sandbox);
  sandbox.abortLifecycle = () => lifecycle.abort();
  return sandbox;
}

function okResponse(payload) {
  return { ok: true, status: 200, json: async () => payload };
}

test('safe reads recover from two transient network failures', async () => {
  let calls = 0;
  const sandbox = sandboxWith(async () => {
    calls += 1;
    if (calls < 3) throw new TypeError('temporary network failure');
    return okResponse({ status: 'healthy' });
  });

  const result = await sandbox.apiUnderTest('/health');

  assert.equal(calls, 3);
  assert.equal(result.status, 'healthy');
});

test('safe reads retry when the response body is interrupted', async () => {
  let calls = 0;
  const sandbox = sandboxWith(async () => {
    calls += 1;
    if (calls === 1) {
      return { ok: true, status: 200, json: async () => { throw new TypeError('body stream interrupted'); } };
    }
    return okResponse({ status: 'healthy' });
  });

  const result = await sandbox.apiUnderTest('/health');

  assert.equal(calls, 2);
  assert.equal(result.status, 'healthy');
});

test('HEAD reads succeed without attempting to parse an absent body', async () => {
  let parsed = false;
  const sandbox = sandboxWith(async () => ({
    ok: true,
    status: 200,
    json: async () => { parsed = true; throw new SyntaxError('no body'); },
  }));

  const result = await sandbox.apiUnderTest('/health', { method: 'HEAD' });

  assert.equal(Object.keys(result).length, 0);
  assert.equal(parsed, false);
});

test('safe reads retry timeouts and keep the final timeout error', async () => {
  let calls = 0;
  const sandbox = sandboxWith((_url, options) => {
    calls += 1;
    return new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true });
    });
  });

  await assert.rejects(sandbox.apiUnderTest('/health'), (error) => error.code === 'timeout');
  assert.equal(calls, 3);
});

test('ending the login lifecycle cancels a read during retry backoff', async () => {
  let calls = 0;
  const sandbox = sandboxWith(async () => { calls += 1; throw new TypeError('offline'); });
  sandbox.READ_RETRY_DELAYS_MS[0] = 20;

  const request = sandbox.apiUnderTest('/health');
  setTimeout(sandbox.abortLifecycle, 1);

  await assert.rejects(request, (error) => error.name === 'StaleRequestError');
  assert.equal(calls, 1);
});

test('write requests are never retried after an uncertain network failure', async () => {
  let calls = 0;
  const sandbox = sandboxWith(async () => {
    calls += 1;
    throw new TypeError('connection dropped');
  });

  await assert.rejects(
    sandbox.apiUnderTest('/automatic-tasks', { method: 'POST', body: { content: '测试' } }),
    (error) => error.code === 'network_error',
  );
  assert.equal(calls, 1);
});

test('polling network notices are rate limited while server errors remain visible', () => {
  const sandbox = sandboxWith(async () => okResponse({}));
  const messages = [];
  sandbox.showToast = (message) => messages.push(message);
  vm.runInNewContext(
    `showPollingError('读取失败', new ApiError(0, 'network_error', '无法连接管理器。'), 1000);
     showPollingError('读取失败', new ApiError(0, 'timeout', '连接管理器超时。'), 2000);
     showPollingError('读取失败', new ApiError(500, 'server_error', '服务器错误'), 3000);`,
    sandbox,
  );

  assert.deepEqual(messages, ['网络连接不稳定，页面将自动重试。', '读取失败：服务器错误']);
});

test('a structured log error is shown before the parallel request finishes retrying', async () => {
  let finishAudit;
  const messages = [];
  const sandbox = {
    document: { hidden: false },
    api: (path) => path.startsWith('/operations')
      ? Promise.reject(new Error('明确错误'))
      : new Promise((resolve) => { finishAudit = resolve; }),
    showPollingError: (prefix, error) => messages.push(`${prefix}：${error.message}`),
    Promise,
    state: { operations: [] },
    defaultSceneOperation: (value) => value,
    renderOperations: () => {},
    renderOperationTimeline: () => {},
  };
  vm.runInNewContext(`${refreshLogsSource}\nthis.refreshLogsUnderTest = refreshLogs;`, sandbox);

  const pending = sandbox.refreshLogsUnderTest();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(messages, ['操作日志读取失败：明确错误']);
  finishAudit({ events: [] });
  await pending;
});
