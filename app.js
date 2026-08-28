'use strict';

const API_PREFIX = '/api/v1';
const SNAPSHOT_INTERVAL_MS = 3000;
const HISTORY_INTERVAL_MS = 12000;
const SERVICE_INTERVAL_MS = 5000;
const REQUEST_TIMEOUT_MS = 8000;
const ACTION_TIMEOUT_MS = 30000;
const SVG_NS = 'http://www.w3.org/2000/svg';
const gpuLayout = window.AxisGpuLayout;
if (!gpuLayout) throw new Error('GPU layout helper is unavailable.');
const requestGuard = new RequestGuard();
const actionGuard = new ExclusiveActionGuard();
let sceneProgressOperationId = null;
let draggedSceneId = null;
const state = {
  activePage: 'overview', authMode: 'login', csrfToken: null, username: '', snapshot: null,
  history: [], services: [], scenes: [], users: [], operations: [], timers: new Map(),
  chartSpecs: [], gpus: [], gpuCardSignature: null, monitorGpuSignature: null, serviceFilter: 'all',
};

class ApiError extends Error {
  constructor(status, code, message) { super(message); this.name = 'ApiError'; this.status = status; this.code = code; }
}
class StaleRequestError extends Error { constructor() { super('stale request'); this.name = 'StaleRequestError'; } }
function byId(id) { return document.getElementById(id); }
function text(id, value) { const node = byId(id); if (node) node.textContent = value; }
function element(tag, className, value) { const node = document.createElement(tag); if (className) node.className = className; if (value !== undefined) node.textContent = value; return node; }
function userElement(tag, className, value) { const node = element(tag, className, value); node.dataset.i18nSkip = ''; return node; }
function userOrUiElement(tag, className, value, fallback) { return value ? userElement(tag, className, value) : element(tag, className, fallback); }
function ui(value) { return window.axisI18n.translate(value); }
function dataText(id, value, fallback) { const node = byId(id); if (!node) return; node.toggleAttribute('data-i18n-skip', Boolean(value)); node.textContent = value || fallback; }
function confirmUi(message) { return confirm(window.axisI18n.translate(message)); }
function svgElement(tag, attributes = {}) { const node = document.createElementNS(SVG_NS, tag); Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value)); return node; }
function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
function normalizedPercent(value) { return finite(value) ? Math.min(100, Math.max(0, value)) : null; }
function percent(value) { const normalized = normalizedPercent(value); return normalized === null ? '不支持' : `${Math.round(normalized)}%`; }
function gib(value) { return finite(value) ? value / (1024 ** 3) : null; }
function mibToGib(value) { return finite(value) ? value / 1024 : null; }
function compactUuid(value) { return value ? `${value.slice(0, 8)}…${value.slice(-6)}` : ui('未知'); }
function formatDate(value, includeDate = false) {
  const date = new Date(value); if (Number.isNaN(date.getTime())) return '时间未知';
  return date.toLocaleString(window.axisI18n.language === 'zh' ? 'zh-CN' : 'en-US', includeDate ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false } : { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
}

async function api(path, options = {}) {
  const ticket = requestGuard.begin(options.resource);
  const method = (options.method || 'GET').toUpperCase();
  const controller = new AbortController();
  const abortLifecycle = () => controller.abort('lifecycle');
  if (ticket.signal.aborted) abortLifecycle(); else ticket.signal.addEventListener('abort', abortLifecycle, { once: true });
  const timeout = setTimeout(() => controller.abort('timeout'), options.timeout || REQUEST_TIMEOUT_MS);
  const headers = new Headers(options.headers || {});
  headers.set('Accept-Language', window.axisI18n.language);
  if (options.body !== undefined) headers.set('Content-Type', 'application/json');
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && state.csrfToken && !options.skipCsrf) headers.set('X-CSRF-Token', state.csrfToken);
  try {
    const response = await fetch(`${API_PREFIX}${path}`, { method, headers, credentials: 'same-origin', cache: 'no-store', signal: controller.signal, body: options.body === undefined ? undefined : JSON.stringify(options.body) });
    let payload = {};
    if (response.status !== 204) {
      try { payload = await response.json(); }
      catch (_) { throw new ApiError(response.status, 'invalid_response', '服务器返回了无法识别的数据。'); }
    }
    if (!requestGuard.isCurrent(ticket)) throw new StaleRequestError();
    if (!response.ok) {
      const serverError = payload?.error;
      const error = new ApiError(response.status, serverError?.code || 'request_failed', serverError?.message || `请求失败（${response.status}）`);
      if (response.status === 401 && !options.authRequest) showAuth('login', '登录状态已过期，请重新登录。');
      throw error;
    }
    return payload;
  } catch (error) {
    if (error instanceof StaleRequestError || !requestGuard.isCurrent(ticket)) throw new StaleRequestError();
    if (error.name === 'AbortError') throw new ApiError(0, 'timeout', '连接管理器超时。');
    if (error instanceof ApiError) throw error;
    throw new ApiError(0, 'network_error', '无法连接管理器。');
  } finally { clearTimeout(timeout); ticket.signal.removeEventListener('abort', abortLifecycle); }
}

const pages = [...document.querySelectorAll('.page')];
const navItems = [...document.querySelectorAll('.nav-item[data-page]')];
function navigate(page) {
  const next = byId(`page-${page}`); if (!next) return;
  state.activePage = page; pages.forEach((item) => item.classList.toggle('active', item === next)); navItems.forEach((item) => item.classList.toggle('active', item.dataset.page === page));
  text('pageTitle', next.dataset.title); closeSidebar(mobileViewport.matches); window.scrollTo({ top: 0, behavior: 'smooth' });
}
navItems.forEach((item) => item.addEventListener('click', () => navigate(item.dataset.page)));
document.querySelectorAll('[data-nav]').forEach((item) => item.addEventListener('click', () => navigate(item.dataset.nav)));
const sidebar = byId('sidebar'); const menuButton = byId('menuButton'); const mainContent = document.querySelector('main.main'); const mobileViewport = matchMedia('(max-width: 720px)');
function closeSidebar(restoreFocus = false) { const open = sidebar.classList.contains('open'); sidebar.classList.remove('open'); menuButton.setAttribute('aria-expanded', 'false'); if (mobileViewport.matches) { sidebar.inert = true; mainContent.inert = false; } if (restoreFocus && open) menuButton.focus(); }
function openSidebar() { if (!mobileViewport.matches) return; sidebar.inert = false; sidebar.classList.add('open'); mainContent.inert = true; menuButton.setAttribute('aria-expanded', 'true'); }
function syncSidebar() { if (mobileViewport.matches) closeSidebar(); else { sidebar.inert = false; mainContent.inert = false; sidebar.classList.remove('open'); } }
menuButton.addEventListener('click', () => sidebar.classList.contains('open') ? closeSidebar(true) : openSidebar()); mobileViewport.addEventListener('change', syncSidebar);

