'use strict';

const API_PREFIX = '/api/v1';
const SNAPSHOT_INTERVAL_MS = 3000;
const HISTORY_INTERVAL_MS = 12000;
const SERVICE_INTERVAL_MS = 5000;
const REQUEST_TIMEOUT_MS = 8000;
const ACTION_TIMEOUT_MS = 30000;
const READ_RETRY_DELAYS_MS = [400, 1200];
const NETWORK_NOTICE_COOLDOWN_MS = 15000;
const PAGE_STORAGE_KEY = 'axis-active-page';
const SVG_NS = 'http://www.w3.org/2000/svg';
const MONITOR_GPU_COLORS = ['#a78bfa', '#fb923c', '#22c55e', '#f472b6', '#38bdf8', '#eab308'];
const gpuLayout = window.AxisGpuLayout;
if (!gpuLayout) throw new Error('GPU layout helper is unavailable.');
const monitorChart = window.AxisMonitorChart;
if (!monitorChart) throw new Error('Monitor chart helper is unavailable.');
const requestGuard = new RequestGuard();
const actionGuard = new ExclusiveActionGuard();
let sceneProgressOperationId = null;
let sceneProgressExpectedTotal = null;
let progressCancelLabel = '终止切换并返回';
let draggedSceneId = null;
let fileThumbnailObserver = null;
let automaticTaskOrderSaving = false;
const state = {
  activePage: 'overview', authMode: 'login', csrfToken: null, username: '', snapshot: null,
  history: [], services: [], scenes: [], users: [], operations: [], videoJobs: [], videoQueueSummary: { queued_segments: 0 }, automaticTasks: [], automaticTaskSummary: { pending: 0, running: 0, total: 0 }, timers: new Map(),
  fileService: null, files: [], filePath: '', fileSort: 'modified-desc', fileView: 'thumbnail',
  historyWindowMinutes: 15, historyLoading: false,
  chartSpecs: [], correlationControllers: [], monitorDetails: null, monitorView: 'summary', selectedMonitorGpuKey: null, selectedMonitorDisk: null, gpus: [], gpuCardSignature: null, monitorGpuSignature: null, serviceFilter: 'all',
};

class ApiError extends Error {
  constructor(status, code, message) { super(message); this.name = 'ApiError'; this.status = status; this.code = code; }
}
class StaleRequestError extends Error { constructor() { super('stale request'); this.name = 'StaleRequestError'; } }
let lastPollingNetworkNoticeAt = -Infinity;
function showPollingError(prefix, error, now = Date.now()) {
  if (error instanceof StaleRequestError) return;
  if (['network_error', 'timeout'].includes(error.code)) {
    if (now - lastPollingNetworkNoticeAt < NETWORK_NOTICE_COOLDOWN_MS) return;
    lastPollingNetworkNoticeAt = now;
    showToast(window.axisI18n.language === 'zh' ? '网络连接不稳定，页面将自动重试。' : 'The network is unstable. The page will retry automatically.');
    return;
  }
  showToast(`${prefix}：${error.message}`);
}
async function waitForReadRetry(milliseconds, ticket) {
  await new Promise((resolve) => setTimeout(resolve, milliseconds));
  if (!requestGuard.isCurrent(ticket)) throw new StaleRequestError();
}
function byId(id) { return document.getElementById(id); }
function text(id, value) { const node = byId(id); if (node) node.textContent = value; }
function element(tag, className, value) { const node = document.createElement(tag); if (className) node.className = className; if (value !== undefined) node.textContent = value; return node; }
function icon(name) { const svg = document.createElementNS(SVG_NS, 'svg'); svg.setAttribute('aria-hidden', 'true'); const use = document.createElementNS(SVG_NS, 'use'); use.setAttribute('href', `#i-${name}`); svg.append(use); return svg; }
function iconButton(label, iconName, className = '') { const button = element('button', `icon-button${className ? ` ${className}` : ''}`); button.type = 'button'; button.title = ui(label); button.dataset.tooltip = ui(label); button.setAttribute('aria-label', ui(label)); button.append(icon(iconName)); return button; }
function labeledIconButton(label, iconName, className) { const button = element('button', className); button.type = 'button'; button.append(icon(iconName), element('span', '', label)); return button; }
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
function formatDuration(milliseconds) { if (!Number.isFinite(milliseconds) || milliseconds < 0) return '--'; const seconds = Math.floor(milliseconds / 1000); const days = Math.floor(seconds / 86400); const hours = Math.floor(seconds % 86400 / 3600); const minutes = Math.floor(seconds % 3600 / 60); const remainder = seconds % 60; const zh = window.axisI18n.language === 'zh'; const parts = []; if (days) parts.push(`${days}${zh ? '天' : 'd'}`); if (hours || days) parts.push(`${hours}${zh ? '小时' : 'h'}`); if (minutes || hours || days) parts.push(`${minutes}${zh ? '分' : 'm'}`); parts.push(`${remainder}${zh ? '秒' : 's'}`); return parts.join(' '); }

async function api(path, options = {}) {
  const ticket = requestGuard.begin(options.resource);
  const method = (options.method || 'GET').toUpperCase();
  const headers = new Headers(options.headers || {});
  headers.set('Accept-Language', window.axisI18n.language);
  if (options.body !== undefined) headers.set('Content-Type', 'application/json');
  if (options.rawBody !== undefined && !headers.has('Content-Type')) headers.set('Content-Type', 'application/octet-stream');
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && state.csrfToken && !options.skipCsrf) headers.set('X-CSRF-Token', state.csrfToken);
  const body = options.rawBody !== undefined ? options.rawBody : options.body === undefined ? undefined : JSON.stringify(options.body);
  const retryDelays = ['GET', 'HEAD'].includes(method) ? READ_RETRY_DELAYS_MS : [];
  for (let attempt = 0; attempt <= retryDelays.length; attempt += 1) {
    if (attempt) await waitForReadRetry(retryDelays[attempt - 1], ticket);
    const controller = new AbortController(); let timedOut = false;
    const abortLifecycle = () => controller.abort('lifecycle');
    if (ticket.signal.aborted) abortLifecycle(); else ticket.signal.addEventListener('abort', abortLifecycle, { once: true });
    const timeout = options.timeout === null ? null : setTimeout(() => { timedOut = true; controller.abort('timeout'); }, options.timeout || REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(`${API_PREFIX}${path}`, { method, headers, credentials: 'same-origin', cache: 'no-store', signal: controller.signal, body });
      let payload = {};
      if (method !== 'HEAD' && response.status !== 204) {
        try { payload = await response.json(); }
        catch (error) {
          if (error instanceof SyntaxError) throw new ApiError(response.status, 'invalid_response', '服务器返回了无法识别的数据。');
          throw error;
        }
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
      const requestError = timedOut || error?.name === 'AbortError'
        ? new ApiError(0, 'timeout', '连接管理器超时。')
        : error instanceof ApiError ? error : new ApiError(0, 'network_error', '无法连接管理器。');
      if (attempt < retryDelays.length && ['network_error', 'timeout'].includes(requestError.code)) continue;
      throw requestError;
    } finally {
      if (timeout !== null) clearTimeout(timeout);
      ticket.signal.removeEventListener('abort', abortLifecycle);
    }
  }
  throw new ApiError(0, 'network_error', '无法连接管理器。');
}

const pages = [...document.querySelectorAll('.page')];
const navItems = [...document.querySelectorAll('.nav-item[data-page]')];
function rememberedPage() {
  try { const page = sessionStorage.getItem(PAGE_STORAGE_KEY); return byId(`page-${page}`) ? page : 'overview'; }
  catch (error) { console.warn('Unable to restore the active page.', error); return 'overview'; }
}
function navigate(page) {
  const next = byId(`page-${page}`); if (!next) return;
  try { sessionStorage.setItem(PAGE_STORAGE_KEY, page); }
  catch (error) { console.warn('Unable to remember the active page.', error); }
  state.activePage = page; pages.forEach((item) => item.classList.toggle('active', item === next)); navItems.forEach((item) => item.classList.toggle('active', item.dataset.page === page));
  text('pageTitle', next.dataset.title); closeSidebar(mobileViewport.matches); window.scrollTo({ top: 0, behavior: 'smooth' });
  if (page === 'files') refreshFiles(state.filePath).catch(() => {});
  if (page === 'automatic-tasks') refreshAutomaticTasks().catch(() => {});
}
navItems.forEach((item) => item.addEventListener('click', () => navigate(item.dataset.page)));
document.querySelectorAll('[data-nav]').forEach((item) => item.addEventListener('click', () => navigate(item.dataset.nav)));
const sidebar = byId('sidebar'); const sidebarBackdrop = byId('sidebarBackdrop'); const menuButton = byId('menuButton'); const mainContent = document.querySelector('main.main'); const mobileViewport = matchMedia('(max-width: 720px)');
function closeSidebar(restoreFocus = false) { const open = sidebar.classList.contains('open'); sidebar.classList.remove('open'); sidebarBackdrop.classList.remove('open'); sidebarBackdrop.setAttribute('aria-hidden', 'true'); menuButton.setAttribute('aria-expanded', 'false'); if (mobileViewport.matches) { sidebar.inert = true; mainContent.inert = false; } if (restoreFocus && open) menuButton.focus(); }
function openSidebar() { if (!mobileViewport.matches) return; sidebar.inert = false; sidebar.classList.add('open'); sidebarBackdrop.classList.add('open'); sidebarBackdrop.setAttribute('aria-hidden', 'false'); mainContent.inert = true; menuButton.setAttribute('aria-expanded', 'true'); }
function syncSidebar() { if (mobileViewport.matches) closeSidebar(); else { sidebar.inert = false; mainContent.inert = false; sidebar.classList.remove('open'); sidebarBackdrop.classList.remove('open'); sidebarBackdrop.setAttribute('aria-hidden', 'true'); } }
menuButton.addEventListener('click', () => sidebar.classList.contains('open') ? closeSidebar(true) : openSidebar()); sidebarBackdrop.addEventListener('click', () => closeSidebar(true)); mobileViewport.addEventListener('change', syncSidebar);
document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && mobileViewport.matches && sidebar.classList.contains('open')) closeSidebar(true); });

const toast = byId('toast'); let toastTimer;
function showToast(message) { toast.querySelector('span').textContent = message; toast.classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => toast.classList.remove('show'), 2800); }
function clearTimers() { state.timers.forEach(clearTimeout); state.timers.clear(); requestGuard.reset(); }
function startPolling(name, operation, interval) { const generation = requestGuard.generation; const run = async () => { if (generation !== requestGuard.generation || document.body.classList.contains('auth-pending')) return; try { await operation(); } catch (_) {} if (generation !== requestGuard.generation) return; state.timers.set(name, setTimeout(run, interval)); }; state.timers.set(name, setTimeout(run, interval)); }

function showAuth(mode, message = '') {
  clearTimers(); state.authMode = mode; state.csrfToken = null; document.body.classList.remove('app-initializing'); document.body.classList.add('auth-pending');
  const setup = mode === 'setup'; text('authEyebrow', setup ? '首次设置' : '安全访问'); text('authTitle', setup ? '创建本机管理员' : '登录工作站'); text('authDescription', setup ? '首次设置仅允许在本机完成。密码至少 4 个字符。' : '使用管理员账户继续。'); text('authSubmit', setup ? '创建管理员并进入' : '登录'); text('authError', message);
  byId('authForm').hidden = false; byId('confirmPasswordLabel').hidden = !setup; byId('confirmPasswordInput').hidden = !setup; byId('confirmPasswordInput').required = setup; byId('rememberLoginLabel').hidden = setup; byId('rememberLoginInput').disabled = setup;
}
async function submitAuth(event) {
  event.preventDefault(); const form = event.currentTarget; if (!form.reportValidity()) return;
  const username = byId('usernameInput').value.trim(); const password = byId('passwordInput').value; const setup = state.authMode === 'setup';
  if (setup && password !== byId('confirmPasswordInput').value) { text('authError', '两次输入的密码不一致。'); return; }
  const body = { username, password }; if (!setup) body.remember = byId('rememberLoginInput').checked;
  try { const result = await api(setup ? '/auth/setup' : '/auth/login', { method: 'POST', body, skipCsrf: true, authRequest: true }); state.csrfToken = result.csrf_token; state.username = username; enterApplication(); }
  catch (error) { text('authError', error.message); }
}
async function bootstrap() {
  buildMonitorCharts(); syncSidebar();
  try { const status = await api('/auth/status', { authRequest: true }); if (!status.configured) return showAuth('setup'); if (!status.authenticated) return showAuth('login'); const me = await api('/auth/me', { authRequest: true }); state.csrfToken = me.csrf_token; state.username = me.username; enterApplication(); }
  catch (error) { showAuth('login', error.message); }
}
function enterApplication() { navigate(rememberedPage()); document.body.classList.remove('auth-pending', 'app-initializing'); text('logoutButton', (state.username || '管理员').slice(0, 2).toUpperCase()); clearTimers(); refreshAll(); startPolling('snapshot', refreshSnapshot, SNAPSHOT_INTERVAL_MS); startPolling('history', refreshHistory, HISTORY_INTERVAL_MS); startPolling('services', refreshServicesAndScenes, SERVICE_INTERVAL_MS); startPolling('video-jobs', refreshVideoJobs, 2000); startPolling('automatic-tasks', refreshAutomaticTasks, 2000); startPolling('logs', refreshLogs, SERVICE_INTERVAL_MS); }
async function logout() { try { await api('/auth/logout', { method: 'POST', authRequest: true }); showAuth('login', '已退出登录。'); } catch (error) { showToast(error.message); } }

async function refreshAll() { await Promise.allSettled([refreshSnapshot(), refreshHistory(), refreshServicesAndScenes(), refreshVideoJobs(), refreshAutomaticTasks(), refreshUsers(), refreshLogs(), refreshSystemInfo(), refreshFiles(state.filePath)]); }
async function refreshSystemInfo() {
  try { const health = await api('/health', { resource: 'system-info' }); dataText('systemVersion', health.version, '版本未知'); }
  catch (error) { dataText('systemVersion', '', '读取失败'); throw error; }
}
async function refreshSnapshot() { if (document.hidden) return; try { state.snapshot = normalizeSnapshot(await api('/snapshot', { resource: 'snapshot' })); renderSnapshot(); } catch (error) { if (!(error instanceof StaleRequestError)) { text('freshnessLabel', '监控离线'); } } }
async function refreshHistory() {
  if (document.hidden) return;
  setHistoryLoading(true);
  try {
    const result = await api(`/history?window=${state.historyWindowMinutes}m`, { resource: 'history' });
    state.history = (result.samples || []).map(normalizeHistorySample); renderCharts();
  } catch (error) {
    showPollingError('历史数据读取失败', error);
  } finally { setHistoryLoading(false); }
}
function setHistoryLoading(loading) { state.historyLoading = loading; const select = byId('historyRangeSelect'); if (select) { select.classList.toggle('loading', loading); select.setAttribute('aria-busy', String(loading)); } }
async function selectHistoryWindow(minutes) {
  if (minutes === state.historyWindowMinutes || state.historyLoading) return;
  monitorChart.windowMilliseconds(minutes); state.historyWindowMinutes = minutes; state.history = [];
  byId('historyRangeSelect').querySelectorAll('[data-history-minutes]').forEach((button) => { const selected = Number(button.dataset.historyMinutes) === minutes; button.classList.toggle('active', selected); button.setAttribute('aria-pressed', String(selected)); });
  buildMonitorCharts(); await refreshHistory();
}
async function refreshServicesAndScenes() {
  if (document.hidden) return;
  try { const [serviceData, sceneData] = await Promise.all([api('/registered-services', { resource: 'services' }), api('/scenes', { resource: 'scenes' })]); state.services = serviceData.services || []; state.scenes = sceneData.scenes || []; renderServices(); renderScenes(); }
  catch (error) { showPollingError('服务状态读取失败', error); }
}
async function refreshLogs() {
  if (document.hidden) return;
  const operationRequest = api('/operations?limit=50', { resource: 'operations' }).catch((error) => { showPollingError('操作日志读取失败', error); return null; });
  const auditRequest = api('/audit?limit=100', { resource: 'audit' }).catch((error) => { showPollingError('默认场景记录读取失败', error); return null; });
  const [operationData, auditData] = await Promise.all([operationRequest, auditRequest]);
  if (!operationData) return;
  const auditEvents = auditData?.events || []; const defaultSceneEvents = auditEvents.filter((event) => ['management.scene.default.set', 'management.scene.default.clear'].includes(event.event)).map(defaultSceneOperation);
  state.operations = [...(operationData.operations || []), ...defaultSceneEvents].sort((left, right) => new Date(right.created_at) - new Date(left.created_at)).slice(0, 50); renderOperations(); renderOperationTimeline();
}
async function refreshVideoJobs() { if (document.hidden) return; try { const result = await api('/video-jobs?limit=100', { resource: 'video-jobs' }); state.videoJobs = result.jobs || []; state.videoQueueSummary = result.queue_summary || { queued_segments: 0 }; renderVideoJobs(); } catch (error) { showPollingError('视频任务读取失败', error); } }
async function refreshAutomaticTasks() {
  if (document.hidden || automaticTaskOrderSaving) return;
  try {
    const tasks = []; let offset = 0; let result;
    do {
      result = await api(`/automatic-tasks?limit=200&offset=${offset}`, { resource: 'automatic-tasks' });
      tasks.push(...(result.tasks || [])); offset = tasks.length;
    } while (result.has_more);
    tasks.sort((left, right) => { const rank = (task) => task.status === 'running' ? 0 : task.status === 'pending' ? 1 : 2; const rankDelta = rank(left) - rank(right); if (rankDelta) return rankDelta; if (['running', 'pending'].includes(left.status)) return (left.queue_position ?? Number.MAX_SAFE_INTEGER) - (right.queue_position ?? Number.MAX_SAFE_INTEGER); return String(right.updated_at).localeCompare(String(left.updated_at)) || String(right.id).localeCompare(String(left.id)); });
    state.automaticTasks = tasks; state.automaticTaskSummary = result?.summary || { pending: 0, running: 0, total: 0 }; renderAutomaticTasks();
  } catch (error) { showPollingError(ui('自动任务读取失败'), error); }
}
async function refreshUsers() { if (document.hidden) return; try { const result = await api('/users', { resource: 'users' }); state.users = result.users || []; renderUsers(); } catch (error) { const rows = byId('userRows'); rows.replaceChildren(element('p', 'empty-state', `用户加载失败：${error.message}`)); throw error; } }

