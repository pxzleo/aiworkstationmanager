'use strict';

const API_PREFIX = '/api/v1';
const SNAPSHOT_INTERVAL_MS = 3000;
const HISTORY_INTERVAL_MS = 12000;
const SUPPORT_INTERVAL_MS = 15000;
const STALE_AFTER_MS = 12000;
const RESOURCE_STALE_AFTER_MS = { history: 25000, services: 30000, audit: 30000, discovery: 30000, webuis: 30000, logs: 30000 };
const REQUEST_TIMEOUT_MS = 8000;
const CONTROL_REQUEST_TIMEOUT_MS = 30000;
const SVG_NS = 'http://www.w3.org/2000/svg';
const requestGuard = new RequestGuard();
const controlActionGuard = new ExclusiveActionGuard();
const RESOURCE_NAMES = ['snapshot', 'history', 'services', 'audit', 'discovery', 'webuis', 'logs'];
const state = {
  activePage: 'overview', authMode: 'login', csrfToken: null, username: '', snapshot: null,
  history: [], timers: new Map(), chartSpecs: [], discoveredEntries: [],
  environments: [], scenes: [], operations: [], webuis: [], logSources: [], controlEnabled: false,
  recoveryRequired: null, processPoisoned: false, controlConfirmationFinish: null,
  gpuBinding: { slots: [null, null], extras: [] },
  resources: Object.fromEntries(RESOURCE_NAMES.map((name) => [name, { failures: 0, lastSuccess: 0, lastAttempt: 0, lastError: '' }])),
};