const toast = byId('toast'); let toastTimer;
function showToast(message) { toast.querySelector('span').textContent = message; toast.classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => toast.classList.remove('show'), 2800); }
function clearTimers() { state.timers.forEach(clearTimeout); state.timers.clear(); requestGuard.reset(); }
function startPolling(name, operation, interval) { const generation = requestGuard.generation; const run = async () => { if (generation !== requestGuard.generation || document.body.classList.contains('auth-pending')) return; try { await operation(); } catch (_) {} if (generation !== requestGuard.generation) return; state.timers.set(name, setTimeout(run, interval)); }; state.timers.set(name, setTimeout(run, interval)); }

function showAuth(mode, message = '') {
  clearTimers(); state.authMode = mode; state.csrfToken = null; document.body.classList.add('auth-pending');
  const setup = mode === 'setup'; text('authEyebrow', setup ? '首次设置' : '安全访问'); text('authTitle', setup ? '创建本机管理员' : '登录工作站'); text('authDescription', setup ? '首次设置仅允许在本机完成。密码至少 4 个字符。' : '使用管理员账户继续。'); text('authSubmit', setup ? '创建管理员并进入' : '登录'); text('authError', message);
  byId('authForm').hidden = false; byId('confirmPasswordLabel').hidden = !setup; byId('confirmPasswordInput').hidden = !setup; byId('confirmPasswordInput').required = setup;
}
async function submitAuth(event) {
  event.preventDefault(); const form = event.currentTarget; if (!form.reportValidity()) return;
  const username = byId('usernameInput').value.trim(); const password = byId('passwordInput').value; const setup = state.authMode === 'setup';
  if (setup && password !== byId('confirmPasswordInput').value) { text('authError', '两次输入的密码不一致。'); return; }
  try { const result = await api(setup ? '/auth/setup' : '/auth/login', { method: 'POST', body: { username, password }, skipCsrf: true, authRequest: true }); state.csrfToken = result.csrf_token; state.username = username; enterApplication(); }
  catch (error) { text('authError', error.message); }
}
async function bootstrap() {
  buildMonitorCharts(); syncSidebar();
  try { const status = await api('/auth/status', { authRequest: true }); if (!status.configured) return showAuth('setup'); if (!status.authenticated) return showAuth('login'); const me = await api('/auth/me', { authRequest: true }); state.csrfToken = me.csrf_token; state.username = me.username; enterApplication(); }
  catch (error) { showAuth('login', error.message); }
}
function enterApplication() { document.body.classList.remove('auth-pending'); text('logoutButton', (state.username || '管理员').slice(0, 2).toUpperCase()); clearTimers(); refreshAll(); startPolling('snapshot', refreshSnapshot, SNAPSHOT_INTERVAL_MS); startPolling('history', refreshHistory, HISTORY_INTERVAL_MS); startPolling('services', refreshServicesAndScenes, SERVICE_INTERVAL_MS); startPolling('logs', refreshLogs, SERVICE_INTERVAL_MS); }
async function logout() { try { await api('/auth/logout', { method: 'POST', authRequest: true }); showAuth('login', '已退出登录。'); } catch (error) { showToast(error.message); } }

async function refreshAll() { await Promise.allSettled([refreshSnapshot(), refreshHistory(), refreshServicesAndScenes(), refreshUsers(), refreshLogs()]); }
async function refreshSnapshot() { if (document.hidden) return; try { state.snapshot = normalizeSnapshot(await api('/snapshot', { resource: 'snapshot' })); renderSnapshot(); } catch (error) { if (!(error instanceof StaleRequestError)) { text('freshnessLabel', '监控离线'); } } }
async function refreshHistory() { if (document.hidden) return; try { const result = await api('/history?window=15m', { resource: 'history' }); state.history = (result.samples || []).map(normalizeHistorySample); renderCharts(); } catch (_) {} }
async function refreshServicesAndScenes() {
  if (document.hidden) return;
  try { const [serviceData, sceneData] = await Promise.all([api('/registered-services', { resource: 'services' }), api('/scenes', { resource: 'scenes' })]); state.services = serviceData.services || []; state.scenes = sceneData.scenes || []; renderServices(); renderScenes(); }
  catch (error) { if (!(error instanceof StaleRequestError)) showToast(`服务状态读取失败：${error.message}`); }
}
async function refreshLogs() { if (document.hidden) return; try { const result = await api('/operations?limit=50', { resource: 'operations' }); state.operations = result.operations || []; renderOperations(); renderOperationTimeline(); } catch (_) {} }
async function refreshUsers() { if (document.hidden) return; try { const result = await api('/users', { resource: 'users' }); state.users = result.users || []; renderUsers(); } catch (error) { const rows = byId('userRows'); rows.replaceChildren(element('p', 'empty-state', `用户加载失败：${error.message}`)); throw error; } }

function normalizeGpu(gpu) { return { ...gpu, load_percent: normalizedPercent(gpu.load_percent), memory_percent: normalizedPercent(gpu.memory_percent) }; }
function normalizeHistorySample(sample) { return { ...sample, cpu_load_percent: normalizedPercent(sample.cpu_load_percent), memory_percent: normalizedPercent(sample.memory_percent), gpus: Array.isArray(sample.gpus) ? sample.gpus.map(normalizeGpu) : [] }; }
function normalizeSnapshot(snapshot) { const host = snapshot.host || {}; return { ...snapshot, host: { ...host, cpu: { ...(host.cpu || {}), load_percent: normalizedPercent(host.cpu?.load_percent) }, memory: { ...(host.memory || {}), percent: normalizedPercent(host.memory?.percent) }, disks: Array.isArray(host.disks) ? host.disks.map((disk) => ({ ...disk, percent: normalizedPercent(disk.percent) })) : [] }, gpus: Array.isArray(snapshot.gpus) ? snapshot.gpus.map(normalizeGpu) : [] }; }
function snapshotAsHistory(snapshot) { return snapshot ? { sampled_at: snapshot.sampled_at, cpu_load_percent: snapshot.host?.cpu?.load_percent, memory_percent: snapshot.host?.memory?.percent, gpus: snapshot.gpus || [] } : null; }
function currentSeries() { const samples = state.history.slice(); const current = snapshotAsHistory(state.snapshot); if (current && !samples.some((sample) => sample.sampled_at === current.sampled_at)) samples.push(current); return samples.sort((a, b) => new Date(a.sampled_at) - new Date(b.sampled_at)); }

