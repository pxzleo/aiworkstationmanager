'use strict';

const API_PREFIX = '/api/v1';
const SNAPSHOT_INTERVAL_MS = 3000;
const HISTORY_INTERVAL_MS = 12000;
const SERVICE_INTERVAL_MS = 5000;
const REQUEST_TIMEOUT_MS = 8000;
const ACTION_TIMEOUT_MS = 30000;
const SVG_NS = 'http://www.w3.org/2000/svg';
const requestGuard = new RequestGuard();
const actionGuard = new ExclusiveActionGuard();
const state = {
  activePage: 'overview', authMode: 'login', csrfToken: null, username: '', snapshot: null,
  history: [], services: [], scenes: [], operations: [], timers: new Map(),
  chartSpecs: [], gpuBinding: { slots: [null, null], extras: [] }, serviceFilter: 'all',
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
  const ticket = requestGuard.begin(options.resource);
  const method = (options.method || 'GET').toUpperCase();
  const controller = new AbortController();
  const abortLifecycle = () => controller.abort('lifecycle');
  if (ticket.signal.aborted) abortLifecycle(); else ticket.signal.addEventListener('abort', abortLifecycle, { once: true });
  const timeout = setTimeout(() => controller.abort('timeout'), options.timeout || REQUEST_TIMEOUT_MS);
  const headers = new Headers(options.headers || {});
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
  const setup = mode === 'setup'; text('authEyebrow', setup ? '首次设置' : '安全访问'); text('authTitle', setup ? '创建本机管理员' : '登录工作站'); text('authDescription', setup ? '首次设置仅允许在本机完成。密码至少 12 个字符。' : '使用管理员账户继续。'); text('authSubmit', setup ? '创建管理员并进入' : '登录'); text('authError', message);
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

async function refreshAll() { await Promise.allSettled([refreshSnapshot(), refreshHistory(), refreshServicesAndScenes(), refreshLogs()]); }
async function refreshSnapshot() { if (document.hidden) return; try { state.snapshot = normalizeSnapshot(await api('/snapshot', { resource: 'snapshot' })); renderSnapshot(); } catch (error) { if (!(error instanceof StaleRequestError)) { text('freshnessLabel', '监控离线'); } } }
async function refreshHistory() { if (document.hidden) return; try { const result = await api('/history?window=15m', { resource: 'history' }); state.history = (result.samples || []).map(normalizeHistorySample); renderCharts(); } catch (_) {} }
async function refreshServicesAndScenes() {
  if (document.hidden) return;
  try { const [serviceData, sceneData] = await Promise.all([api('/registered-services', { resource: 'services' }), api('/scenes', { resource: 'scenes' })]); state.services = serviceData.services || []; state.scenes = sceneData.scenes || []; renderServices(); renderScenes(); }
  catch (error) { if (!(error instanceof StaleRequestError)) showToast(`服务状态读取失败：${error.message}`); }
}
async function refreshLogs() { if (document.hidden) return; try { const result = await api('/operations?limit=50', { resource: 'operations' }); state.operations = result.operations || []; renderOperations(); renderOperationTimeline(); } catch (_) {} }

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
  state.gpuBinding = bindGpuSlots(snapshot.gpus || []); renderGpuSlot(0, state.gpuBinding.slots[0]); renderGpuSlot(1, state.gpuBinding.slots[1]); renderCollectorErrors(snapshot.collector_errors || []); renderCharts();
  text('freshnessLabel', '实时'); text('clock', formatDate(snapshot.sampled_at));
}
function bindGpuSlots(gpus) { const slots = [null, null]; const selected = new Set(); [0, 1].forEach((slot) => { const index = gpus.findIndex((gpu) => Number(gpu.index) === slot); if (index >= 0) { slots[slot] = gpus[index]; selected.add(index); } }); return { slots, extras: gpus.filter((_, index) => !selected.has(index)) }; }
function renderGpuSlot(slot, gpu) {
  const prefix = `gpu${slot}`; const lane = document.querySelectorAll('.gpu-lane')[slot]; if (!gpu) { lane.classList.add('gpu-missing'); text(`${prefix}Name`, '未检测到'); text(`${prefix}Util`, '--'); text(`${prefix}MemoryValue`, '--'); return; }
  lane.classList.remove('gpu-missing'); const used = mibToGib(gpu.memory_used_mib); const total = mibToGib(gpu.memory_total_mib); const free = used !== null && total !== null ? Math.max(0, total - used) : null;
  text(`${prefix}Name`, gpu.name || `GPU ${gpu.index}`); text(`${prefix}Role`, compactUuid(gpu.uuid)); text(`${prefix}Util`, finite(gpu.load_percent) ? String(Math.round(gpu.load_percent)) : '--'); text(`${prefix}MemoryValue`, used === null ? '--' : used.toFixed(1)); const totalNode = byId(`${prefix}MemoryValue`)?.parentElement?.querySelector('small'); if (totalNode) totalNode.textContent = total === null ? '/ 不支持' : `/ ${total.toFixed(1)} GB`;
  text(`${prefix}MemoryPercent`, percent(gpu.memory_percent)); text(`${prefix}MemoryFree`, free === null ? '不支持' : `剩余 ${free.toFixed(1)} GB`); text(`${prefix}Temp`, finite(gpu.temperature_c) ? `${Math.round(gpu.temperature_c)}°C` : '不支持'); text(`${prefix}Power`, finite(gpu.power_w) ? `${Math.round(gpu.power_w)} W` : '不支持'); text(`${prefix}Uuid`, compactUuid(gpu.uuid)); text(slot === 0 ? 'gpu0State' : 'gpu1OperationalState', '已检测'); lane.querySelector('.memory-visual').style.setProperty('--used', `${gpu.memory_percent || 0}%`);
  const values = currentSeries().map((sample) => (sample.gpus || []).find((entry) => entry.uuid === gpu.uuid || Number(entry.index) === Number(gpu.index))?.load_percent).filter(finite); renderPolyline(byId(`${prefix}Sparkline`), values, gpu.name || `GPU ${slot}`);
}
function renderPolyline(polyline, values, device) { if (!polyline) return; const normalized = values.map(normalizedPercent).filter((value) => value !== null); if (!normalized.length) { polyline.setAttribute('points', ''); return; } polyline.setAttribute('points', normalized.map((value, index) => `${normalized.length === 1 ? 150 : index * (300 / (normalized.length - 1))},${110 - value}`).join(' ')); polyline.closest('svg').setAttribute('aria-label', `${device} GPU 负载曲线`); }
function renderCollectorErrors(errors) { const banner = byId('collectorStatus'); banner.hidden = !errors.length; banner.replaceChildren(); if (errors.length) banner.append(element('strong', '', '部分数据降级'), element('span', '', errors.map((error) => error.message || '采集失败').join('；'))); }
function summarize(values) { if (!values.length) return { current: '--', peak: '--' }; return { current: Math.round(values.at(-1)), peak: Math.round(Math.max(...values)) }; }
function buildMonitorCharts() {
  const grid = document.querySelector('.monitor-grid'); grid.replaceChildren(); const specs = [
    ['HOST', 'CPU 总负载', (sample) => sample.cpu_load_percent], ['HOST', '内存占用', (sample) => sample.memory_percent],
    ['GPU 0', 'GPU 负载', (sample) => metricForGpu(sample, 0, 'load_percent')], ['GPU 0', '显存占用', (sample) => metricForGpu(sample, 0, 'memory_percent')],
    ['GPU 1', 'GPU 负载', (sample) => metricForGpu(sample, 1, 'load_percent')], ['GPU 1', '显存占用', (sample) => metricForGpu(sample, 1, 'memory_percent')],
  ];
  state.chartSpecs = specs.map(([kicker, title, getter]) => { const section = element('section', 'chart-section'); const heading = element('div', 'chart-title'); const copy = element('div'); copy.append(element('span', 'chart-kicker', kicker), element('h2', '', title)); const current = element('strong', '', '--'); current.append(element('small', '', '% 当前')); heading.append(copy, current); const frame = element('div', 'chart-frame'); const svg = svgElement('svg', { class: 'line-chart', viewBox: '0 0 900 230', preserveAspectRatio: 'none' }); const line = svgElement('polyline', { class: 'line primary-line', fill: 'none', points: '' }); svg.append(line); frame.append(svg); section.append(heading, frame); grid.append(section); return { title, getter, current, line }; });
}
function metricForGpu(sample, slot, field) { const current = state.gpuBinding.slots[slot]; if (!current) return null; return (sample.gpus || []).find((gpu) => gpu.uuid === current.uuid || Number(gpu.index) === Number(current.index))?.[field]; }
function renderCharts() { const samples = currentSeries(); state.chartSpecs.forEach((spec) => { const values = samples.map(spec.getter).filter(finite); spec.current.firstChild.nodeValue = values.length ? String(summarize(values).current) : '--'; spec.line.setAttribute('points', values.map((value, index) => `${values.length === 1 ? 450 : index * (900 / (values.length - 1))},${215 - Math.min(100, Math.max(0, value)) * 2}`).join(' ')); }); }

