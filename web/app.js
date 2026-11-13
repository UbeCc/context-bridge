/* Context Bridge web console.
 *
 * Plain browser JavaScript, no framework, no build step, no external resources.
 * Everything is rendered from the HTTP API described in API.md.
 */
'use strict';

(() => {
  // ---------------------------------------------------------------------------
  // Constants
  // ---------------------------------------------------------------------------

  const ROUTES = {
    overview: { title: '概览', subtitle: '舰队状态、活动趋势与需要关注的事' },
    bots: { title: '智能体', subtitle: '每个 bot 的状态、记忆时间线与每日摘要' },
    memory: { title: '记忆', subtitle: '跨 bot 的统一时间线，支持全文检索' },
    board: { title: '看板', subtitle: '共享任务板：人与 bot 在这里交接工作' },
    console: { title: '控制台', subtitle: '选择 bot，通过聊天源下发一条指令' },
    about: { title: '关于', subtitle: '运行环境、数据模型、脱敏与部署说明' },
  };

  const KIND_META = {
    ticket: { label: '工单', icon: 'i-board', cls: 'ticket' },
    decision: { label: '决策', icon: 'i-spark', cls: 'decision' },
    trigger: { label: '触发', icon: 'i-activity', cls: 'trigger' },
    session: { label: '会话', icon: 'i-book', cls: 'session' },
    note: { label: '备注', icon: 'i-edit', cls: 'note' },
    command: { label: '指令', icon: 'i-send', cls: 'command' },
  };

  const STATUS_META = {
    active: { label: '活跃', cls: 'ok', dot: true },
    idle: { label: '空闲', cls: 'warn', dot: true },
    offline: { label: '离线', cls: 'bad', dot: true },
  };

  const RULE_LABELS = {
    api_key: 'API 密钥',
    github_token: 'GitHub Token',
    aws_key: 'AWS Access Key',
    slack_token: 'Slack Token',
    jwt: 'JWT 令牌',
    pem_key: '私钥 PEM',
    bearer_token: 'Bearer 令牌',
    password_assign: '密码赋值',
    url_credentials: 'URL 内嵌凭据',
    private_ip: '内网 IP',
    internal_domain: '内部域名',
    high_entropy: '高熵密钥串',
  };

  const BOT_PALETTE = ['#7c5cff', '#2fc8b5', '#f0a13b', '#f46b8c', '#4ea8ff', '#b7e05a'];
  const TOKEN_KEY = 'ctxbridge.token';
  const THEME_KEY = 'ctxbridge.theme';

  const XSS_SAFE_COLOR = /^#[0-9a-f]{3,8}$/i;

  // ---------------------------------------------------------------------------
  // Small utilities
  // ---------------------------------------------------------------------------

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function escAttr(value) {
    return esc(value).replace(/`/g, '&#96;');
  }

  function safeColor(color, fallback = '#7c5cff') {
    return typeof color === 'string' && XSS_SAFE_COLOR.test(color) ? color : fallback;
  }

  function hashHue(text) {
    let hash = 0;
    for (let i = 0; i < text.length; i += 1) hash = (hash * 31 + text.charCodeAt(i)) | 0;
    return Math.abs(hash);
  }

  function fmtNum(value) {
    if (typeof value !== 'number' || Number.isNaN(value)) return '0';
    return value.toLocaleString('zh-CN');
  }

  function parseDate(value) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function relTime(value) {
    const date = parseDate(value);
    if (!date) return '—';
    const diff = (Date.now() - date.getTime()) / 1000;
    if (diff < 0) return '刚刚';
    if (diff < 45) return '刚刚';
    if (diff < 3600) return `${Math.max(1, Math.round(diff / 60))} 分钟前`;
    if (diff < 86400) return `${Math.round(diff / 3600)} 小时前`;
    if (diff < 86400 * 7) return `${Math.round(diff / 86400)} 天前`;
    return `${date.getMonth() + 1} 月 ${date.getDate()} 日`;
  }

  function absTime(value) {
    const date = parseDate(value);
    if (!date) return '—';
    const pad = (n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function dayKeyOf(value) {
    const date = parseDate(value);
    if (!date) return '';
    const pad = (n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  }

  function dayLabel(key) {
    if (!key) return '未知日期';
    const today = dayKeyOf(new Date().toISOString());
    const yesterdayDate = new Date(Date.now() - 86400000);
    const yesterday = dayKeyOf(yesterdayDate.toISOString());
    if (key === today) return '今天';
    if (key === yesterday) return '昨天';
    const date = new Date(`${key}T12:00:00`);
    const weekdays = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
    return `${date.getMonth() + 1} 月 ${date.getDate()} 日 ${weekdays[date.getDay()]}`;
  }

  function fmtDuration(seconds) {
    if (typeof seconds !== 'number' || seconds < 0) return '—';
    if (seconds < 60) return `${Math.round(seconds)} 秒`;
    if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`;
    if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} 小时`;
    return `${(seconds / 86400).toFixed(1)} 天`;
  }

  function fmtUptime(seconds) {
    if (typeof seconds !== 'number' || seconds < 0) return '—';
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days) return `${days} 天 ${hours} 小时`;
    if (hours) return `${hours} 小时 ${minutes} 分`;
    return `${minutes} 分 ${Math.round(seconds % 60)} 秒`;
  }

  function debounce(fn, wait) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  }

  function throttle(fn, wait) {
    let last = 0;
    let timer = null;
    return (...args) => {
      const now = Date.now();
      const remaining = wait - (now - last);
      if (remaining <= 0) {
        last = now;
        fn(...args);
      } else if (!timer) {
        timer = setTimeout(() => {
          last = Date.now();
          timer = null;
          fn(...args);
        }, remaining);
      }
    };
  }

  function truncate(text, length = 160) {
    const value = String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
    return value.length > length ? `${value.slice(0, length - 1)}…` : value;
  }

  function sum(values) {
    return values.reduce((acc, value) => acc + (Number(value) || 0), 0);
  }

  function totalCounts(counts) {
    return sum(Object.values(counts || {}));
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      const area = document.createElement('textarea');
      area.value = text;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      area.remove();
      return ok;
    }
  }

  // Safe, dependency-free Markdown renderer for summaries and daily digests.
  // Escapes first, then applies a deliberately small subset of Markdown.
  function md(text) {
    if (!text) return '';
    const escaped = esc(text).replace(/\r\n/g, '\n');
    const inline = (value) => value
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

    const lines = escaped.split('\n');
    const out = [];
    let listType = null;
    let codeLines = null;
    let paragraph = [];

    const flushParagraph = () => {
      if (paragraph.length) {
        out.push(`<p>${inline(paragraph.join('<br>'))}</p>`);
        paragraph = [];
      }
    };
    const closeList = () => {
      if (listType) {
        out.push(`</${listType}>`);
        listType = null;
      }
    };

    for (const raw of lines) {
      if (codeLines) {
        if (raw.trim() === '```') {
          out.push(`<pre class="code-block"><code>${codeLines.join('\n')}</code></pre>`);
          codeLines = null;
        } else {
          codeLines.push(raw);
        }
        continue;
      }
      if (raw.trim() === '```') {
        flushParagraph();
        closeList();
        codeLines = [];
        continue;
      }
      const heading = raw.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        flushParagraph();
        closeList();
        const level = Math.min(heading[1].length + 1, 5);
        out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
        continue;
      }
      if (/^\s*[-*]\s+/.test(raw)) {
        flushParagraph();
        if (listType !== 'ul') {
          closeList();
          out.push('<ul>');
          listType = 'ul';
        }
        out.push(`<li>${inline(raw.replace(/^\s*[-*]\s+/, ''))}</li>`);
        continue;
      }
      const ordered = raw.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ordered) {
        flushParagraph();
        if (listType !== 'ol') {
          closeList();
          out.push('<ol>');
          listType = 'ol';
        }
        out.push(`<li>${inline(ordered[1])}</li>`);
        continue;
      }
      if (/^\s*>\s?/.test(raw)) {
        flushParagraph();
        closeList();
        out.push(`<blockquote>${inline(raw.replace(/^\s*>\s?/, ''))}</blockquote>`);
        continue;
      }
      if (/^\s*(---|\*\*\*)\s*$/.test(raw)) {
        flushParagraph();
        closeList();
        out.push('<hr>');
        continue;
      }
      if (raw.trim() === '') {
        flushParagraph();
        closeList();
        continue;
      }
      closeList();
      paragraph.push(raw);
    }
    if (codeLines) out.push(`<pre class="code-block"><code>${codeLines.join('\n')}</code></pre>`);
    flushParagraph();
    closeList();
    return out.join('\n');
  }

  function jsonHtml(value) {
    let text;
    try {
      text = JSON.stringify(value, null, 2);
    } catch (err) {
      text = String(value);
    }
    return esc(text)
      .replace(/(&quot;(?:\\.|[^&]|&(?!quot;))*?&quot;)(\s*:)/g, '<span class="j-key">$1</span>$2')
      .replace(/:\s*(&quot;.*?&quot;)/g, ': <span class="j-str">$1</span>')
      .replace(/(:\s*)(-?\d+(?:\.\d+)?)(\s*[,\n}])/g, '$1<span class="j-num">$2</span>$3')
      .replace(/\b(true|false|null)\b/g, '<span class="j-bool">$1</span>');
  }

  function snippetHtml(snippet) {
    return esc(snippet)
      .replace(/&lt;b&gt;/g, '<b>')
      .replace(/&lt;\/b&gt;/g, '</b>');
  }

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------

  const state = {
    route: { name: 'overview' },
    bots: [],
    health: null,
    overview: null,
    viewToken: 0,
    conn: 'connecting',
    tokenPrompted: false,
    memory: {
      bot: '',
      kind: '',
      q: '',
      days: '0',
      items: [],
      total: 0,
      offset: 0,
      loading: false,
      requestId: 0,
      mode: 'timeline',
    },
    board: null,
    boardDragId: null,
    console: { bot: '', draft: '', sending: false, result: null, history: [], historyLoading: false },
    botDetail: { botId: null, bot: null, records: [], digests: [], digestDate: '', digestHtml: '', digestFallback: false, loadingDigest: false },
    searchTimer: null,
  };

  // ---------------------------------------------------------------------------
  // Theme + navigation chrome helpers
  // ---------------------------------------------------------------------------

  function applyTheme(theme) {
    const next = theme === 'light' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem(THEME_KEY, next); } catch (err) { /* private mode */ }
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', next === 'light' ? '#f4f2ee' : '#0b0d12');
    $$('.theme-switch-text').forEach((el) => { el.textContent = next === 'light' ? '浅色' : '深色'; });
    $$('.topbar-theme').forEach((el) => el.setAttribute('aria-label', next === 'light' ? '切换到深色主题' : '切换到浅色主题'));
  }

  function initTheme() {
    let theme = 'dark';
    try {
      const stored = localStorage.getItem(THEME_KEY);
      if (stored === 'light' || stored === 'dark') theme = stored;
    } catch (err) { /* ignore */ }
    applyTheme(theme);
  }

  function toggleTheme() {
    applyTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light');
  }

  function navigate(routeName) {
    const hash = `#/${routeName}`;
    if (location.hash === hash) {
      renderRoute();
    } else {
      location.hash = hash;
    }
    closeMobileNav();
  }

  function currentRoute() {
    const raw = location.hash.replace(/^#\/?/, '');
    const parts = raw.split('/').filter(Boolean);
    const name = parts[0] || 'overview';
    if (name === 'bots' && parts[1]) {
      return { name: 'bot', botId: decodeURIComponent(parts[1]) };
    }
    return ROUTES[name] ? { name, param: parts[1] || '' } : { name: 'overview' };
  }

  function setActiveNav(routeName) {
    const active = routeName === 'bot' ? 'bots' : routeName;
    $$('[data-route]').forEach((el) => {
      const isActive = el.dataset.route === active;
      el.classList.toggle('is-active', isActive);
      if (isActive) {
        el.setAttribute('aria-current', 'page');
      } else {
        el.removeAttribute('aria-current');
      }
    });
  }

  function setPageHead(route) {
    const meta = route.name === 'bot'
      ? { title: '智能体详情', subtitle: '状态、记忆时间线与每日摘要' }
      : ROUTES[route.name] || ROUTES.overview;
    $('#page-title').textContent = meta.title;
    $('#page-subtitle').textContent = meta.subtitle;
    document.title = `${meta.title} · Context Bridge`;
  }

  function showLoading() {
    $('#view').innerHTML = `
      <div class="view-loading" role="status" aria-label="正在加载">
        <div class="skeleton skeleton-title"></div>
        <div class="skeleton-grid">
          <div class="skeleton skeleton-card"></div>
          <div class="skeleton skeleton-card"></div>
          <div class="skeleton skeleton-card"></div>
          <div class="skeleton skeleton-card"></div>
        </div>
        <div class="skeleton skeleton-panel"></div>
      </div>`;
    $('#view').setAttribute('aria-busy', 'true');
  }

  function showViewError(error) {
    $('#view').innerHTML = `
      <div class="state-card state-error">
        <div class="state-icon"><svg class="icon"><use href="#i-alert"/></svg></div>
        <h2>加载失败</h2>
        <p>${esc(error && error.message ? error.message : '无法连接到 Context Bridge 服务。')}</p>
        <p class="state-hint">确认 <code>ctxbridge serve</code> 正在运行，或检查网络与访问令牌。</p>
        <div class="state-actions">
          <button class="btn primary" type="button" data-action="retry-route">重试</button>
        </div>
      </div>`;
    $('#view').setAttribute('aria-busy', 'false');
  }

  function closeMobileNav() {
    document.body.classList.remove('nav-open');
    $('#nav-scrim').hidden = true;
    const toggle = $('.nav-open');
    if (toggle) toggle.setAttribute('aria-expanded', 'false');
  }

  function toggleMobileNav() {
    const opening = !document.body.classList.contains('nav-open');
    document.body.classList.toggle('nav-open', opening);
    $('#nav-scrim').hidden = !opening;
    const toggle = $('.nav-open');
    if (toggle) toggle.setAttribute('aria-expanded', String(opening));
    if (opening) {
      const first = $('#sidebar .nav-item');
      if (first) first.focus({ preventScroll: true });
    }
  }

  // ---------------------------------------------------------------------------
  // API client
  // ---------------------------------------------------------------------------

  class ApiError extends Error {
    constructor(message, status, code, data) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.code = code;
      this.data = data;
    }
  }

  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (err) { return ''; }
  }

  function saveToken(token) {
    try {
      if (token) localStorage.setItem(TOKEN_KEY, token);
      else localStorage.removeItem(TOKEN_KEY);
    } catch (err) { /* ignore */ }
  }

  async function api(path, options = {}) {
    const { method = 'GET', body, signal } = options;
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    let response;
    try {
      response = await fetch(path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal,
      });
    } catch (err) {
      if (err && err.name === 'AbortError') throw err;
      throw new ApiError('无法连接服务，请确认 ctxbridge serve 正在运行。', 0, 'network_error', null);
    }
    let data = null;
    const contentType = response.headers.get('content-type') || '';
    try {
      if (contentType.includes('application/json')) data = await response.json();
      else data = await response.text();
    } catch (err) {
      data = null;
    }
    if (!response.ok) {
      const message = data && data.error && data.error.message
        ? data.error.message
        : `请求失败（HTTP ${response.status}）`;
      const code = data && data.error ? data.error.code : '';
      if (response.status === 401) promptForToken();
      throw new ApiError(message, response.status, code, data);
    }
    return data;
  }

  const API = {
    health: () => api('/api/health'),
    overview: () => api('/api/overview'),
    bots: () => api('/api/bots'),
    bot: (id) => api(`/api/bots/${encodeURIComponent(id)}`),
    digest: (bot, date) => api(`/api/digests/${encodeURIComponent(bot)}/${encodeURIComponent(date)}`),
    records: (params) => api(`/api/records?${new URLSearchParams(params).toString()}`),
    record: (id) => api(`/api/records/${encodeURIComponent(id)}`),
    search: (params) => api(`/api/search?${new URLSearchParams(params).toString()}`),
    board: () => api('/api/board'),
    commands: (limit = 30) => api(`/api/commands?limit=${limit}`),
    createCard: (body) => api('/api/board/cards', { method: 'POST', body }),
    updateCard: (id, body) => api(`/api/board/cards/${encodeURIComponent(id)}`, { method: 'PATCH', body }),
    deleteCard: (id) => api(`/api/board/cards/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    sendCommand: (body) => api('/api/commands', { method: 'POST', body }),
    sync: () => api('/api/sync', { method: 'POST', body: {} }),
  };

  // ---------------------------------------------------------------------------
  // Shared data helpers
  // ---------------------------------------------------------------------------

  function botInfo(id) {
    const pools = [state.bots, (state.overview && state.overview.bots) || []];
    for (const pool of pools) {
      const found = (pool || []).find((bot) => bot.id === id);
      if (found) return found;
    }
    return {
      id,
      label: id || '未知',
      profile: '',
      color: BOT_PALETTE[hashHue(String(id)) % BOT_PALETTE.length],
      emoji: '🤖',
      status: 'offline',
      counts: {},
      open_tickets: 0,
      attention: 0,
    };
  }

  function botColor(id) {
    return safeColor(botInfo(id).color, BOT_PALETTE[hashHue(String(id)) % BOT_PALETTE.length]);
  }

  // Emoji glyphs are rendered only when the host actually has a colour emoji font. Headless
  // browsers and minimal servers usually do not, and a tofu box looks broken, so fall back to a
  // styled initial avatar in that case.
  const emojiSupportCache = new Map();

  function emojiRenderable(emoji) {
    if (!emoji) return false;
    if (emojiSupportCache.has(emoji)) return emojiSupportCache.get(emoji);
    let ok = false;
    try {
      const canvas = document.createElement('canvas');
      canvas.width = 28;
      canvas.height = 28;
      const ctx = canvas.getContext('2d', { willReadFrequently: true });
      if (ctx) {
        ctx.clearRect(0, 0, 28, 28);
        ctx.textBaseline = 'top';
        ctx.font = '22px "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", "Twemoji", sans-serif';
        ctx.fillStyle = '#000';
        ctx.fillText(emoji, 1, 1);
        const pixels = ctx.getImageData(0, 0, 28, 28).data;
        let coloured = 0;
        let opaque = 0;
        for (let i = 0; i < pixels.length; i += 4) {
          if (pixels[i + 3] <= 8) continue;
          opaque += 1;
          const max = Math.max(pixels[i], pixels[i + 1], pixels[i + 2]);
          const min = Math.min(pixels[i], pixels[i + 1], pixels[i + 2]);
          if (max - min > 24) coloured += 1;
        }
        ok = coloured >= 8 && opaque > 0;
      }
    } catch (err) {
      ok = false;
    }
    emojiSupportCache.set(emoji, ok);
    return ok;
  }

  function avatarHtml(bot, size = 'md') {
    const info = typeof bot === 'string' ? botInfo(bot) : bot;
    const color = safeColor(info.color, BOT_PALETTE[hashHue(String(info.id)) % BOT_PALETTE.length]);
    const useEmoji = Boolean(info.emoji) && emojiRenderable(info.emoji);
    const inner = useEmoji
      ? `<span class="avatar-emoji">${esc(info.emoji)}</span>`
      : `<span class="avatar-initial">${esc((info.label || info.id || '?').slice(0, 1).toUpperCase())}</span>`;
    const title = info.emoji ? ` title="${escAttr(`${info.emoji} ${info.label || info.id}`)}"` : '';
    return `<span class="avatar avatar-${size} ${useEmoji ? 'has-emoji' : 'has-initial'}" style="--bot-color:${color}"${title} aria-hidden="true">${inner}</span>`;
  }

  function botNameHtml(id) {
    const info = botInfo(id);
    return `<span class="bot-name" style="--bot-color:${botColor(id)}"><span class="bot-dot" aria-hidden="true"></span>${esc(info.label || id)}</span>`;
  }

  function statusBadge(status) {
    const meta = STATUS_META[status] || { label: status || '未知', cls: 'muted', dot: true };
    return `<span class="badge status status-${meta.cls}"><span class="status-dot" aria-hidden="true"></span>${esc(meta.label)}</span>`;
  }

  function kindBadge(kind) {
    const meta = KIND_META[kind] || { label: kind || '其他', icon: 'i-tag', cls: 'other' };
    return `<span class="badge kind kind-${meta.cls}"><svg class="icon"><use href="#${meta.icon}"/></svg>${esc(meta.label)}</span>`;
  }

  function tagsHtml(tags, limit = 4) {
    if (!Array.isArray(tags) || !tags.length) return '';
    const shown = tags.slice(0, limit).map((tag) => `<span class="tag">${esc(tag)}</span>`).join('');
    const more = tags.length > limit ? `<span class="tag tag-more">+${tags.length - limit}</span>` : '';
    return `<span class="tags">${shown}${more}</span>`;
  }

  function emptyState(icon, title, hint, actionHtml = '') {
    return `
      <div class="state-card state-empty">
        <div class="state-icon"><svg class="icon"><use href="#${icon}"/></svg></div>
        <h2>${esc(title)}</h2>
        ${hint ? `<p>${hint}</p>` : ''}
        ${actionHtml ? `<div class="state-actions">${actionHtml}</div>` : ''}
      </div>`;
  }

  function sectionHead(title, sub, actions = '') {
    return `
      <div class="section-head">
        <div class="section-head-text">
          <h2>${esc(title)}</h2>
          ${sub ? `<span class="section-sub">${sub}</span>` : ''}
        </div>
        ${actions ? `<div class="section-actions">${actions}</div>` : ''}
      </div>`;
  }

  // ---------------------------------------------------------------------------
  // Toasts
  // ---------------------------------------------------------------------------

  const TOAST_ICONS = { ok: 'i-check', warn: 'i-alert', danger: 'i-alert', info: 'i-spark' };

  function toast(message, tone = 'info') {
    const root = $('#toast-root');
    if (!root) return;
    const node = document.createElement('div');
    node.className = `toast toast-${tone}`;
    node.innerHTML = `<svg class="icon"><use href="#${TOAST_ICONS[tone] || 'i-spark'}"/></svg><span>${esc(message)}</span>`;
    root.appendChild(node);
    requestAnimationFrame(() => node.classList.add('is-in'));
    setTimeout(() => {
      node.classList.remove('is-in');
      node.classList.add('is-out');
      setTimeout(() => node.remove(), 320);
    }, 3600);
  }

  // ---------------------------------------------------------------------------
  // Overlays: drawer, modal, confirms, token prompt
  // ---------------------------------------------------------------------------

  const overlayStack = [];

  function closeTopOverlay() {
    const top = overlayStack[overlayStack.length - 1];
    if (top) top.close();
  }

  function openOverlay(options) {
    const {
      kind = 'modal',
      title = '',
      body = '',
      width = '',
      onClose = null,
    } = options;
    const root = $('#overlay-root');
    const previousFocus = document.activeElement;
    const overlay = document.createElement('div');
    overlay.className = `overlay overlay-${kind}`;
    overlay.innerHTML = `
      <div class="overlay-backdrop" data-overlay-close></div>
      <section class="overlay-panel ${kind}" role="dialog" aria-modal="true" aria-label="${escAttr(title)}" ${width ? `style="--overlay-width:${escAttr(width)}"` : ''}>
        <header class="overlay-head">
          <h2 class="overlay-title">${esc(title)}</h2>
          <button class="icon-btn" type="button" data-overlay-close aria-label="关闭">
            <svg class="icon"><use href="#i-close"/></svg>
          </button>
        </header>
        <div class="overlay-body"></div>
      </section>`;
    root.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add('is-open'));

    const bodyNode = $('.overlay-body', overlay);
    bodyNode.innerHTML = body;

    const handleKeydown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        handle.close();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusables = $$('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])', overlay)
        .filter((el) => !el.disabled && el.offsetParent !== null);
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    overlay.addEventListener('keydown', handleKeydown);

    const handle = {
      node: overlay,
      body: bodyNode,
      closed: false,
      setBody(html) { bodyNode.innerHTML = html; },
      setTitle(nextTitle) { $('.overlay-title', overlay).textContent = nextTitle; },
      close() {
        if (handle.closed) return;
        handle.closed = true;
        overlay.classList.remove('is-open');
        overlay.classList.add('is-closing');
        overlay.removeEventListener('keydown', handleKeydown);
        const index = overlayStack.indexOf(handle);
        if (index >= 0) overlayStack.splice(index, 1);
        setTimeout(() => {
          overlay.remove();
          if (!overlayStack.length) document.body.classList.remove('has-overlay');
          if (previousFocus && typeof previousFocus.focus === 'function') {
            try { previousFocus.focus({ preventScroll: true }); } catch (err) { /* ignore */ }
          }
          if (onClose) onClose();
        }, 220);
      },
    };
    overlay.addEventListener('click', (event) => {
      if (event.target.closest('[data-overlay-close]')) {
        event.preventDefault();
        handle.close();
      }
    });
    overlayStack.push(handle);
    document.body.classList.add('has-overlay');
    setTimeout(() => {
      const target = $('input, textarea, select, button', bodyNode) || $('.overlay-panel', overlay);
      if (target) target.focus({ preventScroll: true });
    }, 40);
    return handle;
  }

  function confirmDialog(options) {
    const { title = '确认操作', message = '', confirmLabel = '确认', danger = false } = options;
    return new Promise((resolve) => {
      const overlay = openOverlay({
        title,
        body: `
          <p class="confirm-message">${esc(message)}</p>
          <div class="form-actions">
            <button class="btn btn-ghost" type="button" data-overlay-close>取消</button>
            <button class="btn ${danger ? 'danger' : 'primary'}" type="button" data-confirm>${esc(confirmLabel)}</button>
          </div>`,
        onClose: () => resolve(false),
      });
      $('[data-confirm]', overlay.node).addEventListener('click', () => {
        const index = overlayStack.indexOf(overlay);
        if (index >= 0) overlayStack.splice(index, 1);
        overlay.closed = true;
        overlay.node.remove();
        if (!overlayStack.length) document.body.classList.remove('has-overlay');
        resolve(true);
      });
    });
  }

  function promptForToken() {
    if (state.tokenPrompted) return;
    state.tokenPrompted = true;
    const overlay = openOverlay({
      title: '需要访问令牌',
      body: `
        <p class="overlay-hint">服务端启用了 Bearer 令牌校验。令牌只保存在浏览器的 localStorage 中，不会发往其他任何地方。</p>
        <form id="token-form" class="form">
          <label class="field">
            <span class="field-label">访问令牌</span>
            <input class="input" name="token" type="password" autocomplete="off" placeholder="粘贴 config.toml 中 server.token 的值">
          </label>
          <div class="form-actions">
            <button class="btn btn-ghost" type="button" data-overlay-close>取消</button>
            <button class="btn primary" type="submit">保存并重试</button>
          </div>
        </form>`,
    });
    $('#token-form', overlay.node).addEventListener('submit', (event) => {
      event.preventDefault();
      const token = new FormData(event.target).get('token');
      saveToken(String(token || '').trim());
      overlay.close();
      state.tokenPrompted = false;
      toast('令牌已保存，正在重试', 'ok');
      renderRoute();
      connectEvents();
    });
  }

  // ---------------------------------------------------------------------------
  // Record drawer
  // ---------------------------------------------------------------------------

  async function openRecordDrawer(recordId) {
    const overlay = openOverlay({
      kind: 'drawer',
      title: '记忆详情',
      body: `
        <div class="drawer-loading" role="status" aria-label="正在加载记录">
          <div class="skeleton skeleton-line w-40"></div>
          <div class="skeleton skeleton-line w-90"></div>
          <div class="skeleton skeleton-line w-70"></div>
          <div class="skeleton skeleton-panel"></div>
        </div>`,
    });
    try {
      const record = await API.record(recordId);
      if (overlay.closed) return;
      overlay.setTitle(record.title || '记忆详情');
      overlay.setBody(recordDrawerHtml(record));
    } catch (error) {
      if (overlay.closed) return;
      overlay.setTitle('记忆详情');
      overlay.setBody(`
        <div class="state-card state-error">
          <div class="state-icon"><svg class="icon"><use href="#i-alert"/></svg></div>
          <h2>无法加载记录</h2>
          <p>${esc(error.message)}</p>
          <p class="state-hint"><code>${esc(recordId)}</code></p>
        </div>`);
    }
  }

  function refsHtml(refs) {
    if (!refs || typeof refs !== 'object') return '';
    const entries = Object.entries(refs).filter(([, value]) => value != null && value !== '');
    if (!entries.length) return '';
    const rows = entries.map(([key, value]) => {
      const text = String(value);
      const isLink = /^https?:\/\//i.test(text);
      return `<div class="kv-row"><span class="kv-key">${esc(key)}</span><span class="kv-value">${
        isLink
          ? `<a href="${escAttr(text)}" target="_blank" rel="noopener noreferrer">${esc(text)} <svg class="icon"><use href="#i-external"/></svg></a>`
          : `<code>${esc(text)}</code>`
      }</span></div>`;
    }).join('');
    return `<section class="record-section"><h3>引用</h3><div class="kv-list">${rows}</div></section>`;
  }

  function recordDrawerHtml(record) {
    const source = record.source || {};
    const sourceRows = Object.entries(source).filter(([, value]) => value != null && value !== '').map(([key, value]) => `
      <div class="kv-row"><span class="kv-key">${esc(key)}</span><span class="kv-value"><code>${esc(value)}</code></span></div>`).join('');
    return `
      <div class="record-detail">
        <div class="record-labels">
          ${kindBadge(record.kind)}
          ${botNameHtml(record.bot)}
          <span class="record-time">${esc(absTime(record.created_at))}</span>
        </div>
        <h2 class="record-title">${esc(record.title)}</h2>
        <div class="record-meta">
          <span>创建于 ${esc(relTime(record.created_at))}</span>
          <span class="dot-sep" aria-hidden="true">·</span>
          <span>更新于 ${esc(relTime(record.updated_at))}</span>
          <span class="dot-sep" aria-hidden="true">·</span>
          <span class="record-redactions ${record.redactions ? 'has' : ''}">
            <svg class="icon"><use href="#i-shield"/></svg>脱敏 ${fmtNum(record.redactions || 0)} 处
          </span>
        </div>
        ${record.summary ? `<section class="record-section"><h3>摘要</h3><div class="markdown">${md(record.summary)}</div></section>` : ''}
        ${Array.isArray(record.tags) && record.tags.length ? `<section class="record-section"><h3>标签</h3><div>${tagsHtml(record.tags, 12)}</div></section>` : ''}
        ${refsHtml(record.refs)}
        ${sourceRows ? `<section class="record-section"><h3>来源</h3><div class="kv-list">${sourceRows}</div></section>` : ''}
        ${record.payload && Object.keys(record.payload).length ? `<section class="record-section"><details class="payload-details" open><summary>payload</summary><pre class="json">${jsonHtml(record.payload)}</pre></details></section>` : ''}
        <div class="record-footer">
          <button class="btn btn-ghost" type="button" data-action="copy-record-id" data-id="${escAttr(record.id)}">
            <svg class="icon"><use href="#i-copy"/></svg>复制记录 ID
          </button>
          <span class="record-id"><code>${esc(record.id)}</code></span>
        </div>
      </div>`;
  }

  // ---------------------------------------------------------------------------
  // Overview view
  // ---------------------------------------------------------------------------

  function statCards(overview, health) {
    const counts = overview.counts || {};
    const byKind = counts.by_kind || {};
    const activity = [...(overview.activity || [])].sort((a, b) => String(a.date).localeCompare(String(b.date)));
    const today = activity[activity.length - 1];
    const previous = activity[activity.length - 2];
    const todayCount = today ? today.count : 0;
    const delta = previous ? todayCount - previous.count : 0;
    const attention = counts.attention || (overview.attention || []).length;
    const redaction = (health && health.redaction) || { total: 0, by_rule: {} };
    const topRule = Object.entries(redaction.by_rule || {}).sort((a, b) => b[1] - a[1])[0];
    const kindMini = ['ticket', 'session', 'decision', 'note']
      .filter((kind) => byKind[kind])
      .map((kind) => `<span class="mini-stat"><span class="mini-dot kind-${kind}"></span>${KIND_META[kind].label} <b>${fmtNum(byKind[kind])}</b></span>`)
      .join('');
    return `
      <section class="stat-grid" aria-label="关键指标">
        <article class="stat-card accent-violet">
          <div class="stat-head"><span class="stat-label">记忆记录</span><span class="stat-icon"><svg class="icon"><use href="#i-memory"/></svg></span></div>
          <div class="stat-value">${fmtNum(counts.records || 0)}<span class="stat-unit">条</span></div>
          <div class="stat-sub mini-stats">${kindMini || '<span class="mini-stat">暂无记录</span>'}</div>
        </article>
        <article class="stat-card accent-teal">
          <div class="stat-head"><span class="stat-label">今日新增</span><span class="stat-icon"><svg class="icon"><use href="#i-activity"/></svg></span></div>
          <div class="stat-value">${fmtNum(todayCount)}<span class="stat-unit">条</span></div>
          <div class="stat-sub">${delta === 0 ? '与昨日持平' : delta > 0 ? `<span class="trend up">↑ ${delta}</span> 较昨日` : `<span class="trend down">↓ ${Math.abs(delta)}</span> 较昨日`}</div>
        </article>
        <article class="stat-card accent-amber">
          <div class="stat-head"><span class="stat-label">需要关注</span><span class="stat-icon"><svg class="icon"><use href="#i-alert"/></svg></span></div>
          <div class="stat-value">${fmtNum(attention)}<span class="stat-unit">项</span></div>
          <div class="stat-sub">${attention ? '<button class="link-btn" type="button" data-action="focus-attention">查看待处理事项</button>' : '舰队运行平稳'}</div>
        </article>
        <article class="stat-card accent-slate">
          <div class="stat-head"><span class="stat-label">写入脱敏</span><span class="stat-icon"><svg class="icon"><use href="#i-shield"/></svg></span></div>
          <div class="stat-value">${fmtNum(redaction.total || 0)}<span class="stat-unit">处</span></div>
          <div class="stat-sub">${topRule ? `最多：${esc(RULE_LABELS[topRule[0]] || topRule[0])} ${fmtNum(topRule[1])}` : '未发现敏感内容'}</div>
        </article>
      </section>`;
  }

  function fleetCard(bot) {
    const info = botInfo(bot.id);
    const counts = bot.counts || {};
    const color = safeColor(info.color);
    const kindChips = ['ticket', 'session', 'decision', 'note']
      .filter((kind) => counts[kind])
      .slice(0, 4)
      .map((kind) => `<span class="count-chip"><span class="mini-dot kind-${kind}"></span>${KIND_META[kind].label} ${fmtNum(counts[kind])}</span>`)
      .join('');
    return `
      <article class="fleet-card" style="--bot-color:${color}" tabindex="0" role="button"
               data-action="open-bot" data-bot-id="${escAttr(bot.id)}" aria-label="打开 ${escAttr(info.label)} 的详情">
        <header class="fleet-head">
          ${avatarHtml(info, 'lg')}
          <div class="fleet-id">
            <h3>${esc(info.label || bot.id)}<span class="bot-handle">@${esc(bot.id)}</span></h3>
            <p>${esc(info.profile || 'agent')}</p>
          </div>
          ${statusBadge(bot.status)}
        </header>
        <p class="fleet-sync">
          <svg class="icon"><use href="#i-clock"/></svg>
          最近同步 ${esc(relTime(bot.last_sync))}
        </p>
        <div class="fleet-counts">${kindChips || '<span class="count-chip muted">暂无记忆</span>'}</div>
        <footer class="fleet-foot">
          <span class="fleet-foot-item">未结工单 <b>${fmtNum(bot.open_tickets || 0)}</b></span>
          ${bot.attention ? `<span class="fleet-foot-item attention"><svg class="icon"><use href="#i-alert"/></svg>${fmtNum(bot.attention)} 项关注</span>` : '<span class="fleet-foot-item muted">无待关注</span>'}
          <span class="fleet-open">打开 <svg class="icon"><use href="#i-arrow-right"/></svg></span>
        </footer>
      </article>`;
  }

  function activityChart(activity) {
    const data = [...(activity || [])].sort((a, b) => String(a.date).localeCompare(String(b.date)));
    if (!data.length) {
      return emptyState('i-activity', '暂无活动数据', '完成一次 <code>ctxbridge sync</code> 后，这里会出现近 14 天的记录趋势。');
    }
    const total = sum(data.map((item) => item.count));
    const max = Math.max(1, ...data.map((item) => item.count));
    const width = 680;
    const height = 250;
    const pad = { top: 20, right: 14, bottom: 40, left: 38 };
    const innerW = width - pad.left - pad.right;
    const innerH = height - pad.top - pad.bottom;
    const step = innerW / data.length;
    const barW = Math.min(26, step * 0.54);
    const grid = [0, 0.5, 1].map((fraction) => {
      const y = pad.top + innerH - innerH * fraction;
      return `<line class="chart-grid" x1="${pad.left}" y1="${y}" x2="${width - pad.right}" y2="${y}"/>
              <text class="chart-axis" x="${pad.left - 10}" y="${y + 4}" text-anchor="end">${Math.round(max * fraction)}</text>`;
    }).join('');
    const bars = data.map((item, index) => {
      const date = parseDate(item.date);
      const value = Number(item.count) || 0;
      const barH = Math.max(value ? 4 : 2, (value / max) * innerH);
      const x = pad.left + step * index + (step - barW) / 2;
      const y = pad.top + innerH - barH;
      const showLabel = index % 2 === 0 || index === data.length - 1;
      const label = date ? `${date.getMonth() + 1}/${date.getDate()}` : String(item.date).slice(5);
      const labelEl = showLabel
        ? `<text class="chart-label" x="${pad.left + step * index + step / 2}" y="${height - 14}" text-anchor="middle">${label}</text>`
        : '';
      const tipW = 118;
      const tipX = Math.min(Math.max(x + barW / 2 - tipW / 2, pad.left), width - pad.right - tipW);
      return `
        <g class="bar-group">
          <rect class="bar-hit" x="${pad.left + step * index}" y="${pad.top}" width="${step}" height="${innerH}"/>
          <rect class="chart-bar ${value === 0 ? 'is-zero' : ''} ${index === data.length - 1 ? 'is-today' : ''}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${barH.toFixed(1)}" rx="5"
                style="--bar-delay:${(index * 34).toFixed(0)}ms"/>
          ${labelEl}
          <g class="bar-tip" transform="translate(${tipX.toFixed(1)}, ${Math.max(pad.top, y - 44).toFixed(1)})">
            <rect width="${tipW}" height="32" rx="8"/>
            <text x="${tipW / 2}" y="21" text-anchor="middle">${value} 条 · ${label}</text>
          </g>
        </g>`;
    }).join('');
    const avg = Math.round(total / data.length);
    return `
      <div class="chart-wrap">
        <svg class="activity-chart" viewBox="0 0 ${width} ${height}" role="img"
             aria-label="近 14 天记忆记录数量，共 ${total} 条，日均 ${avg} 条">
          <defs>
            <linearGradient id="bar-grad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="var(--chart-top)"/>
              <stop offset="100%" stop-color="var(--chart-bottom)"/>
            </linearGradient>
            <linearGradient id="bar-grad-today" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="var(--accent-2)"/>
              <stop offset="100%" stop-color="color-mix(in srgb, var(--accent-2) 28%, transparent)"/>
            </linearGradient>
          </defs>
          ${grid}
          ${bars}
        </svg>
        <div class="chart-foot">
          <span><b>${fmtNum(total)}</b> 条 · 日均 <b>${avg}</b> 条</span>
          <span class="chart-foot-muted">最近 14 天</span>
        </div>
      </div>`;
  }

  function attentionPanel(overview) {
    const items = overview.attention || [];
    if (!items.length) {
      return emptyState('i-check', '没有待处理事项', '所有 bot 都在正常推进，没有挂起的提问或阻塞。');
    }
    const rows = items.map((item) => `
      <div class="attention-item">
        <div class="attention-top">
          ${avatarHtml(item.bot, 'sm')}
          <span class="attention-bot">${esc(botInfo(item.bot).label || item.bot)}</span>
          <span class="attention-time">${esc(relTime(item.at))}</span>
        </div>
        <p class="attention-question">${esc(item.question || item.title || '')}</p>
        <p class="attention-record" data-record-id="${escAttr(item.record_id)}" role="button" tabindex="0">${esc(item.title || item.record_id)}</p>
        <button class="btn btn-small" type="button" data-action="handle-attention" data-bot="${escAttr(item.bot)}" data-question="${escAttr(item.question || '')}">
          去处理 <svg class="icon"><use href="#i-arrow-right"/></svg>
        </button>
      </div>`).join('');
    return `<div class="attention-list">${rows}</div>`;
  }

  function recentFeed(overview) {
    const items = overview.recent || [];
    if (!items.length) return emptyState('i-memory', '还没有任何记忆', '运行 <code>ctxbridge sync</code> 采集第一轮数据。');
    return `<div class="feed-list">${items.map((item) => `
      <button class="feed-item" type="button" data-record-id="${escAttr(item.id)}">
        <span class="feed-avatar">${avatarHtml(item.bot, 'sm')}</span>
        <span class="feed-main">
          <span class="feed-title">${esc(item.title)}</span>
          <span class="feed-meta">${esc(botInfo(item.bot).label || item.bot)} · ${esc((KIND_META[item.kind] || {}).label || item.kind)} · ${esc(relTime(item.updated_at))}</span>
        </span>
        ${kindBadge(item.kind)}
        <svg class="icon feed-chevron"><use href="#i-chevron-right"/></svg>
      </button>`).join('')}</div>`;
  }

  function redactionPanel(health) {
    const redaction = (health && health.redaction) || { total: 0, by_rule: {} };
    const entries = Object.entries(redaction.by_rule || {}).sort((a, b) => b[1] - a[1]);
    if (!entries.length) {
      return emptyState('i-shield', '脱敏计数为空', '当采集器写入含敏感内容的记录时，这里会按规则显示命中次数。');
    }
    const max = Math.max(...entries.map(([, count]) => count), 1);
    const rows = entries.map(([rule, count]) => `
      <div class="rule-row">
        <span class="rule-name">${esc(RULE_LABELS[rule] || rule)}</span>
        <span class="rule-bar"><span style="width:${Math.max(3, Math.round((count / max) * 100))}%"></span></span>
        <span class="rule-count">${fmtNum(count)}</span>
      </div>`).join('');
    return `
      <div class="redaction-total">
        <svg class="icon"><use href="#i-shield"/></svg>
        <span>累计掩码 <b>${fmtNum(redaction.total || 0)}</b> 处敏感内容</span>
      </div>
      <div class="rule-list">${rows}</div>
      <p class="card-note">脱敏在写入 hub 之前执行；控制台读到的内容已经不含明文密钥与内部地址。</p>`;
  }

  function overviewHero(overview, health) {
    const active = (overview.bots || []).filter((bot) => bot.status === 'active').length;
    const totalBots = (overview.bots || []).length;
    const counts = overview.counts || {};
    const attention = counts.attention || (overview.attention || []).length;
    const redaction = (health && health.redaction) || { total: 0 };
    const lastSync = (overview.bots || [])
      .map((bot) => parseDate(bot.last_sync))
      .filter(Boolean)
      .sort((a, b) => b - a)[0];
    return `
      <section class="overview-hero">
        <div class="hero-aurora" aria-hidden="true"></div>
        <div class="hero-copy">
          <p class="hero-eyebrow"><span class="pulse-dot" aria-hidden="true"></span>LIVE · 舰队实时状态</p>
          <h2>一支舰队，<em>一份共享记忆</em>。</h2>
          <p class="hero-sub">${totalBots} 个智能体正在把工单、决策与会话摘要写进同一个 git hub。从这里检索、交接与下指令。</p>
        </div>
        <div class="hero-stats" aria-label="舰队摘要">
          <div class="hero-stat"><span class="hero-stat-label">记忆记录</span><span class="hero-stat-value">${fmtNum(counts.records || 0)}<small>条</small></span></div>
          <div class="hero-stat"><span class="hero-stat-label">活跃智能体</span><span class="hero-stat-value">${active}<small>/ ${totalBots}</small></span></div>
          <div class="hero-stat"><span class="hero-stat-label">待关注</span><span class="hero-stat-value ${attention ? 'is-warn' : ''}">${fmtNum(attention)}<small>项</small></span></div>
          <div class="hero-stat"><span class="hero-stat-label">最近同步</span><span class="hero-stat-value hero-stat-time">${lastSync ? esc(relTime(lastSync.toISOString())) : '—'}</span></div>
        </div>
      </section>`;
  }

  async function renderOverview(token) {
    const [overview, health] = await Promise.all([
      API.overview(),
      API.health().catch(() => state.health),
    ]);
    if (token !== state.viewToken) return;
    state.overview = overview;
    if (health) state.health = health;
    const active = (overview.bots || []).filter((bot) => bot.status === 'active').length;
    $('#view').innerHTML = `
      <div class="view-content overview-view">
        ${overviewHero(overview, state.health)}
        ${statCards(overview, state.health)}
        <section class="section">
          ${sectionHead('舰队', `${(overview.bots || []).length} 个智能体 · ${active} 活跃`,
            `<button class="btn btn-ghost" type="button" data-route="bots">全部智能体 <svg class="icon"><use href="#i-arrow-right"/></svg></button>`)}
          <div class="fleet-grid">${(overview.bots || []).map(fleetCard).join('')}</div>
        </section>
        <div class="overview-grid">
          <section class="card activity-card">
            <div class="card-head">
              <div><h2>近 14 天活动</h2><p class="card-sub">每条记录在写入 hub 时计数</p></div>
              ${kindBadge('session')}
            </div>
            ${activityChart(overview.activity)}
          </section>
          <section class="card attention-card" id="attention-panel">
            <div class="card-head">
              <div><h2>需要关注</h2><p class="card-sub">bot 在等你做决定</p></div>
              <span class="count-pill">${fmtNum((overview.attention || []).length)}</span>
            </div>
            ${attentionPanel(overview)}
          </section>
        </div>
        <div class="overview-grid overview-grid-bottom">
          <section class="card feed-card">
            <div class="card-head">
              <div><h2>最近动态</h2><p class="card-sub">跨 bot 的最新记忆</p></div>
              <button class="link-btn" type="button" data-route="memory">查看全部 <svg class="icon"><use href="#i-arrow-right"/></svg></button>
            </div>
            ${recentFeed(overview)}
          </section>
          <section class="card redaction-card">
            <div class="card-head">
              <div><h2>脱敏摘要</h2><p class="card-sub">按规则统计的写入时掩码</p></div>
              <svg class="icon head-icon"><use href="#i-shield"/></svg>
            </div>
            ${redactionPanel(state.health)}
          </section>
        </div>
      </div>`;
    updateAttentionBadge();
    $('#view').setAttribute('aria-busy', 'false');
  }

  function updateAttentionBadge() {
    const count = state.overview && state.overview.counts
      ? state.overview.counts.attention || 0
      : (state.overview && (state.overview.attention || []).length) || 0;
    $$('[data-attention-badge]').forEach((badge) => {
      badge.textContent = count > 99 ? '99+' : String(count);
      badge.hidden = !count;
    });
  }

  // ---------------------------------------------------------------------------
  // Bots views
  // ---------------------------------------------------------------------------

  function botCard(bot) {
    const info = botInfo(bot.id);
    const color = safeColor(info.color);
    const counts = bot.counts || {};
    const kindChips = Object.keys(counts)
      .sort((a, b) => counts[b] - counts[a])
      .slice(0, 5)
      .map((kind) => `<span class="count-chip"><span class="mini-dot kind-${esc(kind)}"></span>${esc((KIND_META[kind] || {}).label || kind)} ${fmtNum(counts[kind])}</span>`)
      .join('');
    return `
      <article class="bot-card" style="--bot-color:${color}" tabindex="0" role="button"
               data-action="open-bot" data-bot-id="${escAttr(bot.id)}" aria-label="打开 ${escAttr(info.label)} 详情">
        <header class="bot-card-head">
          ${avatarHtml(info, 'xl')}
          <div class="bot-card-id">
            <h3>${esc(info.label || bot.id)}</h3>
            <p class="bot-card-profile">${esc(info.profile || 'agent')}</p>
            <div class="bot-card-status">${statusBadge(bot.status)}<span class="bot-card-sync">同步于 ${esc(relTime(bot.last_sync))}</span></div>
          </div>
        </header>
        <div class="bot-card-stats">
          <div><span class="bot-stat-value">${fmtNum(totalCounts(counts))}</span><span class="bot-stat-label">记忆</span></div>
          <div><span class="bot-stat-value">${fmtNum(bot.open_tickets || 0)}</span><span class="bot-stat-label">未结工单</span></div>
          <div><span class="bot-stat-value ${bot.attention ? 'warn-value' : ''}">${fmtNum(bot.attention || 0)}</span><span class="bot-stat-label">待关注</span></div>
        </div>
        <div class="bot-card-kinds">${kindChips || '<span class="count-chip muted">暂无记忆</span>'}</div>
        <footer class="bot-card-foot">
          <span>查看时间线与日报</span>
          <svg class="icon"><use href="#i-arrow-right"/></svg>
        </footer>
      </article>`;
  }

  async function renderBots(token) {
    const data = await API.bots();
    if (token !== state.viewToken) return;
    state.bots = data.items || [];
    const active = state.bots.filter((bot) => bot.status === 'active').length;
    $('#view').innerHTML = `
      <div class="view-content bots-view">
        ${sectionHead('全部智能体', `${state.bots.length} 个 bot · ${active} 活跃`,
          `<button class="btn btn-ghost" type="button" data-action="manual-sync"><svg class="icon"><use href="#i-refresh"/></svg>同步全部</button>`)}
        <div class="bots-grid">${state.bots.map(botCard).join('')}</div>
        <section class="card bots-hint-card">
          <div class="bots-hint-icon"><svg class="icon"><use href="#i-hub"/></svg></div>
          <div>
            <h3>这些 bot 共享同一份记忆</h3>
            <p>每个 bot 的工单、决策、会话摘要与备注都会进入 hub；在「记忆」里可以跨 bot 检索，在「看板」上可以交接任务。</p>
          </div>
        </section>
      </div>`;
    updateAttentionBadge();
    $('#view').setAttribute('aria-busy', 'false');
  }

  function timelineGroups(items) {
    const groups = new Map();
    for (const item of items) {
      const key = dayKeyOf(item.updated_at || item.created_at);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(item);
    }
    return Array.from(groups.entries()).sort((a, b) => b[0].localeCompare(a[0]));
  }

  function timelineHtml(items, options = {}) {
    if (!items.length) {
      return emptyState('i-memory', options.emptyTitle || '还没有记忆', options.emptyHint || '当这个 bot 产生工单、会话或备注后，时间线会出现在这里。');
    }
    return timelineGroups(items).map(([day, group]) => `
      <div class="timeline-day">
        <div class="timeline-day-head"><span class="timeline-day-label">${esc(dayLabel(day))}</span><span class="timeline-day-count">${group.length} 条</span></div>
        <div class="timeline-items">
          ${group.map((item) => `
            <button class="timeline-item" type="button" data-record-id="${escAttr(item.id)}">
              <span class="timeline-rail" style="--bot-color:${botColor(item.bot)}"><span class="timeline-dot"></span></span>
              <span class="timeline-body">
                <span class="timeline-row">
                  ${kindBadge(item.kind)}
                  <span class="timeline-title">${esc(item.title)}</span>
                  <span class="timeline-time">${esc(relTime(item.updated_at))}</span>
                </span>
                ${item.summary ? `<span class="timeline-summary">${esc(truncate(item.summary, 150))}</span>` : ''}
                <span class="timeline-foot">${botNameHtml(item.bot)}${tagsHtml(item.tags, 3)}</span>
              </span>
            </button>`).join('')}
        </div>
      </div>`).join('');
  }

  function digestFallbackHtml(records, date) {
    const dayRecords = records.filter((record) => dayKeyOf(record.created_at) === date || dayKeyOf(record.updated_at) === date);
    if (!dayRecords.length) {
      return emptyState('i-book', '这一天没有记录', '服务端没有生成该日期的摘要，也没有当日记录可合成。');
    }
    const groups = new Map();
    for (const record of dayRecords) {
      if (!groups.has(record.kind)) groups.set(record.kind, []);
      groups.get(record.kind).push(record);
    }
    const sections = Array.from(groups.entries()).map(([kind, items]) => `
      <h3>${esc((KIND_META[kind] || {}).label || kind)} · ${items.length}</h3>
      <ul>${items.map((item) => `<li><strong>${esc(item.title)}</strong> — ${esc(truncate(item.summary, 120))}</li>`).join('')}</ul>`).join('');
    return `
      <div class="digest-fallback-note">
        <svg class="icon"><use href="#i-info"/></svg>
        服务端暂未提供该日期的日报正文，以下内容由当天的 ${dayRecords.length} 条记录即时合成。
      </div>
      <div class="markdown">${sections}</div>`;
  }

  async function refreshDigest(date) {
    const detail = state.botDetail;
    detail.digestDate = date;
    detail.loadingDigest = true;
    renderDigestPanel();
    try {
      const data = await API.digest(detail.botId, date);
      detail.digestHtml = md(data.content || '');
      detail.digestFallback = false;
    } catch (error) {
      if (error.status === 404 || error.status === 0 || error.status >= 500) {
        detail.digestHtml = digestFallbackHtml(detail.records, date);
        detail.digestFallback = true;
      } else {
        detail.digestHtml = `<div class="state-card state-error"><div class="state-icon"><svg class="icon"><use href="#i-alert"/></svg></div><h2>无法加载日报</h2><p>${esc(error.message)}</p></div>`;
        detail.digestFallback = true;
      }
    }
    detail.loadingDigest = false;
    renderDigestPanel();
  }

  function renderDigestPanel() {
    const body = $('#digest-body');
    if (!body) return;
    const detail = state.botDetail;
    const dates = detail.digests || [];
    const dateButtons = dates.map((entry) => `
      <button class="digest-date ${entry.date === detail.digestDate ? 'is-active' : ''}" type="button"
              data-action="digest-date" data-date="${escAttr(entry.date)}">
        <span class="digest-date-day">${esc(entry.date.slice(5).replace('-', '/'))}</span>
        <span class="digest-date-label">${esc(dayLabel(entry.date))}</span>
      </button>`).join('');
    const dateList = $('#digest-dates');
    if (dateList) dateList.innerHTML = dateButtons;
    body.innerHTML = detail.loadingDigest
      ? `<div class="digest-loading"><div class="skeleton skeleton-line w-40"></div><div class="skeleton skeleton-line w-90"></div><div class="skeleton skeleton-line w-80"></div><div class="skeleton skeleton-line w-60"></div></div>`
      : (detail.digestHtml || emptyState('i-book', '暂无日报', '采集器生成当日 digest 后会显示在这里。'));
  }

  async function renderBotDetail(botId, token) {
    const detail = state.botDetail;
    detail.botId = botId;
    const [botData, recordsData] = await Promise.all([
      API.bot(botId),
      API.records({ bot: botId, limit: 100, offset: 0 }),
    ]);
    if (token !== state.viewToken) return;
    detail.bot = botData.bot;
    detail.records = recordsData.items || [];
    detail.digests = botData.digests || [];
    detail.digestDate = detail.digests.length ? detail.digests[0].date : '';
    detail.digestHtml = '';
    const bot = botData.bot || botInfo(botId);
    const counts = bot.counts || {};
    const color = safeColor(bot.color);
    state.bots = state.bots.some((item) => item.id === botId)
      ? state.bots.map((item) => (item.id === botId ? { ...item, ...bot } : item))
      : [...state.bots, bot];
    $('#view').innerHTML = `
      <div class="view-content bot-detail-view">
        <button class="back-link" type="button" data-route="bots"><svg class="icon"><use href="#i-chevron-left"/></svg>全部智能体</button>
        <section class="card bot-hero" style="--bot-color:${color}">
          <div class="bot-hero-main">
            ${avatarHtml(bot, 'xl')}
            <div class="bot-hero-id">
              <h2>${esc(bot.label || botId)}<span class="bot-handle">@${esc(bot.id)}</span></h2>
              <div class="bot-hero-meta">
                ${statusBadge(bot.status)}
                <span class="bot-hero-profile">${esc(bot.profile || 'agent')}</span>
                <span class="dot-sep" aria-hidden="true">·</span>
                <span>同步于 ${esc(relTime(bot.last_sync))}</span>
              </div>
            </div>
          </div>
          <div class="bot-hero-stats">
            <div><span class="stat-value-sm">${fmtNum(totalCounts(counts))}</span><span class="stat-label-sm">记忆</span></div>
            <div><span class="stat-value-sm">${fmtNum(bot.open_tickets || 0)}</span><span class="stat-label-sm">未结工单</span></div>
            <div><span class="stat-value-sm">${fmtNum(counts.session || 0)}</span><span class="stat-label-sm">会话</span></div>
            <div><span class="stat-value-sm ${bot.attention ? 'warn-value' : ''}">${fmtNum(bot.attention || 0)}</span><span class="stat-label-sm">待关注</span></div>
          </div>
          <div class="bot-hero-actions">
            <button class="btn primary" type="button" data-action="bot-console" data-bot="${escAttr(bot.id)}">
              <svg class="icon"><use href="#i-send"/></svg>发送指令
            </button>
            <button class="btn btn-ghost" type="button" data-action="manual-sync">
              <svg class="icon"><use href="#i-refresh"/></svg>同步
            </button>
          </div>
        </section>
        <div class="bot-detail-grid">
          <section class="card timeline-card">
            <div class="card-head">
              <div><h2>记忆时间线</h2><p class="card-sub">${detail.records.length} 条记录 · 按天聚合</p></div>
              <button class="link-btn" type="button" data-route="memory">跨 bot 检索 <svg class="icon"><use href="#i-arrow-right"/></svg></button>
            </div>
            <div class="timeline">${timelineHtml(detail.records, { emptyTitle: '这个 bot 还没有记忆' })}</div>
          </section>
          <section class="card digest-card">
            <div class="card-head">
              <div><h2>每日摘要</h2><p class="card-sub">由记录确定性生成的 Markdown</p></div>
              <svg class="icon head-icon"><use href="#i-book"/></svg>
            </div>
            ${detail.digests.length ? `<div class="digest-dates" id="digest-dates"></div>` : ''}
            <div class="markdown digest-body" id="digest-body"></div>
          </section>
        </div>
      </div>`;
    updateAttentionBadge();
    $('#view').setAttribute('aria-busy', 'false');
    if (detail.digestDate) refreshDigest(detail.digestDate);
  }

  // ---------------------------------------------------------------------------
  // Memory view
  // ---------------------------------------------------------------------------

  function memoryFilterChips() {
    const m = state.memory;
    const botChips = [
      `<button class="chip ${!m.bot ? 'is-active' : ''}" type="button" data-action="memory-bot" data-bot="">全部 bot</button>`,
      ...state.bots.map((bot) => `<button class="chip ${m.bot === bot.id ? 'is-active' : ''}" type="button" data-action="memory-bot" data-bot="${escAttr(bot.id)}">
        <span class="chip-dot" style="--bot-color:${safeColor(bot.color)}"></span>${esc(bot.label || bot.id)}
      </button>`),
    ].join('');
    const kinds = ['', 'ticket', 'session', 'decision', 'trigger', 'note'];
    const kindChips = kinds.map((kind) => `
      <button class="chip ${m.kind === kind ? 'is-active' : ''}" type="button" data-action="memory-kind" data-kind="${escAttr(kind)}">
        ${kind ? esc(KIND_META[kind].label) : '全部类型'}
      </button>`).join('');
    return `
      <div class="memory-toolbar">
        <div class="search-field">
          <svg class="icon"><use href="#i-search"/></svg>
          <input class="input search-input" id="memory-search" type="search" autocomplete="off"
                 placeholder="全文检索标题、摘要与标签…（快捷键 /）" value="${escAttr(m.q)}" aria-label="全文检索记忆">
          ${m.q ? `<button class="icon-btn search-clear" type="button" data-action="memory-clear" aria-label="清除搜索"><svg class="icon"><use href="#i-close"/></svg></button>` : ''}
        </div>
        <div class="filter-row">
          <div class="filter-group">${botChips}</div>
          <div class="filter-group">${kindChips}</div>
          <label class="select-field">
            <svg class="icon"><use href="#i-clock"/></svg>
            <select id="memory-days" aria-label="时间范围">
              <option value="0" ${m.days === '0' ? 'selected' : ''}>全部时间</option>
              <option value="1" ${m.days === '1' ? 'selected' : ''}>今天</option>
              <option value="7" ${m.days === '7' ? 'selected' : ''}>近 7 天</option>
              <option value="30" ${m.days === '30' ? 'selected' : ''}>近 30 天</option>
            </select>
          </label>
        </div>
      </div>
      <div class="memory-summary" id="memory-summary"></div>
      <div class="memory-results" id="memory-results"></div>`;
  }

  function filterByDays(items, days) {
    const value = parseInt(days, 10);
    if (!value) return items;
    const cutoff = Date.now() - value * 86400000;
    return items.filter((item) => {
      const date = parseDate(item.updated_at || item.created_at);
      return date && date.getTime() >= cutoff;
    });
  }

  function searchResultItem(entry) {
    const record = entry.record || entry;
    return `
      <button class="search-result" type="button" data-record-id="${escAttr(record.id)}">
        <span class="search-result-head">
          ${kindBadge(record.kind)}
          ${botNameHtml(record.bot)}
          <span class="search-result-time">${esc(relTime(record.updated_at))}</span>
        </span>
        <span class="search-result-title">${esc(record.title)}</span>
        <span class="search-result-snippet">${snippetHtml(entry.snippet || record.summary || '')}</span>
        <span class="search-result-foot">${tagsHtml(record.tags, 4)}</span>
      </button>`;
  }

  async function loadMemory(options = {}) {
    const m = state.memory;
    const requestId = ++m.requestId;
    const results = $('#memory-results');
    const summary = $('#memory-summary');
    if (!results) return;
    if (options.reset) {
      m.items = [];
      m.offset = 0;
      m.total = 0;
    }
    m.loading = true;
    results.innerHTML = `<div class="results-loading"><div class="skeleton skeleton-line w-60"></div><div class="skeleton skeleton-line w-90"></div><div class="skeleton skeleton-line w-70"></div></div>`;
    const params = {};
    if (m.bot) params.bot = m.bot;
    if (m.kind) params.kind = m.kind;
    try {
      let payload;
      if (m.q.trim()) {
        m.mode = 'search';
        params.q = m.q.trim();
        params.limit = 50;
        payload = await API.search(params);
      } else {
        m.mode = 'timeline';
        params.limit = 30;
        params.offset = m.offset;
        payload = await API.records(params);
      }
      if (requestId !== m.requestId || $('#memory-results') !== results) return;
      const items = payload.items || [];
      if (m.mode === 'search') {
        m.items = items;
        m.total = items.length;
      } else {
        m.items = m.offset === 0 ? items : m.items.concat(items);
        m.total = payload.total || m.items.length;
        m.offset = m.items.length;
      }
      let filtered = m.items;
      if (m.mode === 'timeline') filtered = filterByDays(m.items, m.days);
      const hasQuery = Boolean(m.q.trim());
      if (summary) {
        summary.innerHTML = hasQuery
          ? `搜索 <b>“${esc(m.q.trim())}”</b> · 命中 <b>${fmtNum(m.total)}</b> 条${m.bot ? ` · ${esc(botInfo(m.bot).label)}` : ''}`
          : `共 <b>${fmtNum(m.total)}</b> 条记忆 · 当前显示 ${fmtNum(filtered.length)} 条${m.days !== '0' ? ` · ${esc({ 1: '今天', 7: '近 7 天', 30: '近 30 天' }[m.days] || '')}` : ''}`;
      }
      if (!filtered.length) {
        const q = m.q.trim();
        results.innerHTML = emptyState(
          'i-search',
          q ? `没有找到“${esc(q)}”` : '没有符合条件的记忆',
          q ? '换个关键词试试，或清空 bot / 类型筛选。' : '调整时间范围或筛选条件后再试。',
          q ? '<button class="btn btn-ghost" type="button" data-action="memory-clear">清空搜索</button>' : '',
        );
      } else if (m.mode === 'search') {
        results.innerHTML = `<div class="search-results">${filtered.map(searchResultItem).join('')}</div>`;
      } else {
        const moreBtn = m.items.length < m.total && filterByDays(m.items, m.days).length === m.items.length
          ? `<div class="load-more-wrap"><button class="btn btn-ghost" type="button" data-action="memory-load-more">加载更多 · 还剩 ${fmtNum(Math.max(0, m.total - m.items.length))} 条</button></div>`
          : '';
        results.innerHTML = `<div class="timeline">${timelineHtml(filtered)}</div>${moreBtn}`;
      }
    } catch (error) {
      if (requestId !== m.requestId) return;
      if (summary) summary.textContent = '';
      results.innerHTML = `<div class="state-card state-error">
        <div class="state-icon"><svg class="icon"><use href="#i-alert"/></svg></div>
        <h2>搜索失败</h2><p>${esc(error.message)}</p>
        <div class="state-actions"><button class="btn primary" type="button" data-action="memory-retry">重试</button></div>
      </div>`;
    } finally {
      if (requestId === m.requestId) m.loading = false;
    }
  }

  function renderMemory(token) {
    if (!state.memory.initialized) {
      state.memory.initialized = true;
      state.memory.bot = '';
      state.memory.kind = '';
    }
    $('#view').innerHTML = `<div class="view-content memory-view">${memoryFilterChips()}</div>`;
    $('#view').setAttribute('aria-busy', 'false');
    const searchInput = $('#memory-search');
    if (searchInput) {
      searchInput.addEventListener('input', debounce((event) => {
        state.memory.q = event.target.value;
        const clear = $('.search-clear');
        if (clear) clear.hidden = !state.memory.q;
        loadMemory({ reset: true });
      }, 260));
      searchInput.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
          event.target.value = '';
          state.memory.q = '';
          loadMemory({ reset: true });
        }
      });
    }
    const daysSelect = $('#memory-days');
    if (daysSelect) {
      daysSelect.addEventListener('change', (event) => {
        state.memory.days = event.target.value;
        loadMemory({ reset: false });
      });
    }
    loadMemory({ reset: true });
  }

  // ---------------------------------------------------------------------------
  // Board view
  // ---------------------------------------------------------------------------

  function boardCardHtml(card, columnId, columnIndex, columns) {
    const assignee = card.assignee ? botInfo(card.assignee) : null;
    const prevColumn = columns[columnIndex - 1];
    const nextColumn = columns[columnIndex + 1];
    return `
      <article class="kanban-card" draggable="true" data-card-id="${escAttr(card.id)}" data-column="${escAttr(columnId)}">
        <header class="kanban-card-head">
          <span class="drag-handle" aria-hidden="true"><svg class="icon"><use href="#i-grip"/></svg></span>
          <button class="icon-btn card-edit" type="button" data-action="card-edit" data-id="${escAttr(card.id)}" aria-label="编辑卡片">
            <svg class="icon"><use href="#i-edit"/></svg>
          </button>
        </header>
        <h3 class="kanban-title">${esc(card.title)}</h3>
        ${card.desc ? `<p class="kanban-desc">${esc(truncate(card.desc, 130))}</p>` : ''}
        ${tagsHtml(card.tags, 3)}
        <footer class="kanban-foot">
          <span class="kanban-assignee">
            ${assignee ? `${avatarHtml(assignee, 'xs')}<span>${esc(assignee.label || card.assignee)}</span>` : '<span class="kanban-unassigned">未指派</span>'}
          </span>
          <span class="kanban-age">${esc(relTime(card.updated_at))}</span>
        </footer>
        <div class="kanban-move">
          <button class="icon-btn" type="button" data-action="card-move" data-id="${escAttr(card.id)}" data-column="${prevColumn ? escAttr(prevColumn.id) : ''}" ${prevColumn ? '' : 'disabled'} aria-label="移到 ${prevColumn ? escAttr(prevColumn.title) : '上一列'}">
            <svg class="icon"><use href="#i-chevron-left"/></svg>
          </button>
          <span class="kanban-move-label">${esc(columns[columnIndex].title)}</span>
          <button class="icon-btn" type="button" data-action="card-move" data-id="${escAttr(card.id)}" data-column="${nextColumn ? escAttr(nextColumn.id) : ''}" ${nextColumn ? '' : 'disabled'} aria-label="移到 ${nextColumn ? escAttr(nextColumn.title) : '下一列'}">
            <svg class="icon"><use href="#i-chevron-right"/></svg>
          </button>
        </div>
      </article>`;
  }

  function boardHtml(board) {
    const columns = board.columns || [];
    const cards = board.cards || [];
    return `
      <div class="board-toolbar">
        <div class="board-meta">
          <span class="board-stat"><b>${fmtNum(cards.length)}</b> 张卡片</span>
          <span class="board-stat"><b>${fmtNum(cards.filter((card) => card.assignee).length)}</b> 已指派</span>
          <span class="board-stat"><b>${fmtNum(cards.filter((card) => !card.assignee).length)}</b> 未指派</span>
        </div>
        <div class="board-actions">
          <button class="btn btn-ghost" type="button" data-action="board-refresh"><svg class="icon"><use href="#i-refresh"/></svg>刷新</button>
          <button class="btn primary" type="button" data-action="card-new"><svg class="icon"><use href="#i-plus"/></svg>新建卡片</button>
        </div>
      </div>
      <div class="board-columns">
        ${columns.map((column, index) => {
          const columnCards = cards.filter((card) => card.column === column.id);
          return `
            <section class="board-col" data-column="${escAttr(column.id)}" aria-label="${escAttr(column.title)}">
              <header class="board-col-head">
                <span class="board-col-dot col-${escAttr(column.id)}" aria-hidden="true"></span>
                <h2>${esc(column.title)}</h2>
                <span class="board-col-count">${columnCards.length}</span>
                <button class="icon-btn board-add" type="button" data-action="card-new" data-column="${escAttr(column.id)}" aria-label="在 ${escAttr(column.title)} 新建卡片">
                  <svg class="icon"><use href="#i-plus"/></svg>
                </button>
              </header>
              <div class="board-col-body">
                ${columnCards.length
                  ? columnCards.map((card) => boardCardHtml(card, column.id, index, columns)).join('')
                  : `<button class="board-empty" type="button" data-action="card-new" data-column="${escAttr(column.id)}">
                      <svg class="icon"><use href="#i-plus"/></svg><span>拖动卡片到这里，或点击新建</span>
                    </button>`}
              </div>
            </section>`;
        }).join('')}
      </div>`;
  }

  function bindBoardDnd() {
    const cards = $$('.kanban-card');
    cards.forEach((card) => {
      card.addEventListener('dragstart', (event) => {
        state.boardDragId = card.dataset.cardId;
        card.classList.add('is-dragging');
        if (event.dataTransfer) {
          event.dataTransfer.effectAllowed = 'move';
          event.dataTransfer.setData('text/plain', card.dataset.cardId);
        }
      });
      card.addEventListener('dragend', () => {
        state.boardDragId = null;
        card.classList.remove('is-dragging');
        $$('.board-col').forEach((col) => col.classList.remove('is-drop-target'));
      });
    });
    $$('.board-col').forEach((column) => {
      column.addEventListener('dragover', (event) => {
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
        column.classList.add('is-drop-target');
      });
      column.addEventListener('dragleave', (event) => {
        if (!column.contains(event.relatedTarget)) column.classList.remove('is-drop-target');
      });
      column.addEventListener('drop', (event) => {
        event.preventDefault();
        column.classList.remove('is-drop-target');
        const cardId = state.boardDragId || (event.dataTransfer && event.dataTransfer.getData('text/plain'));
        if (!cardId) return;
        moveCard(cardId, column.dataset.column);
      });
    });
  }

  async function renderBoard(token) {
    const board = await API.board();
    if (token !== state.viewToken) return;
    state.board = board;
    $('#view').innerHTML = `<div class="view-content board-view">${boardHtml(board)}</div>`;
    bindBoardDnd();
    $('#view').setAttribute('aria-busy', 'false');
  }

  async function refreshBoard(silent = true) {
    if (state.route.name !== 'board') return;
    try {
      const board = await API.board();
      if (state.route.name !== 'board') return;
      state.board = board;
      const view = $('#view');
      if (view) {
        view.innerHTML = `<div class="view-content board-view">${boardHtml(board)}</div>`;
        bindBoardDnd();
        if (!silent) toast('看板已刷新', 'ok');
      }
    } catch (error) {
      if (!silent) toast(error.message, 'danger');
    }
  }

  async function moveCard(cardId, columnId) {
    if (!state.board || !columnId) return;
    const card = state.board.cards.find((item) => item.id === cardId);
    if (!card || card.column === columnId || !columnId) return;
    const previous = card.column;
    card.column = columnId;
    card.updated_at = new Date().toISOString();
    const view = $('#view');
    const scrollY = window.scrollY;
    view.innerHTML = `<div class="view-content board-view">${boardHtml(state.board)}</div>`;
    bindBoardDnd();
    window.scrollTo({ top: scrollY });
    try {
      const data = await API.updateCard(cardId, { column: columnId });
      if (data && data.card) {
        Object.assign(card, data.card);
      }
      toast(`已移到「${(state.board.columns.find((c) => c.id === columnId) || {}).title || columnId}」`, 'ok');
    } catch (error) {
      card.column = previous;
      view.innerHTML = `<div class="view-content board-view">${boardHtml(state.board)}</div>`;
      bindBoardDnd();
      toast(`移动失败：${error.message}`, 'danger');
    }
  }

  function openCardModal(card, defaultColumn) {
    const isEdit = Boolean(card);
    const columns = (state.board && state.board.columns) || [];
    const assignees = state.bots.map((bot) => `<option value="${escAttr(bot.id)}" ${card && card.assignee === bot.id ? 'selected' : ''}>${esc(bot.label || bot.id)}</option>`).join('');
    const columnOptions = columns.map((column) => `<option value="${escAttr(column.id)}" ${(card ? card.column : defaultColumn || 'inbox') === column.id ? 'selected' : ''}>${esc(column.title)}</option>`).join('');
    const overlay = openOverlay({
      title: isEdit ? '编辑卡片' : '新建卡片',
      body: `
        <form id="card-form" class="form">
          <label class="field">
            <span class="field-label">标题</span>
            <input class="input" name="title" required maxlength="120" value="${escAttr(card ? card.title : '')}" placeholder="一句话说明要做什么">
          </label>
          <label class="field">
            <span class="field-label">描述</span>
            <textarea class="input textarea" name="desc" rows="4" placeholder="背景、验收标准、相关链接…">${esc(card ? card.desc : '')}</textarea>
          </label>
          <div class="form-row">
            <label class="field">
              <span class="field-label">所在列</span>
              <select class="input" name="column">${columnOptions}</select>
            </label>
            <label class="field">
              <span class="field-label">负责人</span>
              <select class="input" name="assignee">
                <option value="" ${!card || !card.assignee ? 'selected' : ''}>未指派</option>
                ${assignees}
              </select>
            </label>
          </div>
          <label class="field">
            <span class="field-label">标签</span>
            <input class="input" name="tags" value="${escAttr(card && card.tags ? card.tags.join(', ') : '')}" placeholder="用逗号分隔，如 web, sse">
          </label>
          <div class="form-actions">
            ${isEdit ? `<button class="btn danger btn-ghost-danger" type="button" data-action="card-delete" data-id="${escAttr(card.id)}"><svg class="icon"><use href="#i-trash"/></svg>删除</button>` : '<span></span>'}
            <span class="form-actions-right">
              <button class="btn btn-ghost" type="button" data-overlay-close>取消</button>
              <button class="btn primary" type="submit">${isEdit ? '保存修改' : '创建卡片'}</button>
            </span>
          </div>
        </form>`,
    });
    const form = $('#card-form', overlay.node);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const formData = new FormData(form);
      const title = String(formData.get('title') || '').trim();
      if (!title) {
        toast('标题不能为空', 'warn');
        return;
      }
      const body = {
        title,
        desc: String(formData.get('desc') || ''),
        column: String(formData.get('column') || 'inbox'),
        assignee: String(formData.get('assignee') || '') || null,
        tags: String(formData.get('tags') || '').split(',').map((tag) => tag.trim()).filter(Boolean),
      };
      const submit = $('button[type="submit"]', form);
      submit.disabled = true;
      try {
        if (isEdit) {
          const data = await API.updateCard(card.id, body);
          if (data && data.card) Object.assign(card, data.card);
        } else {
          body.author = 'human';
          await API.createCard(body);
        }
        overlay.close();
        toast(isEdit ? '卡片已保存' : '卡片已创建', 'ok');
        refreshBoard(true);
      } catch (error) {
        submit.disabled = false;
        toast(error.message, 'danger');
      }
    });
  }

  // ---------------------------------------------------------------------------
  // Console view
  // ---------------------------------------------------------------------------

  const CONSOLE_TEMPLATES = [
    '汇报你最近 2 小时的进展，并更新看板上的卡片。',
    '检查共享板「待验收」的卡片，逐条验证并说明结论。',
    '检索其他 bot 最近的工作，指出可能与当前任务冲突的地方。',
  ];

  function consoleStatusBadge(status) {
    const map = {
      sent: { label: '已送达', cls: 'ok' },
      skipped: { label: '已跳过', cls: 'warn' },
      failed: { label: '投递失败', cls: 'bad' },
      pending: { label: '投递中', cls: 'muted' },
    };
    const meta = map[status] || { label: status || '未知', cls: 'muted' };
    return `<span class="badge status status-${meta.cls}">${esc(meta.label)}</span>`;
  }

  function consoleBotCards() {
    return state.bots.map((bot) => `
      <button class="console-bot ${state.console.bot === bot.id ? 'is-active' : ''}" type="button"
              data-action="console-bot" data-bot="${escAttr(bot.id)}" aria-pressed="${state.console.bot === bot.id}">
        ${avatarHtml(bot, 'md')}
        <span class="console-bot-text">
          <span class="console-bot-name">${esc(bot.label || bot.id)}</span>
          <span class="console-bot-meta">${esc(bot.profile || 'agent')} · ${STATUS_META[bot.status] ? STATUS_META[bot.status].label : '未知'}</span>
        </span>
        <span class="console-bot-check" aria-hidden="true"><svg class="icon"><use href="#i-check"/></svg></span>
      </button>`).join('');
  }

  function consoleResultHtml() {
    const result = state.console.result;
    if (!result) return '';
    const botLabel = botInfo(result.target_bot || state.console.bot).label || state.console.bot;
    const tone = result.status === 'sent' ? 'ok' : result.status === 'failed' ? 'danger' : 'warn';
    return `
      <div class="console-result result-${tone}" role="status">
        <div class="console-result-head">
          ${consoleStatusBadge(result.status)}
          <span>指令已提交给 <b>${esc(botLabel)}</b></span>
          <span class="console-result-time">${esc(relTime(result.created_at || new Date().toISOString()))}</span>
        </div>
        <p class="console-result-text">${esc(truncate(result.text || '', 240))}</p>
        ${result.message_id ? `<p class="console-result-meta">消息 ID <code>${esc(result.message_id)}</code></p>` : ''}
        ${result.status === 'skipped'
          ? `<p class="console-result-meta"><svg class="icon"><use href="#i-info"/></svg>中继未启用（dry-run）：指令已写入 hub 并记录投递状态，但不会通过聊天源发送给 bot。启用飞书中继后即可真实送达。</p>`
          : ''}
        ${result.error ? `<p class="console-result-meta error">${esc(result.error)}</p>` : ''}
      </div>`;
  }

  function commandHistoryHtml() {
    const items = state.console.history || [];
    if (!items.length) return emptyState('i-console', '还没有指令记录', '从上方选择一个 bot 并发送第一条指令。');
    return `<div class="history-list">${items.map((command) => `
      <div class="history-item">
        <div class="history-head">
          ${consoleStatusBadge(command.status)}
          ${botNameHtml(command.target_bot)}
          <span class="history-time">${esc(relTime(command.created_at))}</span>
        </div>
        <p class="history-text">${esc(truncate(command.text, 180))}</p>
        <div class="history-meta">
          <span>via ${esc(command.via || 'feishu')}</span>
          ${command.message_id ? `<span>消息 <code>${esc(command.message_id)}</code></span>` : ''}
          ${command.error ? `<span class="error">${esc(command.error)}</span>` : ''}
        </div>
      </div>`).join('')}</div>`;
  }

  async function loadCommandHistory() {
    state.console.historyLoading = true;
    try {
      const data = await API.commands(30);
      state.console.history = data.items || [];
    } catch (error) {
      state.console.history = [];
    }
    state.console.historyLoading = false;
    const history = $('#command-history');
    if (history) history.innerHTML = commandHistoryHtml();
  }

  function renderConsole(token) {
    if (!state.console.bot && state.bots.length) {
      const paramBot = state.route.name === 'console' ? state.route.param : '';
      const preferred = paramBot || (state.overview && state.overview.bots && state.overview.bots[0] && state.overview.bots[0].id) || state.bots[0].id;
      state.console.bot = state.bots.some((bot) => bot.id === preferred) ? preferred : state.bots[0].id;
    }
    const draft = state.console.draft || '';
    $('#view').innerHTML = `
      <div class="view-content console-view">
        <div class="console-grid">
          <section class="card console-compose">
            <div class="card-head">
              <div><h2>发送指令</h2><p class="card-sub">指令经聊天源送达，bot 的正常授权与审计链路仍然生效</p></div>
              <svg class="icon head-icon"><use href="#i-console"/></svg>
            </div>
            <div class="field">
              <span class="field-label">目标智能体</span>
              <div class="console-bots" role="radiogroup" aria-label="选择目标智能体">${consoleBotCards()}</div>
            </div>
            <label class="field">
              <span class="field-label">指令内容</span>
              <textarea class="input textarea console-textarea" id="console-text" maxlength="800"
                        placeholder="例如：检查共享板「待验收」的卡片，逐条验证并说明结论。">${esc(draft)}</textarea>
            </label>
            <div class="console-templates">
              <span class="templates-label">快捷模板</span>
              ${CONSOLE_TEMPLATES.map((template, index) => `<button class="chip" type="button" data-action="console-template" data-index="${index}">${esc(template.slice(0, 14))}…</button>`).join('')}
            </div>
            <div class="console-send-row">
              <p class="console-hint">
                <svg class="icon"><use href="#i-info"/></svg>
                将以桥接机器人身份在聊天群中 @${esc(botInfo(state.console.bot).label || state.console.bot)}，不会直接注入 bot 进程。
              </p>
              <div class="console-send-actions">
                <span class="char-count" id="console-count">${draft.length}/800</span>
                <button class="btn primary console-send" type="button" data-action="console-send">
                  <svg class="icon"><use href="#i-send"/></svg><span>发送指令</span>
                </button>
              </div>
            </div>
            <div id="console-result">${consoleResultHtml()}</div>
          </section>
          <section class="card console-history">
            <div class="card-head">
              <div><h2>指令历史</h2><p class="card-sub">最近 30 条投递记录</p></div>
              <button class="icon-btn" type="button" data-action="history-refresh" aria-label="刷新历史">
                <svg class="icon"><use href="#i-refresh"/></svg>
              </button>
            </div>
            <div id="command-history"><div class="results-loading"><div class="skeleton skeleton-line w-60"></div><div class="skeleton skeleton-line w-90"></div><div class="skeleton skeleton-line w-70"></div></div></div>
          </section>
        </div>
      </div>`;
    $('#view').setAttribute('aria-busy', 'false');
    const textarea = $('#console-text');
    if (textarea) {
      textarea.addEventListener('input', () => {
        state.console.draft = textarea.value;
        const counter = $('#console-count');
        if (counter) counter.textContent = `${textarea.value.length}/800`;
      });
      textarea.addEventListener('keydown', (event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
          event.preventDefault();
          sendConsoleCommand();
        }
      });
    }
    loadCommandHistory();
  }

  async function sendConsoleCommand() {
    if (state.console.sending) return;
    const textarea = $('#console-text');
    const text = (textarea ? textarea.value : state.console.draft || '').trim();
    const target = state.console.bot;
    if (!target) {
      toast('请先选择一个智能体', 'warn');
      return;
    }
    if (!text) {
      toast('指令内容不能为空', 'warn');
      if (textarea) textarea.focus();
      return;
    }
    state.console.sending = true;
    const button = $('.console-send');
    if (button) {
      button.disabled = true;
      button.classList.add('is-loading');
    }
    try {
      const data = await API.sendCommand({ target_bot: target, text });
      const command = (data && data.command) || data || { status: 'pending', target_bot: target, text };
      state.console.result = command;
      state.console.draft = '';
      if (textarea) {
        textarea.value = '';
        textarea.focus();
      }
      const counter = $('#console-count');
      if (counter) counter.textContent = '0/800';
      const resultNode = $('#console-result');
      if (resultNode) resultNode.innerHTML = consoleResultHtml();
      const status = command.status || 'pending';
      if (status === 'sent') toast(`指令已送达 ${botInfo(target).label || target}`, 'ok');
      else if (status === 'skipped') toast('中继未启用，指令已记录（skipped）', 'warn');
      else toast(`投递状态：${status}`, 'warn');
      loadCommandHistory();
    } catch (error) {
      const status = error.status === 502 ? 'failed' : 'failed';
      state.console.result = {
        status,
        target_bot: target,
        text,
        error: error.message,
        created_at: new Date().toISOString(),
        message_id: (error.data && error.data.command && error.data.command.message_id) || null,
      };
      const resultNode = $('#console-result');
      if (resultNode) resultNode.innerHTML = consoleResultHtml();
      toast(`投递失败：${error.message}`, 'danger');
    } finally {
      state.console.sending = false;
      if (button) {
        button.disabled = false;
        button.classList.remove('is-loading');
      }
    }
  }

  // ---------------------------------------------------------------------------
  // About view
  // ---------------------------------------------------------------------------

  function aboutHtml() {
    const health = state.health || {};
    const hub = health.hub || {};
    const redaction = health.redaction || { total: 0, by_rule: {} };
    const rules = Object.entries(redaction.by_rule || {}).sort((a, b) => b[1] - a[1]);
    const ruleRows = rules.length
      ? rules.map(([rule, count]) => `<tr><td>${esc(RULE_LABELS[rule] || rule)}</td><td><code>${esc(rule)}</code></td><td class="num">${fmtNum(count)}</td></tr>`).join('')
      : '<tr><td colspan="3" class="muted">暂无脱敏统计</td></tr>';
    const kinds = Object.entries(KIND_META).map(([kind, meta]) => `<tr><td>${kindBadge(kind)}</td><td><code>${esc(kind)}</code></td><td>${esc(meta.label)}</td></tr>`).join('');
    const endpoints = [
      ['GET', '/api/health', '服务与 hub 健康状态'],
      ['GET', '/api/overview', '概览：bot 摘要、计数、14 天活动、关注项'],
      ['GET', '/api/bots/{id}', '单个 bot 摘要、最近记录与日报列表'],
      ['GET', '/api/digests/{bot}/{date}', '某个 bot 某天的 Markdown 日报'],
      ['GET', '/api/records?bot=&kind=&q=', '记忆列表与分页'],
      ['GET', '/api/records/{id}', '单条完整记录'],
      ['GET', '/api/search?q=', 'SQLite FTS5 全文检索（带摘要高亮）'],
      ['GET', '/api/board', '共享看板'],
      ['POST', '/api/board/cards', '新建卡片'],
      ['PATCH', '/api/board/cards/{id}', '移动 / 编辑卡片'],
      ['POST', '/api/commands', '向一个 bot 投递指令'],
      ['GET', '/api/events', 'Server-Sent Events 变更推送'],
    ].map(([method, path, desc]) => `<tr><td><span class="method method-${method.toLowerCase()}">${method}</span></td><td><code>${esc(path)}</code></td><td>${esc(desc)}</td></tr>`).join('');
    return `
      <div class="view-content about-view">
        <div class="about-grid">
          <section class="card about-card">
            <div class="card-head"><div><h2>运行环境</h2><p class="card-sub">当前连接的服务与 hub</p></div><svg class="icon head-icon"><use href="#i-hub"/></svg></div>
            <div class="kv-list">
              <div class="kv-row"><span class="kv-key">hub 目录</span><span class="kv-value"><code>${esc(hub.dir || '—')}</code></span></div>
              <div class="kv-row"><span class="kv-key">记录数</span><span class="kv-value">${fmtNum(hub.records || 0)} 条 · ${fmtNum(hub.bots || 0)} 个 bot</span></div>
              <div class="kv-row"><span class="kv-key">最近同步</span><span class="kv-value">${esc(hub.last_sync ? absTime(hub.last_sync) : '—')}（${esc(relTime(hub.last_sync))}）</span></div>
              <div class="kv-row"><span class="kv-key">服务版本</span><span class="kv-value"><code>${esc(health.version || '—')}</code></span></div>
              <div class="kv-row"><span class="kv-key">已运行</span><span class="kv-value">${esc(fmtUptime(health.uptime_s))}</span></div>
              <div class="kv-row"><span class="kv-key">实时连接</span><span class="kv-value">${state.conn === 'live' ? '<span class="badge status status-ok">SSE 已连接</span>' : `<span class="badge status status-warn">${state.conn === 'connecting' ? '正在连接' : '已断开（自动重连中）'}</span>`}</span></div>
            </div>
          </section>
          <section class="card about-card">
            <div class="card-head"><div><h2>访问令牌</h2><p class="card-sub">可选：服务端启用 token 校验时使用</p></div><svg class="icon head-icon"><use href="#i-lock"/></svg></div>
            <p class="about-text">令牌只保存在当前浏览器的 localStorage 中，随请求以 <code>Authorization: Bearer …</code> 头发送。</p>
            <div class="token-row">
              <span class="token-status ${getToken() ? 'is-set' : ''}">${getToken() ? '已设置访问令牌' : '未设置访问令牌'}</span>
              <div class="token-actions">
                <button class="btn btn-ghost" type="button" data-action="set-token">${getToken() ? '更新' : '设置'}令牌</button>
                ${getToken() ? '<button class="btn btn-ghost btn-ghost-danger" type="button" data-action="clear-token">清除</button>' : ''}
              </div>
            </div>
          </section>
          <section class="card about-card about-wide">
            <div class="card-head"><div><h2>脱敏策略</h2><p class="card-sub">写入 hub 前统一掩码，控制台展示的是掩码后的内容</p></div><svg class="icon head-icon"><use href="#i-shield"/></svg></div>
            <p class="about-text">密钥形状、私网地址与内部域名在写入时被替换；git SHA、UUID 与飞书 id 等正常标识保留。累计掩码 <b>${fmtNum(redaction.total || 0)}</b> 处。</p>
            <div class="table-wrap"><table class="data-table"><thead><tr><th>规则</th><th>标识</th><th class="num">命中次数</th></tr></thead><tbody>${ruleRows}</tbody></table></div>
          </section>
          <section class="card about-card about-wide">
            <div class="card-head"><div><h2>记忆模型</h2><p class="card-sub">记录信封 schema v1，一个 JSON 文件一条记录</p></div><svg class="icon head-icon"><use href="#i-memory"/></svg></div>
            <div class="table-wrap"><table class="data-table"><thead><tr><th>类型</th><th>kind</th><th>说明</th></tr></thead><tbody>${kinds}</tbody></table></div>
            <p class="about-text">每条记录包含 <code>id / bot / kind / title / summary / created_at / updated_at / tags / refs / source / payload / redactions</code>。记录按 <code>&lt;bot&gt;:&lt;kind&gt;:&lt;source-id&gt;</code> 命名，一个写入者一个文件。</p>
          </section>
          <section class="card about-card about-wide">
            <div class="card-head"><div><h2>HTTP API</h2><p class="card-sub">控制台构建所依据的冻结契约（API.md）</p></div><svg class="icon head-icon"><use href="#i-console"/></svg></div>
            <div class="table-wrap"><table class="data-table api-table"><thead><tr><th>方法</th><th>路径</th><th>说明</th></tr></thead><tbody>${endpoints}</tbody></table></div>
          </section>
          <section class="card about-card">
            <div class="card-head"><div><h2>部署</h2><p class="card-sub">标准库单进程，无外部依赖</p></div><svg class="icon head-icon"><use href="#i-bots"/></svg></div>
            <pre class="code-block"><code># 1. 初始化 hub 与配置
ctxbridge init /srv/context-hub
ctxbridge gen-config &gt; config.toml

# 2. 采集一轮并建索引
ctxbridge sync
ctxbridge index

# 3. 启动控制台
ctxbridge serve --port 8080
# → http://localhost:8080/</code></pre>
            <p class="about-text">控制台与 API 同源同端口；hub 数据留在本机，公开仓库只包含代码与合成演示数据。</p>
          </section>
          <section class="card about-card">
            <div class="card-head"><div><h2>快捷键</h2><p class="card-sub">键盘操作</p></div><svg class="icon head-icon"><use href="#i-spark"/></svg></div>
            <div class="kv-list">
              <div class="kv-row"><span class="kv-key">全文检索</span><span class="kv-value"><kbd>/</kbd> 或 <kbd>Ctrl</kbd>+<kbd>K</kbd></span></div>
              <div class="kv-row"><span class="kv-key">发送指令</span><span class="kv-value"><kbd>Ctrl</kbd>+<kbd>Enter</kbd></span></div>
              <div class="kv-row"><span class="kv-key">关闭弹层</span><span class="kv-value"><kbd>Esc</kbd></span></div>
              <div class="kv-row"><span class="kv-key">切换主题</span><span class="kv-value"><kbd>T</kbd></span></div>
            </div>
          </section>
        </div>
      </div>`;
  }

  async function renderAbout(token) {
    if (!state.health) {
      state.health = await API.health().catch(() => null);
    }
    if (token !== state.viewToken) return;
    $('#view').innerHTML = aboutHtml();
    $('#view').setAttribute('aria-busy', 'false');
  }

  // ---------------------------------------------------------------------------
  // Routing
  // ---------------------------------------------------------------------------

  async function renderRoute() {
    const route = currentRoute();
    state.route = route;
    closeTopOverlay();
    setActiveNav(route.name);
    document.body.dataset.page = route.name;
    setPageHead(route);
    closeMobileNav();
    const token = ++state.viewToken;
    showLoading();
    if (!state.bots.length) {
      try {
        const data = await API.bots();
        state.bots = data.items || [];
      } catch (error) {
        if (token === state.viewToken) showViewError(error);
        return;
      }
    }
    try {
      if (route.name === 'overview') await renderOverview(token);
      else if (route.name === 'bots') await renderBots(token);
      else if (route.name === 'bot') await renderBotDetail(route.botId, token);
      else if (route.name === 'memory') await renderMemory(token);
      else if (route.name === 'board') await renderBoard(token);
      else if (route.name === 'console') await renderConsole(token);
      else if (route.name === 'about') await renderAbout(token);
      if (token === state.viewToken) {
        window.scrollTo({ top: 0, behavior: 'auto' });
      }
    } catch (error) {
      if (token === state.viewToken) showViewError(error);
    }
  }

  // ---------------------------------------------------------------------------
  // SSE live updates
  // ---------------------------------------------------------------------------

  let eventSource = null;
  let reconnectTimer = null;
  let reconnectDelay = 2000;

  function setConn(stateName) {
    state.conn = stateName;
    const node = $('#conn-status');
    if (!node) return;
    node.dataset.state = stateName;
    const text = { live: '实时连接正常', connecting: '连接中…', offline: '连接断开，重连中' }[stateName] || stateName;
    const label = $('.conn-text', node);
    if (label) label.textContent = text;
    // Keep the 关于 view live if it is open.
    if (state.route.name === 'about') {
      const badges = $$('.about-view .badge');
      const liveBadge = state.conn === 'live'
        ? '<span class="badge status status-ok">SSE 已连接</span>'
        : `<span class="badge status status-warn">${state.conn === 'connecting' ? '正在连接' : '已断开（自动重连中）'}</span>`;
      if (badges.length) {
        const target = badges[badges.length - 1];
        if (target && target.parentElement && target.parentElement.querySelector('.badge')) {
          // best-effort: find the realtime row by text
          const rows = $$('.about-view .kv-row');
          const row = rows.find((item) => item.textContent.includes('实时连接'));
          if (row) {
            const value = $('.kv-value', row);
            if (value) value.innerHTML = liveBadge;
          }
        }
      }
    }
  }

  const silentRefresh = throttle(async () => {
    if (document.hidden) return;
    const route = state.route.name;
    try {
      if (route === 'overview') {
        state.overview = await API.overview();
        updateAttentionBadge();
        if (state.route.name === 'overview') await renderOverview(state.viewToken);
      } else if (route === 'board') {
        await refreshBoard(true);
      } else if (route === 'console') {
        await loadCommandHistory();
      } else if (route === 'bot') {
        if (state.botDetail.botId) await renderBotDetail(state.botDetail.botId, state.viewToken);
      } else if (route === 'bots') {
        const data = await API.bots();
        state.bots = data.items || [];
        if (state.route.name === 'bots') await renderBots(state.viewToken);
      }
    } catch (error) {
      /* live refresh must never break the view */
    }
  }, 1200);

  function connectEvents() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    clearTimeout(reconnectTimer);
    setConn('connecting');
    const token = getToken();
    const url = token ? `/api/events?token=${encodeURIComponent(token)}` : '/api/events';
    try {
      eventSource = new EventSource(url);
    } catch (error) {
      setConn('offline');
      scheduleReconnect();
      return;
    }
    eventSource.addEventListener('hello', () => {
      reconnectDelay = 2000;
      setConn('live');
    });
    eventSource.addEventListener('sync', () => {
      setConn('live');
      silentRefresh();
    });
    eventSource.addEventListener('board', () => {
      setConn('live');
      if (state.route.name === 'board') silentRefresh();
    });
    eventSource.addEventListener('command', () => {
      setConn('live');
      if (state.route.name === 'console') loadCommandHistory();
    });
    eventSource.onerror = () => {
      setConn('offline');
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
      scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => {
      if (document.hidden) {
        scheduleReconnect();
        return;
      }
      connectEvents();
    }, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 1.7, 20000);
  }

  // ---------------------------------------------------------------------------
  // Global event wiring
  // ---------------------------------------------------------------------------

  const memoryDebouncedReload = debounce(() => {
    if (state.route.name === 'memory') loadMemory({ reset: true });
  }, 200);

  function handleGlobalClick(event) {
    const overlayClose = event.target.closest('[data-overlay-close]');
    if (overlayClose) {
      // Overlay's own click listener also handles this; the generic path is a no-op.
      return;
    }
    const routeEl = event.target.closest('[data-route]');
    if (routeEl) {
      event.preventDefault();
      navigate(routeEl.dataset.route);
      return;
    }
    const recordEl = event.target.closest('[data-record-id]');
    if (recordEl && !recordEl.closest('[data-action]')) {
      event.preventDefault();
      openRecordDrawer(recordEl.dataset.recordId);
      return;
    }
    const actionEl = event.target.closest('[data-action]');
    if (!actionEl) {
      const botEl = event.target.closest('[data-bot-id]');
      if (botEl) navigate(`bots/${botEl.dataset.botId}`);
      return;
    }
    const action = actionEl.dataset.action;
    if (actionEl.disabled) return;
    switch (action) {
      case 'toggle-theme':
        toggleTheme();
        break;
      case 'toggle-nav':
        toggleMobileNav();
        break;
      case 'retry-route':
        renderRoute();
        break;
      case 'manual-sync':
        triggerSync();
        break;
      case 'focus-attention': {
        const panel = $('#attention-panel');
        if (panel) {
          panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
          panel.classList.add('is-highlighted');
          setTimeout(() => panel.classList.remove('is-highlighted'), 1600);
        }
        break;
      }
      case 'open-bot':
        navigate(`bots/${actionEl.dataset.botId}`);
        break;
      case 'handle-attention': {
        state.console.bot = actionEl.dataset.bot || state.console.bot;
        state.console.draft = `关于「${actionEl.dataset.question || '当前问题'}」，请给出你的判断与下一步计划。`;
        navigate('console');
        break;
      }
      case 'bot-console':
        state.console.bot = actionEl.dataset.bot || state.console.bot;
        navigate('console');
        break;
      case 'memory-bot':
        state.memory.bot = actionEl.dataset.bot || '';
        reloadMemoryView();
        break;
      case 'memory-kind':
        state.memory.kind = actionEl.dataset.kind || '';
        reloadMemoryView();
        break;
      case 'memory-clear':
        state.memory.q = '';
        reloadMemoryView();
        break;
      case 'memory-load-more':
        loadMemory({ reset: false });
        break;
      case 'memory-retry':
        loadMemory({ reset: true });
        break;
      case 'card-new':
        openCardModal(null, actionEl.dataset.column || 'inbox');
        break;
      case 'card-edit': {
        const card = state.board && state.board.cards.find((item) => item.id === actionEl.dataset.id);
        if (card) openCardModal(card);
        break;
      }
      case 'card-delete':
        deleteCard(actionEl.dataset.id);
        break;
      case 'card-move':
        moveCard(actionEl.dataset.id, actionEl.dataset.column);
        break;
      case 'board-refresh':
        refreshBoard(false);
        break;
      case 'console-bot':
        state.console.bot = actionEl.dataset.bot;
        $$('.console-bot').forEach((node) => {
          const active = node.dataset.bot === state.console.bot;
          node.classList.toggle('is-active', active);
          node.setAttribute('aria-pressed', String(active));
        });
        {
          const hint = $('.console-hint');
          if (hint) {
            hint.innerHTML = `<svg class="icon"><use href="#i-info"/></svg>将以桥接机器人身份在聊天群中 @${esc(botInfo(state.console.bot).label || state.console.bot)}，不会直接注入 bot 进程。`;
          }
        }
        break;
      case 'console-template': {
        const template = CONSOLE_TEMPLATES[Number(actionEl.dataset.index)] || '';
        state.console.draft = template;
        const textarea = $('#console-text');
        if (textarea) {
          textarea.value = template;
          textarea.focus();
        }
        const counter = $('#console-count');
        if (counter) counter.textContent = `${template.length}/800`;
        break;
      }
      case 'console-send':
        sendConsoleCommand();
        break;
      case 'history-refresh':
        loadCommandHistory();
        break;
      case 'digest-date':
        refreshDigest(actionEl.dataset.date);
        break;
      case 'copy-record-id':
        copyText(actionEl.dataset.id).then((ok) => {
          toast(ok ? '记录 ID 已复制' : '复制失败，请手动选择', ok ? 'ok' : 'warn');
        });
        break;
      case 'set-token': {
        const overlay = openOverlay({
          title: '设置访问令牌',
          body: `
            <p class="overlay-hint">粘贴部署配置中的 <code>server.token</code>。令牌只保存在本机浏览器。</p>
            <form id="set-token-form" class="form">
              <label class="field"><span class="field-label">令牌</span>
                <input class="input" name="token" type="password" value="${escAttr(getToken())}" autocomplete="off">
              </label>
              <div class="form-actions">
                <button class="btn btn-ghost" type="button" data-overlay-close>取消</button>
                <button class="btn primary" type="submit">保存</button>
              </div>
            </form>`,
        });
        $('#set-token-form', overlay.node).addEventListener('submit', (event) => {
          event.preventDefault();
          saveToken(String(new FormData(event.target).get('token') || '').trim());
          overlay.close();
          toast('令牌已保存', 'ok');
          renderRoute();
          connectEvents();
        });
        break;
      }
      case 'clear-token':
        saveToken('');
        toast('访问令牌已清除', 'ok');
        renderRoute();
        connectEvents();
        break;
      default:
        break;
    }
  }

  function reloadMemoryView() {
    if (state.route.name !== 'memory') {
      navigate('memory');
      return;
    }
    const view = $('#view');
    view.innerHTML = `<div class="view-content memory-view">${memoryFilterChips()}</div>`;
    const searchInput = $('#memory-search');
    if (searchInput) {
      searchInput.value = state.memory.q;
      searchInput.addEventListener('input', debounce((event) => {
        state.memory.q = event.target.value;
        loadMemory({ reset: true });
      }, 260));
    }
    const daysSelect = $('#memory-days');
    if (daysSelect) {
      daysSelect.value = state.memory.days;
      daysSelect.addEventListener('change', (event) => {
        state.memory.days = event.target.value;
        loadMemory({ reset: false });
      });
    }
    loadMemory({ reset: true });
  }

  async function triggerSync() {
    const button = $('[data-action="manual-sync"]');
    if (button) button.classList.add('is-spinning');
    try {
      await API.sync();
      toast('已触发一次采集同步，稍后自动刷新', 'ok');
    } catch (error) {
      if (error.status === 404 || error.status === 405) toast('服务端未启用手动同步端点', 'warn');
      else toast(`同步失败：${error.message}`, 'danger');
    } finally {
      setTimeout(() => {
        if (button) button.classList.remove('is-spinning');
      }, 600);
    }
  }

  async function deleteCard(cardId) {
    if (!cardId) return;
    const card = state.board && state.board.cards.find((item) => item.id === cardId);
    const confirmed = await confirmDialog({
      title: '删除卡片',
      message: `确定删除「${card ? card.title : cardId}」？该操作会记录在 hub 的 git 历史中，可回滚。`,
      confirmLabel: '删除',
      danger: true,
    });
    if (!confirmed) return;
    try {
      await API.deleteCard(cardId);
      closeTopOverlay();
      toast('卡片已删除', 'ok');
      refreshBoard(true);
    } catch (error) {
      toast(`删除失败：${error.message}`, 'danger');
    }
  }

  function handleGlobalKeydown(event) {
    const target = event.target;
    const typing = target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable);
    if (event.key === 'Escape') {
      if (overlayStack.length) {
        event.preventDefault();
        closeTopOverlay();
      } else if (document.body.classList.contains('nav-open')) {
        closeMobileNav();
      }
      return;
    }
    if (typing) return;
    if (event.key === '/' || ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k')) {
      event.preventDefault();
      if (state.route.name !== 'memory') navigate('memory');
      const input = $('#memory-search');
      if (input) input.focus();
      return;
    }
    if (event.key.toLowerCase() === 't' && !event.metaKey && !event.ctrlKey && !event.altKey) {
      toggleTheme();
    }
  }

  function wireChrome() {
    const retry = $('[data-action="retry-route"]');
    if (retry) retry.addEventListener('click', () => renderRoute());
    const navButtons = $$('.nav-item, .bottom-item');
    navButtons.forEach((button) => {
      button.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          navigate(button.dataset.route);
        }
      });
    });
    window.addEventListener('hashchange', renderRoute);
    document.addEventListener('click', handleGlobalClick);
    document.addEventListener('keydown', handleGlobalKeydown);
    window.addEventListener('online', () => toast('网络已恢复', 'ok'));
    window.addEventListener('offline', () => toast('网络已断开，操作可能失败', 'warn'));
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && state.conn === 'offline') connectEvents();
    });
  }

  async function refreshShell() {
    try {
      const [health, botsData] = await Promise.all([
        API.health().catch(() => null),
        API.bots().catch(() => ({ items: [] })),
      ]);
      state.health = health;
      state.bots = botsData.items || [];
      const hubMeta = $('#hub-meta');
      if (hubMeta && health && health.hub) {
        const text = $('.hub-meta-text', hubMeta);
        const records = fmtNum(health.hub.records || 0);
        if (text) text.textContent = `${records} 条记忆 · ${fmtNum(health.hub.bots || 0)} bots`;
        hubMeta.title = health.hub.dir || '';
      }
      updateAttentionBadge();
    } catch (error) {
      /* shell refresh is best effort */
    }
  }

  async function boot() {
    initTheme();
    wireChrome();
    await refreshShell();
    connectEvents();
    await renderRoute();
    // Keep hub meta fresh even when SSE is unavailable.
    setInterval(() => {
      if (!document.hidden) refreshShell();
    }, 60000);
  }

  boot();
})();