function renderSnapshot() {
  const snapshot = state.snapshot; if (!snapshot) return; const host = snapshot.host || {}; const cpu = host.cpu || {}; const memory = host.memory || {};
  text('cpuMetric', percent(cpu.load_percent)); text('cpuDetail', `温度 ${finite(cpu.temperature_c) ? `${Math.round(cpu.temperature_c)}°C` : '不支持'}`);
  const used = gib(memory.used_bytes); const total = gib(memory.total_bytes); text('memoryMetric', used === null ? '不支持' : `${used.toFixed(1)} GB`); text('memoryDetail', total === null ? '总量不可用' : `${percent(memory.percent)} 已使用 · 共 ${total.toFixed(1)} GB`);
  const disks = (host.disks || []).filter((disk) => finite(disk.total_bytes)); const disk = disks.find((item) => String(item.mountpoint || item.device).toUpperCase().startsWith('C')) || disks[0];
  if (disk) { text('diskLabel', `${disk.device || disk.mountpoint} 可用`); text('diskMetric', `${gib(disk.total_bytes - disk.used_bytes).toFixed(1)} GB`); text('diskDetail', `${percent(disk.percent)} 已使用`); } else { text('diskMetric', '不支持'); }
  const containers = snapshot.docker?.containers || []; const running = containers.filter((item) => String(item.state).toLowerCase() === 'running').length; text('dockerMetric', `${running}/${containers.length}`); text('dockerDetail', `${running} 运行 · ${containers.length - running} 停止`);
  document.querySelector('.host-mini strong').textContent = location.hostname || '本机'; document.querySelector('.host-load').textContent = percent(cpu.load_percent);
  syncGpuCards(snapshot.gpus || []); renderCollectorErrors(snapshot.collector_errors || []); renderCharts();
  text('freshnessLabel', '实时'); text('clock', formatDate(snapshot.sampled_at));
}
function createGpuCard(gpu, position) {
  const lane = element('article', `gpu-lane${position === 0 ? ' gpu-primary' : ''}`); lane.dataset.gpuKey = gpu._uiKey; lane.style.setProperty('--gpu-order', String(position));
  const head = element('div', 'gpu-head'); const identity = element('div'); const index = element('span', 'gpu-index', `GPU ${gpu.index}`); const name = userElement('h2', 'gpu-name', ''); const role = userElement('p', 'gpu-role', ''); identity.append(index, name, role); const util = element('div', 'gpu-util'); util.append(userElement('strong', 'gpu-util-value', '--'), element('span', '', '% 负载')); head.append(identity, util);
  const telemetry = element('div', 'gpu-telemetry'); const memory = element('div', 'memory-visual'); memory.style.setProperty('--used', '0%'); const ring = element('div', 'memory-ring'); const ringCopy = element('span'); ringCopy.append(userElement('strong', 'gpu-memory-value', '--'), element('small', 'gpu-memory-total', '/ -- GB')); ring.append(ringCopy); const memoryCopy = element('div', 'memory-copy'); memoryCopy.append(element('span', '', '显存占用'), element('strong', 'gpu-memory-percent', '--'), element('small', 'gpu-memory-free', '等待数据')); memory.append(ring, memoryCopy);
  const trend = element('div', 'gpu-load-trend'); const trendHead = element('div', 'trend-head'); trendHead.append(element('span', '', 'GPU 负载趋势'), element('small', '', '最近 15 分钟')); const plot = element('div', 'trend-plot'); const scale = element('span', 'trend-scale'); scale.append(element('i', '', '100%'), element('i', '', '50%'), element('i', '', '0')); const svg = svgElement('svg', { viewBox: '0 0 300 120', preserveAspectRatio: 'none', role: 'img', 'aria-label': ui(`${gpu.name || `GPU ${gpu.index}`} GPU 负载曲线`) }); svg.append(svgElement('path', { class: 'spark-grid', d: 'M0 10H300M0 60H300M0 110H300' }), svgElement('polyline', { class: 'gpu-sparkline', points: '' })); plot.append(scale, svg); const axis = element('div', 'trend-axis'); ['-15m', '-10m', '-5m', '现在'].forEach((label) => axis.append(element('span', '', label))); trend.append(trendHead, plot, axis); telemetry.append(memory, trend);
  const services = element('div', 'gpu-services'); const mark = element('div', 'workload-icon', String(gpu.index ?? '?')); const serviceCopy = element('div', 'gpu-service-copy'); serviceCopy.append(element('strong', 'gpu-service-names', '尚未登记服务'), element('span', 'gpu-service-meta', 'GPU 为用户登记标签')); const serviceLink = element('button', 'quiet-link', '查看服务'); serviceLink.type = 'button'; serviceLink.addEventListener('click', () => navigate('environments')); services.append(mark, serviceCopy, serviceLink);
  const metrics = element('div', 'metric-row'); [['gpu-temp', '温度'], ['gpu-power', '功耗'], ['gpu-uuid', 'UUID'], ['gpu-state', '状态']].forEach(([className, label]) => { const item = element('span'); item.append(element('b', className, '--'), document.createTextNode(label)); metrics.append(item); });
  lane.append(head, telemetry, services, metrics); return lane;
}
function updateGpuCard(lane, gpu) {
  const used = mibToGib(gpu.memory_used_mib); const total = mibToGib(gpu.memory_total_mib); const free = used !== null && total !== null ? Math.max(0, total - used) : null;
  lane.querySelector('.gpu-index').textContent = `GPU ${gpu.index}`; lane.querySelector('.gpu-name').textContent = gpu.name || `GPU ${gpu.index}`; lane.querySelector('.gpu-role').textContent = compactUuid(gpu.uuid); lane.querySelector('.gpu-util-value').textContent = finite(gpu.load_percent) ? String(Math.round(gpu.load_percent)) : '--'; lane.querySelector('.gpu-memory-value').textContent = used === null ? '--' : used.toFixed(1); lane.querySelector('.gpu-memory-total').textContent = total === null ? `/ ${ui('不支持')}` : `/ ${total.toFixed(1)} GB`;
  lane.querySelector('.gpu-memory-percent').textContent = percent(gpu.memory_percent); lane.querySelector('.gpu-memory-free').textContent = free === null ? '不支持' : `剩余 ${free.toFixed(1)} GB`; lane.querySelector('.gpu-temp').textContent = finite(gpu.temperature_c) ? `${Math.round(gpu.temperature_c)}°C` : '不支持'; lane.querySelector('.gpu-power').textContent = finite(gpu.power_w) ? `${Math.round(gpu.power_w)} W` : '不支持'; lane.querySelector('.gpu-uuid').textContent = compactUuid(gpu.uuid); lane.querySelector('.gpu-state').textContent = '已检测'; lane.querySelector('.memory-visual').style.setProperty('--used', `${gpu.memory_percent || 0}%`);
  const values = currentSeries().map((sample) => gpuLayout.metricForGpu(sample, gpu, 'load_percent')).filter(finite); renderPolyline(lane.querySelector('.gpu-sparkline'), values, gpu.name || `GPU ${gpu.index}`);
}
function syncGpuCards(gpus) {
  const ordered = gpuLayout.prepareGpus(gpus); const signature = gpuLayout.gpuSetSignature(ordered); const stage = byId('gpuStage'); state.gpus = ordered;
  if (signature !== state.gpuCardSignature) { stage.replaceChildren(); if (!ordered.length) stage.append(element('p', 'gpu-empty', '未检测到 NVIDIA GPU。')); else ordered.forEach((gpu, position) => stage.append(createGpuCard(gpu, position))); state.gpuCardSignature = signature; }
  ordered.forEach((gpu) => { const lane = [...stage.querySelectorAll('.gpu-lane')].find((item) => item.dataset.gpuKey === gpu._uiKey); if (lane) updateGpuCard(lane, gpu); }); renderGpuServiceLabels(); syncMonitorCharts();
}
function renderPolyline(polyline, values, device) { if (!polyline) return; polyline.closest('svg').setAttribute('aria-label', ui(`${device} GPU 负载曲线`)); const normalized = values.map(normalizedPercent).filter((value) => value !== null); if (!normalized.length) { polyline.setAttribute('points', ''); return; } polyline.setAttribute('points', normalized.map((value, index) => `${normalized.length === 1 ? 150 : index * (300 / (normalized.length - 1))},${110 - value}`).join(' ')); }
function renderCollectorErrors(errors) { const banner = byId('collectorStatus'); banner.hidden = !errors.length; banner.replaceChildren(); if (errors.length) banner.append(element('strong', '', '部分数据降级'), element('span', '', errors.map((error) => error.message || '采集失败').join('；'))); }
function summarize(values) { if (!values.length) return { current: '--', peak: '--' }; return { current: Math.round(values.at(-1)), peak: Math.round(Math.max(...values)) }; }
function buildMonitorCharts() {
  const grid = document.querySelector('.monitor-grid'); grid.replaceChildren(); const specs = [['HOST', 'CPU 总负载', (sample) => sample.cpu_load_percent], ['HOST', '内存占用', (sample) => sample.memory_percent], ...state.gpus.flatMap((gpu) => [[`GPU ${gpu.index}`, 'GPU 负载', (sample) => gpuLayout.metricForGpu(sample, gpu, 'load_percent')], [`GPU ${gpu.index}`, '显存占用', (sample) => gpuLayout.metricForGpu(sample, gpu, 'memory_percent')]])];
  state.chartSpecs = specs.map(([kicker, title, getter]) => { const section = element('section', 'chart-section'); const heading = element('div', 'chart-title'); const copy = element('div'); copy.append(element('span', 'chart-kicker', kicker), element('h2', '', title)); const current = element('strong', '', '--'); current.append(element('small', '', '% 当前')); heading.append(copy, current); const frame = element('div', 'chart-frame'); const svg = svgElement('svg', { class: 'line-chart', viewBox: '0 0 900 230', preserveAspectRatio: 'none' }); const line = svgElement('polyline', { class: 'line primary-line', fill: 'none', points: '' }); svg.append(line); frame.append(svg); section.append(heading, frame); grid.append(section); return { title, getter, current, line }; });
  if (!state.gpus.length) grid.append(element('p', 'empty-state monitor-gpu-empty', '未检测到 NVIDIA GPU，当前仅显示主机资源。'));
}
function syncMonitorCharts() { const signature = gpuLayout.gpuSetSignature(state.gpus); if (signature !== state.monitorGpuSignature) { state.monitorGpuSignature = signature; buildMonitorCharts(); } }
function renderCharts() { const samples = currentSeries(); state.chartSpecs.forEach((spec) => { const values = samples.map(spec.getter).filter(finite); spec.current.firstChild.nodeValue = values.length ? String(summarize(values).current) : '--'; spec.line.setAttribute('points', values.map((value, index) => `${values.length === 1 ? 450 : index * (900 / (values.length - 1))},${215 - Math.min(100, Math.max(0, value)) * 2}`).join(' ')); }); }