const statusLabels = { running: '运行中', stopped: '已停止', unhealthy: '异常', unknown: '未知' };
function statusClass(value) { return value === 'running' ? 'ready' : value === 'stopped' ? 'stopped' : value === 'unhealthy' ? 'danger' : 'partial'; }
function serviceMatchesGpu(service, slot) { const label = (service.gpu_label || '').toLowerCase(); const gpu = state.gpuBinding.slots[slot]; return label.includes(`gpu ${slot}`) || label.includes(slot === 0 ? '4090' : '3090') || (gpu?.name && label.includes(gpu.name.toLowerCase())); }
function renderServices() {
  renderOverviewServices(); renderRegisteredServiceTable(); renderGpuServiceLabels(); renderOperationTimeline();
}
function renderOverviewServices() {
  const list = byId('serviceList'); list.replaceChildren(); if (!state.services.length) { list.append(element('p', 'empty-state', '尚未添加已登记服务。')); return; }
  state.services.slice(0, 8).forEach((service) => { const row = element('div', 'service-row'); row.append(element('span', `service-state ${statusClass(service.status.state)}`)); const copy = element('div'); copy.append(element('strong', '', service.name), element('small', '', service.description || service.gpu_label || '无说明')); const action = element('button', 'row-action', service.ui_url ? '打开 UI' : '查看'); action.addEventListener('click', () => service.ui_url ? window.open(service.ui_url, '_blank', 'noopener,noreferrer') : navigate('environments')); row.append(copy, element('span', 'port', service.port ? `:${service.port}` : '无端口'), element('span', 'uptime', statusLabels[service.status.state] || '未知'), action); list.append(row); });
}
function renderGpuServiceLabels() {
  const gpu0 = state.services.filter((item) => serviceMatchesGpu(item, 0)); const gpu1 = state.services.filter((item) => serviceMatchesGpu(item, 1));
  text('gpu0WorkloadName', gpu0.map((item) => item.name).join(' · ') || '尚未登记 GPU 0 服务'); text('gpu0WorkloadMeta', gpu0.map((item) => `${item.name} ${statusLabels[item.status.state]}`).join('；') || 'GPU 为用户登记标签');
  text('gpu1ServiceName', gpu1[0]?.name || '尚未登记服务'); text('gpu1ServiceState', gpu1[0] ? statusLabels[gpu1[0].status.state] : '未配置'); text('gpu1AsrState', gpu1[1] ? `${gpu1[1].name} · ${statusLabels[gpu1[1].status.state]}` : '等待登记'); text('gpu1TtsState', gpu1[2] ? `${gpu1[2].name} · ${statusLabels[gpu1[2].status.state]}` : '等待登记');
}
function renderRegisteredServiceTable() {
  const rows = byId('registeredServiceRows'); rows.replaceChildren(); const query = byId('serviceSearch').value.trim().toLowerCase(); const filtered = state.services.filter((item) => (state.serviceFilter === 'all' || item.status.state === state.serviceFilter) && [item.name, item.description, item.gpu_label, item.port].join(' ').toLowerCase().includes(query));
  if (!filtered.length) { rows.append(element('p', 'empty-state', state.services.length ? '没有符合筛选条件的服务。' : '尚未添加服务。')); return; }
  filtered.forEach((service) => { const row = element('div', 'table-row'); const title = element('span'); const logo = element('i', 'env-logo', service.name.slice(0, 1).toUpperCase()); const copy = element('span'); copy.append(element('b', '', service.name), element('small', '', service.script_path)); title.append(logo, copy); const status = element('i', `status-label ${statusClass(service.status.state)}`, service.busy ? '操作中' : statusLabels[service.status.state] || '未知'); const actions = element('span', 'row-buttons'); ['start', 'stop', 'restart'].forEach((action) => { const button = element('button', '', { start: '启动', stop: '停止', restart: '重启' }[action]); button.disabled = service.busy || actionGuard.pending; button.addEventListener('click', () => runServiceAction(service, action)); actions.append(button); }); if (service.ui_url) { const ui = element('button', '', 'UI'); ui.addEventListener('click', () => window.open(service.ui_url, '_blank', 'noopener,noreferrer')); actions.append(ui); } const edit = element('button', 'icon-only', '编辑'); edit.disabled = service.busy || actionGuard.pending; edit.addEventListener('click', () => openServiceDialog(service)); const remove = element('button', 'icon-only', '删除'); remove.disabled = service.busy || actionGuard.pending; remove.addEventListener('click', () => deleteService(service)); actions.append(edit, remove); row.append(title, element('span', '', service.description || '—'), element('span', '', service.gpu_label || '未标注'), element('span', 'mono', service.port ? String(service.port) : '—'), status, actions); rows.append(row); });
}