function fileContentUrl(path, download = false) { const params = new URLSearchParams({ path }); if (download) params.set('download', 'true'); return `${API_PREFIX}/file-service/content?${params}`; }
function formatFileSize(value) { if (!Number.isFinite(value)) return '—'; if (value < 1024) return `${value} B`; const units = ['KB', 'MB', 'GB', 'TB']; let size = value; let unit = -1; do { size /= 1024; unit += 1; } while (size >= 1024 && unit < units.length - 1); return `${size.toFixed(size >= 10 ? 1 : 2)} ${units[unit]}`; }
function fileLabel(entry) { if (entry.type === 'directory') return '目录'; if (entry.playable) return entry.media_type?.startsWith('audio/') ? '音频' : '视频'; return '文件'; }
function fileActionLabel(entry) { return `${ui(entry.type === 'directory' ? '打开' : entry.playable ? '播放' : '下载')} ${entry.name}`; }
function downloadFile(entry) { const link = document.createElement('a'); link.href = fileContentUrl(entry.path, true); link.download = entry.name; document.body.append(link); link.click(); link.remove(); }
function closeMedia() { const stage = byId('mediaStage'); stage.querySelectorAll('audio, video').forEach((player) => { player.pause(); player.removeAttribute('src'); player.load(); }); stage.replaceChildren(); if (byId('mediaDialog').open) byId('mediaDialog').close(); }
function requestMediaFullscreen(player) { try { if (player.requestFullscreen) { player.requestFullscreen().catch((error) => showToast(`${ui('无法进入全屏')}：${error.message}`)); } else if (player.webkitEnterFullscreen) { player.webkitEnterFullscreen(); } } catch (error) { showToast(`${ui('无法进入全屏')}：${error.message}`); } }
function openMedia(entry, fullscreen = false) { const kind = entry.media_type?.startsWith('audio/') ? 'audio' : 'video'; const player = document.createElement(kind); player.controls = true; player.autoplay = true; player.preload = 'metadata'; player.src = fileContentUrl(entry.path); player.dataset.i18nSkip = ''; text('mediaTitle', entry.name); const download = byId('mediaDownloadLink'); download.href = fileContentUrl(entry.path, true); download.download = entry.name; byId('mediaStage').replaceChildren(player); byId('mediaDialog').showModal(); if (fullscreen && kind === 'video') requestMediaFullscreen(player); }
function openFileEntry(entry, fullscreen = false) { if (entry.type === 'directory') refreshFiles(entry.path).catch(() => {}); else if (entry.playable) openMedia(entry, fullscreen); else downloadFile(entry); }
function releaseFileThumbnailVideos(rows = byId('fileRows')) {
  fileThumbnailObserver?.disconnect(); fileThumbnailObserver = null;
  rows.querySelectorAll('.file-thumbnail video').forEach((video) => { video.removeAttribute('src'); video.load(); });
}
function observeFileThumbnail(video, source) {
  video.dataset.src = source;
  if (!('IntersectionObserver' in window)) { video.src = source; delete video.dataset.src; return; }
  if (!fileThumbnailObserver) fileThumbnailObserver = new IntersectionObserver((entries, observer) => entries.forEach((entry) => { if (!entry.isIntersecting) return; entry.target.src = entry.target.dataset.src; delete entry.target.dataset.src; observer.unobserve(entry.target); }), { rootMargin: '240px 0px' });
  fileThumbnailObserver.observe(video);
}
function fileThumbnail(entry) {
  const preview = element('button', 'file-thumbnail'); preview.type = 'button'; preview.setAttribute('aria-label', fileActionLabel(entry)); preview.addEventListener('click', () => openFileEntry(entry, entry.playable));
  if (entry.media_type?.startsWith('image/')) {
    const image = document.createElement('img'); image.src = fileContentUrl(entry.path); image.alt = ''; image.loading = 'lazy'; image.decoding = 'async'; preview.append(image);
  } else if (entry.media_type?.startsWith('video/')) {
    const video = document.createElement('video'); video.muted = true; video.preload = 'metadata'; video.playsInline = true; observeFileThumbnail(video, fileContentUrl(entry.path)); preview.append(video);
  } else {
    preview.append(icon(entry.type === 'directory' ? 'box' : entry.media_type?.startsWith('audio/') ? 'play' : 'file'));
  }
  return preview;
}
function setFileView(view) {
  if (!['list', 'thumbnail'].includes(view)) return;
  state.fileView = view;
  document.querySelectorAll('[data-file-view]').forEach((button) => { const selected = button.dataset.fileView === view; button.classList.toggle('active', selected); button.setAttribute('aria-pressed', String(selected)); });
  renderFiles();
}
function renderFileBreadcrumbs() {
  const breadcrumbs = byId('fileBreadcrumbs'); breadcrumbs.replaceChildren();
  const parts = state.filePath ? state.filePath.split('/') : [];
  const add = (label, path, current, userText = true) => { const button = userText ? userElement('button', current ? 'current' : '', label) : element('button', current ? 'current' : '', label); button.type = 'button'; button.disabled = current; button.addEventListener('click', () => refreshFiles(path).catch(() => {})); breadcrumbs.append(button); };
  add('根目录', '', parts.length === 0, false);
  parts.forEach((part, index) => { breadcrumbs.append(icon('chevron')); add(part, parts.slice(0, index + 1).join('/'), index === parts.length - 1); });
}
function renderFiles() {
  renderFileBreadcrumbs(); const rows = byId('fileRows'); const browser = byId('fileBrowser'); releaseFileThumbnailVideos(rows); rows.replaceChildren(); browser.classList.toggle('list-view', state.fileView === 'list'); browser.classList.toggle('thumbnail-view', state.fileView === 'thumbnail');
  if (!state.files.length) { rows.append(element('p', 'empty-state', '这个目录是空的。')); return; }
  state.files.forEach((entry) => { const row = element('article', 'file-row'); const name = element('button', 'file-name'); name.type = 'button'; name.setAttribute('aria-label', fileActionLabel(entry)); name.append(icon(entry.type === 'directory' ? 'box' : entry.playable ? 'play' : 'file')); const copy = element('span'); const title = userElement('strong', '', entry.name); const type = element('small', 'file-entry-type', fileLabel(entry)); copy.append(title); name.append(copy); name.addEventListener('click', () => openFileEntry(entry, entry.playable)); const actions = element('span', 'file-actions'); const rename = iconButton('更名', 'edit', 'file-rename-button'); rename.addEventListener('click', () => openFileRenameDialog(entry)); const remove = iconButton('删除', 'trash', 'file-delete-button'); remove.addEventListener('click', () => deleteFileEntry(entry)); actions.append(rename, remove); if (state.fileView === 'thumbnail') { const meta = element('span', 'file-card-meta'); meta.append(type, actions); row.append(fileThumbnail(entry), name, meta); } else { copy.append(type); row.append(name, element('span', 'file-size', entry.type === 'directory' ? '—' : formatFileSize(entry.size)), userElement('time', '', formatDate(entry.modified_at, true)), actions); } rows.append(row); });
}
async function refreshFiles(path = '') {
  if (document.hidden) return; const rows = byId('fileRows'); releaseFileThumbnailVideos(rows); rows.setAttribute('aria-busy', 'true');
  try {
    const info = state.fileService || await api('/file-service', { resource: 'file-service-info' }); state.fileService = info; text('fileServicePort', `:${info.port}`); dataText('fileServiceRoot', info.root, '根目录未配置');
    if (!info.root_available) throw new ApiError(404, 'file_root_unavailable', '配置的根目录不存在或不可访问。');
    const [sortBy, sortOrder] = state.fileSort.split('-'); const params = new URLSearchParams({ path, sort_by: sortBy, sort_order: sortOrder }); const result = await api(`/file-service/files?${params}`, { resource: 'file-service-files' }); state.filePath = result.path || ''; state.files = result.entries || []; text('fileServiceStatus', window.axisI18n.language === 'zh' ? `${state.files.length} 个项目 · HTTP 端口 ${info.port}` : `${state.files.length} items · HTTP port ${info.port}`); byId('fileServiceStatus').classList.remove('error'); renderFiles();
  } catch (error) {
    if (error instanceof StaleRequestError) return; state.files = []; rows.replaceChildren(element('p', 'empty-state', `目录加载失败：${error.message}`)); text('fileServiceStatus', error.message); byId('fileServiceStatus').classList.add('error'); throw error;
  } finally { rows.removeAttribute('aria-busy'); }
}

async function uploadSelectedFiles(files) {
  if (!files.length) return;
  const uploadPath = state.filePath; const button = byId('uploadFilesButton'); const original = button.querySelector('span').textContent;
  button.disabled = true;
  try {
    for (let index = 0; index < files.length; index += 1) {
      const file = files[index]; button.querySelector('span').textContent = window.axisI18n.language === 'zh' ? `上传中 ${index + 1}/${files.length}` : `Uploading ${index + 1}/${files.length}`;
      const params = new URLSearchParams({ path: uploadPath, name: file.name });
      await api(`/file-service/upload?${params}`, { method: 'POST', rawBody: file, timeout: null });
    }
    showToast(window.axisI18n.language === 'zh' ? `${files.length} 个文件上传完成` : `${files.length} files uploaded`);
    if (state.filePath === uploadPath) await refreshFiles(uploadPath);
  } catch (error) {
    showToast(`${window.axisI18n.language === 'zh' ? '上传失败' : 'Upload failed'}：${error.message}`);
    if (state.filePath === uploadPath) await refreshFiles(uploadPath).catch(() => {});
  } finally {
    button.disabled = false; button.querySelector('span').textContent = original; byId('fileUploadInput').value = '';
  }
}

function openFileRenameDialog(entry) {
  byId('fileRenameForm').reset(); text('fileRenameError', ''); byId('fileRenamePath').value = entry.path; byId('fileRenameName').value = entry.name; byId('fileRenameDialog').showModal(); byId('fileRenameName').focus(); byId('fileRenameName').select();
}
async function renameFileEntry(event) {
  event.preventDefault(); const path = byId('fileRenamePath').value; const newName = byId('fileRenameName').value.trim();
  if (!newName) return text('fileRenameError', ui('请输入新名称。'));
  try { await api('/file-service/rename', { method: 'POST', body: { path, new_name: newName } }); byId('fileRenameDialog').close(); showToast(ui('更名完成')); await refreshFiles(state.filePath); }
  catch (error) { text('fileRenameError', error.message); }
}
async function deleteFileEntry(entry) {
  const message = window.axisI18n.language === 'zh' ? `将“${entry.name}”移入回收站？之后可从 Windows 回收站恢复。` : `Move “${entry.name}” to the Recycle Bin? You can restore it later from Windows.`;
  if (!confirm(message)) return;
  try { await api(`/file-service/entry?path=${encodeURIComponent(entry.path)}`, { method: 'DELETE' }); showToast(ui('已移入回收站')); }
  catch (error) { showToast(error.message); }
  finally { await refreshFiles(state.filePath); }
}

function normalizeGpu(gpu) { return { ...gpu, load_percent: normalizedPercent(gpu.load_percent), memory_percent: normalizedPercent(gpu.memory_percent) }; }
function normalizeHistorySample(sample) { return { ...sample, cpu_load_percent: normalizedPercent(sample.cpu_load_percent), memory_percent: normalizedPercent(sample.memory_percent), gpus: Array.isArray(sample.gpus) ? sample.gpus.map(normalizeGpu) : [], disks: Array.isArray(sample.disks) ? sample.disks : [] }; }
function normalizeSnapshot(snapshot) { const host = snapshot.host || {}; const staleGpu = snapshot.stale_collectors?.nvidia || snapshot.stale_collectors?.snapshot; return { ...snapshot, host: { ...host, cpu: { ...(host.cpu || {}), load_percent: normalizedPercent(host.cpu?.load_percent) }, memory: { ...(host.memory || {}), percent: normalizedPercent(host.memory?.percent) }, disks: Array.isArray(host.disks) ? host.disks.map((disk) => ({ ...disk, percent: normalizedPercent(disk.percent) })) : [] }, gpus: Array.isArray(snapshot.gpus) ? snapshot.gpus.map((gpu) => normalizeGpu({ ...gpu, _stale: Boolean(staleGpu), _lastSuccessAt: staleGpu?.last_success_at || null })) : [] }; }
function snapshotAsHistory(snapshot) { const host = snapshot?.host; const memory = host?.memory; const network = host?.primary_network; const wsl = host?.wsl; const staleSnapshot = Boolean(snapshot?.stale_collectors?.snapshot); const staleGpu = staleSnapshot || Boolean(snapshot?.stale_collectors?.nvidia); return snapshot ? { sampled_at: snapshot.sampled_at, cpu_load_percent: staleSnapshot ? null : host?.cpu?.load_percent, cpu_temperature_c: staleSnapshot ? null : host?.cpu?.temperature_c, cpu_frequency_mhz: staleSnapshot ? null : host?.cpu?.frequency_mhz, memory_percent: staleSnapshot ? null : memory?.percent, memory_used_bytes: staleSnapshot ? null : memory?.used_bytes, memory_total_bytes: memory?.total_bytes, memory_available_bytes: staleSnapshot ? null : memory?.available_bytes, commit_used_bytes: staleSnapshot ? null : memory?.commit_used_bytes, commit_limit_bytes: memory?.commit_limit_bytes, swap_used_bytes: staleSnapshot ? null : memory?.swap_used_bytes, swap_total_bytes: memory?.swap_total_bytes, network_received_bytes_per_second: staleSnapshot ? null : network?.received_bytes_per_second, network_sent_bytes_per_second: staleSnapshot ? null : network?.sent_bytes_per_second, wsl_memory_used_bytes: staleSnapshot ? null : wsl?.memory_used_bytes, wsl_swap_used_bytes: staleSnapshot ? null : wsl?.swap_used_bytes, disks: staleSnapshot ? [] : host?.disk_io || [], gpus: staleGpu ? [] : snapshot.gpus || [] } : null; }
function currentSeries() { const samples = state.history.slice(); const current = snapshotAsHistory(state.snapshot); if (current && !samples.some((sample) => sample.sampled_at === current.sampled_at)) samples.push(current); return samples.sort((a, b) => new Date(a.sampled_at) - new Date(b.sampled_at)); }
function historyWindowLabel() { return { 15: '15m', 60: '1h', 1440: '24h' }[state.historyWindowMinutes]; }