const statusLabels = { running: '已启动', stopped: '已停止', unhealthy: '异常', unknown: '状态未知' };
const sceneStatusLabels = { active: '已激活', partial: '部分启动', inactive: '未激活' };
function statusClass(value) { return value === 'running' ? 'ready' : value === 'stopped' ? 'stopped' : value === 'unhealthy' ? 'danger' : 'partial'; }
function renderServices() {
  renderOverviewServices(); renderRegisteredServiceTable(); renderGpuServiceLabels(); renderOperationTimeline();
  const stopAll = byId('stopAllServicesButton');
  stopAll.disabled = !state.services.length || state.services.some((service) => service.operation_pending) || actionGuard.pending;
}
function renderOverviewServices() {
  const list = byId('serviceList'); list.replaceChildren(); if (!state.services.length) { list.append(element('p', 'empty-state', '尚未添加已登记服务。')); return; }
  state.services.slice(0, 8).forEach((service) => { const row = element('div', 'service-row'); row.append(element('span', `service-state ${statusClass(service.status.state)}`)); const copy = element('div'); copy.append(userElement('strong', '', service.name), userOrUiElement('small', '', service.description || service.gpu_label, '无说明')); const action = element('button', 'row-action', service.ui_url ? '打开 UI' : '查看'); action.addEventListener('click', () => service.ui_url ? window.open(service.ui_url, '_blank', 'noopener,noreferrer') : navigate('environments')); row.append(copy, element('span', 'port', service.port ? `:${service.port}` : '无端口'), element('span', 'uptime', statusLabels[service.status.state] || '未知'), action); list.append(row); });
}
function renderGpuServiceLabels() {
  document.querySelectorAll('.gpu-lane[data-gpu-key]').forEach((lane) => { const gpu = state.gpus.find((item) => item._uiKey === lane.dataset.gpuKey); if (!gpu) return; const matches = state.services.filter((item) => gpuLayout.serviceGpuKeys(item, state.gpus).includes(gpu._uiKey)); const names = lane.querySelector('.gpu-service-names'); const meta = lane.querySelector('.gpu-service-meta'); names.toggleAttribute('data-i18n-skip', Boolean(matches.length)); meta.toggleAttribute('data-i18n-skip', Boolean(matches.length)); names.textContent = matches.length ? matches.map((item) => item.name).join(' · ') : '尚未登记服务'; meta.textContent = matches.length ? matches.map((item) => `${item.name} · ${ui(statusLabels[item.status.state] || '状态未知')}`).join(ui('；')) : 'GPU 为用户登记标签'; });
}
function renderRegisteredServiceTable() {
  const rows = byId('registeredServiceRows'); rows.replaceChildren(); const query = byId('serviceSearch').value.trim().toLowerCase(); const filtered = state.services.filter((item) => (state.serviceFilter === 'all' || item.status.state === state.serviceFilter) && [item.name, item.description, item.gpu_label, item.port].join(' ').toLowerCase().includes(query));
  if (!filtered.length) { rows.append(element('p', 'empty-state', state.services.length ? '没有符合筛选条件的服务。' : '尚未添加服务。')); return; }
  filtered.forEach((service) => { const row = element('div', 'table-row'); const title = element('span'); const logo = userElement('i', 'env-logo', service.name.slice(0, 1).toUpperCase()); const copy = element('span'); copy.append(userElement('b', '', service.name), userElement('small', '', service.script_path)); title.append(logo, copy); const status = element('i', `status-label ${statusClass(service.status.state)}`, service.busy ? '操作中' : statusLabels[service.status.state] || '状态未知'); status.title = service.status.checked_at ? `状态记录于 ${formatDate(service.status.checked_at, true)}` : '尚未记录状态'; const actions = element('span', 'row-buttons'); const check = element('button', '', '检查状态'); check.disabled = service.operation_pending || actionGuard.pending; check.addEventListener('click', () => runServiceStatusCheck(service)); actions.append(check); ['start', 'stop', 'restart'].forEach((action) => { const button = element('button', '', { start: '启动', stop: '停止', restart: '重启' }[action]); button.disabled = service.operation_pending || actionGuard.pending; button.addEventListener('click', () => runServiceAction(service, action)); actions.append(button); }); if (service.ui_url) { const ui = element('button', '', 'UI'); ui.addEventListener('click', () => window.open(service.ui_url, '_blank', 'noopener,noreferrer')); actions.append(ui); } const edit = element('button', 'icon-only', '编辑'); edit.disabled = service.operation_pending || actionGuard.pending; edit.addEventListener('click', () => openServiceDialog(service)); const remove = element('button', 'icon-only', '删除'); remove.disabled = service.operation_pending || actionGuard.pending; remove.addEventListener('click', () => deleteService(service)); actions.append(edit, remove); row.append(title, userOrUiElement('span', '', service.description, '—'), userOrUiElement('span', '', service.gpu_label, '未标注'), element('span', 'mono', service.port ? String(service.port) : '—'), status, actions); rows.append(row); });
}