function renderScenes() {
  text('sceneNavCount', String(state.scenes.length)); const list = byId('sceneList'); list.replaceChildren(); if (!state.scenes.length) { list.append(element('p', 'empty-state', '尚未添加场景。')); text('activeSceneName', '尚未添加场景'); byId('switchSceneButton').disabled = true; return; }
  state.scenes.forEach((scene, index) => { const panel = element('article', `scene-panel${scene.state === 'active' ? ' selected' : ''}`); const top = element('div', 'scene-panel-top'); top.append(element('span', '', `场景 ${String(index + 1).padStart(2, '0')}`), element('i', scene.state === 'active' ? '已激活' : '部分完成')); panel.append(top, element('h2', '', scene.name), element('p', '', scene.description || '无说明')); const map = element('div', 'scene-map'); if (!scene.service_names.length) map.append(element('div', '', '此场景不启动任何服务')); scene.service_names.forEach((name, order) => { const item = element('div'); item.append(element('span', '', `启动顺序 ${order + 1}`), element('strong', '', name)); map.append(item); }); const actions = element('div', 'scene-card-actions'); const activate = element('button', 'button primary', scene.state === 'active' ? '重新切换' : '切换到此场景'); activate.disabled = scene.busy || actionGuard.pending; activate.addEventListener('click', () => activateScene(scene)); const edit = element('button', 'button secondary', '编辑'); edit.disabled = scene.busy || actionGuard.pending; edit.addEventListener('click', () => openSceneDialog(scene)); const remove = element('button', 'button secondary', '删除'); remove.disabled = scene.busy || actionGuard.pending; remove.addEventListener('click', () => deleteScene(scene)); actions.append(activate, edit, remove); panel.append(map, actions); list.append(panel); });
  const active = state.scenes.find((item) => item.state === 'active'); text('activeSceneName', active ? `${active.name} · 已激活` : '场景未完整激活'); text('activeSceneSummary', active ? active.description || `包含 ${active.service_ids.length} 个服务` : '当前服务状态不完整符合任何场景。'); const switchButton = byId('switchSceneButton'); switchButton.disabled = !state.scenes.length; switchButton.onclick = () => navigate('scenes');
  renderOperationTimeline();
}