function renderSnapshot() {
  const snapshot = state.snapshot; if (!snapshot) return; const host = snapshot.host || {}; const cpu = host.cpu || {}; const memory = host.memory || {};
  text('cpuMetric', percent(cpu.load_percent)); text('cpuDetail', `温度 ${finite(cpu.temperature_c) ? `${Math.round(cpu.temperature_c)}°C` : '不支持'}`);
  const used = gib(memory.used_bytes); const total = gib(memory.total_bytes); text('memoryMetric', used === null ? '不支持' : `${used.toFixed(1)} GB`); text('memoryDetail', total === null ? '总量不可用' : `${percent(memory.percent)} 已使用 · 共 ${total.toFixed(1)} GB`);
  const disks = (host.disks || []).filter((disk) => finite(disk.total_bytes)); const disk = disks.find((item) => String(item.mountpoint || item.device).toUpperCase().startsWith('C')) || disks[0];
  if (disk) { text('diskLabel', `${disk.device || disk.mountpoint} 可用`); text('diskMetric', `${gib(disk.total_bytes - disk.used_bytes).toFixed(1)} GB`); text('diskDetail', `${percent(disk.percent)} 已使用`); } else { text('diskMetric', '不支持'); }
  const containers = snapshot.docker?.containers || []; const running = containers.filter((item) => String(item.state).toLowerCase() === 'running').length; text('dockerMetric', `${running}/${containers.length}`); text('dockerDetail', `${running} 运行 · ${containers.length - running} 停止`);
  document.querySelector('.host-mini strong').textContent = location.hostname || '本机'; document.querySelector('.host-load').textContent = percent(cpu.load_percent);
  syncGpuCards(snapshot.gpus || []); renderCollectorErrors(snapshot.collector_errors || []); renderCharts();
  text('freshnessLabel', snapshot.stale_collectors?.snapshot ? '监控数据延迟' : snapshot.stale_collectors?.nvidia ? 'GPU 数据延迟' : '实时'); text('clock', formatDate(snapshot.sampled_at));
}
function createGpuCard(gpu, position) {
  const lane = element('article', `gpu-lane${position === 0 ? ' gpu-primary' : ''}`); lane.dataset.gpuKey = gpu._uiKey; lane.style.setProperty('--gpu-order', String(position));
  const head = element('div', 'gpu-head'); const identity = element('div'); const index = element('span', 'gpu-index', `GPU ${gpu.index}`); const name = userElement('h2', 'gpu-name', ''); const role = userElement('p', 'gpu-role', ''); identity.append(index, name, role); const util = element('div', 'gpu-util'); util.append(userElement('strong', 'gpu-util-value', '--'), element('span', '', '% 负载')); head.append(identity, util);
  const telemetry = element('div', 'gpu-telemetry'); const memory = element('div', 'memory-visual'); memory.style.setProperty('--used', '0%'); const ring = element('div', 'memory-ring'); const ringCopy = element('span'); ringCopy.append(userElement('strong', 'gpu-memory-value', '--'), element('small', 'gpu-memory-total', '/ -- GB')); ring.append(ringCopy); const memoryCopy = element('div', 'memory-copy'); memoryCopy.append(element('span', '', '显存占用'), element('strong', 'gpu-memory-percent', '--'), element('small', 'gpu-memory-free', '等待数据')); memory.append(ring, memoryCopy);
  const trend = element('div', 'gpu-load-trend'); const trendHead = element('div', 'trend-head'); trendHead.append(element('span', '', 'GPU 负载趋势'), element('small', '', '最近 15 分钟')); const plot = element('div', 'trend-plot'); const scale = element('span', 'trend-scale'); scale.append(element('i', '', '100%'), element('i', '', '50%'), element('i', '', '0')); const svg = svgElement('svg', { viewBox: '0 0 300 120', preserveAspectRatio: 'none', role: 'img', 'aria-label': ui(`${gpu.name || `GPU ${gpu.index}`} GPU 负载曲线`) }); svg.append(svgElement('path', { class: 'spark-grid', d: 'M0 10H300M0 60H300M0 110H300' }), svgElement('path', { class: 'gpu-sparkline', d: '' })); plot.append(scale, svg); const axis = element('div', 'trend-axis'); ['-15m', '-10m', '-5m', '现在'].forEach((label) => axis.append(element('span', '', label))); trend.append(trendHead, plot, axis); telemetry.append(memory, trend);
  const services = element('div', 'gpu-services'); const mark = element('div', 'workload-icon', String(gpu.index ?? '?')); const serviceCopy = element('div', 'gpu-service-copy'); serviceCopy.append(element('strong', 'gpu-service-names', '尚未登记服务'), element('span', 'gpu-service-meta', 'GPU 为用户登记标签')); const serviceLink = element('button', 'quiet-link', '查看服务'); serviceLink.type = 'button'; serviceLink.addEventListener('click', () => navigate('environments')); services.append(mark, serviceCopy, serviceLink);
  const metrics = element('div', 'metric-row'); [['gpu-temp', '温度'], ['gpu-power', '功耗'], ['gpu-uuid', 'UUID'], ['gpu-state', '状态']].forEach(([className, label]) => { const item = element('span'); item.append(element('b', className, '--'), document.createTextNode(label)); metrics.append(item); });
  lane.append(head, telemetry, services, metrics); return lane;
}
function updateGpuCard(lane, gpu) {
  const used = mibToGib(gpu.memory_used_mib); const total = mibToGib(gpu.memory_total_mib); const free = used !== null && total !== null ? Math.max(0, total - used) : null;
  lane.querySelector('.gpu-index').textContent = `GPU ${gpu.index}`; lane.querySelector('.gpu-name').textContent = gpu.name || `GPU ${gpu.index}`; lane.querySelector('.gpu-role').textContent = compactUuid(gpu.uuid); lane.querySelector('.gpu-util-value').textContent = finite(gpu.load_percent) ? String(Math.round(gpu.load_percent)) : '--'; lane.querySelector('.gpu-memory-value').textContent = used === null ? '--' : used.toFixed(1); lane.querySelector('.gpu-memory-total').textContent = total === null ? `/ ${ui('不支持')}` : `/ ${total.toFixed(1)} GB`;
  const lastSuccess = gpu._lastSuccessAt ? formatDate(gpu._lastSuccessAt, true) : '--'; lane.classList.toggle('stale', gpu._stale); lane.querySelector('.gpu-memory-percent').textContent = percent(gpu.memory_percent); lane.querySelector('.gpu-memory-free').textContent = free === null ? '不支持' : `剩余 ${free.toFixed(1)} GB`; lane.querySelector('.gpu-temp').textContent = finite(gpu.temperature_c) ? `${Math.round(gpu.temperature_c)}°C` : '不支持'; lane.querySelector('.gpu-power').textContent = finite(gpu.power_w) ? `${Math.round(gpu.power_w)} W` : '不支持'; lane.querySelector('.gpu-uuid').textContent = compactUuid(gpu.uuid); lane.querySelector('.gpu-state').textContent = gpu._stale ? `${ui('上次数据')} · ${lastSuccess}` : '已检测'; lane.querySelector('.gpu-state').title = ''; lane.querySelector('.memory-visual').style.setProperty('--used', `${gpu.memory_percent || 0}%`);
  const values = currentSeries().map((sample) => gpuLayout.metricForGpu(sample, gpu, 'load_percent')); renderPolyline(lane.querySelector('.gpu-sparkline'), values, gpu.name || `GPU ${gpu.index}`);
}
function syncGpuCards(gpus) {
  const ordered = gpuLayout.prepareGpus(gpus); const signature = gpuLayout.gpuSetSignature(ordered); const stage = byId('gpuStage'); state.gpus = ordered;
  if (signature !== state.gpuCardSignature) { stage.replaceChildren(); if (!ordered.length) stage.append(element('p', 'gpu-empty', '未检测到 NVIDIA GPU。')); else ordered.forEach((gpu, position) => stage.append(createGpuCard(gpu, position))); state.gpuCardSignature = signature; }
  ordered.forEach((gpu) => { const lane = [...stage.querySelectorAll('.gpu-lane')].find((item) => item.dataset.gpuKey === gpu._uiKey); if (lane) updateGpuCard(lane, gpu); }); renderGpuServiceLabels(); syncMonitorCharts();
}
function renderPolyline(polyline, values, device) { if (!polyline) return; polyline.closest('svg').setAttribute('aria-label', ui(`${device} GPU 负载曲线`)); polyline.setAttribute('d', gpuLayout.sparklinePath(values.map(normalizedPercent))); }
function renderCollectorErrors(errors) { const banner = byId('collectorStatus'); banner.hidden = !errors.length; banner.replaceChildren(); if (errors.length) banner.append(element('strong', '', '部分数据降级'), element('span', '', errors.map((error) => error.message || '采集失败').join('；'))); }
function monitorDetail(label) { const node = element('span', 'monitor-detail'); node.append(element('small', '', label), userElement('strong', '', '--')); return { node, value: node.querySelector('strong') }; }
function niceMetricMaximum(samples, getter, step, fallback) { const peak = Math.max(0, ...(samples || []).map(getter).filter(finite)); return Math.max(fallback, Math.ceil(peak / step) * step); }
function chartScale(spec, samples) { return { minimum: spec.minimum ?? 0, maximum: typeof spec.maximum === 'function' ? spec.maximum(samples) : spec.maximum ?? 100 }; }
function chartValue(value, spec, includeUnit = true) { if (value === '--' || !finite(value)) return '--'; const rendered = Number(value).toFixed(spec.decimals ?? 0); if (!includeUnit || !spec.unit) return rendered; return `${rendered}${['%', '°C'].includes(spec.unit) ? '' : ' '}${spec.unit}`; }
function chartAxisValues(scale, spec) { return [1, .75, .5, .25, 0].map((ratio) => chartValue(scale.minimum + (scale.maximum - scale.minimum) * ratio, spec)); }
function chartCurrentValue(value, spec, includeUnit = false) { const rendered = chartValue(value, spec, includeUnit); const maximum = spec.lastModel?.maximumScale; if (!spec.showMaximumInCurrent || rendered === '--' || !finite(maximum)) return rendered; return `${rendered} / ${chartValue(maximum, spec, includeUnit)}`; }
function updateChartCurrent(spec, value, selected = false) { spec.current.firstChild.nodeValue = chartCurrentValue(value, spec); spec.currentLabel.textContent = `${spec.unit || '%'} ${selected ? ui('选中') : ui('当前')}`; }
function createMonitorChart(spec, chartIndex) {
  const section = element('section', `chart-section${spec.compact ? ' chart-compact' : ''}${spec.showXAxis === false ? ' chart-no-x-axis' : ''}`); section.style.setProperty('--chart-color', spec.color);
  const heading = element('div', 'chart-title'); const copy = element('div'); copy.append(element('span', 'chart-kicker', spec.kicker), element('h3', '', spec.title), element('p', '', spec.description));
  const current = element('strong', 'chart-current', '--'); const currentLabel = element('small', '', `${spec.unit || '%'} 当前`); current.append(currentLabel); heading.append(copy, current);
  const statistics = element('div', 'chart-statistics'); const statisticRefs = {};
  [['average', '平均'], ['peak', '峰值'], ['minimum', '最低']].forEach(([key, label]) => { const item = element('span'); const value = userElement('b', '', '--'); item.append(element('small', '', label), value); statistics.append(item); statisticRefs[key] = value; });
  const frame = element('div', 'chart-frame'); const yAxis = element('span', 'chart-y-axis'); chartAxisValues({ minimum: spec.minimum ?? 0, maximum: typeof spec.maximum === 'number' ? spec.maximum : spec.initialMaximum ?? 100 }, spec).forEach((label) => yAxis.append(element('i', '', label)));
  const plot = element('div', 'chart-plot'); const svg = svgElement('svg', { class: 'line-chart', viewBox: '0 0 900 200', preserveAspectRatio: 'none', role: 'img', 'aria-label': `${ui(spec.title)} · ${historyWindowLabel()}` });
  const gradientId = `monitorGradient${chartIndex}`; const defs = svgElement('defs'); const gradient = svgElement('linearGradient', { id: gradientId, x1: '0', y1: '0', x2: '0', y2: '1' }); gradient.append(svgElement('stop', { class: 'chart-gradient-start', offset: '0%' }), svgElement('stop', { class: 'chart-gradient-end', offset: '100%' })); defs.append(gradient);
  const grid = svgElement('path', { class: 'chart-grid-lines', d: 'M0 1H900M0 50H900M0 100H900M0 150H900M0 199H900M1 0V200M300 0V200M600 0V200M899 0V200' }); const areaLayer = svgElement('g', { class: 'chart-areas' }); const lineLayer = svgElement('g', { class: 'chart-lines' }); const isolatedLayer = svgElement('g', { class: 'chart-isolated-points' }); const cursor = svgElement('line', { class: 'chart-cursor', x1: '0', x2: '0', y1: '0', y2: '200', hidden: '' }); const marker = svgElement('circle', { class: 'chart-marker', cx: '0', cy: '0', r: '4', hidden: '' }); svg.append(defs, grid, areaLayer, lineLayer, isolatedLayer, cursor, marker);
  const noData = element('span', 'chart-no-data', '暂无采样数据'); plot.append(svg, noData); const xAxis = element('div', 'chart-x-axis'); monitorChart.axisLabels(state.historyWindowMinutes).forEach((label) => xAxis.append(element('span', '', label))); xAxis.hidden = spec.showXAxis === false; frame.append(yAxis, plot, xAxis);
  section.append(heading, statistics, frame); return { ...spec, section, current, currentLabel, statisticRefs, yAxis, svg, gradientId, areaLayer, lineLayer, isolatedLayer, cursor, marker, noData, lastModel: null };
}
function bindCorrelationCursor(charts, announcement) {
  let selectedTimestamp = null;
  let touchPointer = null;
  const availableSamples = () => { const model = charts[0]?.lastModel; if (!model) return []; return currentSeries().filter((sample) => { const timestamp = Date.parse(sample?.sampled_at); return Number.isFinite(timestamp) && timestamp >= model.startTimeMs && timestamp <= model.endTimeMs; }).sort((left, right) => Date.parse(left.sampled_at) - Date.parse(right.sampled_at)); };
  const syncTimeText = (sample) => sample ? formatDate(sample.sampled_at, true) : '--';
  const selectSample = (sample, persist = false) => {
    const model = charts[0]?.lastModel; if (!sample || !model) return;
    const timestamp = Date.parse(sample.sampled_at); if (persist) selectedTimestamp = timestamp; const x = Math.min(900, Math.max(0, ((timestamp - model.startTimeMs) / (model.endTimeMs - model.startTimeMs)) * 900));
    charts.forEach((chart) => { chart.cursor.setAttribute('x1', String(x)); chart.cursor.setAttribute('x2', String(x)); chart.cursor.removeAttribute('hidden'); updateChartCurrent(chart, chart.getter(sample), true); });
    announcement.textContent = syncTimeText(sample);
  };
  const sync = () => { const samples = availableSamples(); const selectedIndex = selectedTimestamp === null ? -1 : samples.findIndex((sample) => Date.parse(sample.sampled_at) === selectedTimestamp); if (selectedIndex >= 0) { selectSample(samples[selectedIndex], true); } else { selectedTimestamp = null; announcement.textContent = syncTimeText(samples.at(-1)); } };
  const restoreSelection = () => { const samples = availableSamples(); const selectedIndex = selectedTimestamp === null ? -1 : samples.findIndex((sample) => Date.parse(sample.sampled_at) === selectedTimestamp); if (selectedIndex >= 0) { selectSample(samples[selectedIndex], true); return; } charts.forEach((chart) => { chart.cursor.setAttribute('hidden', ''); updateChartCurrent(chart, chart.lastModel?.current ?? '--'); }); announcement.textContent = syncTimeText(samples.at(-1)); };
  const selectPointerSample = (event, persist = false) => {
    const model = charts[0]?.lastModel; const plot = event.currentTarget; if (!model || !plot) return;
    const bounds = plot.getBoundingClientRect(); const ratio = Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width)); const targetTimestamp = model.startTimeMs + ratio * (model.endTimeMs - model.startTimeMs); const samples = availableSamples(); const sample = monitorChart.nearestSample(samples, targetTimestamp, model.startTimeMs, model.endTimeMs); if (!sample) return; selectSample(sample, persist);
  };
  const beginTouchSelection = (event) => {
    if (event.pointerType === 'mouse') return;
    touchPointer = monitorChart.beginPointerGesture(event.pointerId, event.clientX, event.clientY);
    event.currentTarget.setPointerCapture(event.pointerId);
  };
  const movePointer = (event) => {
    if (event.pointerType === 'mouse') { selectPointerSample(event); return; }
    const movement = monitorChart.movePointerGesture(touchPointer, event.pointerId, event.clientX, event.clientY);
    touchPointer = movement.gesture;
    if (movement.select) selectPointerSample(event, true);
  };
  const finishTouchSelection = (event) => {
    const finish = monitorChart.finishPointerGesture(touchPointer, event.pointerId, event.type === 'pointercancel');
    if (!finish.finished) return;
    if (finish.select) selectPointerSample(event, true);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    touchPointer = finish.gesture;
  };
  charts.forEach((chart) => {
    const plot = chart.section.querySelector('.chart-plot');
    plot.addEventListener('pointerdown', beginTouchSelection);
    plot.addEventListener('pointermove', movePointer);
    plot.addEventListener('pointerup', finishTouchSelection);
    plot.addEventListener('pointercancel', finishTouchSelection);
    plot.addEventListener('pointerleave', (event) => { if (event.pointerType === 'mouse') restoreSelection(); });
  });
  return { sync };
}
function createMonitorGroup({ className, kicker, title, description, descriptionDetail = '', titleIsUserData = false, color, details, charts = [], correlationCharts = [], contextCharts = [] }) {
  const group = element('section', `monitor-group ${className}`); group.style.setProperty('--monitor-color', color);
  const header = element('div', 'monitor-group-header'); const identity = element('div'); const descriptionNode = element('p', '', description); if (descriptionDetail) descriptionNode.append(document.createTextNode(' · '), userElement('span', '', descriptionDetail)); identity.append(element('span', 'monitor-group-kicker', kicker), titleIsUserData ? userElement('h2', '', title) : element('h2', '', title), descriptionNode); const detailRow = element('div', 'monitor-detail-row'); details.forEach((detail) => detailRow.append(detail.node)); header.append(identity, detailRow);
  const body = element('div', `monitor-chart-grid${correlationCharts.length ? ' monitor-gpu-layout' : ''}`);
  if (correlationCharts.length) {
    const correlation = element('section', 'gpu-correlation-stack'); const correlationHead = element('div', 'correlation-heading'); const copy = element('div'); copy.append(element('div', '', '核心遥测相关性'), element('small', '', '拖动曲线对比同一时刻')); const time = element('div', 'correlation-time'); const announcement = element('output', 'correlation-time-value', '--'); announcement.setAttribute('aria-live', 'polite'); time.append(element('small', '', '同步时间'), announcement); correlationHead.append(copy, time);
    correlation.append(correlationHead, ...correlationCharts.map((chart) => chart.section)); const context = element('section', 'gpu-context-column'); context.append(element('div', 'context-heading', '容量与分配'), ...contextCharts.map((chart) => chart.section)); body.append(context, correlation);
    state.correlationControllers.push(bindCorrelationCursor(correlationCharts, announcement));
  } else charts.forEach((chart) => body.append(chart.section));
  group.append(header, body); return group;
}
function byteRate(value) { if (!finite(value)) return '--'; const mib = value / 1024 ** 2; return mib >= 1024 ? `${(mib / 1024).toFixed(1)} GB/s` : `${mib.toFixed(mib >= 10 ? 0 : 1)} MB/s`; }
function processDisplayName(value) { return String(value || '').split(/[\\/]/).at(-1) || ui('未知'); }
function clockEventReason(value) { if (!value) return '--'; const bits = Number.parseInt(value, 16); if (!Number.isFinite(bits)) return value; if (bits === 0) return ui('无'); const reasons = [[1, '空闲'], [2, '应用时钟设置'], [4, '软件功率限制'], [8, '硬件降速'], [16, '同步加速'], [32, '软件温度限制'], [64, '硬件温度限制'], [128, '外部功率制动'], [256, '显示时钟设置']].filter(([mask]) => (bits & mask) !== 0).map(([, label]) => ui(label)); return reasons.join(' · ') || value; }
function diskForSample(sample, name) { return (sample?.disks || []).find((disk) => disk.name === name) || {}; }
function monitorSummaryItem(label, hint, target) { const button = element('button', 'monitor-summary-item'); button.type = 'button'; button.dataset.monitorTarget = target; const status = element('i', 'monitor-health-dot'); const copy = element('span'); copy.append(element('small', '', label), userElement('strong', '', '--'), element('em', '', hint)); button.append(status, copy); return { node: button, value: copy.querySelector('strong'), hint: copy.querySelector('em'), status }; }
function appendChart(container, spec, chartIndex) { const chart = createMonitorChart(spec, chartIndex); state.chartSpecs.push(chart); container.push(chart); return chartIndex + 1; }
function selectMonitorView(view) { if (!['summary', 'gpu', 'host', 'system'].includes(view) || state.monitorView === view) return; state.monitorView = view; byId('monitorTabbar').querySelectorAll('[data-monitor-view]').forEach((button) => { const selected = button.dataset.monitorView === view; button.classList.toggle('active', selected); button.setAttribute('aria-pressed', String(selected)); }); buildMonitorCharts(); }
function buildMonitorSummary(container, details) {
  const status = element('section', 'monitor-status-band'); const heading = element('div'); heading.append(element('span', 'monitor-group-kicker', 'WORKSTATION'), element('h2', '', '整机状态'), element('p', '', '异常优先显示，点击项目进入详细监控')); const health = element('strong', 'monitor-health-label', '正在采样'); status.append(heading, health); details.health = health; container.append(status);
  const list = element('section', 'monitor-summary-list'); details.summary = new Map(); const items = [{ key: 'CPU', label: 'CPU', hint: '负载与温度', target: 'host' }, { key: '系统内存', label: '系统内存', hint: '物理与提交内存', target: 'host' }, ...state.gpus.map((gpu) => ({ key: gpu._uiKey, label: `GPU ${gpu.index}`, hint: gpu.name || 'NVIDIA GPU', target: 'gpu', gpuKey: gpu._uiKey })), { key: '存储', label: '存储', hint: '容量、吞吐与延迟', target: 'system' }, { key: '网络', label: '网络', hint: '上传与下载', target: 'system' }, { key: 'WSL / Docker', label: 'WSL / Docker', hint: '运行平台压力', target: 'system' }]; items.forEach(({ key, label, hint, target, gpuKey }, index) => { const item = monitorSummaryItem(label, hint, target); item.node.style.setProperty('--summary-order', String(index)); item.node.addEventListener('click', () => { if (gpuKey) state.selectedMonitorGpuKey = gpuKey; selectMonitorView(target); }); list.append(item.node); details.summary.set(key, item); }); container.append(list);
}
function buildHostMonitor(container, details, chartIndex) {
  const cpuTemp = monitorDetail('CPU 温度'); const cpuFrequency = monitorDetail('CPU 频率'); const memoryUsage = monitorDetail('内存用量'); const commitUsage = monitorDetail('提交内存'); details.cpuTemp = cpuTemp.value; details.cpuFrequency = cpuFrequency.value; details.memoryUsage = memoryUsage.value; details.commitUsage = commitUsage.value;
  const memoryCapacity = (samples) => Math.max(1, ...[gib(state.snapshot?.host?.memory?.total_bytes), ...(samples || []).map((sample) => gib(sample.memory_total_bytes))].filter(finite)); const commitCapacity = (samples) => Math.max(1, ...[gib(state.snapshot?.host?.memory?.commit_limit_bytes), ...(samples || []).map((sample) => gib(sample.commit_limit_bytes))].filter(finite)); const swapCapacity = (samples) => Math.max(1, ...[gib(state.snapshot?.host?.memory?.swap_total_bytes), ...(samples || []).map((sample) => gib(sample.swap_total_bytes))].filter(finite));
  const charts = []; [{ kicker: 'CPU', title: '处理器负载', description: '全部逻辑处理器综合使用率', color: 'var(--accent)', getter: (sample) => sample.cpu_load_percent }, { kicker: 'CLOCK', title: 'CPU 频率', description: '处理器当前平均频率', color: '#38bdf8', getter: (sample) => sample.cpu_frequency_mhz, unit: 'MHz', maximum: (samples) => niceMetricMaximum(samples, (sample) => sample.cpu_frequency_mhz, 500, 6000) }, { kicker: 'RAM', title: '系统内存', description: '已用内存与物理内存容量', color: '#60a5fa', getter: (sample) => gib(sample.memory_used_bytes), unit: 'GB', decimals: 1, maximum: memoryCapacity, initialMaximum: memoryCapacity([]), showMaximumInCurrent: true, statisticsIncludeUnit: true }, { kicker: 'COMMIT', title: '提交内存', description: '系统已承诺内存与提交上限', color: '#a78bfa', getter: (sample) => gib(sample.commit_used_bytes), unit: 'GB', decimals: 1, maximum: commitCapacity, initialMaximum: commitCapacity([]), showMaximumInCurrent: true, statisticsIncludeUnit: true }, { kicker: 'PAGEFILE', title: '页面文件', description: 'Windows 页面文件实际占用', color: '#f59e0b', getter: (sample) => gib(sample.swap_used_bytes), unit: 'GB', decimals: 1, maximum: swapCapacity, initialMaximum: swapCapacity([]), showMaximumInCurrent: true, statisticsIncludeUnit: true }].forEach((spec) => { chartIndex = appendChart(charts, spec, chartIndex); });
  container.append(createMonitorGroup({ className: 'monitor-host-group', kicker: 'HOST', title: '主机资源', description: 'CPU、物理内存与提交压力', color: 'var(--accent)', details: [cpuTemp, cpuFrequency, memoryUsage, commitUsage], charts })); return chartIndex;
}
function buildGpuMonitor(container, details, chartIndex) {
  if (!state.gpus.length) { container.append(element('p', 'empty-state monitor-gpu-empty', '未检测到 NVIDIA GPU。')); return chartIndex; }
  if (!state.gpus.some((gpu) => gpu._uiKey === state.selectedMonitorGpuKey)) state.selectedMonitorGpuKey = state.gpus[0]._uiKey;
  const selector = element('div', 'monitor-device-selector'); state.gpus.forEach((gpu) => { const button = userElement('button', gpu._uiKey === state.selectedMonitorGpuKey ? 'active' : '', `GPU ${gpu.index} · ${gpu.name}`); button.type = 'button'; button.addEventListener('click', () => { state.selectedMonitorGpuKey = gpu._uiKey; buildMonitorCharts(); }); selector.append(button); }); container.append(selector);
  const gpu = state.gpus.find((item) => item._uiKey === state.selectedMonitorGpuKey); const position = state.gpus.indexOf(gpu); const color = MONITOR_GPU_COLORS[position % MONITOR_GPU_COLORS.length]; const frequency = monitorDetail('核心频率'); const temperature = monitorDetail('温度'); const power = monitorDetail('功率'); const memory = monitorDetail('显存用量'); const engine = monitorDetail('显存控制器'); const media = monitorDetail('编解码'); details.gpus.set(gpu._uiKey, { frequency: frequency.value, temperature: temperature.value, power: power.value, memory: memory.value, engine: engine.value, media: media.value });
  const metric = (field) => (sample) => gpuLayout.metricForGpu(sample, gpu, field); const metricGib = (field) => { const getter = metric(field); return (sample) => mibToGib(getter(sample)); }; const memoryCapacity = (samples) => Math.max(1, ...[mibToGib(gpu.memory_total_mib), ...(samples || []).map(metricGib('memory_total_mib'))].filter(finite)); const correlationSpecs = [{ kicker: 'LOAD', title: '核心负载', description: '图形与计算核心综合使用率', color, getter: metric('load_percent'), unit: '%', maximum: 100 }, { kicker: 'CLOCK', title: '核心频率', description: '当前图形时钟', color: '#38bdf8', getter: metric('graphics_clock_mhz'), unit: 'MHz', maximum: (samples) => niceMetricMaximum(samples, metric('graphics_clock_mhz'), 500, 3000) }, { kicker: 'POWER', title: '功率', description: '当前 GPU 板卡功耗', color: '#f59e0b', getter: metric('power_w'), unit: 'W', maximum: (samples) => niceMetricMaximum(samples, metric('power_w'), 50, 500) }, { kicker: 'THERMAL', title: '温度', description: '核心温度变化', color: '#f87171', getter: metric('temperature_c'), unit: '°C', maximum: 100, showXAxis: true }].map((spec, index) => ({ ...spec, compact: true, showXAxis: index === 3 })); const correlationCharts = []; correlationSpecs.forEach((spec) => { chartIndex = appendChart(correlationCharts, spec, chartIndex); }); const contextCharts = []; chartIndex = appendChart(contextCharts, { kicker: 'VRAM', title: '显存占用', description: '已用显存与物理显存容量', color, getter: metricGib('memory_used_mib'), unit: 'GB', decimals: 1, maximum: memoryCapacity, initialMaximum: memoryCapacity([]), showMaximumInCurrent: true, statisticsIncludeUnit: true, compact: true }, chartIndex);
  container.append(createMonitorGroup({ className: 'monitor-gpu-group', kicker: `GPU ${gpu.index}`, title: gpu.name || `GPU ${gpu.index}`, titleIsUserData: true, description: '独立设备遥测', descriptionDetail: compactUuid(gpu.uuid), color, details: [frequency, power, temperature, memory, engine, media], correlationCharts, contextCharts }));
  const runtime = element('section', 'monitor-runtime-section'); const runtimeHead = element('div', 'monitor-runtime-heading'); runtimeHead.append(element('div', '', 'GPU 运行状态'), element('small', '', '实时状态与进程归属')); const stateRow = element('div', 'gpu-state-row'); details.gpuStateRow = stateRow; const processes = element('div', 'runtime-table'); details.gpuProcesses = processes; runtime.append(runtimeHead, stateRow, processes); container.append(runtime); return chartIndex;
}
function buildSystemMonitor(container, details, chartIndex) {
  const host = state.snapshot?.host || {}; const diskNames = (host.disk_io || []).map((disk) => disk.name); if (!diskNames.includes(state.selectedMonitorDisk)) state.selectedMonitorDisk = diskNames[0] || null;
  const volumes = element('section', 'monitor-runtime-section'); const volumesHead = element('div', 'monitor-runtime-heading'); volumesHead.append(element('div', '', '存储容量'), element('small', '', '逻辑卷当前用量')); const volumeTable = element('div', 'runtime-table'); details.storageVolumes = volumeTable; volumes.append(volumesHead, volumeTable); container.append(volumes);
  if (diskNames.length) { const selector = element('div', 'monitor-device-selector'); diskNames.forEach((name) => { const button = userElement('button', name === state.selectedMonitorDisk ? 'active' : '', name); button.type = 'button'; button.addEventListener('click', () => { state.selectedMonitorDisk = name; buildMonitorCharts(); }); selector.append(button); }); container.append(selector); const diskCharts = []; const diskMetric = (field) => (sample) => diskForSample(sample, state.selectedMonitorDisk)[field]; const mibRate = (field) => { const getter = diskMetric(field); return (sample) => finite(getter(sample)) ? getter(sample) / 1024 ** 2 : null; }; [{ kicker: 'READ', title: '磁盘读取', description: '物理磁盘读取吞吐', color: '#38bdf8', getter: mibRate('read_bytes_per_second'), unit: 'MB/s', decimals: 1, maximum: (samples) => niceMetricMaximum(samples, mibRate('read_bytes_per_second'), 100, 100) }, { kicker: 'WRITE', title: '磁盘写入', description: '物理磁盘写入吞吐', color: '#a78bfa', getter: mibRate('write_bytes_per_second'), unit: 'MB/s', decimals: 1, maximum: (samples) => niceMetricMaximum(samples, mibRate('write_bytes_per_second'), 100, 100) }, { kicker: 'LATENCY', title: '磁盘延迟', description: '每次读写操作平均等待', color: '#f59e0b', getter: diskMetric('latency_ms'), unit: 'ms', decimals: 1, maximum: (samples) => niceMetricMaximum(samples, diskMetric('latency_ms'), 5, 20) }].forEach((spec) => { chartIndex = appendChart(diskCharts, spec, chartIndex); }); container.append(createMonitorGroup({ className: 'monitor-system-group', kicker: 'STORAGE', title: state.selectedMonitorDisk, titleIsUserData: true, description: '容量、吞吐与延迟', color: '#38bdf8', details: [], charts: diskCharts })); }
  const networkCharts = []; const toMib = (field) => (sample) => finite(sample[field]) ? sample[field] / 1024 ** 2 : null; [{ kicker: 'DOWNLOAD', title: '网络下载', description: '主物理网卡接收速度', color: '#22c55e', getter: toMib('network_received_bytes_per_second'), unit: 'MB/s', decimals: 1, maximum: (samples) => niceMetricMaximum(samples, toMib('network_received_bytes_per_second'), 10, 10) }, { kicker: 'UPLOAD', title: '网络上传', description: '主物理网卡发送速度', color: '#60a5fa', getter: toMib('network_sent_bytes_per_second'), unit: 'MB/s', decimals: 1, maximum: (samples) => niceMetricMaximum(samples, toMib('network_sent_bytes_per_second'), 10, 10) }].forEach((spec) => { chartIndex = appendChart(networkCharts, spec, chartIndex); }); container.append(createMonitorGroup({ className: 'monitor-system-group', kicker: 'NETWORK', title: '网络吞吐', description: '主物理网卡上传与下载', color: '#22c55e', details: [], charts: networkCharts }));
  const wslMemory = monitorDetail('WSL 内存'); const wslSwap = monitorDetail('WSL Swap'); details.wslMemory = wslMemory.value; details.wslSwap = wslSwap.value; const wslCharts = []; const wslGib = (field) => (sample) => gib(sample[field]); [{ kicker: 'WSL RAM', title: 'WSL 内存', description: 'vmmemWSL 主机工作集', color: '#a78bfa', getter: wslGib('wsl_memory_used_bytes'), unit: 'GB', decimals: 1, maximum: (samples) => niceMetricMaximum(samples, wslGib('wsl_memory_used_bytes'), 4, 8) }, { kicker: 'WSL SWAP', title: 'WSL Swap', description: '运行中 WSL 发行版交换空间', color: '#f59e0b', getter: wslGib('wsl_swap_used_bytes'), unit: 'GB', decimals: 1, maximum: (samples) => niceMetricMaximum(samples, wslGib('wsl_swap_used_bytes'), 2, 4) }].forEach((spec) => { chartIndex = appendChart(wslCharts, spec, chartIndex); }); container.append(createMonitorGroup({ className: 'monitor-system-group', kicker: 'PLATFORM', title: 'WSL / Docker', description: '虚拟化运行平台资源压力', color: '#a78bfa', details: [wslMemory, wslSwap], charts: wslCharts })); const runtime = element('section', 'monitor-runtime-section'); const runtimeHead = element('div', 'monitor-runtime-heading'); runtimeHead.append(element('div', '', 'Docker 容器'), element('small', '', '资源统计每 30 秒更新')); const docker = element('div', 'runtime-table'); details.dockerContainers = docker; runtime.append(runtimeHead, docker); container.append(runtime); return chartIndex;
}
function buildMonitorCharts() {
  const container = document.querySelector('.monitor-grid'); container.replaceChildren(); state.chartSpecs = []; state.correlationControllers = []; const details = { gpus: new Map() }; state.monitorDetails = details; let chartIndex = 0;
  if (state.monitorView === 'summary') buildMonitorSummary(container, details); else if (state.monitorView === 'host') chartIndex = buildHostMonitor(container, details, chartIndex); else if (state.monitorView === 'gpu') chartIndex = buildGpuMonitor(container, details, chartIndex); else chartIndex = buildSystemMonitor(container, details, chartIndex); renderCharts();
}
function syncMonitorCharts() { const signature = gpuLayout.gpuSetSignature(state.gpus); if (signature !== state.monitorGpuSignature) { state.monitorGpuSignature = signature; if (!state.gpus.some((gpu) => gpu._uiKey === state.selectedMonitorGpuKey)) state.selectedMonitorGpuKey = state.gpus[0]?._uiKey || null; buildMonitorCharts(); } }
function setSummaryStatus(item, value, hint, severity = 'ready') { if (!item) return; item.value.textContent = value; item.hint.textContent = hint; item.status.className = `monitor-health-dot ${severity}`; }
function renderMonitorDetails(samples) {
  const details = state.monitorDetails; if (!details) return; const snapshot = state.snapshot || {}; const host = snapshot.host || {}; const memory = host.memory || {};
  if (details.summary) { let severity = 'ready'; const commitRatio = finite(memory.commit_used_bytes) && finite(memory.commit_limit_bytes) && memory.commit_limit_bytes > 0 ? memory.commit_used_bytes / memory.commit_limit_bytes : null; const lowDisk = (host.disks || []).filter((disk) => finite(disk.percent)).sort((a, b) => b.percent - a.percent)[0]; const mark = (next) => { if (next === 'danger' || (next === 'warning' && severity === 'ready')) severity = next; }; setSummaryStatus(details.summary.get('CPU'), percent(host.cpu?.load_percent), finite(host.cpu?.temperature_c) ? `${Math.round(host.cpu.temperature_c)}°C` : ui('温度不支持')); setSummaryStatus(details.summary.get('系统内存'), `${gib(memory.used_bytes)?.toFixed(1) ?? '--'} / ${gib(memory.total_bytes)?.toFixed(1) ?? '--'} GB`, commitRatio === null ? ui('提交内存不可用') : `${ui('提交')} ${Math.round(commitRatio * 100)}%`, commitRatio !== null && commitRatio >= .9 ? 'danger' : commitRatio !== null && commitRatio >= .75 ? 'warning' : 'ready'); if (commitRatio >= .75) mark(commitRatio >= .9 ? 'danger' : 'warning'); state.gpus.forEach((gpu) => { const hot = finite(gpu.temperature_c) && gpu.temperature_c >= 85 ? 'danger' : finite(gpu.temperature_c) && gpu.temperature_c >= 75 ? 'warning' : 'ready'; const gpuSeverity = gpu._stale ? 'warning' : hot; const gpuHint = gpu._stale ? `${ui('上次数据')} · ${gpu._lastSuccessAt ? formatDate(gpu._lastSuccessAt, true) : '--'}` : `${gpu.temperature_c ?? '--'}°C · ${gpu.performance_state || '--'}`; setSummaryStatus(details.summary.get(gpu._uiKey), `${percent(gpu.load_percent)} · ${mibToGib(gpu.memory_used_mib)?.toFixed(1) ?? '--'} GB`, gpuHint, gpuSeverity); mark(gpuSeverity); }); const diskSeverity = lowDisk?.percent >= 95 ? 'danger' : lowDisk?.percent >= 90 ? 'warning' : 'ready'; setSummaryStatus(details.summary.get('存储'), lowDisk ? `${Math.max(0, gib(lowDisk.total_bytes - lowDisk.used_bytes)).toFixed(1)} GB ${ui('可用')}` : '--', lowDisk?.device || ui('未检测到'), diskSeverity); mark(diskSeverity); const network = host.primary_network; setSummaryStatus(details.summary.get('网络'), network ? `${byteRate(network.received_bytes_per_second)} ↓` : '--', network ? `${byteRate(network.sent_bytes_per_second)} ↑ · ${network.name}` : ui('未检测到')); const wsl = host.wsl || {}; const runningContainers = (snapshot.docker?.containers || []).filter((item) => String(item.state).toLowerCase() === 'running').length; setSummaryStatus(details.summary.get('WSL / Docker'), wsl.running ? `${gib(wsl.memory_used_bytes)?.toFixed(1) ?? '--'} GB` : ui('未运行'), `${runningContainers} ${ui('个容器运行')}`, 'ready'); details.health.textContent = severity === 'danger' ? ui('存在异常') : severity === 'warning' ? ui('需要关注') : ui('运行正常'); details.health.className = `monitor-health-label ${severity}`; }
  if (details.cpuTemp) details.cpuTemp.textContent = finite(host.cpu?.temperature_c) ? `${Math.round(host.cpu.temperature_c)}°C` : ui('不支持'); if (details.cpuFrequency) details.cpuFrequency.textContent = finite(host.cpu?.frequency_mhz) ? `${Math.round(host.cpu.frequency_mhz)} MHz` : ui('不支持'); const used = gib(memory.used_bytes); const total = gib(memory.total_bytes); if (details.memoryUsage) details.memoryUsage.textContent = used !== null && total !== null ? `${used.toFixed(1)} / ${total.toFixed(1)} GB` : ui('不支持'); if (details.commitUsage) details.commitUsage.textContent = finite(memory.commit_used_bytes) && finite(memory.commit_limit_bytes) ? `${gib(memory.commit_used_bytes).toFixed(1)} / ${gib(memory.commit_limit_bytes).toFixed(1)} GB` : ui('不支持');
  state.gpus.forEach((gpu) => { const refs = details.gpus.get(gpu._uiKey); if (!refs) return; const gpuUsed = mibToGib(gpu.memory_used_mib); const gpuTotal = mibToGib(gpu.memory_total_mib); refs.frequency.textContent = finite(gpu.graphics_clock_mhz) ? `${Math.round(gpu.graphics_clock_mhz)} MHz` : ui('不支持'); refs.temperature.textContent = finite(gpu.temperature_c) ? `${Math.round(gpu.temperature_c)}°C` : ui('不支持'); refs.power.textContent = finite(gpu.power_w) ? `${Math.round(gpu.power_w)} W` : ui('不支持'); refs.memory.textContent = gpuUsed !== null && gpuTotal !== null ? `${gpuUsed.toFixed(1)} / ${gpuTotal.toFixed(1)} GB` : ui('不支持'); refs.engine.textContent = percent(gpu.memory_utilization_percent); refs.media.textContent = `${percent(gpu.encoder_percent)} / ${percent(gpu.decoder_percent)}`; });
  const selectedGpu = state.gpus.find((gpu) => gpu._uiKey === state.selectedMonitorGpuKey); if (details.gpuStateRow && selectedGpu) { details.gpuStateRow.replaceChildren(); const stateRows = selectedGpu._stale ? [['采样状态', `${ui('上次数据')} · ${selectedGpu._lastSuccessAt ? formatDate(selectedGpu._lastSuccessAt, true) : '--'}`]] : []; [...stateRows, ['P-State', selectedGpu.performance_state], ['风扇', finite(selectedGpu.fan_percent) ? `${Math.round(selectedGpu.fan_percent)}%` : null], ['PCIe', finite(selectedGpu.pcie_generation) && finite(selectedGpu.pcie_width) ? `Gen${selectedGpu.pcie_generation} ×${selectedGpu.pcie_width}` : null], ['时钟限制', clockEventReason(selectedGpu.clock_event_reasons)]].forEach(([label, value]) => { const item = element('span'); item.append(element('small', '', label), userElement('strong', '', value || '--')); details.gpuStateRow.append(item); }); }
  if (details.gpuProcesses) { details.gpuProcesses.replaceChildren(); const processes = selectedGpu?.processes || []; if (!processes.length) details.gpuProcesses.append(element('p', 'empty-state', '当前没有可识别的 GPU 计算进程。')); else { processes.slice(0, 12).forEach((process) => { const row = element('div', 'runtime-row'); row.title = process.name || ''; row.append(userElement('strong', '', processDisplayName(process.name)), element('span', 'mono', `PID ${process.pid ?? '--'}`), element('span', 'mono', finite(process.memory_used_mib) ? `${mibToGib(process.memory_used_mib).toFixed(1)} GB` : ui('WDDM 不提供进程显存'))); details.gpuProcesses.append(row); }); if (processes.length > 12) { const more = element('p', 'runtime-more'); more.append(document.createTextNode(`${ui('另有')} `), userElement('b', '', String(processes.length - 12)), document.createTextNode(` ${ui('个 GPU 进程')}`)); details.gpuProcesses.append(more); } } }
  if (details.storageVolumes) { details.storageVolumes.replaceChildren(); const volumes = (host.disks || []).filter((disk) => finite(disk.total_bytes)); if (!volumes.length) details.storageVolumes.append(element('p', 'empty-state', '存储容量不可用。')); else volumes.forEach((disk) => { const used = gib(disk.used_bytes); const total = gib(disk.total_bytes); const free = total !== null && used !== null ? total - used : null; const row = element('div', 'runtime-row'); row.append(userElement('strong', '', disk.device || disk.mountpoint || '--'), element('span', 'mono', `${percent(disk.percent)} ${ui('已使用')}`), element('span', 'mono', `${used?.toFixed(1) ?? '--'} / ${total?.toFixed(1) ?? '--'} GB`), element('span', 'mono', `${free?.toFixed(1) ?? '--'} GB ${ui('可用')}`)); details.storageVolumes.append(row); }); }
  const wsl = host.wsl || {}; if (details.wslMemory) details.wslMemory.textContent = finite(wsl.memory_used_bytes) ? `${gib(wsl.memory_used_bytes).toFixed(1)} GB` : ui('未运行'); if (details.wslSwap) details.wslSwap.textContent = finite(wsl.swap_used_bytes) ? `${gib(wsl.swap_used_bytes).toFixed(1)} GB` : ui('不支持'); if (details.dockerContainers) { details.dockerContainers.replaceChildren(); const containers = (snapshot.docker?.containers || []).filter((container) => String(container.state).toLowerCase() === 'running'); if (!containers.length) details.dockerContainers.append(element('p', 'empty-state', '没有运行中的 Docker 容器。')); else containers.forEach((container) => { const row = element('div', 'runtime-row'); const resources = container.resources || {}; row.append(userElement('strong', '', container.name || container.id), element('span', 'status-label ready', ui('运行中')), element('span', 'mono', resources.cpu_percent || '--'), element('span', 'mono', resources.memory_usage || '--'), element('span', 'mono', resources.network_io || '--')); details.dockerContainers.append(row); }); }
}
function renderCharts() {
  const samples = currentSeries(); const endTimeMs = Date.now(); renderMonitorDetails(samples); state.chartSpecs.forEach((spec) => {
    const scale = chartScale(spec, samples); const model = monitorChart.buildChartModel(samples, spec.getter, endTimeMs, monitorChart.windowMilliseconds(state.historyWindowMinutes), { ...scale, precision: spec.decimals ?? 0 }); const geometry = monitorChart.buildChartGeometry(model); spec.lastModel = model; updateChartCurrent(spec, model.current); Object.entries(spec.statisticRefs).forEach(([key, node]) => { node.textContent = chartValue(model[key], spec, spec.statisticsIncludeUnit === true); }); const axisValues = chartAxisValues(scale, spec); [...spec.yAxis.children].forEach((node, index) => { node.textContent = axisValues[index]; }); spec.svg.setAttribute('aria-label', model.pointCount ? `${ui(spec.title)}: ${ui('当前')} ${chartCurrentValue(model.current, spec, true)}, ${ui('峰值')} ${chartValue(model.peak, spec)}, ${ui('平均')} ${chartValue(model.average, spec)}` : `${ui(spec.title)}: ${ui('暂无采样数据')}`);
    spec.lineLayer.replaceChildren(...geometry.lines.map((segment) => svgElement('polyline', { class: 'chart-line', points: segment.map(({ x, y }) => `${x},${y}`).join(' ') })));
    spec.areaLayer.replaceChildren(...geometry.areas.map((segment) => { const points = segment.map(({ x, y }) => `${x},${y}`).join(' '); return svgElement('polygon', { class: 'chart-area', fill: `url(#${spec.gradientId})`, points: `${segment[0].x},200 ${points} ${segment.at(-1).x},200` }); }));
    spec.isolatedLayer.replaceChildren(...geometry.isolatedPoints.map((point) => svgElement('circle', { class: 'chart-isolated-point', cx: String(point.x), cy: String(point.y), r: '3' })));
    spec.noData.hidden = Boolean(model.pointCount);
    if (model.lastPoint) { spec.marker.setAttribute('cx', String(model.lastPoint.x)); spec.marker.setAttribute('cy', String(model.lastPoint.y)); spec.marker.removeAttribute('hidden'); } else spec.marker.setAttribute('hidden', '');
  });
  state.correlationControllers.forEach((controller) => controller.sync());
}