function clearSceneDropMarkers() { document.querySelectorAll('.scene-panel').forEach((panel) => panel.classList.remove('drop-before', 'drop-after', 'drop-horizontal')); }
function sceneDropPosition(event, targetPanel) { const source = draggedSceneId ? document.querySelector(`[data-scene-id="${draggedSceneId}"]`) : null; const targetRect = targetPanel.getBoundingClientRect(); const sourceRect = source?.getBoundingClientRect(); const horizontal = Boolean(sourceRect && Math.abs(sourceRect.top - targetRect.top) < targetRect.height / 2); const after = horizontal ? event.clientX > targetRect.left + targetRect.width / 2 : event.clientY > targetRect.top + targetRect.height / 2; return { after, horizontal }; }
function moveSceneCard(sceneId, targetId, after = false) {
  if (!sceneId || sceneId === targetId) return;
  const ids = state.scenes.map((scene) => scene.id).filter((id) => id !== sceneId);
  let index = ids.indexOf(targetId); if (index < 0) return; if (after) index += 1;
  ids.splice(index, 0, sceneId); saveSceneOrder(ids);
}
function moveSceneByOffset(sceneId, offset) {
  const ids = state.scenes.map((scene) => scene.id); const index = ids.indexOf(sceneId); const next = index + offset;
  if (index < 0 || next < 0 || next >= ids.length) return;
  [ids[index], ids[next]] = [ids[next], ids[index]]; saveSceneOrder(ids);
}
async function saveSceneOrder(sceneIds) {
  const owner = actionGuard.acquire(); if (!owner) return showToast('已有操作正在执行');
  const previous = [...state.scenes]; const scenes = new Map(previous.map((scene) => [scene.id, scene])); state.scenes = sceneIds.map((id) => scenes.get(id)).filter(Boolean); renderScenes();
  try { const result = await api('/scenes/reorder', { method: 'POST', body: { scene_ids: sceneIds } }); state.scenes = result.scenes || state.scenes; showToast('场景顺序已保存'); }
  catch (error) { state.scenes = previous; showToast(error.message); }
  finally { actionGuard.release(owner); renderScenes(); }
}

function renderOverviewSceneSelect() {
  const select = byId('overviewSceneSelect'); const signature = JSON.stringify([window.axisI18n.language, ...state.scenes.map((scene) => [scene.id, scene.name, scene.state])]);
  if (select.dataset.sceneSignature !== signature) {
    const placeholder = element('option', '', '切换场景'); placeholder.value = ''; select.replaceChildren(placeholder);
    state.scenes.forEach((scene) => { const option = userElement('option', '', `${scene.name}${scene.state === 'active' ? ui('（当前）') : ''}`); option.value = scene.id; select.append(option); });
    select.dataset.sceneSignature = signature;
  }
  if (document.activeElement !== select) select.value = '';
  select.disabled = !state.scenes.length || actionGuard.pending || state.scenes.some((scene) => scene.busy);
}

function handleOverviewSceneChange(event) {
  const select = event.currentTarget; const sceneId = select.value; select.value = '';
  if (!sceneId) return;
  const scene = state.scenes.find((item) => String(item.id) === sceneId);
  if (!scene) return showToast('场景不存在，请刷新后重试');
  select.disabled = true; activateScene(scene).finally(renderOverviewSceneSelect);
}

