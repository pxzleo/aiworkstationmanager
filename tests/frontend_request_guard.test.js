'use strict';

const assert = require('node:assert/strict');
const { RequestGuard, ExclusiveActionGuard } = require('../request-guard.js');

const guard = new RequestGuard();
const slowOldResponse = guard.begin('snapshot');
const fastNewResponse = guard.begin('snapshot');
assert.equal(guard.isCurrent(slowOldResponse), false, '慢旧响应不得覆盖新请求');
assert.equal(guard.isCurrent(fastNewResponse), true, '最新请求应可提交');

const oldSession401 = guard.begin('snapshot');
guard.reset();
const newLoginResponse = guard.begin('snapshot');
assert.equal(guard.isCurrent(oldSession401), false, '旧登录生命周期的 401 必须失效');
assert.equal(oldSession401.signal.aborted, true, '切换登录生命周期必须取消旧请求');
assert.equal(guard.isCurrent(newLoginResponse), true, '新登录生命周期请求应可提交');

const exclusive = new ExclusiveActionGuard();
const firstOwner = exclusive.acquire();
assert.equal(typeof firstOwner, 'number', '首个控制确认应取得全局 pending owner');
assert.equal(exclusive.acquire(), null, '重复点击不得覆盖已有确认 Promise');
exclusive.release(firstOwner);
const oldOwner = exclusive.acquire();
exclusive.reset();
const newOwner = exclusive.acquire();
exclusive.release(oldOwner);
assert.equal(exclusive.pending, true, '旧生命周期 finally 不得释放新登录的 pending');
exclusive.release(newOwner);
assert.equal(exclusive.pending, false, '当前确认完成或取消后必须释放 pending');

const pollLifecycle = guard.begin('operation:test');
guard.reset();
assert.equal(pollLifecycle.signal.aborted, true, '退出或重登必须取消旧 operation 轮询');
assert.equal(guard.isCurrent(pollLifecycle), false, '旧轮询不得跨登录生命周期刷新 UI');

console.log('frontend request guard tests passed');