const statusLabels = { running: '已启动', stopped: '已停止', unhealthy: '异常', unknown: '状态未知' };
const sceneStatusLabels = { active: '已激活', partial: '部分启动', inactive: '未激活' };
function statusClass(value) { return value === 'running' ? 'ready' : value === 'stopped' ? 'stopped' : value === 'unhealthy' ? 'danger' : 'partial'; }
function serviceStatusLabel(service) { if (service.wsl_portproxy_error) return '映射异常'; const actual = service.status?.state || 'unknown'; if (service.desired_state === 'running' && actual === 'stopped') return '意外停止'; if (service.desired_state === 'stopped' && actual === 'running') return '外部启动'; return statusLabels[actual] || '状态未知'; }
function renderServices() {
  renderOverviewServices(); renderRegisteredServiceTable(); renderGpuServiceLabels(); renderOperationTimeline();
  const stopAll = byId('stopAllServicesButton');
  stopAll.disabled = !state.services.length || state.services.some((service) => service.operation_pending) || actionGuard.pending;
}
function runningServices() { return state.services.filter((service) => service.status.state === 'running'); }
function renderOverviewServices() {
  const list = byId('serviceList'); const running = runningServices(); list.replaceChildren(); if (!running.length) { list.append(element('p', 'empty-state', '当前没有已启动服务。')); return; }
  running.slice(0, 8).forEach((service) => { const row = element('div', 'service-row'); row.append(element('span', `service-state ${statusClass(service.status.state)}`)); const copy = element('div'); copy.append(userElement('strong', '', service.name), userOrUiElement('small', '', service.description || service.gpu_label, '无说明')); const action = element('button', 'row-action', service.ui_url ? '打开 UI' : '查看'); action.addEventListener('click', () => service.ui_url ? window.open(service.ui_url, '_blank', 'noopener,noreferrer') : navigate('environments')); row.append(copy, element('span', 'port', service.port ? `:${service.port}` : '无端口'), element('span', 'uptime', statusLabels[service.status.state] || '未知'), action); list.append(row); });
}
function renderGpuServiceLabels() {
  const running = runningServices(); document.querySelectorAll('.gpu-lane[data-gpu-key]').forEach((lane) => { const gpu = state.gpus.find((item) => item._uiKey === lane.dataset.gpuKey); if (!gpu) return; const matches = running.filter((item) => gpuLayout.serviceGpuKeys(item, state.gpus).includes(gpu._uiKey)); const names = lane.querySelector('.gpu-service-names'); const meta = lane.querySelector('.gpu-service-meta'); names.toggleAttribute('data-i18n-skip', Boolean(matches.length)); meta.toggleAttribute('data-i18n-skip', Boolean(matches.length)); names.textContent = matches.length ? matches.map((item) => item.name).join(' · ') : '没有已启动服务'; meta.textContent = matches.length ? matches.map((item) => item.name).join(ui('；')) : 'GPU 标签下没有已启动服务'; });
}
function renderRegisteredServiceTable() {
  const rows = byId('registeredServiceRows'); rows.replaceChildren(); const query = byId('serviceSearch').value.trim().toLowerCase(); const filtered = state.services.filter((item) => (state.serviceFilter === 'all' || item.status.state === state.serviceFilter) && [item.name, item.description, item.gpu_label, item.port].join(' ').toLowerCase().includes(query));
  if (!filtered.length) { rows.append(element('p', 'empty-state', state.services.length ? '没有符合筛选条件的服务。' : '尚未添加服务。')); return; }
  filtered.forEach((service, index) => { const row = element('div', 'table-row'); const title = element('span'); const logo = userElement('i', 'env-logo', String(index + 1)); const copy = element('span'); copy.append(userElement('b', '', service.name), userElement('small', '', service.script_path)); title.append(logo, copy); const status = element('i', `status-label ${service.wsl_portproxy_error ? 'danger' : statusClass(service.status.state)}`, service.busy ? '操作中' : serviceStatusLabel(service)); status.title = service.wsl_portproxy_error || (service.status.checked_at ? `状态检查于 ${formatDate(service.status.checked_at, true)}` : '尚未检查状态'); const actions = element('span', 'row-buttons'); const check = element('button', '', '深度检查'); check.disabled = service.operation_pending || actionGuard.pending; check.addEventListener('click', () => runServiceStatusCheck(service)); actions.append(check); ['start', 'stop', 'restart'].forEach((action) => { const button = element('button', '', { start: '启动', stop: '停止', restart: '重启' }[action]); button.disabled = service.operation_pending || actionGuard.pending; button.addEventListener('click', () => runServiceAction(service, action)); actions.append(button); }); if (service.ui_url) { const ui = element('button', '', 'UI'); ui.addEventListener('click', () => window.open(service.ui_url, '_blank', 'noopener,noreferrer')); actions.append(ui); } const edit = element('button', 'icon-only', '编辑'); edit.disabled = service.operation_pending || actionGuard.pending; edit.addEventListener('click', () => openServiceDialog(service)); const remove = element('button', 'icon-only', '删除'); remove.disabled = service.operation_pending || actionGuard.pending; remove.addEventListener('click', () => deleteService(service)); actions.append(edit, remove); row.append(title, userOrUiElement('span', '', service.description, '—'), userOrUiElement('span', '', service.gpu_label, '未标注'), element('span', 'mono', service.port ? String(service.port) : '—'), status, actions); rows.append(row); });
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
    const panel = element('article', `scene-panel${scene.state === 'active' ? ' selected' : ''}${scene.is_default ? ' scene-default' : ''}`); panel.dataset.sceneId = scene.id; panel.draggable = !scene.busy && !actionGuard.pending;
    panel.addEventListener('dragstart', (event) => { if (!panel.draggable) return event.preventDefault(); draggedSceneId = scene.id; panel.classList.add('dragging'); panel.setAttribute('aria-grabbed', 'true'); event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', scene.id); });
    panel.addEventListener('dragover', (event) => { if (!draggedSceneId || draggedSceneId === scene.id) return; event.preventDefault(); clearSceneDropMarkers(); const position = sceneDropPosition(event, panel); panel.classList.add(position.after ? 'drop-after' : 'drop-before'); if (position.horizontal) panel.classList.add('drop-horizontal'); });
    panel.addEventListener('drop', (event) => { event.preventDefault(); const after = panel.classList.contains('drop-after'); const sourceId = draggedSceneId || event.dataTransfer.getData('text/plain'); document.querySelector(`[data-scene-id="${sourceId}"]`)?.classList.remove('dragging'); clearSceneDropMarkers(); draggedSceneId = null; moveSceneCard(sourceId, scene.id, after); });
    panel.addEventListener('dragend', () => { draggedSceneId = null; panel.classList.remove('dragging'); panel.setAttribute('aria-grabbed', 'false'); clearSceneDropMarkers(); });
    const top = element('div', 'scene-panel-top'); const topActions = element('span', 'scene-panel-meta'); const handle = element('button', 'scene-drag-handle', '⠿'); handle.type = 'button'; handle.title = '拖动调整场景位置'; handle.setAttribute('aria-label', `拖动调整 ${scene.name} 的位置`); const sceneStatusClass = scene.state === 'active' ? 'scene-status' : scene.state === 'inactive' ? 'scene-inactive' : ''; topActions.append(element('i', sceneStatusClass, sceneStatusLabels[scene.state] || '状态未知')); topActions.append(handle); top.append(element('span', '', `场景 ${String(index + 1).padStart(2, '0')}`), topActions);
    let cardHeader = top;
    if (scene.is_default || scene.state === 'active') {
      const banners = element('div', 'scene-state-banners');
      const combined = scene.is_default && scene.state === 'active';
      const banner = element('div', scene.is_default ? `scene-default-banner${combined ? ' scene-combined-banner' : ''}` : 'scene-active-banner');
      const bannerMeta = element('div', 'scene-banner-meta');
      bannerMeta.append(element('span', '', scene.is_default ? 'AXIS 启动时自动切换' : '服务组合正在生效'), handle);
      banner.append(element('strong', '', combined ? '默认场景 · 已激活' : scene.is_default ? '默认启动场景' : '当前已激活场景'), bannerMeta);
      banners.append(banner); cardHeader = banners;
    }
    panel.append(cardHeader, userElement('h2', '', scene.name), userOrUiElement('p', '', scene.description, '无说明'));
    const map = element('div', 'scene-map'); const sceneServices = scene.services || scene.service_ids.map((id, order) => ({ id, name: scene.service_names[order], ui_url: '', status: { state: 'unknown' } })); if (!sceneServices.length) map.append(element('div', '', '此场景不启动任何服务'));
    sceneServices.forEach((service, order) => { const item = element('div'); item.append(element('span', '', `启动顺序 ${order + 1}`), userElement('strong', '', service.name)); const meta = element('small', 'scene-service-meta'); const status = element('i', 'scene-service-status'); status.append(element('i', `service-state ${statusClass(service.status.state)}`), document.createTextNode(service.busy ? '操作中' : serviceStatusLabel(service))); meta.append(status); item.append(meta); if (service.ui_url) { const uiButton = element('button', 'scene-ui-link', '打开 UI ↗'); uiButton.type = 'button'; uiButton.addEventListener('click', () => window.open(service.ui_url, '_blank', 'noopener,noreferrer')); item.append(uiButton); } map.append(item); });
    const actions = element('div', 'scene-card-actions');
    const utilities = element('div', 'scene-utility-actions');
    const reorder = element('span', 'scene-reorder-controls');
    const up = iconButton('上移场景', 'arrow-up'); up.disabled = index === 0 || scene.busy || actionGuard.pending; up.addEventListener('click', () => moveSceneByOffset(scene.id, -1));
    const down = iconButton('下移场景', 'arrow-down'); down.disabled = index === state.scenes.length - 1 || scene.busy || actionGuard.pending; down.addEventListener('click', () => moveSceneByOffset(scene.id, 1));
    reorder.append(up, down);
    const details = iconButton('查看详细说明', 'info'); details.addEventListener('click', () => openSceneDetails(scene));
    const defaultButton = iconButton(scene.is_default ? '取消默认场景' : '设为默认场景', 'star', scene.is_default ? 'is-default' : ''); defaultButton.disabled = scene.busy || actionGuard.pending; defaultButton.addEventListener('click', () => setDefaultScene(scene));
    const edit = iconButton('编辑场景', 'edit'); edit.disabled = scene.busy || actionGuard.pending; edit.addEventListener('click', () => openSceneDialog(scene));
    const remove = iconButton('删除场景', 'trash', 'danger'); remove.disabled = scene.busy || actionGuard.pending; remove.addEventListener('click', () => deleteScene(scene));
    utilities.append(reorder, details, defaultButton, edit, remove);
    const activateLabel = scene.state === 'active' ? '重新切换' : '切换到此场景';
    const activate = labeledIconButton(activateLabel, 'switch', 'button primary scene-activate-button'); activate.disabled = scene.busy || actionGuard.pending; activate.addEventListener('click', () => activateScene(scene));
    actions.append(utilities, activate); panel.append(map, actions); list.append(panel);
  });
  const active = state.scenes.find((item) => item.state === 'active'); dataText('activeSceneName', active ? `${active.name} · ${ui('已激活')}` : '', '场景未完整激活'); dataText('activeSceneSummary', active?.description || '', active ? ui(`包含 ${active.service_ids.length} 个服务`) : '当前服务状态不完整符合任何场景。');
  renderOperationTimeline();
}