function renderScenes() {
  text('sceneNavCount', String(state.scenes.length)); renderOverviewSceneSelect(); const list = byId('sceneList'); list.replaceChildren(); if (!state.scenes.length) { list.append(element('p', 'empty-state', '尚未添加场景。')); dataText('activeSceneName', '', '尚未添加场景'); renderOperationTimeline(); return; }
  state.scenes.forEach((scene, index) => {
    const panel = element('article', `scene-panel${scene.state === 'active' ? ' selected' : ''}`); panel.dataset.sceneId = scene.id; panel.draggable = !scene.busy && !actionGuard.pending;
    panel.addEventListener('dragstart', (event) => { if (!panel.draggable) return event.preventDefault(); draggedSceneId = scene.id; panel.classList.add('dragging'); panel.setAttribute('aria-grabbed', 'true'); event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', scene.id); });
    panel.addEventListener('dragover', (event) => { if (!draggedSceneId || draggedSceneId === scene.id) return; event.preventDefault(); clearSceneDropMarkers(); const position = sceneDropPosition(event, panel); panel.classList.add(position.after ? 'drop-after' : 'drop-before'); if (position.horizontal) panel.classList.add('drop-horizontal'); });
    panel.addEventListener('drop', (event) => { event.preventDefault(); const after = panel.classList.contains('drop-after'); const sourceId = draggedSceneId || event.dataTransfer.getData('text/plain'); document.querySelector(`[data-scene-id="${sourceId}"]`)?.classList.remove('dragging'); clearSceneDropMarkers(); draggedSceneId = null; moveSceneCard(sourceId, scene.id, after); });
    panel.addEventListener('dragend', () => { draggedSceneId = null; panel.classList.remove('dragging'); panel.setAttribute('aria-grabbed', 'false'); clearSceneDropMarkers(); });
    const top = element('div', 'scene-panel-top'); const topActions = element('span', 'scene-panel-meta'); const handle = element('button', 'scene-drag-handle', '⠿'); handle.type = 'button'; handle.title = '拖动调整场景位置'; handle.setAttribute('aria-label', `拖动调整 ${scene.name} 的位置`); const sceneStatusClass = scene.state === 'active' ? 'scene-status' : scene.state === 'inactive' ? 'scene-inactive' : ''; topActions.append(element('i', sceneStatusClass, sceneStatusLabels[scene.state] || '状态未知'), handle); top.append(element('span', '', `场景 ${String(index + 1).padStart(2, '0')}`), topActions);
    panel.append(top, userElement('h2', '', scene.name), userOrUiElement('p', '', scene.description, '无说明'));
    const map = element('div', 'scene-map'); const sceneServices = scene.services || scene.service_ids.map((id, order) => ({ id, name: scene.service_names[order], ui_url: '', status: { state: 'unknown' } })); if (!sceneServices.length) map.append(element('div', '', '此场景不启动任何服务'));
    sceneServices.forEach((service, order) => { const item = element('div'); item.append(element('span', '', `启动顺序 ${order + 1}`), userElement('strong', '', service.name)); const meta = element('small', 'scene-service-meta'); const status = element('i', 'scene-service-status'); status.append(element('i', `service-state ${statusClass(service.status.state)}`), document.createTextNode(service.busy ? '操作中' : statusLabels[service.status.state] || '未知')); meta.append(status); item.append(meta); if (service.ui_url) { const uiButton = element('button', 'scene-ui-link', '打开 UI ↗'); uiButton.type = 'button'; uiButton.addEventListener('click', () => window.open(service.ui_url, '_blank', 'noopener,noreferrer')); item.append(uiButton); } map.append(item); });
    const actions = element('div', 'scene-card-actions'); const reorder = element('span', 'scene-reorder-controls'); const up = element('button', 'button secondary', '上移'); up.disabled = index === 0 || scene.busy || actionGuard.pending; up.addEventListener('click', () => moveSceneByOffset(scene.id, -1)); const down = element('button', 'button secondary', '下移'); down.disabled = index === state.scenes.length - 1 || scene.busy || actionGuard.pending; down.addEventListener('click', () => moveSceneByOffset(scene.id, 1)); reorder.append(up, down); const activate = element('button', 'button primary', scene.state === 'active' ? '重新切换' : '切换到此场景'); activate.disabled = scene.busy || actionGuard.pending; activate.addEventListener('click', () => activateScene(scene)); const edit = element('button', 'button secondary', '编辑'); edit.disabled = scene.busy || actionGuard.pending; edit.addEventListener('click', () => openSceneDialog(scene)); const remove = element('button', 'button secondary', '删除'); remove.disabled = scene.busy || actionGuard.pending; remove.addEventListener('click', () => deleteScene(scene)); actions.append(reorder, activate, edit, remove); panel.append(map, actions); list.append(panel);
  });
  const active = state.scenes.find((item) => item.state === 'active'); dataText('activeSceneName', active ? `${active.name} · ${ui('已激活')}` : '', '场景未完整激活'); dataText('activeSceneSummary', active?.description || '', active ? ui(`包含 ${active.service_ids.length} 个服务`) : '当前服务状态不完整符合任何场景。');
  renderOperationTimeline();
}

async function runServiceAction(service, action) { const owner = actionGuard.acquire(); if (!owner) return showToast('已有操作正在执行'); try { const result = await api(`/registered-services/${service.id}/actions`, { method: 'POST', body: { action } }); showToast('服务操作已开始'); await pollOperation(result.operation_id); } catch (error) { showToast(error.message); } finally { actionGuard.release(owner); await refreshServicesAndScenes(); } }
async function runServiceStatusCheck(service) { const owner = actionGuard.acquire(); if (!owner) return showToast('已有操作正在执行'); try { await api(`/registered-services/${service.id}/status`, { method: 'POST' }); showToast('状态检查完成'); } catch (error) { showToast(error.message); } finally { actionGuard.release(owner); await refreshServicesAndScenes(); } }
async function stopAllServices() { if (!state.services.length || !confirmUi('停止所有已登记服务？管理器将依次调用每个服务脚本的 stop 动作。')) return; const owner = actionGuard.acquire(); if (!owner) return showToast('已有操作正在执行'); try { const result = await api('/registered-services/actions/stop-all', { method: 'POST' }); showToast('正在停止全部服务'); await pollOperation(result.operation_id); } catch (error) { showToast(error.message); } finally { actionGuard.release(owner); await refreshServicesAndScenes(); } }
async function activateScene(scene) { const owner = actionGuard.acquire(); if (!owner) return showToast('已有操作正在执行'); try { const result = await api(`/scenes/${scene.id}/activate`, { method: 'POST' }); openSceneProgress(scene, result.operation_id); await pollOperation(result.operation_id, (item) => renderSceneProgress(scene, item), null); } catch (error) { showToast(error.message); if (byId('sceneProgressDialog').open) byId('sceneProgressDialog').close(); } finally { sceneProgressOperationId = null; actionGuard.release(owner); await refreshServicesAndScenes(); } }
async function pollOperation(id, onUpdate = null, maxAttempts = 240) { for (let i = 0; maxAttempts === null || i < maxAttempts; i += 1) { await new Promise((resolve) => setTimeout(resolve, 1000)); const item = await api(`/operations/${id}`, { timeout: ACTION_TIMEOUT_MS }); if (onUpdate) onUpdate(item); if (!['queued', 'running'].includes(item.status)) { showToast(item.status === 'succeeded' ? '操作成功' : item.status === 'interrupted' ? '操作已终止' : item.error_summary || '操作失败'); await refreshLogs(); return item; } } throw new ApiError(0, 'operation_timeout', '操作仍在后台执行，请到日志中心查看。'); }

function openSceneProgress(scene, operationId) {
  sceneProgressOperationId = operationId;
  text('sceneProgressTitle', `正在切换到 ${scene.name}`); text('sceneProgressSummary', '管理器正在按顺序停止和启动服务。'); text('sceneProgressPercent', '0%'); text('sceneProgressCurrent', '等待第一项服务操作');
  byId('sceneProgressBar').style.width = '0%'; byId('sceneProgressLog').replaceChildren(element('li', '', '等待第一条服务操作记录。'));
  byId('cancelSceneSwitchButton').hidden = false; byId('cancelSceneSwitchButton').disabled = false; text('cancelSceneSwitchButton', '终止切换并返回'); byId('closeSceneProgressButton').hidden = true;
  const dialog = byId('sceneProgressDialog'); if (!dialog.open) dialog.showModal();
}
function renderSceneProgress(scene, operation) {
  const steps = operation.steps || []; const total = Math.max(1, state.services.filter((service) => !scene.service_ids.includes(service.id) && service.status.state === 'running').length + scene.service_ids.length); const finished = steps.filter((step) => step.status !== 'running').length; const terminal = !['queued', 'running'].includes(operation.status); const progress = terminal ? 100 : Math.min(99, Math.round((finished / total) * 100));
  text('sceneProgressPercent', `${progress}%`); byId('sceneProgressBar').style.width = `${progress}%`;
  const current = [...steps].reverse().find((step) => step.status === 'running');
  if (current) text('sceneProgressCurrent', `${current.action === 'start' ? '正在启动' : '正在停止'} · ${targetName('service', current.target_id)}`);
  else if (terminal) text('sceneProgressCurrent', operation.status === 'succeeded' ? '场景切换完成' : operation.status === 'interrupted' ? '场景切换已终止' : operation.error_summary || '场景切换失败');
  const log = byId('sceneProgressLog'); log.replaceChildren();
  if (!steps.length) log.append(element('li', '', terminal ? '没有需要执行的服务步骤。' : '等待第一条服务操作记录。'));
  steps.forEach((step) => { const item = element('li', step.status); item.append(element('time', '', formatDate(step.started_at)), element('span', '', `${ui(step.action === 'start' ? '启动' : '停止')} ${targetName('service', step.target_id)}`), element('em', '', step.status === 'running' ? '进行中' : step.status === 'succeeded' ? '成功' : step.status === 'interrupted' ? '已终止' : '失败')); log.append(item); });
  log.scrollTop = log.scrollHeight;
  if (terminal) { text('sceneProgressTitle', operation.status === 'succeeded' ? `${scene.name} 已就绪` : operation.status === 'interrupted' ? '切换已终止' : '场景切换未完成'); text('sceneProgressSummary', operation.error_summary || (operation.status === 'succeeded' ? '所有服务均已达到目标状态。' : '请在日志中心查看失败步骤。')); byId('cancelSceneSwitchButton').hidden = true; byId('closeSceneProgressButton').hidden = false; }
}
async function cancelSceneSwitch() {
  const operationId = sceneProgressOperationId; if (!operationId) return;
  const button = byId('cancelSceneSwitchButton'); button.disabled = true; text('cancelSceneSwitchButton', '正在提交终止请求…');
  try { await api(`/operations/${operationId}/cancel`, { method: 'POST' }); byId('sceneProgressDialog').close(); showToast('终止请求已提交，当前步骤结束后停止'); }
  catch (error) { button.disabled = false; text('cancelSceneSwitchButton', '终止切换并返回'); showToast(error.message); }
}

function openServiceDialog(service = null) { byId('serviceForm').reset(); text('serviceFormError', ''); text('serviceDialogTitle', service ? '编辑服务' : '添加服务'); byId('serviceId').value = service?.id || ''; byId('serviceName').value = service?.name || ''; byId('serviceDescription').value = service?.description || ''; byId('serviceScriptPath').value = service?.script_path || ''; byId('serviceGpu').value = service?.gpu_label || ''; byId('servicePort').value = service?.port || ''; byId('serviceUiUrl').value = service?.ui_url || ''; byId('serviceDialog').showModal(); }
async function saveService(event) { event.preventDefault(); const id = byId('serviceId').value; const payload = { name: byId('serviceName').value.trim(), description: byId('serviceDescription').value.trim(), script_path: byId('serviceScriptPath').value.trim(), gpu_label: byId('serviceGpu').value.trim(), port: byId('servicePort').value ? Number(byId('servicePort').value) : null, ui_url: byId('serviceUiUrl').value.trim() }; try { await api(id ? `/registered-services/${id}` : '/registered-services', { method: id ? 'PUT' : 'POST', body: payload }); byId('serviceDialog').close(); showToast(id ? '服务已更新' : '服务已添加'); await refreshServicesAndScenes(); } catch (error) { text('serviceFormError', error.message); } }
async function deleteService(service) { if (!confirmUi(`删除服务“${service.name}”的登记记录？原始脚本和服务不会被删除。`)) return; try { await api(`/registered-services/${service.id}`, { method: 'DELETE' }); showToast('服务登记已删除'); await refreshServicesAndScenes(); } catch (error) { showToast(error.message); } }

function openSceneDialog(scene = null) { byId('sceneForm').reset(); text('sceneFormError', ''); text('sceneDialogTitle', scene ? '编辑场景' : '添加场景'); byId('sceneId').value = scene?.id || ''; byId('sceneName').value = scene?.name || ''; byId('sceneDescription').value = scene?.description || ''; const selected = scene?.service_ids || []; const container = byId('sceneServiceChoices'); container.replaceChildren(); const ordered = [...selected.map((id) => state.services.find((item) => item.id === id)).filter(Boolean), ...state.services.filter((item) => !selected.includes(item.id))]; ordered.forEach((service) => { const row = element('div', 'scene-service-choice'); row.dataset.id = service.id; const label = element('label'); label.dataset.i18nSkip = ''; const checkbox = element('input'); checkbox.type = 'checkbox'; checkbox.checked = selected.includes(service.id); label.append(checkbox, document.createTextNode(service.name)); const controls = element('span'); const up = element('button', '', '↑'); up.type = 'button'; up.addEventListener('click', () => row.previousElementSibling && container.insertBefore(row, row.previousElementSibling)); const down = element('button', '', '↓'); down.type = 'button'; down.addEventListener('click', () => row.nextElementSibling && container.insertBefore(row.nextElementSibling, row)); controls.append(up, down); row.append(label, controls); container.append(row); }); byId('sceneDialog').showModal(); }
async function saveScene(event) { event.preventDefault(); const id = byId('sceneId').value; const serviceIds = [...byId('sceneServiceChoices').children].filter((row) => row.querySelector('input').checked).map((row) => row.dataset.id); const payload = { name: byId('sceneName').value.trim(), description: byId('sceneDescription').value.trim(), service_ids: serviceIds }; try { await api(id ? `/scenes/${id}` : '/scenes', { method: id ? 'PUT' : 'POST', body: payload }); byId('sceneDialog').close(); showToast(id ? '场景已更新' : '场景已添加'); await refreshServicesAndScenes(); } catch (error) { text('sceneFormError', error.message); } }
async function deleteScene(scene) { if (!confirmUi(`删除场景“${scene.name}”？不会停止或删除任何服务。`)) return; try { await api(`/scenes/${scene.id}`, { method: 'DELETE' }); showToast('场景已删除'); await refreshServicesAndScenes(); } catch (error) { showToast(error.message); } }

function renderUsers() {
  text('userNavCount', String(state.users.length)); const rows = byId('userRows'); rows.replaceChildren();
  if (!state.users.length) { rows.append(element('p', 'empty-state', '尚无用户。')); return; }
  state.users.forEach((user) => {
    const row = element('div', 'table-row'); const identity = element('span'); const avatar = userElement('i', 'user-avatar', user.username.slice(0, 1).toUpperCase()); const copy = element('span'); copy.append(userElement('b', '', user.username), element('small', '', user.is_current ? '当前登录账户' : '管理账户')); identity.append(avatar, copy);
    const status = element('i', `status-label ${user.is_current ? 'ready' : 'stopped'}`, user.is_current ? '当前用户' : '可用');
    const actions = element('span', 'row-buttons'); const password = element('button', '', '修改密码'); password.addEventListener('click', () => openPasswordDialog(user)); const remove = element('button', '', '删除'); remove.disabled = user.is_current || state.users.length <= 1; remove.title = user.is_current ? '不能删除当前登录用户' : state.users.length <= 1 ? '不能删除最后一个用户' : ''; remove.addEventListener('click', () => deleteUser(user)); actions.append(password, remove);
    row.append(identity, element('span', '', formatDate(user.created_at, true)), element('span', 'mono', String(user.active_sessions)), status, actions); rows.append(row);
  });
}

function openUserDialog() { byId('userForm').reset(); text('userFormError', ''); byId('userDialog').showModal(); }
async function saveUser(event) {
  event.preventDefault(); const password = byId('newUserPassword').value;
  if (password !== byId('newUserPasswordConfirm').value) return text('userFormError', '两次输入的密码不一致。');
  try { await api('/users', { method: 'POST', body: { username: byId('newUsername').value.trim(), password } }); byId('userDialog').close(); showToast('用户已添加'); await refreshUsers(); }
  catch (error) { text('userFormError', error.message); }
}
function openPasswordDialog(user) { byId('passwordForm').reset(); text('passwordFormError', ''); byId('passwordUserId').value = user.id; text('passwordDialogTitle', `修改 ${user.username} 的密码`); text('passwordDialogNote', user.is_current ? '保存后当前会话将失效，需要使用新密码重新登录。' : '保存后，该用户的现有登录会话将全部失效。'); byId('passwordDialog').showModal(); }
async function saveUserPassword(event) {
  event.preventDefault(); const password = byId('changedPassword').value;
  if (password !== byId('changedPasswordConfirm').value) return text('passwordFormError', '两次输入的密码不一致。');
  try { const result = await api(`/users/${byId('passwordUserId').value}/password`, { method: 'PUT', body: { password } }); byId('passwordDialog').close(); if (result.current_session_invalidated) { showAuth('login', '密码已修改，请使用新密码重新登录。'); return; } showToast('密码已修改，旧会话已失效'); await refreshUsers(); }
  catch (error) { text('passwordFormError', error.message); }
}
async function deleteUser(user) { if (!confirmUi(`删除用户“${user.username}”？该用户的登录会话将立即失效。`)) return; try { await api(`/users/${user.id}`, { method: 'DELETE' }); showToast('用户已删除'); await refreshUsers(); } catch (error) { showToast(error.message); } }

function targetName(kind, id) { if (kind === 'service_group' && id === 'all') return '全部服务'; const collection = kind === 'scene' ? state.scenes : state.services; return collection.find((item) => item.id === id)?.name || id; }
function operationStatusLabel(status) { return { succeeded: '成功', failed: '失败', interrupted: '已终止', queued: '等待执行', running: '执行中' }[status] || '状态未知'; }
function operationActionLabel(action) { return { start: '启动', stop: '停止', restart: '重启', activate: '切换场景', stop_all: '停止全部服务' }[action] || action; }
function operationPhaseLabel(phase) { return { stop_unselected: '停止未选服务', start_selected: '启动目标服务' }[phase] || phase; }
function operationResultLabel(result) { return { success: '成功', partial: '部分启动', stop_failed: '停止失败', cancelled: '已终止' }[result] || result; }
function renderOperationTimeline() { const timeline = byId('auditTimeline'); timeline.replaceChildren(); const recent = state.operations.filter((item) => Date.now() - new Date(item.created_at).getTime() <= 30 * 60 * 1000).slice(0, 5); if (!recent.length) { timeline.append(element('li', 'empty-state', '最近 30 分钟没有服务或场景操作。')); return; } recent.forEach((operation) => { const item = element('li'); const success = operation.status === 'succeeded'; item.append(element('span', `event-dot ${success ? 'good' : 'warn'}`)); const copy = element('div'); copy.append(element('strong', '', `${ui(operation.kind === 'scene' ? '场景切换' : '服务操作')} · ${targetName(operation.kind, operation.target_id)}`), element('small', '', `${formatDate(operation.created_at, true)} · ${ui(operationStatusLabel(operation.status))}`)); item.append(copy); timeline.append(item); }); }
function renderOperations() { const list = byId('operationList'); list.replaceChildren(); if (!state.operations.length) { list.append(element('p', 'empty-state', '暂无服务或场景操作。')); return; } state.operations.forEach((item) => { const failed = ['failed', 'interrupted'].includes(item.status); const row = element('article', `operation-row${failed ? ' operation-failed' : ''}`); const copy = element('div'); copy.append(element('strong', '', `${ui(item.kind === 'scene' ? '场景' : '服务')} · ${targetName(item.kind, item.target_id)} · ${ui(operationActionLabel(item.action))}`), element('small', '', item.error_summary || ui(operationResultLabel(item.result)) || ui('等待执行'))); row.append(copy, element('span', `status-label ${item.status === 'succeeded' ? 'ready' : failed ? 'danger' : 'partial'}`, ui(operationStatusLabel(item.status))), element('time', '', formatDate(item.created_at, true))); const steps = element('ol', 'operation-steps'); (item.steps || []).forEach((step) => { const entry = element('li', step.status === 'failed' ? 'failed' : ''); entry.append(element('b', '', `${step.sequence}. ${ui(operationPhaseLabel(step.phase))} · ${targetName('service', step.target_id)} · ${ui(operationActionLabel(step.action))}`), element('span', 'step-status', ui(operationStatusLabel(step.status))), element('small', '', `${formatDate(step.started_at, true)} → ${step.finished_at ? formatDate(step.finished_at, true) : ui('进行中')}`)); if (step.error_summary) entry.append(element('code', '', step.error_summary)); steps.append(entry); }); if (steps.childNodes.length) row.append(steps); list.append(row); }); }

byId('authForm').addEventListener('submit', submitAuth); byId('logoutButton').addEventListener('click', logout); byId('refreshButton').addEventListener('click', refreshAll); byId('refreshLogsButton').addEventListener('click', refreshLogs); byId('overviewSceneSelect').addEventListener('change', handleOverviewSceneChange); byId('stopAllServicesButton').addEventListener('click', stopAllServices); byId('addServiceButton').addEventListener('click', () => openServiceDialog()); byId('addSceneButton').addEventListener('click', () => openSceneDialog()); byId('addUserButton').addEventListener('click', openUserDialog); byId('cancelSceneSwitchButton').addEventListener('click', cancelSceneSwitch); byId('closeSceneProgressButton').addEventListener('click', () => byId('sceneProgressDialog').close()); byId('sceneProgressDialog').addEventListener('cancel', (event) => { if (sceneProgressOperationId) event.preventDefault(); }); byId('serviceForm').addEventListener('submit', saveService); byId('sceneForm').addEventListener('submit', saveScene); byId('userForm').addEventListener('submit', saveUser); byId('passwordForm').addEventListener('submit', saveUserPassword); byId('serviceSearch').addEventListener('input', renderRegisteredServiceTable); byId('serviceFilters').addEventListener('click', (event) => { const button = event.target.closest('[data-filter]'); if (!button) return; state.serviceFilter = button.dataset.filter; byId('serviceFilters').querySelectorAll('.filter').forEach((item) => item.classList.toggle('active', item === button)); renderRegisteredServiceTable(); }); document.querySelectorAll('[data-close]').forEach((button) => button.addEventListener('click', () => byId(button.dataset.close).close())); document.addEventListener('visibilitychange', () => { if (!document.hidden && !document.body.classList.contains('auth-pending')) refreshAll(); });
document.addEventListener('languagechange', () => { buildMonitorCharts(); if (state.snapshot) renderSnapshot(); renderServices(); renderScenes(); renderUsers(); renderOperations(); renderOperationTimeline(); text('pageTitle', byId(`page-${state.activePage}`)?.dataset.title || ''); });

bootstrap();