async function runServiceAction(service, action) { const owner = actionGuard.acquire(); if (!owner) return showToast('已有操作正在执行'); try { const result = await api(`/registered-services/${service.id}/actions`, { method: 'POST', body: { action } }); showToast('服务操作已开始'); await pollOperation(result.operation_id); } catch (error) { showToast(error.message); } finally { actionGuard.release(owner); await refreshServicesAndScenes(); } }
async function activateScene(scene) { const owner = actionGuard.acquire(); if (!owner) return showToast('已有操作正在执行'); try { const result = await api(`/scenes/${scene.id}/activate`, { method: 'POST' }); showToast('场景切换已开始'); await pollOperation(result.operation_id); } catch (error) { showToast(error.message); } finally { actionGuard.release(owner); await refreshServicesAndScenes(); } }
async function pollOperation(id) { for (let i = 0; i < 240; i += 1) { await new Promise((resolve) => setTimeout(resolve, 1000)); const item = await api(`/operations/${id}`, { timeout: ACTION_TIMEOUT_MS }); if (!['queued', 'running'].includes(item.status)) { showToast(item.status === 'succeeded' ? '操作成功' : item.error_summary || '操作失败'); await refreshLogs(); return item; } } throw new ApiError(0, 'operation_timeout', '操作仍在后台执行，请到日志中心查看。'); }