async function runServiceAction(service, action) { const owner = actionGuard.acquire(); if (!owner) return showToast('已有操作正在执行'); try { const result = await api(`/registered-services/${service.id}/actions`, { method: 'POST', body: { action } }); showToast('服务操作已开始'); await pollOperation(result.operation_id); } catch (error) { showToast(error.message); } finally { actionGuard.release(owner); await refreshServicesAndScenes(); } }
async function runServiceStatusCheck(service) { const owner = actionGuard.acquire(); if (!owner) return showToast('已有操作正在执行'); try { await api(`/registered-services/${service.id}/status`, { method: 'POST' }); showToast('状态检查完成'); } catch (error) { showToast(error.message); } finally { actionGuard.release(owner); await refreshServicesAndScenes(); } }
async function stopAllServices() { if (!state.services.length || !confirmUi('停止所有已登记服务？管理器将依次调用每个服务脚本的 stop 动作。')) return; const owner = actionGuard.acquire(); if (!owner) return showToast('已有操作正在执行'); try { const result = await api('/registered-services/actions/stop-all', { method: 'POST' }); openStopAllProgress(result.operation_id); showToast('正在停止全部服务'); await pollOperation(result.operation_id, (item) => renderStopAllProgress(item), null); } catch (error) { showToast(error.message); if (byId('sceneProgressDialog').open) byId('sceneProgressDialog').close(); } finally { sceneProgressOperationId = null; actionGuard.release(owner); await refreshServicesAndScenes(); } }
async function activateScene(scene) { const owner = actionGuard.acquire(); if (!owner) return showToast('已有操作正在执行'); try { const result = await api(`/scenes/${scene.id}/activate`, { method: 'POST' }); openSceneProgress(scene, result.operation_id); await pollOperation(result.operation_id, (item) => renderSceneProgress(scene, item), null); } catch (error) { showToast(error.message); if (byId('sceneProgressDialog').open) byId('sceneProgressDialog').close(); } finally { sceneProgressOperationId = null; sceneProgressExpectedTotal = null; actionGuard.release(owner); await refreshServicesAndScenes(); } }
async function setDefaultScene(scene) { if (!scene.is_default && !confirmUi(`将“${scene.name}”设为默认场景？AXIS 下次启动时会自动切换到该场景。`)) return; try { await api(`/scenes/${scene.id}/default`, { method: scene.is_default ? 'DELETE' : 'PUT' }); showToast(scene.is_default ? '已取消默认场景' : '已设置默认场景'); await refreshServicesAndScenes(); } catch (error) { showToast(error.message); } }
async function pollOperation(id, onUpdate = null, maxAttempts = 240) { for (let i = 0; maxAttempts === null || i < maxAttempts; i += 1) { await new Promise((resolve) => setTimeout(resolve, 1000)); const item = await api(`/operations/${id}`, { timeout: ACTION_TIMEOUT_MS }); if (onUpdate) onUpdate(item); if (!['queued', 'running'].includes(item.status)) { showToast(item.status === 'succeeded' ? '操作成功' : item.status === 'interrupted' ? '操作已终止' : item.error_summary || '操作失败'); await refreshLogs(); return item; } } throw new ApiError(0, 'operation_timeout', '操作仍在后台执行，请到日志中心查看。'); }