class ApiError extends Error {
  constructor(status, code, message) { super(message); this.name = 'ApiError'; this.status = status; this.code = code; }
}
class StaleRequestError extends Error { constructor() { super('stale request'); this.name = 'StaleRequestError'; } }
function byId(id) { return document.getElementById(id); }
function text(id, value) { const node = byId(id); if (node) node.textContent = value; }
function element(tag, className, value) { const node = document.createElement(tag); if (className) node.className = className; if (value !== undefined) node.textContent = value; return node; }
function svgElement(tag, attributes = {}) { const node = document.createElementNS(SVG_NS, tag); Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value)); return node; }
function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
function normalizedPercent(value) { return finite(value) ? Math.min(100, Math.max(0, value)) : null; }
function percent(value) { const normalized = normalizedPercent(value); return normalized === null ? '不支持' : `${Math.round(normalized)}%`; }
function gib(value) { return finite(value) ? value / (1024 ** 3) : null; }
function mibToGib(value) { return finite(value) ? value / 1024 : null; }
function compactUuid(value) { return value ? `${value.slice(0, 8)}…${value.slice(-6)}` : '未知'; }
function formatDate(value, includeDate = false) {
  const date = new Date(value); if (Number.isNaN(date.getTime())) return '时间未知';
  return date.toLocaleString('zh-CN', includeDate ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false } : { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
}

async function api(path, options = {}) {
  const resource = options.resource;
  const ticket = requestGuard.begin(resource);
  const method = (options.method || 'GET').toUpperCase();
  const controller = new AbortController();
  const lifecycleSignal = ticket.signal;
  const abortForLifecycle = () => controller.abort('lifecycle');
  if (lifecycleSignal.aborted) abortForLifecycle(); else lifecycleSignal.addEventListener('abort', abortForLifecycle, { once: true });
  const timeout = window.setTimeout(() => controller.abort(), options.timeout || REQUEST_TIMEOUT_MS);
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined) headers.set('Content-Type', 'application/json');
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && state.csrfToken && !options.skipCsrf) headers.set('X-CSRF-Token', state.csrfToken);
  try {
    const response = await fetch(`${API_PREFIX}${path}`, { method, headers, body: options.body === undefined ? undefined : JSON.stringify(options.body), credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
    let payload;
    try { payload = await response.json(); }
    catch (_) {
      if (response.ok) throw new ApiError(response.status, 'invalid_response', '服务器返回了无法识别的数据，请稍后重试。');
      payload = null;
    }
    if (!requestGuard.isCurrent(ticket)) throw new StaleRequestError();
    if (!response.ok) {
      const serverError = payload && payload.error;
      const code = serverError && typeof serverError.code === 'string' ? serverError.code : 'request_failed';
      let message = serverError && typeof serverError.message === 'string' ? serverError.message : `请求失败（${response.status}）`;
      if (response.status === 429) message = '尝试次数过多，请稍后再试。';
      const error = new ApiError(response.status, code, message);
      if (response.status === 401 && !options.authRequest) showAuth('login', '登录状态已过期，请重新登录。');
      throw error;
    }
    if (payload === null || typeof payload !== 'object') throw new ApiError(response.status, 'invalid_response', '服务器返回了无法识别的数据，请稍后重试。');
    return payload;
  } catch (error) {
    if (error instanceof StaleRequestError || !requestGuard.isCurrent(ticket)) throw new StaleRequestError();
    if (error.name === 'AbortError') throw new ApiError(0, 'timeout', '连接管理器超时，请检查服务状态。');
    if (error instanceof ApiError) throw error;
    throw new ApiError(0, 'network_error', '无法连接管理器，正在等待重试。');
  } finally { clearTimeout(timeout); lifecycleSignal.removeEventListener('abort', abortForLifecycle); }
}

const pages = [...document.querySelectorAll('.page')];
const navItems = [...document.querySelectorAll('.nav-item[data-page]')];
function navigate(page) {
  const next = byId(`page-${page}`); if (!next) return;
  state.activePage = page; pages.forEach((item) => item.classList.toggle('active', item === next)); navItems.forEach((item) => item.classList.toggle('active', item.dataset.page === page));
  text('pageTitle', next.dataset.title); closeSidebar(mobileViewport.matches);
  window.scrollTo({ top: 0, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
}
navItems.forEach((item) => item.addEventListener('click', () => navigate(item.dataset.page)));
document.querySelectorAll('[data-nav]').forEach((item) => item.addEventListener('click', () => navigate(item.dataset.nav)));
const sidebar = byId('sidebar');
const menuButton = byId('menuButton');
const mainContent = document.querySelector('main.main');
const mobileViewport = matchMedia('(max-width: 720px)');
function closeSidebar(restoreFocus = false) {
  const wasOpen = sidebar.classList.contains('open');
  sidebar.classList.remove('open');
  menuButton.setAttribute('aria-expanded', 'false');
  menuButton.setAttribute('aria-label', '打开导航');
  if (mobileViewport.matches) {
    sidebar.inert = true;
    sidebar.setAttribute('aria-hidden', 'true');
    mainContent.inert = false;
    mainContent.removeAttribute('aria-hidden');
  }
  if (restoreFocus && wasOpen) menuButton.focus();
}
function openSidebar() {
  if (!mobileViewport.matches) return;
  sidebar.inert = false;
  sidebar.removeAttribute('aria-hidden');
  sidebar.classList.add('open');
  mainContent.inert = true;
  mainContent.setAttribute('aria-hidden', 'true');
  menuButton.setAttribute('aria-expanded', 'true');
  menuButton.setAttribute('aria-label', '关闭导航');
  requestAnimationFrame(() => sidebar.querySelector('.nav-item[data-page]')?.focus());
}
function syncSidebarForViewport() {
  if (mobileViewport.matches) closeSidebar(false);
  else {
    sidebar.inert = false;
    sidebar.removeAttribute('aria-hidden');
    mainContent.inert = false;
    mainContent.removeAttribute('aria-hidden');
    sidebar.classList.remove('open');
    menuButton.setAttribute('aria-expanded', 'false');
  }
}
menuButton.addEventListener('click', () => sidebar.classList.contains('open') ? closeSidebar(true) : openSidebar());
mobileViewport.addEventListener('change', syncSidebarForViewport);

const toast = byId('toast'); let toastTimer;
function showToast(message) { toast.querySelector('span').textContent = message; toast.classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => toast.classList.remove('show'), 2600); }
function clearTimers() {
  state.timers.forEach((timer) => clearTimeout(timer)); state.timers.clear();
  requestGuard.reset();
}

function showAuth(mode, message = '') {
  cancelControlInteraction(); clearTimers(); state.authMode = mode; state.csrfToken = null; document.body.classList.add('auth-pending');
  const setup = mode === 'setup'; text('authEyebrow', setup ? '首次设置' : '安全访问'); text('authTitle', setup ? '创建本机管理员' : '登录工作站');
  text('authDescription', setup ? '首次设置仅允许在本机完成。密码至少 12 个字符。' : '使用管理员账户继续。'); text('authSubmit', setup ? '创建管理员并进入' : '登录'); text('authError', message);
  byId('authForm').hidden = false; byId('confirmPasswordLabel').hidden = !setup; byId('confirmPasswordInput').hidden = !setup; byId('confirmPasswordInput').required = setup; byId('passwordInput').autocomplete = setup ? 'new-password' : 'current-password';
  requestAnimationFrame(() => byId('usernameInput').focus());
}
async function submitAuth(event) {
  event.preventDefault(); const form = event.currentTarget; const username = byId('usernameInput').value.trim(); const passwordInput = byId('passwordInput'); const confirmationInput = byId('confirmPasswordInput'); const password = passwordInput.value; const setup = state.authMode === 'setup';
  if (!form.reportValidity()) return;
  if (setup && password !== confirmationInput.value) { text('authError', '两次输入的密码不一致。'); confirmationInput.focus(); return; }
  const submit = byId('authSubmit'); submit.disabled = true; submit.textContent = setup ? '正在创建…' : '正在登录…'; text('authError', '');
  try {
    const result = await api(setup ? '/auth/setup' : '/auth/login', { method: 'POST', body: { username, password }, skipCsrf: true, authRequest: true });
    state.csrfToken = result.csrf_token; state.username = username; enterApplication();
  } catch (error) { text('authError', error.message); }
  finally { passwordInput.value = ''; confirmationInput.value = ''; submit.disabled = false; submit.textContent = setup ? '创建管理员并进入' : '登录'; }
}
async function bootstrap() {
  buildMonitorCharts(); disableUnavailableActions();
  try {
    const status = await api('/auth/status', { authRequest: true });
    if (!status.configured) { showAuth('setup'); return; }
    if (!status.authenticated) { showAuth('login'); return; }
    try { const me = await api('/auth/me', { authRequest: true }); state.csrfToken = me.csrf_token; state.username = me.username; enterApplication(); }
    catch (error) { showAuth('login', error.status === 401 ? '' : error.message); }
  } catch (error) { showAuth('login', error.message); }
}
function enterApplication() {
  cancelControlInteraction(); document.body.classList.remove('auth-pending'); byId('authForm').reset(); text('logoutButton', (state.username || '管理员').slice(0, 2).toUpperCase()); clearTimers(); refreshAll();
  if (mobileViewport.matches) menuButton.focus();
  startPolling('snapshot', refreshSnapshot, SNAPSHOT_INTERVAL_MS);
  startPolling('history', refreshHistory, HISTORY_INTERVAL_MS);
  startPolling('services', refreshServices, SUPPORT_INTERVAL_MS);
  startPolling('audit', refreshAudit, SUPPORT_INTERVAL_MS);
  startPolling('discovery', refreshDiscovery, SUPPORT_INTERVAL_MS);
  startPolling('control', refreshControlData, SUPPORT_INTERVAL_MS);
  startPolling('webuis', refreshWebuis, SUPPORT_INTERVAL_MS);
  startPolling('logs', refreshSelectedLog, 5000);
  startPolling('freshness', async () => updateAllResourceStatuses(), 1000);
}
async function logout() {
  const button = byId('logoutButton'); button.disabled = true;
  try {
    await api('/auth/logout', { method: 'POST', authRequest: true });
    showAuth('login', '已退出登录。');
  } catch (error) {
    if (error instanceof StaleRequestError) return;
    if (error.status === 401) showAuth('login', '登录状态已结束，请重新登录。');
    else showToast(`退出失败：${error.message}`);
  } finally { button.disabled = false; }
}

function startPolling(name, operation, interval) {
  const generation = requestGuard.generation;
  const run = async () => {
    if (generation !== requestGuard.generation || document.body.classList.contains('auth-pending')) return;
    await operation();
    if (generation !== requestGuard.generation || document.body.classList.contains('auth-pending')) return;
    const timer = setTimeout(run, interval); state.timers.set(name, timer);
  };
  const timer = setTimeout(run, interval); state.timers.set(name, timer);
}
function resourceSuccess(name) { const status = state.resources[name]; status.failures = 0; status.lastSuccess = Date.now(); status.lastAttempt = Date.now(); status.lastError = ''; renderResourceStatus(name); }
function resourceFailure(name, error) { if (error instanceof StaleRequestError || error.status === 401) return; const status = state.resources[name]; status.failures += 1; status.lastAttempt = Date.now(); status.lastError = error.message; renderResourceStatus(name); }
function ensureStatusNode(name, anchor) {
  let node = byId(`${name}ApiStatus`); if (node) return node;
  node = element('p', 'api-status'); node.id = `${name}ApiStatus`; node.setAttribute('aria-live', 'polite'); anchor.before(node); return node;
}
function renderResourceStatus(name) {
  if (name === 'snapshot') { updateFreshness(); return; }
  const status = state.resources[name]; const stale = status.lastSuccess && Date.now() - status.lastSuccess > RESOURCE_STALE_AFTER_MS[name];
  let message = status.failures && stale ? `数据已过期 · 离线重试中（${status.failures}） · ${status.lastError}` : status.failures ? `${status.lastError || '读取失败'} · 重试中（${status.failures}）` : stale ? '数据已过期' : status.lastSuccess ? `最近更新 ${new Date(status.lastSuccess).toLocaleTimeString('zh-CN', { hour12: false })}` : '等待真实数据';
  const anchors = { history: document.querySelector('.monitor-grid'), services: byId('serviceList'), audit: byId('auditTimeline'), discovery: byId('scriptList'), webuis: byId('webuiList'), logs: byId('sourceLogConsole') };
  const node = ensureStatusNode(name, anchors[name]); node.textContent = message; node.classList.toggle('warning', Boolean(status.failures || stale));
}
function updateAllResourceStatuses() { RESOURCE_NAMES.forEach(renderResourceStatus); }
async function refreshAll() { await Promise.allSettled([refreshHistory(), refreshSnapshot(), refreshServices(), refreshAudit(), refreshDiscovery(), refreshControlData(), refreshWebuis(), refreshLogSources()]); }
async function refreshSnapshot() {
  if (document.hidden) return;
  try { state.snapshot = normalizeSnapshot(await api('/snapshot', { resource: 'snapshot' })); resourceSuccess('snapshot'); renderSnapshot(); }
  catch (error) { resourceFailure('snapshot', error); }
}
async function refreshHistory() {
  if (document.hidden) return;
  try { const result = await api('/history?window=15m', { resource: 'history' }); state.history = Array.isArray(result.samples) ? result.samples.map(normalizeHistorySample) : []; resourceSuccess('history'); renderCharts(); }
  catch (error) { resourceFailure('history', error); }
}
async function refreshServices() {
  if (document.hidden) return;
  try { renderServices(await api('/services', { resource: 'services' })); resourceSuccess('services'); }
  catch (error) { resourceFailure('services', error); }
}
async function refreshAudit() { if (document.hidden) return; try { const result = await api('/audit?limit=100', { resource: 'audit' }); renderAudit(result.events || []); resourceSuccess('audit'); } catch (error) { resourceFailure('audit', error); } }
async function refreshWebuis() {
  if (document.hidden) return;
  try { const result = await api('/webuis', { resource: 'webuis' }); state.webuis = Array.isArray(result.webuis) ? result.webuis : []; renderWebuis(result); resourceSuccess('webuis'); }
  catch (error) { resourceFailure('webuis', error); }
}
async function refreshLogSources() {
  try {
    const result = await api('/log-sources', { resource: 'logs' }); state.logSources = Array.isArray(result.sources) ? result.sources : []; renderLogSources(result); resourceSuccess('logs');
    const selected = state.logSources.find((item) => item.id === byId('logSourceSelect').value);
    if (selected?.configured) await refreshSelectedLog(true);
  } catch (error) { resourceFailure('logs', error); }
}
async function refreshSelectedLog(force = false) {
  if (document.hidden || (!force && !byId('autoRefreshLogs').checked)) return;
  const sourceId = byId('logSourceSelect').value; const source = state.logSources.find((item) => item.id === sourceId);
  if (!source?.configured) return;
  try {
    const lines = byId('logLinesSelect').value; const result = await api(`/log-sources/${encodeURIComponent(sourceId)}/entries?lines=${encodeURIComponent(lines)}&since=1h`, { resource: 'logs' }); renderSourceLog(result); resourceSuccess('logs');
  } catch (error) { resourceFailure('logs', error); byId('sourceLogConsole').replaceChildren(element('p', 'empty-state', error.message)); }
}
async function refreshControlData() {
  if (document.hidden) return;
  try {
    const [environmentData, sceneData, operationData] = await Promise.all([
      api('/environments', { resource: 'environments', timeout: CONTROL_REQUEST_TIMEOUT_MS }),
      api('/scenes', { resource: 'scenes', timeout: CONTROL_REQUEST_TIMEOUT_MS }),
      api('/operations?limit=20', { resource: 'operations', timeout: CONTROL_REQUEST_TIMEOUT_MS }),
    ]);
    state.controlEnabled = Boolean(environmentData.control_enabled && sceneData.control_enabled);
    state.recoveryRequired = environmentData.recovery_required || null;
    state.processPoisoned = Boolean(environmentData.process_poisoned);
    state.environments = Array.isArray(environmentData.environments) ? environmentData.environments : [];
    state.scenes = Array.isArray(sceneData.scenes) ? sceneData.scenes : [];
    state.operations = Array.isArray(operationData.operations) ? operationData.operations : [];
    renderEnvironments(); renderScenes(); renderOperations();
  } catch (error) {
    if (!(error instanceof StaleRequestError) && error.status !== 401) showToast(`控制状态读取失败：${error.message}`);
  }
}
async function refreshSupportData() { await Promise.allSettled([refreshServices(), refreshAudit()]); }
function normalizeGpu(gpu) { return { ...gpu, load_percent: normalizedPercent(gpu.load_percent), memory_percent: normalizedPercent(gpu.memory_percent) }; }
function normalizeHistorySample(sample) { return { ...sample, cpu_load_percent: normalizedPercent(sample.cpu_load_percent), memory_percent: normalizedPercent(sample.memory_percent), gpus: Array.isArray(sample.gpus) ? sample.gpus.map(normalizeGpu) : [] }; }
function normalizeSnapshot(snapshot) { const host = snapshot.host || {}; return { ...snapshot, host: { ...host, cpu: { ...(host.cpu || {}), load_percent: normalizedPercent(host.cpu?.load_percent) }, memory: { ...(host.memory || {}), percent: normalizedPercent(host.memory?.percent) }, disks: Array.isArray(host.disks) ? host.disks.map((disk) => ({ ...disk, percent: normalizedPercent(disk.percent) })) : [] }, gpus: Array.isArray(snapshot.gpus) ? snapshot.gpus.map(normalizeGpu) : [] }; }
function snapshotAsHistory(snapshot) { return snapshot ? { sampled_at: snapshot.sampled_at, cpu_load_percent: snapshot.host?.cpu?.load_percent, memory_percent: snapshot.host?.memory?.percent, gpus: snapshot.gpus || [] } : null; }
function currentSeries() {
  const samples = state.history.slice(); const current = snapshotAsHistory(state.snapshot);
  if (current && !samples.some((sample) => sample.sampled_at === current.sampled_at)) samples.push(current);
  return samples.sort((a, b) => new Date(a.sampled_at) - new Date(b.sampled_at));
}

function renderSnapshot() {
  const snapshot = state.snapshot; if (!snapshot) return; const host = snapshot.host || {}; const cpu = host.cpu || {}; const memory = host.memory || {};
  text('cpuMetric', percent(cpu.load_percent)); text('cpuDetail', `温度 ${cpu.temperature_status === 'unsupported' ? '不支持' : finite(cpu.temperature_c) ? `${Math.round(cpu.temperature_c)}°C` : '不可用'}`);
  const memoryUsed = gib(memory.used_bytes); const memoryTotal = gib(memory.total_bytes); text('memoryMetric', memoryUsed === null ? '不支持' : `${memoryUsed.toFixed(1)} GB`); text('memoryDetail', memoryTotal === null ? '总量不可用' : `${percent(memory.percent)} 已使用 · 共 ${memoryTotal.toFixed(1)} GB`);
  const disks = Array.isArray(host.disks) ? host.disks.filter((disk) => finite(disk.total_bytes)) : []; const disk = disks.find((item) => String(item.mountpoint || item.device).toUpperCase().startsWith('C')) || disks[0];
  if (disk) { const free = gib(disk.total_bytes - disk.used_bytes); text('diskLabel', `${disk.device || disk.mountpoint || '磁盘'} 可用`); text('diskMetric', `${free.toFixed(1)} GB`); text('diskDetail', `${percent(disk.percent)} 已使用`); byId('diskMetricBox').classList.toggle('disk-warn', disk.percent >= 95); }
  else { text('diskMetric', '不支持'); text('diskDetail', '未取得磁盘数据'); }
  const containers = snapshot.docker?.containers || []; const running = containers.filter((item) => String(item.state).toLowerCase() === 'running').length;
  text('dockerMetric', containers.length ? `${running}/${containers.length}` : '0'); text('dockerDetail', `${running} 运行 · ${Math.max(0, containers.length - running)} 停止`);
  document.querySelector('.host-mini strong').textContent = location.hostname || '本机'; document.querySelector('.host-mini small').textContent = location.protocol === 'https:' ? 'HTTPS 加密连接' : 'HTTP 未加密连接'; document.querySelector('.host-load').textContent = percent(cpu.load_percent);
  state.gpuBinding = bindGpuSlots(snapshot.gpus || []);
  renderGpuSlot(0, state.gpuBinding.slots[0]); renderGpuSlot(1, state.gpuBinding.slots[1]);
  renderCollectorErrors(snapshot.collector_errors || []); renderExtraGpus(state.gpuBinding.extras); renderCharts(); updateFreshness();
}
function bindGpuSlots(gpus) {
  const slots = [null, null];
  const selected = new Set();
  [0, 1].forEach((slot) => {
    const index = gpus.findIndex((gpu) => Number(gpu.index) === slot);
    if (index >= 0) { slots[slot] = gpus[index]; selected.add(index); }
  });
  return { slots, extras: gpus.filter((_, index) => !selected.has(index)) };
}
function renderGpuSlot(slot, gpu) {
  const prefix = `gpu${slot}`; const lane = document.querySelectorAll('.gpu-lane')[slot];
  if (!gpu) {
    lane.classList.add('gpu-missing'); text(`${prefix}Name`, '未检测到'); text(`${prefix}Role`, '此卡槽暂无设备'); text(`${prefix}Util`, '--'); text(`${prefix}MemoryValue`, '--'); text(`${prefix}MemoryPercent`, '--'); text(`${prefix}MemoryFree`, '无数据'); text(`${prefix}Temp`, '不支持'); text(`${prefix}Power`, '不支持'); text(`${prefix}Uuid`, '--'); text(slot === 0 ? 'gpu0State' : 'gpu1OperationalState', '未检测到'); lane.querySelector('.memory-visual').style.setProperty('--used', '0%'); renderPolyline(byId(`${prefix}Sparkline`), [], `GPU ${slot}`, 'GPU 负载'); return;
  }
  lane.classList.remove('gpu-missing'); const used = mibToGib(gpu.memory_used_mib); const total = mibToGib(gpu.memory_total_mib); const free = used !== null && total !== null ? Math.max(0, total - used) : null;
  text(`${prefix}Name`, gpu.name || `GPU ${gpu.index}`); text(`${prefix}Role`, compactUuid(gpu.uuid)); text(`${prefix}Util`, finite(gpu.load_percent) ? String(Math.round(gpu.load_percent)) : '--'); text(`${prefix}MemoryValue`, used === null ? '--' : used.toFixed(1));
  const valueNode = byId(`${prefix}MemoryValue`); const totalNode = valueNode?.parentElement?.querySelector('small'); if (totalNode) totalNode.textContent = total === null ? '/ 不支持' : `/ ${total.toFixed(1)} GB`;
  text(`${prefix}MemoryPercent`, percent(gpu.memory_percent)); text(`${prefix}MemoryFree`, free === null ? '不支持' : `剩余 ${free.toFixed(1)} GB`); text(`${prefix}Temp`, finite(gpu.temperature_c) ? `${Math.round(gpu.temperature_c)}°C` : '不支持'); text(`${prefix}Power`, finite(gpu.power_w) ? `${Math.round(gpu.power_w)} W` : '不支持'); text(`${prefix}Uuid`, compactUuid(gpu.uuid)); text(slot === 0 ? 'gpu0State' : 'gpu1OperationalState', '已检测'); lane.querySelector('.memory-visual').style.setProperty('--used', `${finite(gpu.memory_percent) ? Math.min(100, Math.max(0, gpu.memory_percent)) : 0}%`);
  const series = currentSeries().map((sample) => { const item = (sample.gpus || []).find((entry) => entry.uuid === gpu.uuid || Number(entry.index) === Number(gpu.index)); return item?.load_percent; }).filter(finite);
  renderPolyline(byId(`${prefix}Sparkline`), series, gpu.name || `GPU ${gpu.index}`, 'GPU 负载');
}
function renderPolyline(polyline, values, device, metric) {
  if (!polyline) return; const svg = polyline.closest('svg'); const normalized = values.map(normalizedPercent).filter((value) => value !== null); const summary = summarize(normalized);
  if (!normalized.length) { polyline.setAttribute('points', ''); svg.setAttribute('aria-label', `${device} ${metric}曲线：暂无数据`); return; }
  const points = normalized.map((value, index) => { const x = normalized.length === 1 ? 150 : index * (300 / (normalized.length - 1)); const y = 110 - value; return `${x.toFixed(1)},${y.toFixed(1)}`; }).join(' ');
  polyline.setAttribute('points', points); svg.setAttribute('aria-label', `${device} ${metric}曲线，当前 ${summary.current}%，峰值 ${summary.peak}%，趋势${summary.trend}`);
}
function summarize(values) { const normalized = values.map(normalizedPercent).filter((value) => value !== null); if (!normalized.length) return { current: '--', peak: '--', trend: '未知' }; const current = Math.round(normalized.at(-1)); const previous = normalized.length > 1 ? normalized.at(-2) : normalized.at(-1); return { current, peak: Math.round(Math.max(...normalized)), trend: current > previous ? '上升' : current < previous ? '下降' : '持平' }; }
function renderCollectorErrors(errors) {
  const banner = byId('collectorStatus'); if (!errors.length) { banner.hidden = true; banner.replaceChildren(); return; }
  const labels = { host: '主机', nvidia: 'GPU', docker: 'Docker', ports: '端口', sampler: '采样器' }; banner.hidden = false; banner.replaceChildren(element('strong', '', '部分数据降级'), element('span', '', errors.map((error) => `${labels[error.collector] || error.collector}：${error.message || '采集失败'}`).join('；')));
}
function updateFreshness() {
  const status = state.resources.snapshot; const sampled = state.snapshot ? new Date(state.snapshot.sampled_at).getTime() : 0; const age = sampled ? Date.now() - sampled : Infinity; const stale = age > STALE_AFTER_MS; const freshness = byId('freshness'); const dot = freshness.querySelector('.status-dot'); dot.className = `status-dot ${status.failures ? 'warning' : stale ? 'stale' : 'live'}`;
  const label = status.failures && stale ? `数据已过期 · 离线重试中（${status.failures}）` : status.failures ? `离线重试中（${status.failures}）` : stale ? '数据已过期' : '实时';
  text('freshnessLabel', label); text('clock', sampled ? formatDate(state.snapshot.sampled_at) : '--:--:--'); freshness.title = sampled ? `最近数据：${new Date(state.snapshot.sampled_at).toLocaleString('zh-CN')}` : '尚无监控数据'; document.body.classList.toggle('data-stale', stale);
}

function buildMonitorCharts() {
  const grid = document.querySelector('.monitor-grid'); grid.replaceChildren();
  const specs = [
    ['cpu', 'HOST', 'CPU 总负载', '整机总量', (sample) => sample.cpu_load_percent], ['memory', 'HOST', '内存占用', '物理内存使用率', (sample) => sample.memory_percent],
    ['gpu0load', 'GPU 0', 'GPU 负载', '按 UUID / index 独立匹配', (sample) => metricForGpu(sample, 0, 'load_percent')], ['gpu0memory', 'GPU 0', '显存占用', '真实显存使用率', (sample) => metricForGpu(sample, 0, 'memory_percent')],
    ['gpu1load', 'GPU 1', 'GPU 负载', '按 UUID / index 独立匹配', (sample) => metricForGpu(sample, 1, 'load_percent')], ['gpu1memory', 'GPU 1', '显存占用', '真实显存使用率', (sample) => metricForGpu(sample, 1, 'memory_percent')],
  ];
  state.chartSpecs = specs.map(([key, kicker, title, description, getter]) => {
    const section = element('section', 'chart-section'); const heading = element('div', 'chart-title'); const copy = element('div'); copy.append(element('span', 'chart-kicker', kicker), element('h2', '', title), element('p', '', description));
    const values = element('div', 'chart-values'); const current = element('strong', '', '--'); current.append(element('small', '', '% 当前')); values.append(current); heading.append(copy, values);
    const frame = element('div', 'chart-frame'); const yAxis = element('span', 'chart-y-axis'); ['100%', '50%', '0'].forEach((label) => yAxis.append(element('i', '', label)));
    const svg = svgElement('svg', { class: 'line-chart', viewBox: '0 0 900 230', preserveAspectRatio: 'none', role: 'img', 'aria-label': `${title}：暂无数据` }); const gridLines = svgElement('g', { class: 'grid-lines' }); gridLines.append(svgElement('path', { d: 'M0 15H900M0 115H900M0 215H900' })); const line = svgElement('polyline', { class: 'line primary-line', fill: 'none', points: '' }); const empty = svgElement('text', { x: '450', y: '120', class: 'chart-empty', 'text-anchor': 'middle' }); empty.textContent = '暂无历史数据'; svg.append(gridLines, line, empty); frame.append(yAxis, svg);
    const axis = element('div', 'chart-axis'); ['--:--', '--:--', '--:--', '现在'].forEach((label) => axis.append(element('span', '', label))); section.append(heading, frame, axis); grid.append(section); return { key, title, getter, section, current, svg, line, empty, axis };
  });
}
function metricForGpu(sample, slot, field) { const currentGpu = state.gpuBinding.slots[slot]; if (!currentGpu) return null; const gpu = (sample.gpus || []).find((item) => item.uuid === currentGpu.uuid || Number(item.index) === Number(currentGpu.index)); return gpu ? gpu[field] : null; }
function renderCharts() {
  const samples = currentSeries(); state.chartSpecs.forEach((spec) => {
    const pairs = samples.map((sample) => [sample.sampled_at, spec.getter(sample)]).filter(([, value]) => finite(value)); const values = pairs.map(([, value]) => value); const summary = summarize(values); spec.current.firstChild.nodeValue = values.length ? String(summary.current) : '--'; spec.empty.style.display = values.length ? 'none' : '';
    const points = values.map((value, index) => { const x = values.length === 1 ? 450 : index * (900 / (values.length - 1)); const y = 215 - Math.min(100, Math.max(0, value)) * 2; return `${x.toFixed(1)},${y.toFixed(1)}`; }).join(' '); spec.line.setAttribute('points', points); spec.svg.setAttribute('aria-label', values.length ? `${spec.title}曲线，当前 ${summary.current}%，峰值 ${summary.peak}%，趋势${summary.trend}` : `${spec.title}曲线：暂无数据`);
    const times = pairs.length ? [pairs[0][0], pairs[Math.floor((pairs.length - 1) / 3)][0], pairs[Math.floor((pairs.length - 1) * 2 / 3)][0], pairs.at(-1)[0]] : []; [...spec.axis.children].forEach((node, index) => { node.textContent = times[index] ? (index === 3 ? '现在' : formatDate(times[index]).slice(0, 5)) : index === 3 ? '现在' : '--:--'; });
  });
}

function renderServices(data) {
  const list = byId('serviceList'); list.replaceChildren(); const containers = data.containers || []; const ports = data.listening_ports || [];
  if (!containers.length && !ports.length) { list.append(element('p', 'empty-state', '未发现 Docker 容器或关键监听端口。')); return; }
  containers.slice(0, 8).forEach((container) => { const running = String(container.state).toLowerCase() === 'running'; const row = element('div', 'service-row'); row.append(element('span', `service-state ${running ? 'ready' : 'stopped'}`)); const copy = element('div'); copy.append(element('strong', '', container.name || '未命名容器'), element('small', '', `${container.image || '镜像未知'} · ${container.status || container.state || '状态未知'}`)); row.append(copy, element('span', 'port', container.ports || '无发布端口'), element('span', 'uptime', running ? '运行中' : '已停止'), disabledButton('只读')); list.append(row); });
  ports.slice(0, 4).forEach((port) => { const row = element('div', 'service-row'); row.append(element('span', 'service-state ready')); const copy = element('div'); copy.append(element('strong', '', `监听端口 ${port.port}`), element('small', '', (port.listeners || []).map((item) => `${item.address || '*'} · PID ${item.pid ?? '未知'}`).join('；') || '监听详情不可用')); row.append(copy, element('span', 'port', `:${port.port}`), element('span', 'uptime', '监听中'), disabledButton('只读')); list.append(row); });
}
function disabledButton(label) { const button = element('button', 'row-action muted', label); button.disabled = true; button.title = '当前后端仅提供只读状态'; return button; }
function renderAudit(events) {
  const timeline = byId('auditTimeline'); timeline.replaceChildren(); if (!events.length) timeline.append(element('li', 'empty-state', '暂无审计事件。'));
  events.slice(0, 5).forEach((event) => { const item = element('li'); item.append(element('span', `event-dot ${event.result === 'success' ? 'good' : event.result === 'failure' ? 'warn' : ''}`)); const copy = element('div'); copy.append(element('strong', '', auditLabel(event.event, event.result)), element('small', '', `${formatDate(event.created_at, true)} · ${event.source_ip || '来源未知'}`)); item.append(copy); timeline.append(item); });
  const consoleNode = byId('auditLogConsole'); if (!consoleNode) return; const head = element('div', 'log-head'); ['时间', '事件', '结果', '摘要'].forEach((label) => head.append(element('span', '', label))); consoleNode.replaceChildren(head);
  events.forEach((event) => { const row = element('div', 'log-line'); row.append(element('time', '', formatDate(event.created_at, true)), element('b', '', event.event || 'unknown'), element('i', `level ${event.result === 'success' ? 'ok' : event.result === 'failure' ? 'warn' : 'info'}`, event.result || 'unknown'), element('code', '', safeSummary(event.summary))); consoleNode.append(row); });
}
function renderWebuis(data) {
  const list = byId('webuiList'); list.replaceChildren();
  text('webuiConfigNote', data.source === 'formal' ? '仅 configured 且在线的目标可以打开隔离只读预览。POST、Cookie、WebSocket、SSE 与完整 SPA 交互不受支持。' : '正式 config/integrations.json 不存在；以下仅为示例预览，不会探测或代理。');
  if (!state.webuis.length) { list.append(element('p', 'empty-state', '没有可显示的固定 WebUI 候选。')); return; }
  state.webuis.forEach((item) => {
    const uiStatus = item.ui_status || item.status; const backendStatus = item.backend_status || 'unknown';
    const row = element('div', `webui-row${uiStatus === 'offline' || backendStatus === 'offline' ? ' warning-row' : ''}`); row.append(element('span', 'webui-mark', (item.kind || 'UI').slice(0, 2).toUpperCase()));
    const main = element('div', 'webui-main'); const title = element('div'); title.append(element('h2', '', item.name || item.id), element('span', `status-label ${uiStatus === 'online' ? 'ready' : uiStatus === 'offline' ? 'danger' : 'partial'}`, `监控界面${uiStatus === 'online' ? '在线' : uiStatus === 'offline' ? '离线' : '未配置'}`), element('span', `status-label ${backendStatus === 'online' ? 'ready' : backendStatus === 'offline' ? 'danger' : 'partial'}`, `模型后端${backendStatus === 'online' ? '在线' : backendStatus === 'offline' ? '离线' : '未知'}`)); const warnings = [...(item.blockers || [])]; if (item.backend_blocker) warnings.push(item.backend_blocker); main.append(title, element('p', backendStatus === 'offline' ? 'backend-warning' : '', warnings.join('；') || '通过管理器隔离只读预览访问'), element('small', '', `ID ${item.id} · ${item.configured ? '正式配置' : '预览配置'} · UI ${item.last_check ? formatDate(item.last_check, true) : '未检查'} · 后端 ${item.backend_checked_at ? formatDate(item.backend_checked_at, true) : '未检查'}`));
    const actions = element('div', 'webui-actions'); const same = element('button', 'button primary', '打开只读预览'); const fresh = element('button', 'button secondary', '新标签预览'); const ready = Boolean(item.configured && uiStatus === 'online' && item.proxy_url); same.disabled = !ready; fresh.disabled = !ready; if (!ready) { same.title = fresh.title = '仅监控界面正式配置且在线时可打开'; }
    same.addEventListener('click', () => { if (ready) window.location.assign(item.proxy_url); }); fresh.addEventListener('click', () => { if (ready) window.open(item.proxy_url, '_blank', 'noopener,noreferrer'); }); actions.append(same, fresh); row.append(main, actions); list.append(row);
  });
}
function renderLogSources(data) {
  const select = byId('logSourceSelect'); const previous = select.value; select.replaceChildren(); state.logSources.forEach((item) => { const option = element('option', '', `${item.name}${item.configured ? '' : '（未配置）'}`); option.value = item.id; option.disabled = !item.configured; select.append(option); });
  if (state.logSources.some((item) => item.id === previous && item.configured)) select.value = previous;
  text('logConfigNote', data.source === 'formal' ? '日志按固定来源读取，输出会限幅、清理 ANSI 并脱敏。' : '外部日志来源仅为示例预览；管理器自身轮转日志仍可读取。');
}
function renderSourceLog(data) {
  const consoleNode = byId('sourceLogConsole'); consoleNode.replaceChildren(); const lines = Array.isArray(data.lines) ? data.lines : [];
  if (!lines.length) { consoleNode.append(element('p', 'empty-state', '该来源当前没有日志。')); return; }
  lines.forEach((line) => consoleNode.append(element('div', 'source-log-line', line))); if (data.truncated) consoleNode.prepend(element('p', 'api-status warning', '输出已按安全上限截断。'));
}
function auditLabel(event, result) { const labels = { 'auth.setup': '管理员设置', 'auth.login': '管理员登录', 'auth.logout': '管理员退出', 'discovery.scripts.scan': '脚本只读扫描' }; return `${labels[event] || event || '系统事件'} · ${result === 'success' ? '成功' : result === 'failure' ? '失败' : '部分完成'}`; }
function safeSummary(summary) { if (!summary || typeof summary !== 'object') return '无摘要'; return Object.entries(summary).slice(0, 8).map(([key, value]) => `${key}: ${typeof value === 'object' ? '[结构化数据]' : String(value)}`).join(' · '); }

async function refreshDiscovery() { try { renderDiscovery(await api('/discovery/scripts', { resource: 'discovery' })); resourceSuccess('discovery'); } catch (error) { resourceFailure('discovery', error); } }
async function scanDiscovery() {
  const button = byId('scanScriptsButton'); button.disabled = true; button.textContent = '扫描中…';
  try { await api('/discovery/scripts/scan', { method: 'POST' }); await Promise.all([refreshDiscovery(), refreshSupportData()]); showToast('扫描完成 · 未执行任何文件'); }
  catch (error) { showToast(error.message); } finally { button.disabled = false; button.textContent = '重新扫描'; }
}
function renderDiscovery(data) {
  state.discoveredEntries = data.entries || []; text('discoveryPath', data.directory || '目录未知'); text('discoveryCount', `${state.discoveredEntries.length} 个入口`); const latest = data.latest_scan; text('discoveryTime', latest ? `上次扫描 ${formatDate(latest.scanned_at, true)} · ${latest.error_count} 项错误` : '尚无扫描记录');
  const list = byId('scriptList'); list.replaceChildren(); const head = element('div', 'script-list-head'); ['来源', '解析线索', '状态'].forEach((label) => head.append(element('span', '', label))); list.append(head);
  if (!state.discoveredEntries.length) list.append(element('p', 'empty-state', latest && !latest.directory_exists ? '扫描目录不存在。' : '未发现支持的脚本或快捷方式。'));
  state.discoveredEntries.forEach((entry, index) => {
    const row = element('button', `script-row${index === 0 ? ' selected' : ''}`); row.type = 'button'; row.dataset.index = String(index); const nameCell = element('span'); const copy = element('span'); copy.append(element('strong', '', entry.name || '未命名'), element('small', '', compactUuid(entry.sha256))); nameCell.append(element('i', `file-type ${entry.type === 'lnk' ? 'link' : ''}`, String(entry.type || 'FILE').toUpperCase()), copy);
    const clues = [...(entry.ports || []).map((port) => `:${port}`), ...(entry.service_names || []), ...(entry.gpu_devices || []).map((gpu) => `GPU ${gpu}`)]; row.append(nameCell, element('em', '', clues.slice(0, 4).join(' · ') || '无明确线索'), element('b', `status-label ${(entry.errors || []).length ? 'danger' : 'partial'}`, (entry.errors || []).length ? `${entry.errors.length} 项错误` : '待审核')); row.addEventListener('click', () => selectDiscovery(index)); list.append(row);
  });
  if (state.discoveredEntries.length) selectDiscovery(0);
  else {
    text('scriptInspectorTitle', '无可用入口'); text('scriptRuntime', '未发现'); text('scriptGpu', '未发现');
    text('scriptPorts', '未发现'); text('scriptHealth', '未发现'); text('scriptActions', '尚未配置');
    text('scriptRisk', latest && !latest.directory_exists ? '扫描目录不存在，未执行任何文件。' : '本次扫描没有可供审核的入口。');
  }
}
function selectDiscovery(index) {
  const entry = state.discoveredEntries[index]; if (!entry) return; document.querySelectorAll('.script-row').forEach((row) => row.classList.toggle('selected', Number(row.dataset.index) === index)); text('scriptInspectorTitle', entry.name || '未命名'); text('scriptRuntime', entry.docker_compose ? `Docker Compose${(entry.wsl_distributions || []).length ? ` · WSL ${entry.wsl_distributions.join(', ')}` : ''}` : entry.type === 'lnk' ? 'Windows 快捷方式' : '脚本入口'); text('scriptGpu', (entry.gpu_devices || []).length ? entry.gpu_devices.map((gpu) => `GPU ${gpu}`).join(' · ') : '未发现明确 GPU 线索');
  const ports = byId('scriptPorts'); ports.replaceChildren(); (entry.ports || []).forEach((port) => ports.append(element('code', '', String(port)))); if (!ports.childNodes.length) ports.textContent = '未发现'; text('scriptHealth', [...(entry.api_candidates || []), ...(entry.webui_candidates || [])].join(' · ') || '未发现健康接口'); text('scriptActions', (entry.service_names || []).join(' · ') || '未导入任何动作');
  const errors = entry.errors || []; const clues = [`服务 ${(entry.service_names || []).join(', ') || '无'}`, `端口 ${(entry.ports || []).join(', ') || '无'}`, `GPU ${(entry.gpu_devices || []).join(', ') || '无'}`]; text('scriptRisk', errors.length ? errors.map((error) => error.message || error.error_type || '解析失败').join('；') : `${clues.join('；')}。仅展示发现结果，尚未配置导入动作。`);
}
function renderExtraGpus(gpus) {
  let box = byId('extraGpuList'); if (!box) { box = element('div', 'extra-gpu-list'); box.id = 'extraGpuList'; document.querySelector('#page-environments .data-table').after(box); }
  box.replaceChildren(); if (!gpus.length) { box.hidden = true; return; }
  box.hidden = false; box.append(element('h2', '', `额外检测到 ${gpus.length} 张 GPU`)); gpus.forEach((gpu) => box.append(element('p', '', `GPU ${gpu.index ?? '未知'} · ${gpu.name || '未知型号'} · 负载 ${percent(gpu.load_percent)} · 显存 ${percent(gpu.memory_percent)} · ${compactUuid(gpu.uuid)}`)));
}

function controlStatusLabel(value) {
  return ({ running: '运行中', stopped: '已停止', unknown: '状态未知', unconfigured: '未接入适配器' })[value] || value || '未知';
}
function overviewEnvironmentState(environmentId) {
  const item = state.environments.find((environment) => environment.id === environmentId);
  if (!item) return '未配置';
  const label = controlStatusLabel(item.status);
  return item.status === 'stopped' && item.action_capabilities?.start?.ready
    ? `${label} · 可启动` : label;
}
function renderOverviewEnvironmentStates() {
  text('gpu1AsrState', overviewEnvironmentState('dev3090_asr'));
  text('gpu1TtsState', overviewEnvironmentState('dev3090_tts'));
}
function updateSceneActionButton(button, item, blockers) {
  if (!button) return;
  if (!item) {
    button.disabled = true;
    button.title = '场景配置未加载';
    return;
  }
  button.disabled = !state.controlEnabled || controlActionGuard.pending || item.current === 'active';
  button.title = item.current === 'active'
    ? '当前场景已经激活'
    : blockers.length ? `点击运行安全预检：${blockers.join('；')}` : `一键切换到${item.name}`;
  const label = button.querySelector('span');
  if (label) label.textContent = item.current === 'active'
    ? '当前场景'
    : button.classList.contains('scene-quick-action') ? `一键切换到${item.name}` : '一键切换';
}
function renderEnvironments() {
  renderOverviewEnvironmentStates();
  const table = document.querySelector('.env-table'); if (!table) return;
  const head = table.querySelector('.table-head'); table.replaceChildren(); if (head) table.append(head);
  if (state.recoveryRequired || state.processPoisoned) {
    const recovery = element('div', 'control-recovery-warning');
    const recoveryItems = Array.isArray(state.recoveryRequired?.items)
      ? state.recoveryRequired.items : [];
    const recoverySummary = recoveryItems.length > 1
      ? recoveryItems.map((item) => `${item.environment_id}→${controlStatusLabel(item.expected_state)}`).join('；')
      : `${state.recoveryRequired?.environment_id || '未知环境'} · 期望恢复为 ${controlStatusLabel(state.recoveryRequired?.expected_state)}`;
    const copy = element('span'); copy.append(
      element('strong', '', '控制已锁定，需要人工恢复'),
      element('small', '', state.processPoisoned
        ? '操作终态未可靠写入；请重启管理器，再按恢复预检处理。'
        : recoverySummary));
    recovery.append(copy);
    if (!state.processPoisoned) {
      const resolve = element('button', 'row-action danger', '核验并解除'); resolve.type = 'button';
      resolve.disabled = !state.controlEnabled || controlActionGuard.pending;
      resolve.addEventListener('click', beginRecoveryResolve); recovery.append(resolve);
    }
    table.append(recovery);
  }
  if (!state.environments.length) { table.append(element('p', 'empty-state', '未加载到控制环境配置。')); return; }
  state.environments.forEach((item) => {
    const row = element('div', 'table-row');
    const title = element('span'); title.append(element('strong', '', item.name || item.id), element('small', '', item.id));
    const blockers = Array.isArray(item.blockers) ? item.blockers : [];
    const capabilities = item.action_capabilities && typeof item.action_capabilities === 'object' ? item.action_capabilities : {};
    row.append(title, element('span', '', item.adapter_configured ? `固定适配器 · ${item.adapter_type || '未知类型'}` : '缺少适配器'),
      element('span', '', item.configured ? '已启用' : item.adapter_configured ? '已登记但阻断' : '未核对'), element('span', '', '配置定义'),
      element('span', `status-label ${item.status === 'running' ? 'ready' : item.status === 'stopped' ? 'stopped' : 'danger'}`, controlStatusLabel(item.status)));
    const actions = element('span', 'env-actions');
    const allowed = Array.isArray(item.allowed_actions) ? item.allowed_actions : [];
    ['start', 'stop', 'restart'].forEach((action) => {
      if (!allowed.includes(action)) return;
      const labels = { start: '启动', stop: '停止', restart: '重启' };
      const button = element('button', 'row-action', labels[action]); button.type = 'button';
      const capability = capabilities[action] || { ready: false, blockers: ['后端未返回逐动作能力'] };
      const actionBlockers = Array.isArray(capability.blockers) ? capability.blockers : [];
      button.disabled = !(state.controlEnabled && capability.ready) || controlActionGuard.pending;
      button.title = button.disabled ? actionBlockers.join('；') || '控制尚未就绪' : `${labels[action]} ${item.name}`;
      button.addEventListener('click', () => beginEnvironmentAction(item, action)); actions.append(button);
    });
    if (!actions.childNodes.length) actions.append(element('small', '', blockers.join('；') || '无允许动作'));
    row.append(actions); table.append(row);
  });
}
function renderScenes() {
  const overviewButton = byId('switchSceneButton');
  if (overviewButton) {
    overviewButton.disabled = false;
    overviewButton.title = '进入工作场景并一键切换';
    const overviewLabel = overviewButton.querySelector('span');
    if (overviewLabel) overviewLabel.textContent = '一键切换';
  }
  document.querySelectorAll('.scene-panel[data-scene]').forEach((panel) => {
    const item = state.scenes.find((scene) => scene.id === panel.dataset.scene);
    const button = panel.querySelector('.scene-action'); const badge = panel.querySelector('.scene-panel-top i');
    let blocker = panel.querySelector('.scene-blocker');
    if (!blocker) { blocker = element('p', 'scene-blocker'); button.before(blocker); }
    if (!item) { blocker.textContent = '场景配置未加载'; button.disabled = true; return; }
    const blockers = Array.isArray(item.blockers) ? item.blockers : [];
    badge.textContent = item.current === 'active' ? '已激活' : item.current === 'partial' ? '部分激活' : item.current === 'degraded' ? '状态异常' : item.current === 'unknown' ? '无法确认' : '未激活';
    blocker.textContent = blockers.length ? blockers.join('；') : '预检条件完整，可查看切换计划。';
    updateSceneActionButton(button, item, blockers);
  });
  document.querySelectorAll('.scene-quick-action[data-switch]').forEach((button) => {
    const item = state.scenes.find((scene) => scene.id === button.dataset.switch);
    const blockers = Array.isArray(item?.blockers) ? item.blockers : [];
    updateSceneActionButton(button, item, blockers);
  });
  const development = state.scenes.find((item) => item.id === 'development' && item.current === 'active');
  const video = state.scenes.find((item) => item.id === 'video' && item.current === 'active');
  text('activeSceneName', development ? '开发/agent场景 · 已激活' : video ? '视频制作场景 · 已激活' : '场景未完整激活');
}
function renderOperations() {
  const list = byId('operationList'); if (!list) return; list.replaceChildren();
  if (!state.operations.length) { list.append(element('p', 'empty-state', '暂无控制操作。')); return; }
  state.operations.slice(0, 10).forEach((item) => {
    const recovery = ['rollback_failed', 'recovery_required'].includes(item.result);
    const failed = ['failed', 'interrupted'].includes(item.status) || recovery;
    const row = element('article', `operation-row${failed ? ' operation-failed' : ''}`); const copy = element('div');
    copy.append(element('strong', '', `${item.kind === 'scene' ? '场景' : '环境'} · ${item.target_id} · ${item.action}`),
      element('small', '', item.error_summary || item.result || '等待执行'));
    const statusText = item.result === 'rollback_failed' ? '回滚失败 · 需人工处理' : item.result === 'recovery_required' ? '需人工恢复' : item.status;
    row.append(copy, element('span', `status-label ${item.status === 'succeeded' ? 'ready' : failed ? 'danger' : 'partial'}`, statusText),
      element('time', '', formatDate(item.created_at, true)));
    const steps = element('ol', 'operation-steps');
    (Array.isArray(item.steps) ? item.steps : []).forEach((step) => {
      const stepFailed = ['failed', 'interrupted'].includes(step.status);
      const entry = element('li', `${step.status === 'running' ? 'current' : ''}${stepFailed ? ' failed' : ''}`);
      const started = new Date(step.started_at); const finished = step.finished_at ? new Date(step.finished_at) : null;
      const duration = !Number.isNaN(started.getTime()) && finished && !Number.isNaN(finished.getTime()) ? `${Math.max(0, finished - started)} ms` : step.status === 'running' ? '进行中' : '耗时未知';
      entry.append(element('b', '', `${step.sequence}. ${step.phase} · ${step.target_id} · ${step.action}`),
        element('span', 'step-status', step.status), element('small', '', `${formatDate(step.started_at, true)} → ${step.finished_at ? formatDate(step.finished_at, true) : '尚未结束'} · ${duration}`));
      if (step.error_summary || step.result) entry.append(element('code', '', step.error_summary || step.result));
      steps.append(entry);
    });
    if (steps.childNodes.length) row.append(steps); list.append(row);
  });
}

function requestControlConfirmation(title, lines, expected) {
  const dialog = byId('controlDialog'); const form = byId('controlDialogForm'); const cancel = byId('controlDialogCancel');
  if (state.controlConfirmationFinish || dialog.open) {
    showToast('已有控制确认正在处理'); return Promise.resolve(null);
  }
  text('controlDialogTitle', title); text('controlConfirmationExpected', expected);
  text('controlDialogError', ''); byId('controlConfirmationInput').value = '';
  const plan = byId('controlDialogPlan'); plan.replaceChildren();
  lines.forEach((line) => plan.append(element('p', line.danger ? 'danger' : '', line.text)));
  return new Promise((resolve) => {
    const finish = (value) => {
      if (state.controlConfirmationFinish !== finish) return;
      state.controlConfirmationFinish = null; form.onsubmit = null; cancel.onclick = null; dialog.oncancel = null;
      if (dialog.open) dialog.close(); resolve(value);
    };
    state.controlConfirmationFinish = finish;
    form.onsubmit = (event) => {
      event.preventDefault();
      const supplied = byId('controlConfirmationInput').value;
      if (supplied !== expected) { text('controlDialogError', '确认文本不匹配，请完整输入上方文本。'); return; }
      finish(supplied);
    };
    cancel.onclick = () => finish(null);
    dialog.oncancel = (event) => { event.preventDefault(); finish(null); };
    dialog.showModal(); requestAnimationFrame(() => byId('controlConfirmationInput').focus());
  });
}
function cancelControlInteraction() {
  const finish = state.controlConfirmationFinish; if (finish) finish(null);
  controlActionGuard.reset();
}
function acquireControlInteraction() {
  const owner = controlActionGuard.acquire();
  if (owner === null) { showToast('已有控制操作正在确认或提交'); return null; }
  document.querySelectorAll('.env-actions button, .scene-action, .control-recovery-warning button').forEach((button) => { button.disabled = true; });
  return owner;
}
function releaseControlInteraction(owner) {
  controlActionGuard.release(owner); renderEnvironments(); renderScenes();
}
async function beginEnvironmentAction(item, action) {
  const controlOwner = acquireControlInteraction(); if (controlOwner === null) return;
  try {
    const preflight = await api(`/environments/${encodeURIComponent(item.id)}/preflight?action=${encodeURIComponent(action)}`, { method: 'POST' });
    const blockers = Array.isArray(preflight.action_blockers) ? preflight.action_blockers : Array.isArray(preflight.blockers) ? preflight.blockers : [];
    const expected = preflight.confirmation?.[action];
    if (blockers.length || !expected) { showToast(blockers.join('；') || '该动作未获得允许'); return; }
    const supplied = await requestControlConfirmation(`${item.name} · ${action}`,
      [{ text: `当前状态：${controlStatusLabel(preflight.status)}` }, { text: `将执行固定白名单动作：${action}` }], expected);
    if (!supplied) return;
    const result = await api(`/environments/${encodeURIComponent(item.id)}/actions`, { method: 'POST', body: { action, confirmation: supplied } });
    showToast('操作已进入后台队列'); refreshControlData(); pollOperation(result.operation_id);
  } catch (error) { if (!(error instanceof StaleRequestError)) showToast(error.message); }
  finally { releaseControlInteraction(controlOwner); }
}
async function beginSceneAction(sceneId) {
  const controlOwner = acquireControlInteraction(); if (controlOwner === null) return;
  try {
    const preflight = await api(`/scenes/${encodeURIComponent(sceneId)}/preflight`, { method: 'POST' });
    const blockers = Array.isArray(preflight.blockers) ? preflight.blockers : [];
    if (blockers.length) { showToast(blockers.join('；')); return; }
    const phaseNames = { drain: '排空活动任务', stop_conflicts: '停止冲突', verify_release_ports: '验证显存释放与端口', validate_safety: '核验路径/磁盘/显存/Profile/依赖', start_desired: '启动目标', verify: '严格验证目标', verify_conflicts: '复核冲突已停止' };
    const lines = (preflight.plan || []).map((step) => {
      const checkNames = step.check?.type
        ? [step.check.type]
        : (Array.isArray(step.checks) ? step.checks.map((check) => check.type).filter(Boolean) : []);
      return { text: `${step.sequence}. ${phaseNames[step.phase] || step.phase} · ${step.target_id}${checkNames.length ? ` · ${checkNames.join(', ')}` : ''}` };
    });
    if (!lines.length) lines.push({ text: '目标场景已满足；提交后将再次核对状态。' });
    const supplied = await requestControlConfirmation(`${preflight.name} · 完整切换计划`, lines, preflight.confirmation);
    if (!supplied) return;
    const result = await api(`/scenes/${encodeURIComponent(sceneId)}/activate`, { method: 'POST', body: { confirmation: supplied } });
    showToast('场景操作已进入后台队列'); refreshControlData(); pollOperation(result.operation_id);
  } catch (error) { if (!(error instanceof StaleRequestError)) showToast(error.message); }
  finally { releaseControlInteraction(controlOwner); }
}
async function beginRecoveryResolve() {
  const controlOwner = acquireControlInteraction(); if (controlOwner === null) return;
  try {
    const preflight = await api('/control/recovery/preflight', { method: 'POST' });
    const blockers = Array.isArray(preflight.blockers) ? preflight.blockers : [];
    if (blockers.length) { showToast(blockers.join('；')); return; }
    const itemLines = (Array.isArray(preflight.items) ? preflight.items : []).map((item) => ({
      text: `${item.environment_id}：${controlStatusLabel(item.status)}（期望 ${controlStatusLabel(item.expected_state)}）`,
    }));
    if (!itemLines.length) itemLines.push({ text: `环境：${preflight.environment_id} · ${controlStatusLabel(preflight.status)}` });
    itemLines.push({ text: '所有恢复项均已严格核验；解除后将重新允许白名单控制动作。', danger: true });
    const supplied = await requestControlConfirmation('解除控制恢复锁', itemLines, preflight.confirmation);
    if (!supplied) return;
    await api('/control/recovery/resolve', { method: 'POST', body: { confirmation: supplied } });
    showToast('控制恢复锁已解除'); await Promise.allSettled([refreshControlData(), refreshAudit()]);
  } catch (error) { if (!(error instanceof StaleRequestError)) showToast(error.message); }
  finally { releaseControlInteraction(controlOwner); }
}
function waitForLifecycle(milliseconds, lifecycle, timerName) {
  return new Promise((resolve) => {
    if (!requestGuard.isCurrent(lifecycle)) { resolve(false); return; }
    const finish = (current) => { clearTimeout(timer); state.timers.delete(timerName); lifecycle.signal.removeEventListener('abort', aborted); resolve(current); };
    const aborted = () => finish(false);
    const timer = setTimeout(() => finish(requestGuard.isCurrent(lifecycle)), milliseconds);
    state.timers.set(timerName, timer); lifecycle.signal.addEventListener('abort', aborted, { once: true });
  });
}
async function pollOperation(operationId) {
  const lifecycle = requestGuard.begin(); const timerName = `operation:${operationId}`;
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (!requestGuard.isCurrent(lifecycle)) return;
    try {
      const operation = await api(`/operations/${encodeURIComponent(operationId)}`, { resource: timerName });
      if (!requestGuard.isCurrent(lifecycle)) return;
      const existing = state.operations.findIndex((item) => item.id === operation.id);
      if (existing >= 0) state.operations.splice(existing, 1, operation); else state.operations.unshift(operation);
      renderOperations();
      if (['succeeded', 'failed', 'interrupted'].includes(operation.status)) {
        showToast(operation.status === 'succeeded' ? '控制操作完成' : `控制操作${operation.status}`);
        await Promise.allSettled([refreshControlData(), refreshAudit()]); return;
      }
    } catch (error) {
      if (error instanceof StaleRequestError || error.status === 401 || !requestGuard.isCurrent(lifecycle)) return;
    }
    if (!await waitForLifecycle(1000, lifecycle, timerName)) return;
  }
  if (requestGuard.isCurrent(lifecycle)) showToast('操作仍在后台执行，请在操作日志中查看。');
}
function disableUnavailableActions() {
  const allowed = new Set(['menuButton', 'refreshButton', 'logoutButton', 'scanScriptsButton', 'refreshLogsButton', 'authSubmit']);
  document.querySelectorAll('button').forEach((button) => { if (allowed.has(button.id) || button.matches('.nav-item[data-page], [data-nav], .filter, .range-select button.active, .script-row, .control-dialog button')) return; button.disabled = true; button.title = button.title || '等待安全状态加载'; if (button.matches('.scene-action')) { const label = button.querySelector('span'); if (label) label.textContent = '加载场景状态…'; } });
  document.querySelectorAll('.env-table .status-label').forEach((label) => { label.className = 'status-label stopped'; label.textContent = '状态未接入'; });
  const consoleNode = byId('auditLogConsole');
  if (consoleNode) consoleNode.replaceChildren(element('p', 'empty-state', '登录后加载真实审计事件。'));
}

byId('authForm').addEventListener('submit', submitAuth); byId('logoutButton').addEventListener('click', logout); byId('scanScriptsButton').addEventListener('click', scanDiscovery); byId('refreshLogsButton').addEventListener('click', () => refreshSelectedLog(true)); byId('logSourceSelect').addEventListener('change', () => refreshSelectedLog(true)); byId('logLinesSelect').addEventListener('change', () => refreshSelectedLog(true));
document.querySelectorAll('.scene-action[data-switch], .scene-quick-action[data-switch]').forEach((button) => button.addEventListener('click', () => beginSceneAction(button.dataset.switch)));
byId('switchSceneButton').addEventListener('click', () => navigate('scenes'));
byId('refreshButton').addEventListener('click', async () => { await refreshAll(); showToast('已请求最新真实数据'); });
document.addEventListener('keydown', (event) => {
  if (!mobileViewport.matches || !sidebar.classList.contains('open')) return;
  if (event.key === 'Escape') { event.preventDefault(); closeSidebar(true); return; }
  if (event.key !== 'Tab') return;
  const focusable = [...sidebar.querySelectorAll('button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])')].filter((node) => !node.hidden);
  if (!focusable.length) { event.preventDefault(); return; }
  const first = focusable[0]; const last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});
document.addEventListener('visibilitychange', () => { if (!document.hidden && !document.body.classList.contains('auth-pending')) refreshAll(); });
syncSidebarForViewport();
bootstrap();