function openServiceDialog(service = null) { byId('serviceForm').reset(); text('serviceFormError', ''); text('serviceDialogTitle', service ? '编辑服务' : '添加服务'); byId('serviceId').value = service?.id || ''; byId('serviceName').value = service?.name || ''; byId('serviceDescription').value = service?.description || ''; byId('serviceScriptPath').value = service?.script_path || ''; byId('serviceGpu').value = service?.gpu_label || ''; byId('servicePort').value = service?.port || ''; byId('serviceUiUrl').value = service?.ui_url || ''; byId('serviceDialog').showModal(); }
async function saveService(event) { event.preventDefault(); const id = byId('serviceId').value; const payload = { name: byId('serviceName').value.trim(), description: byId('serviceDescription').value.trim(), script_path: byId('serviceScriptPath').value.trim(), gpu_label: byId('serviceGpu').value.trim(), port: byId('servicePort').value ? Number(byId('servicePort').value) : null, ui_url: byId('serviceUiUrl').value.trim() }; try { await api(id ? `/registered-services/${id}` : '/registered-services', { method: id ? 'PUT' : 'POST', body: payload }); byId('serviceDialog').close(); showToast(id ? '服务已更新' : '服务已添加'); await refreshServicesAndScenes(); } catch (error) { text('serviceFormError', error.message); } }
async function deleteService(service) { if (!confirm(`删除服务“${service.name}”的登记记录？原始脚本和服务不会被删除。`)) return; try { await api(`/registered-services/${service.id}`, { method: 'DELETE' }); showToast('服务登记已删除'); await refreshServicesAndScenes(); } catch (error) { showToast(error.message); } }

function openSceneDialog(scene = null) { byId('sceneForm').reset(); text('sceneFormError', ''); text('sceneDialogTitle', scene ? '编辑场景' : '添加场景'); byId('sceneId').value = scene?.id || ''; byId('sceneName').value = scene?.name || ''; byId('sceneDescription').value = scene?.description || ''; const selected = scene?.service_ids || []; const container = byId('sceneServiceChoices'); container.replaceChildren(); const ordered = [...selected.map((id) => state.services.find((item) => item.id === id)).filter(Boolean), ...state.services.filter((item) => !selected.includes(item.id))]; ordered.forEach((service) => { const row = element('div', 'scene-service-choice'); row.dataset.id = service.id; const label = element('label'); const checkbox = element('input'); checkbox.type = 'checkbox'; checkbox.checked = selected.includes(service.id); label.append(checkbox, document.createTextNode(service.name)); const controls = element('span'); const up = element('button', '', '↑'); up.type = 'button'; up.addEventListener('click', () => row.previousElementSibling && container.insertBefore(row, row.previousElementSibling)); const down = element('button', '', '↓'); down.type = 'button'; down.addEventListener('click', () => row.nextElementSibling && container.insertBefore(row.nextElementSibling, row)); controls.append(up, down); row.append(label, controls); container.append(row); }); byId('sceneDialog').showModal(); }
async function saveScene(event) { event.preventDefault(); const id = byId('sceneId').value; const serviceIds = [...byId('sceneServiceChoices').children].filter((row) => row.querySelector('input').checked).map((row) => row.dataset.id); const payload = { name: byId('sceneName').value.trim(), description: byId('sceneDescription').value.trim(), service_ids: serviceIds }; try { await api(id ? `/scenes/${id}` : '/scenes', { method: id ? 'PUT' : 'POST', body: payload }); byId('sceneDialog').close(); showToast(id ? '场景已更新' : '场景已添加'); await refreshServicesAndScenes(); } catch (error) { text('sceneFormError', error.message); } }
async function deleteScene(scene) { if (!confirm(`删除场景“${scene.name}”？不会停止或删除任何服务。`)) return; try { await api(`/scenes/${scene.id}`, { method: 'DELETE' }); showToast('场景已删除'); await refreshServicesAndScenes(); } catch (error) { showToast(error.message); } }