function openOperationProgress(operationId, title, summary, waiting, cancelLabel, closeLabel) {
  sceneProgressOperationId = operationId; progressCancelLabel = cancelLabel || '';
  text('sceneProgressTitle', title); text('sceneProgressSummary', summary); text('sceneProgressPercent', '0%'); text('sceneProgressCurrent', waiting);
  byId('sceneProgressBar').style.width = '0%'; byId('sceneProgressLog').replaceChildren(element('li', '', '等待第一条服务操作记录。'));
  byId('cancelSceneSwitchButton').hidden = !cancelLabel; byId('cancelSceneSwitchButton').disabled = false; if (cancelLabel) text('cancelSceneSwitchButton', cancelLabel); byId('closeSceneProgressButton').hidden = true; text('closeSceneProgressButton', closeLabel);
  const dialog = byId('sceneProgressDialog'); if (!dialog.open) dialog.showModal();
}
function openSceneProgress(scene, operationId) { sceneProgressExpectedTotal = state.services.filter((service) => !scene.service_ids.includes(service.id) && service.status.state === 'running').length + scene.service_ids.filter((serviceId) => state.services.find((service) => service.id === serviceId)?.status.state !== 'running').length; openOperationProgress(operationId, `正在切换到 ${scene.name}`, '管理器正在按顺序停止和启动服务。', '等待第一项服务操作', '终止切换并返回', '返回工作场景'); }
function openStopAllProgress(operationId) { openOperationProgress(operationId, '正在停止全部服务', '管理器正在确认需要停止的服务并按顺序执行。', '正在确认需要停止的服务', '', '返回服务列表'); }
function renderOperationProgress(operation, total, terminalCopy) {
  const steps = operation.steps || []; const finished = steps.filter((step) => step.status !== 'running').length; const terminal = !['queued', 'running'].includes(operation.status); const knownTotal = Number.isInteger(total) && total >= 0; const denominator = knownTotal ? Math.max(total, steps.length) : null; const progress = terminal ? 100 : denominator > 0 ? Math.min(99, Math.round((finished / denominator) * 100)) : 0;
  text('sceneProgressPercent', `${progress}%`); byId('sceneProgressBar').style.width = `${progress}%`;
  const current = [...steps].reverse().find((step) => step.status === 'running');
  if (current) text('sceneProgressCurrent', `${current.action === 'start' ? '正在启动' : '正在停止'} · ${targetName('service', current.target_id)}`);
  else if (terminal) text('sceneProgressCurrent', operation.status === 'succeeded' ? terminalCopy.successCurrent : operation.status === 'interrupted' ? terminalCopy.interruptedCurrent : operation.error_summary || terminalCopy.failureCurrent);
  const log = byId('sceneProgressLog'); log.replaceChildren();
  if (!steps.length) log.append(element('li', '', terminal ? '没有需要执行的服务步骤。' : knownTotal && total === 0 ? '无需执行服务步骤，正在确认最终状态。' : '等待第一条服务操作记录。'));
  steps.forEach((step) => { const item = element('li', step.status); item.append(element('time', '', formatDate(step.started_at)), element('span', '', `${ui(step.action === 'start' ? '启动' : '停止')} ${targetName('service', step.target_id)}`), element('em', '', step.status === 'running' ? '进行中' : step.status === 'succeeded' ? '成功' : step.status === 'interrupted' ? '已终止' : '失败')); log.append(item); });
  log.scrollTop = log.scrollHeight;
  if (terminal) { text('sceneProgressTitle', operation.status === 'succeeded' ? terminalCopy.successTitle : operation.status === 'interrupted' ? terminalCopy.interruptedTitle : terminalCopy.failureTitle); text('sceneProgressSummary', operation.error_summary || (operation.status === 'succeeded' ? terminalCopy.successSummary : terminalCopy.failureSummary)); byId('cancelSceneSwitchButton').hidden = true; byId('closeSceneProgressButton').hidden = false; }
}
function renderSceneProgress(scene, operation) { const total = Number.isInteger(operation.total_steps) ? operation.total_steps : sceneProgressExpectedTotal; renderOperationProgress(operation, total, { successCurrent: '场景切换完成', interruptedCurrent: '场景切换已终止', failureCurrent: '场景切换失败', successTitle: `${scene.name} 已就绪`, interruptedTitle: '切换已终止', failureTitle: '场景切换未完成', successSummary: '所有服务均已达到目标状态。', failureSummary: '请在日志中心查看失败步骤。' }); }
function renderStopAllProgress(operation) { renderOperationProgress(operation, operation.total_steps, { successCurrent: '全部服务已停止', interruptedCurrent: '停止操作已终止', failureCurrent: '停止全部服务失败', successTitle: '全部服务已停止', interruptedTitle: '停止操作已终止', failureTitle: '停止全部服务未完成', successSummary: '所有已登记服务均已停止。', failureSummary: '请在日志中心查看失败步骤。' }); }
async function cancelSceneSwitch() {
  const operationId = sceneProgressOperationId; if (!operationId) return;
  const button = byId('cancelSceneSwitchButton'); button.disabled = true; text('cancelSceneSwitchButton', '正在提交终止请求…');
  try { await api(`/operations/${operationId}/cancel`, { method: 'POST' }); byId('sceneProgressDialog').close(); showToast('终止请求已提交，当前步骤结束后停止'); }
  catch (error) { button.disabled = false; text('cancelSceneSwitchButton', progressCancelLabel); showToast(error.message); }
}

function updateServicePortproxyFields() { byId('servicePortproxyFields').hidden = !byId('serviceWslPortproxyEnabled').checked; }
function openServiceDialog(service = null) { byId('serviceForm').reset(); text('serviceFormError', ''); text('serviceDialogTitle', service ? '编辑服务' : '添加服务'); byId('serviceId').value = service?.id || ''; byId('serviceName').value = service?.name || ''; byId('serviceDescription').value = service?.description || ''; byId('serviceScriptPath').value = service?.script_path || ''; byId('serviceGpu').value = service?.gpu_label || ''; byId('servicePort').value = service?.port || ''; byId('serviceUiUrl').value = service?.ui_url || ''; byId('serviceHealthUrl').value = service?.health_url || ''; byId('serviceHealthExpect').value = service?.health_expect || ''; byId('serviceWslPortproxyEnabled').checked = Boolean(service?.wsl_portproxy_enabled); byId('serviceWslDistro').value = service?.wsl_distro || 'Ubuntu-22.04'; byId('serviceWslListenAddress').value = service?.wsl_listen_address || '0.0.0.0'; byId('serviceWslListenPort').value = service?.wsl_listen_port || ''; byId('serviceWslConnectPort').value = service?.wsl_connect_port || ''; updateServicePortproxyFields(); byId('serviceDialog').showModal(); }
async function saveService(event) { event.preventDefault(); const id = byId('serviceId').value; const payload = { name: byId('serviceName').value.trim(), description: byId('serviceDescription').value.trim(), script_path: byId('serviceScriptPath').value.trim(), gpu_label: byId('serviceGpu').value.trim(), port: byId('servicePort').value ? Number(byId('servicePort').value) : null, ui_url: byId('serviceUiUrl').value.trim(), health_url: byId('serviceHealthUrl').value.trim(), health_expect: byId('serviceHealthExpect').value.trim(), wsl_portproxy_enabled: byId('serviceWslPortproxyEnabled').checked, wsl_distro: byId('serviceWslDistro').value.trim(), wsl_listen_address: byId('serviceWslListenAddress').value.trim(), wsl_listen_port: byId('serviceWslListenPort').value ? Number(byId('serviceWslListenPort').value) : null, wsl_connect_port: byId('serviceWslConnectPort').value ? Number(byId('serviceWslConnectPort').value) : null }; try { await api(id ? `/registered-services/${id}` : '/registered-services', { method: id ? 'PUT' : 'POST', body: payload }); byId('serviceDialog').close(); showToast(id ? '服务已更新' : '服务已添加'); await refreshServicesAndScenes(); } catch (error) { text('serviceFormError', error.message); } }
async function deleteService(service) { if (!confirmUi(`删除服务“${service.name}”的登记记录？原始脚本和服务不会被删除。`)) return; try { await api(`/registered-services/${service.id}`, { method: 'DELETE' }); showToast('服务登记已删除'); await refreshServicesAndScenes(); } catch (error) { showToast(error.message); } }

function openSceneDetails(scene) { dataText('sceneDetailTitle', scene.name, '场景名称'); dataText('sceneDetailIntro', scene.description || '', '无说明'); dataText('sceneDetailBody', scene.detailed_description || '', '暂无详细说明。'); byId('sceneDetailDialog').showModal(); }
function openSceneDialog(scene = null) { byId('sceneForm').reset(); text('sceneFormError', ''); text('sceneDialogTitle', scene ? '编辑场景' : '添加场景'); byId('sceneId').value = scene?.id || ''; byId('sceneName').value = scene?.name || ''; byId('sceneDescription').value = scene?.description || ''; byId('sceneDetailedDescription').value = scene?.detailed_description || ''; byId('sceneDefaultGeneration').checked = Boolean(scene?.is_default_generation); const selected = scene?.service_ids || []; const container = byId('sceneServiceChoices'); container.replaceChildren(); const ordered = [...selected.map((id) => state.services.find((item) => item.id === id)).filter(Boolean), ...state.services.filter((item) => !selected.includes(item.id))]; ordered.forEach((service) => { const row = element('div', 'scene-service-choice'); row.dataset.id = service.id; const label = element('label'); label.dataset.i18nSkip = ''; const checkbox = element('input'); checkbox.type = 'checkbox'; checkbox.checked = selected.includes(service.id); label.append(checkbox, document.createTextNode(service.name)); const controls = element('span'); const up = iconButton('上移服务', 'arrow-up'); up.addEventListener('click', () => row.previousElementSibling && container.insertBefore(row, row.previousElementSibling)); const down = iconButton('下移服务', 'arrow-down'); down.addEventListener('click', () => row.nextElementSibling && container.insertBefore(row.nextElementSibling, row)); controls.append(up, down); row.append(label, controls); container.append(row); }); byId('sceneDialog').showModal(); }
async function saveScene(event) { event.preventDefault(); const id = byId('sceneId').value; const serviceIds = [...byId('sceneServiceChoices').children].filter((row) => row.querySelector('input').checked).map((row) => row.dataset.id); const payload = { name: byId('sceneName').value.trim(), description: byId('sceneDescription').value.trim(), detailed_description: byId('sceneDetailedDescription').value.trim(), is_default_generation: byId('sceneDefaultGeneration').checked, service_ids: serviceIds }; try { await api(id ? `/scenes/${id}` : '/scenes', { method: id ? 'PUT' : 'POST', body: payload }); byId('sceneDialog').close(); showToast(id ? '场景已更新' : '场景已添加'); await refreshServicesAndScenes(); } catch (error) { text('sceneFormError', error.message); } }
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
function defaultSceneOperation(event) { const summary = event.summary || {}; return { id: `audit-${event.id}`, kind: 'scene_default', target_id: summary.scene_id, target_name: summary.name, action: event.event === 'management.scene.default.set' ? 'set_default' : 'clear_default', status: event.result === 'success' ? 'succeeded' : 'failed', result: event.result === 'success' ? 'success' : 'failed', requested_by: summary.requested_by, created_at: event.created_at, steps: [] }; }
function operationTargetName(operation) { return operation.target_name || targetName(operation.kind, operation.target_id); }
function operationStatusLabel(status) { return { succeeded: '成功', failed: '失败', interrupted: '已终止', queued: '等待执行', running: '执行中' }[status] || '状态未知'; }
function operationActionLabel(action) { return { start: '启动', stop: '停止', restart: '重启', activate: '切换场景', stop_all: '停止全部服务', set_default: '设为默认', clear_default: '取消默认' }[action] || action; }
function operationPhaseLabel(phase) { return { stop_unselected: '停止未选服务', start_selected: '启动目标服务' }[phase] || phase; }
function operationResultLabel(result) { return { success: '成功', failed: '失败', partial: '部分启动', stop_failed: '停止失败', cancelled: '已终止' }[result] || result; }
function operationActor(operation) { return String(operation.requested_by || '').trim() || ui('未知账号'); }
function renderOperationTimeline() { const timeline = byId('auditTimeline'); timeline.replaceChildren(); const recent = state.operations.filter((item) => Date.now() - new Date(item.created_at).getTime() <= 30 * 60 * 1000).slice(0, 5); if (!recent.length) { timeline.append(element('li', 'empty-state', '最近 30 分钟没有服务或场景操作。')); return; } recent.forEach((operation) => { const item = element('li'); const success = operation.status === 'succeeded'; item.append(element('span', `event-dot ${success ? 'good' : 'warn'}`)); const copy = element('div'); const eventLabel = operation.kind === 'scene_default' ? '默认场景' : operation.kind === 'scene' ? '场景切换' : '服务操作'; copy.append(element('strong', '', `${ui(eventLabel)} · ${operationTargetName(operation)}`), element('small', '', `${formatDate(operation.created_at, true)} · ${ui(operationActionLabel(operation.action))} · ${ui(operationStatusLabel(operation.status))} · ${ui('操作账号')} ${operationActor(operation)}`)); item.append(copy); timeline.append(item); }); }
function renderOperations() { const list = byId('operationList'); list.replaceChildren(); if (!state.operations.length) { list.append(element('p', 'empty-state', '暂无服务或场景操作。')); return; } state.operations.forEach((item) => { const failed = ['failed', 'interrupted'].includes(item.status); const row = element('article', `operation-row${failed ? ' operation-failed' : ''}`); const copy = element('div'); const summary = element('div', 'operation-summary'); summary.append(element('small', '', item.error_summary || ui(operationResultLabel(item.result)) || ui('等待执行')), element('span', 'operation-actor', `${ui('操作账号')} · ${operationActor(item)}`)); const kindLabel = item.kind === 'scene_default' ? '默认场景' : item.kind === 'scene' ? '场景' : '服务'; copy.append(element('strong', '', `${ui(kindLabel)} · ${operationTargetName(item)} · ${ui(operationActionLabel(item.action))}`), summary); row.append(copy, element('span', `status-label ${item.status === 'succeeded' ? 'ready' : failed ? 'danger' : 'partial'}`, ui(operationStatusLabel(item.status))), element('time', '', formatDate(item.created_at, true))); const steps = element('ol', 'operation-steps'); (item.steps || []).forEach((step) => { const entry = element('li', step.status === 'failed' ? 'failed' : ''); entry.append(element('b', '', `${step.sequence}. ${ui(operationPhaseLabel(step.phase))} · ${targetName('service', step.target_id)} · ${ui(operationActionLabel(step.action))}`), element('span', 'step-status', ui(operationStatusLabel(step.status))), element('small', '', `${formatDate(step.started_at, true)} → ${step.finished_at ? formatDate(step.finished_at, true) : ui('进行中')}`)); if (step.error_summary) entry.append(element('code', '', step.error_summary)); steps.append(entry); }); if (steps.childNodes.length) row.append(steps); list.append(row); }); }
function videoJobStageLabel(job) { if (job.status === 'running' && job.progress?.state === 'queued') return window.axisI18n.language === 'zh' ? `ComfyUI 排队 ${job.progress.queue_position || ''}`.trim() : `ComfyUI queue ${job.progress.queue_position || ''}`.trim(); const label = { queued: '等待调度', waiting_for_opencode_idle: '等待 OpenCode 当前响应结束', waiting_for_ninfer_idle: '等待 NInfer 空闲', waiting_for_memory: '等待内存释放', switching_to_video: '切换生成场景', checking_comfy: '检查 ComfyUI', submitting: '提交工作流', running: 'ComfyUI 生成中', collecting_output: '收集输出', publishing_output: '复制到共享目录', restoring_scene: '恢复原场景', restoring_code: '恢复原场景', verifying_ninfer: '恢复原场景', callback_pending: '回调 OpenCode', succeeded: '已完成', failed: '失败', cancelled: '已取消' }[job.status]; return label ? ui(label) : job.status; }
function videoJobProgress(job) { if (['succeeded', 'failed', 'cancelled'].includes(job.status)) return 100; if (job.status === 'running') return job.progress?.state === 'queued' ? 45 : 55; return { queued: 0, waiting_for_opencode_idle: 4, waiting_for_ninfer_idle: 8, waiting_for_memory: 12, switching_to_video: 25, checking_comfy: 35, submitting: 40, collecting_output: 84, publishing_output: 88, restoring_scene: 92, restoring_code: 94, verifying_ninfer: 96, callback_pending: 98 }[job.status] ?? 0; }
function videoJobTitle(job) { return String(job.video_spec?.title || '').trim() || (window.axisI18n.language === 'zh' ? '视频任务' : 'Video job'); }
function videoJobOutputPath(job) { return String(job.output_path || '').trim() || (window.axisI18n.language === 'zh' ? '等待生成' : 'Pending generation'); }
function videoJobTiming(job) { const started = Date.parse(job.started_at); if (!Number.isFinite(started)) return { started: window.axisI18n.language === 'zh' ? '尚未开始' : 'Not started', duration: '--' }; const terminal = ['succeeded', 'failed', 'cancelled'].includes(job.status); const ended = Date.parse(job.finished_at || (terminal ? job.updated_at : new Date().toISOString())); return { started: formatDate(job.started_at, true), duration: formatDuration(ended - started) }; }
function videoJobSpecLabel(job) { const spec = job.video_spec || {}; const parts = []; if (Number.isFinite(spec.duration_seconds)) parts.push(`${spec.duration_seconds.toFixed(spec.duration_seconds < 10 ? 2 : 1)}s`); if (Number.isFinite(spec.width) && Number.isFinite(spec.height)) parts.push(`${spec.width}×${spec.height}`); if (Number.isFinite(spec.frames)) parts.push(`${spec.frames} ${window.axisI18n.language === 'zh' ? '帧' : 'frames'}`); if (Number.isFinite(spec.fps)) parts.push(`${spec.fps} fps`); if (Number.isFinite(spec.steps)) parts.push(`${spec.steps} ${window.axisI18n.language === 'zh' ? '步' : 'steps'}`); return parts; }
function appendVideoJobRealtime(container, job) { if (job.status !== 'running') return; const realtime = job.progress?.realtime; const box = element('div', 'video-job-realtime'); const nodeName = String(realtime?.node_name || '').trim(); const nodeId = String(realtime?.node_id || '').trim(); const node = nodeName && nodeName !== nodeId ? nodeName : ''; if (realtime?.available && realtime.kind === 'sampling' && Number.isFinite(realtime.value) && Number.isFinite(realtime.max) && Number.isFinite(realtime.percent)) { const head = element('span'); head.append(element('small', '', node ? `${window.axisI18n.language === 'zh' ? '实时采样' : 'Live sampling'} · ${node}` : (window.axisI18n.language === 'zh' ? '实时采样' : 'Live sampling')), element('b', '', `${realtime.value}/${realtime.max} · ${realtime.percent.toFixed(1)}%`)); const track = element('i'); track.append(element('b')); track.firstChild.style.width = `${Math.min(100, Math.max(0, realtime.percent))}%`; box.append(head, track); } else if (realtime?.available) { box.append(element('span', '', node ? `${window.axisI18n.language === 'zh' ? '当前节点' : 'Current node'} · ${node}` : (window.axisI18n.language === 'zh' ? '当前节点' : 'Current node'))); } else { const waiting = realtime?.kind === 'connecting'; box.append(element('span', 'video-job-realtime-unavailable', waiting ? (window.axisI18n.language === 'zh' ? '正在连接实时进度…' : 'Connecting live progress…') : (window.axisI18n.language === 'zh' ? '实时进度暂不可用' : 'Live progress unavailable'))); } container.append(box); }
function renderVideoJobs() { const list = byId('videoJobList'); if (!list) return; list.replaceChildren(); const queued = Number(state.videoQueueSummary?.queued_segments) || 0; const nonterminal = Number(state.videoQueueSummary?.nonterminal_segments) || 0; text('videoJobNavCount', String(nonterminal)); text('videoQueuedSegments', window.axisI18n.language === 'zh' ? `等待处理：${queued} 段` : `Waiting: ${queued} segments`); if (!state.videoJobs.length) { list.append(element('p', 'empty-state', '尚无视频任务。')); return; } state.videoJobs.forEach((job) => { const failed = job.status === 'failed'; const row = element('article', `operation-row video-job-row${failed ? ' operation-failed' : ''}`); const copy = element('div'); const segment = job.batch_size > 1 ? (window.axisI18n.language === 'zh' ? ` · 第 ${job.batch_index} / ${job.batch_size} 段` : ` · Segment ${job.batch_index} / ${job.batch_size}`) : ''; const title = userElement('strong', '', `${videoJobTitle(job)}${segment}`); const specs = element('div', 'video-job-specs'); videoJobSpecLabel(job).forEach((value) => specs.append(element('span', '', value))); const details = element('div', 'operation-summary'); const statusDetail = job.error_summary; const route = `${ui('生成场景')} ${job.generation_scene_name || '-'} · ${ui('原场景')} ${job.original_scene_name || ui('等待记录')}`; details.append(userElement('small', '', `${route} · ${window.axisI18n.language === 'zh' ? '输出文件' : 'Output'} `)); if (job.shared_output_path) { const outputLink = userElement('a', 'video-job-output-link', videoJobOutputPath(job)); outputLink.href = fileContentUrl(job.shared_output_path); outputLink.target = '_blank'; outputLink.rel = 'noopener'; details.append(outputLink); } else { details.append(userElement('small', '', videoJobOutputPath(job))); } if (statusDetail) details.append(userElement('small', '', `· ${statusDetail}`)); const progress = videoJobProgress(job); const progressBox = element('div', 'video-job-progress'); const progressHead = element('span'); progressHead.append(element('small', '', window.axisI18n.language === 'zh' ? '阶段进度' : 'Stage progress'), element('b', '', `${progress}%`)); const progressTrack = element('i'); progressTrack.append(element('b')); progressTrack.firstChild.style.width = `${progress}%`; progressBox.append(progressHead, progressTrack); appendVideoJobRealtime(progressBox, job); copy.append(title, specs, details, progressBox); const badge = element('span', `status-label ${job.status === 'succeeded' ? 'ready' : failed || job.status === 'cancelled' ? 'danger' : 'partial'}`, videoJobStageLabel(job)); const timing = videoJobTiming(job); const timeBox = element('div', 'video-job-time'); timeBox.append(element('small', '', window.axisI18n.language === 'zh' ? '开始时间' : 'Started'), userElement('b', '', timing.started), element('small', '', window.axisI18n.language === 'zh' ? '持续时间' : 'Duration'), userElement('b', '', timing.duration)); row.append(copy, badge, timeBox); if (!['callback_pending', 'callback_delivered'].includes(job.phase) && !['callback_pending', 'callback_delivered', 'succeeded', 'failed', 'cancelled'].includes(job.status)) { const cancel = labeledIconButton('取消任务', 'x', 'button secondary video-job-cancel'); cancel.addEventListener('click', () => cancelVideoJob(job)); row.append(cancel); } list.append(row); }); }
async function cancelVideoJob(job) { if (!confirmUi(`取消视频任务“${videoJobTitle(job)}”？若已在 ComfyUI 执行，将请求中断并恢复原场景。`)) return; try { await api(`/video-jobs/${job.id}/cancel`, { method: 'POST' }); showToast('视频任务取消请求已提交'); await refreshVideoJobs(); } catch (error) { showToast(error.message); } }