function targetName(kind, id) { const collection = kind === 'scene' ? state.scenes : state.services; return collection.find((item) => item.id === id)?.name || id; }
function renderOperationTimeline() { const timeline = byId('auditTimeline'); timeline.replaceChildren(); const recent = state.operations.filter((item) => Date.now() - new Date(item.created_at).getTime() <= 30 * 60 * 1000).slice(0, 5); if (!recent.length) { timeline.append(element('li', 'empty-state', '最近 30 分钟没有服务或场景操作。')); return; } recent.forEach((operation) => { const item = element('li'); const success = operation.status === 'succeeded'; item.append(element('span', `event-dot ${success ? 'good' : 'warn'}`)); const copy = element('div'); copy.append(element('strong', '', `${operation.kind === 'scene' ? '场景切换' : '服务操作'} · ${targetName(operation.kind, operation.target_id)}`), element('small', '', `${formatDate(operation.created_at, true)} · ${success ? '成功' : operation.status === 'failed' ? '失败' : '执行中'}`)); item.append(copy); timeline.append(item); }); }
function renderOperations() { const list = byId('operationList'); list.replaceChildren(); if (!state.operations.length) { list.append(element('p', 'empty-state', '暂无服务或场景操作。')); return; } state.operations.forEach((item) => { const failed = ['failed', 'interrupted'].includes(item.status); const row = element('article', `operation-row${failed ? ' operation-failed' : ''}`); const copy = element('div'); copy.append(element('strong', '', `${item.kind === 'scene' ? '场景' : '服务'} · ${targetName(item.kind, item.target_id)} · ${item.action}`), element('small', '', item.error_summary || item.result || '等待执行')); row.append(copy, element('span', `status-label ${item.status === 'succeeded' ? 'ready' : failed ? 'danger' : 'partial'}`, item.status), element('time', '', formatDate(item.created_at, true))); const steps = element('ol', 'operation-steps'); (item.steps || []).forEach((step) => { const entry = element('li', step.status === 'failed' ? 'failed' : ''); entry.append(element('b', '', `${step.sequence}. ${step.phase} · ${targetName('service', step.target_id)} · ${step.action}`), element('span', 'step-status', step.status), element('small', '', `${formatDate(step.started_at, true)} → ${step.finished_at ? formatDate(step.finished_at, true) : '进行中'}`)); if (step.error_summary) entry.append(element('code', '', step.error_summary)); steps.append(entry); }); if (steps.childNodes.length) row.append(steps); list.append(row); }); }

byId('authForm').addEventListener('submit', submitAuth); byId('logoutButton').addEventListener('click', logout); byId('refreshButton').addEventListener('click', refreshAll); byId('refreshLogsButton').addEventListener('click', refreshLogs); byId('addServiceButton').addEventListener('click', () => openServiceDialog()); byId('addSceneButton').addEventListener('click', () => openSceneDialog()); byId('serviceForm').addEventListener('submit', saveService); byId('sceneForm').addEventListener('submit', saveScene); byId('serviceSearch').addEventListener('input', renderRegisteredServiceTable); byId('serviceFilters').addEventListener('click', (event) => { const button = event.target.closest('[data-filter]'); if (!button) return; state.serviceFilter = button.dataset.filter; byId('serviceFilters').querySelectorAll('.filter').forEach((item) => item.classList.toggle('active', item === button)); renderRegisteredServiceTable(); }); document.querySelectorAll('[data-close]').forEach((button) => button.addEventListener('click', () => byId(button.dataset.close).close())); document.addEventListener('visibilitychange', () => { if (!document.hidden && !document.body.classList.contains('auth-pending')) refreshAll(); });

bootstrap();