function automaticTaskStatusLabel(status) { return ui({ pending: '未执行', running: '执行中', succeeded: '已完成', failed: '失败' }[status] || '状态未知'); }
function automaticTaskStatusClass(status) { return status === 'succeeded' ? 'ready' : status === 'failed' ? 'danger' : 'partial'; }
function renderAutomaticTasks() {
  const list = byId('automaticTaskList'); if (!list) return; list.replaceChildren();
  const pending = Number(state.automaticTaskSummary.pending) || 0; const running = Number(state.automaticTaskSummary.running) || 0;
  text('automaticTaskNavCount', String(pending));
  text('automaticTaskQueueStatus', window.axisI18n.language === 'zh' ? (running ? `执行中：${running} · 未执行：${pending}` : `未执行：${pending} 项`) : (running ? `${running} running · ${pending} pending` : `${pending} pending`));
  if (!state.automaticTasks.length) { list.append(element('p', 'empty-state', ui('尚无自动任务。'))); return; }
  const pendingTasks = state.automaticTasks.filter((task) => task.status === 'pending');
  state.automaticTasks.forEach((task) => {
    const runningTask = task.status === 'running'; const failed = task.status === 'failed';
    const row = element('article', `operation-row automatic-task-row${runningTask ? ' automatic-task-running' : ''}${failed ? ' operation-failed' : ''}`);
    const copy = element('div', 'automatic-task-copy'); copy.append(userElement('strong', '', task.title), userElement('p', 'automatic-task-content', task.content));
    const detail = task.error_summary || task.result_summary;
    if (detail) copy.append(userElement('small', failed ? 'automatic-task-error' : 'automatic-task-result', detail));
    const badge = element('span', `status-label ${automaticTaskStatusClass(task.status)}`, automaticTaskStatusLabel(task.status));
    const timing = element('div', 'automatic-task-time'); timing.append(element('small', '', ui(task.finished_at ? '完成时间' : task.started_at ? '开始时间' : '创建时间')), userElement('b', '', formatDate(task.finished_at || task.started_at || task.created_at, true)), element('small', '', ui('执行次数')), userElement('b', '', String(task.attempts || 0)));
    const actions = element('span', 'row-buttons automatic-task-actions');
    if (task.status === 'pending') { const index = pendingTasks.findIndex((item) => item.id === task.id); const reorder = element('span', 'automatic-task-reorder'); const up = iconButton('上移任务', 'arrow-up'); up.disabled = automaticTaskOrderSaving || index === 0; up.addEventListener('click', () => moveAutomaticTask(task.id, -1)); const down = iconButton('下移任务', 'arrow-down'); down.disabled = automaticTaskOrderSaving || index === pendingTasks.length - 1; down.addEventListener('click', () => moveAutomaticTask(task.id, 1)); reorder.append(up, down); actions.append(reorder); }
    const edit = labeledIconButton(ui('编辑'), 'edit', 'button secondary'); edit.disabled = runningTask; edit.title = runningTask ? ui('执行中的任务不能编辑') : ''; edit.addEventListener('click', () => openAutomaticTaskDialog(task));
    const remove = labeledIconButton(ui('删除'), 'trash', 'button secondary'); remove.disabled = runningTask; remove.title = runningTask ? ui('执行中的任务不能删除') : ''; remove.addEventListener('click', () => deleteAutomaticTask(task));
    actions.append(edit, remove);
    if (task.status !== 'pending') { const reset = labeledIconButton(ui('重新排队'), 'refresh', 'button secondary'); reset.addEventListener('click', () => resetAutomaticTask(task)); actions.append(reset); }
    row.append(copy, badge, timing, actions); list.append(row);
  });
}
async function moveAutomaticTask(taskId, offset) {
  if (automaticTaskOrderSaving) return;
  const pending = state.automaticTasks.filter((task) => task.status === 'pending'); const index = pending.findIndex((task) => task.id === taskId); const next = index + offset;
  if (index < 0 || next < 0 || next >= pending.length) return;
  const previous = [...state.automaticTasks]; const previousTaskIds = pending.map((task) => task.id); [pending[index], pending[next]] = [pending[next], pending[index]]; let pendingIndex = 0; state.automaticTasks = previous.map((task) => task.status === 'pending' ? pending[pendingIndex++] : task); automaticTaskOrderSaving = true; renderAutomaticTasks();
  let failed = false;
  try { await api('/automatic-tasks/reorder', { method: 'POST', resource: 'automatic-tasks', body: { previous_task_ids: previousTaskIds, task_ids: pending.map((task) => task.id) } }); showToast(ui('任务顺序已保存')); }
  catch (error) { failed = true; state.automaticTasks = previous; showToast(error.message); }
  finally { automaticTaskOrderSaving = false; renderAutomaticTasks(); }
  if (failed) await refreshAutomaticTasks();
}
function openAutomaticTaskDialog(task = null) { byId('automaticTaskForm').reset(); text('automaticTaskFormError', ''); text('automaticTaskDialogTitle', ui(task ? '编辑任务' : '新增任务')); byId('automaticTaskId').value = task?.id || ''; byId('automaticTaskContent').value = task?.content || ''; byId('automaticTaskDialog').showModal(); byId('automaticTaskContent').focus(); }
async function saveAutomaticTask(event) {
  event.preventDefault(); const id = byId('automaticTaskId').value; const content = byId('automaticTaskContent').value.trim(); if (!content) return text('automaticTaskFormError', ui('请输入任务内容。'));
  try { await api(id ? `/automatic-tasks/${id}` : '/automatic-tasks', { method: id ? 'PUT' : 'POST', body: { content } }); byId('automaticTaskDialog').close(); showToast(ui(id ? '自动任务已更新并设为未执行' : '自动任务已添加')); await refreshAutomaticTasks(); }
  catch (error) { text('automaticTaskFormError', error.message); }
}
async function deleteAutomaticTask(task) { const message = window.axisI18n.language === 'zh' ? `删除自动任务“${task.title}”？` : `Delete automatic task “${task.title}”?`; if (!confirm(message)) return; try { await api(`/automatic-tasks/${task.id}`, { method: 'DELETE' }); showToast(ui('自动任务已删除')); await refreshAutomaticTasks(); } catch (error) { showToast(error.message); } }
async function resetAutomaticTask(task) { const running = task.status === 'running'; const message = window.axisI18n.language === 'zh' ? (running ? `将执行中的任务“${task.title}”重新排队？旧外部操作仍可能继续，重新执行可能重复产生副作用；旧执行者将不能回写结果。` : `将任务“${task.title}”再次排队并重新执行？已有结果将被清除，任务会加入待执行队列末尾。`) : (running ? `Requeue running task “${task.title}”? Existing external work may continue, and running the task again may repeat side effects; the previous worker will no longer be able to save its result.` : `Requeue and run task “${task.title}” again? The previous result will be cleared and the task will be added to the end of the pending queue.`); if (!confirm(message)) return; try { await api(`/automatic-tasks/${task.id}/reset`, { method: 'POST' }); showToast(ui('自动任务已重新排队')); await refreshAutomaticTasks(); } catch (error) { showToast(error.message); } }

byId('authForm').addEventListener('submit', submitAuth); byId('logoutButton').addEventListener('click', logout); byId('refreshButton').addEventListener('click', refreshAll); byId('refreshLogsButton').addEventListener('click', refreshLogs); byId('overviewSceneSelect').addEventListener('change', handleOverviewSceneChange); byId('stopAllServicesButton').addEventListener('click', stopAllServices); byId('addServiceButton').addEventListener('click', () => openServiceDialog()); byId('addSceneButton').addEventListener('click', () => openSceneDialog()); byId('addAutomaticTaskButton').addEventListener('click', () => openAutomaticTaskDialog()); byId('addUserButton').addEventListener('click', openUserDialog); byId('cancelSceneSwitchButton').addEventListener('click', cancelSceneSwitch); byId('closeSceneProgressButton').addEventListener('click', () => byId('sceneProgressDialog').close()); byId('sceneProgressDialog').addEventListener('cancel', (event) => { if (sceneProgressOperationId) event.preventDefault(); }); byId('serviceForm').addEventListener('submit', saveService); byId('serviceWslPortproxyEnabled').addEventListener('change', updateServicePortproxyFields); byId('sceneForm').addEventListener('submit', saveScene); byId('automaticTaskForm').addEventListener('submit', saveAutomaticTask); byId('userForm').addEventListener('submit', saveUser); byId('passwordForm').addEventListener('submit', saveUserPassword); byId('serviceSearch').addEventListener('input', renderRegisteredServiceTable); byId('serviceFilters').addEventListener('click', (event) => { const button = event.target.closest('[data-filter]'); if (!button) return; state.serviceFilter = button.dataset.filter; byId('serviceFilters').querySelectorAll('.filter').forEach((item) => item.classList.toggle('active', item === button)); renderRegisteredServiceTable(); }); document.querySelectorAll('[data-close]').forEach((button) => button.addEventListener('click', () => byId(button.dataset.close).close())); document.addEventListener('visibilitychange', () => { if (!document.hidden && !document.body.classList.contains('auth-pending')) refreshAll(); });
byId('historyRangeSelect').addEventListener('click', (event) => { const button = event.target.closest('[data-history-minutes]'); if (button) selectHistoryWindow(Number(button.dataset.historyMinutes)); });
byId('monitorTabbar').addEventListener('click', (event) => { const button = event.target.closest('[data-monitor-view]'); if (button) selectMonitorView(button.dataset.monitorView); });
document.addEventListener('languagechange', () => { buildMonitorCharts(); if (state.snapshot) renderSnapshot(); renderServices(); renderScenes(); renderUsers(); renderOperations(); renderOperationTimeline(); renderVideoJobs(); renderAutomaticTasks(); renderFiles(); text('pageTitle', byId(`page-${state.activePage}`)?.dataset.title || ''); });
byId('refreshVideoJobsButton').addEventListener('click', refreshVideoJobs);
byId('refreshAutomaticTasksButton').addEventListener('click', refreshAutomaticTasks);
byId('refreshFilesButton').addEventListener('click', () => refreshFiles(state.filePath).catch(() => {}));
byId('uploadFilesButton').addEventListener('click', () => byId('fileUploadInput').click());
byId('fileUploadInput').addEventListener('change', (event) => uploadSelectedFiles([...event.target.files]));
byId('fileRenameForm').addEventListener('submit', renameFileEntry);
byId('fileSortSelect').addEventListener('change', (event) => { state.fileSort = event.target.value; refreshFiles(state.filePath).catch(() => {}); });
byId('fileBrowser').parentElement.querySelector('.file-view-switch').addEventListener('click', (event) => { const button = event.target.closest('[data-file-view]'); if (button) setFileView(button.dataset.fileView); });
byId('closeMediaButton').addEventListener('click', closeMedia);
byId('mediaDialog').addEventListener('close', () => { const stage = byId('mediaStage'); stage.querySelectorAll('audio, video').forEach((player) => { player.pause(); player.removeAttribute('src'); player.load(); }); stage.replaceChildren(); });

bootstrap();
