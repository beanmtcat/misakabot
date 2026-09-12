export const API_PREFIX = import.meta.env.VITE_API_PREFIX || '/emby-manager';
let csrfToken = '';

export function setCsrfToken(value) {
  csrfToken = typeof value === 'string' ? value : '';
}

export function embyItemUrl(itemId, serverId) {
  const query = serverId ? `?server_id=${encodeURIComponent(serverId)}` : '';
  return `${API_PREFIX}/v1/emby/items/${encodeURIComponent(itemId)}/open${query}`;
}

export async function api(path, options = {}) {
  const method = String(options.method || 'GET').toUpperCase();
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && csrfToken && path !== '/auth/login') {
    headers['X-CSRF-Token'] = csrfToken;
  }
  const response = await fetch(`${API_PREFIX}${path}`, {
    credentials: 'same-origin',
    ...options,
    headers,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `请求失败 (${response.status})`);
  }
  return response.status === 204 ? null : response.json();
}

export const formatTime = (value) => {
  if (!value) return '—';
  const text = String(value);
  const hasTimezone = /(?:Z|[+-]\d\d(?::?\d\d)?)$/i.test(text);
  const instant = new Date(hasTimezone ? text : `${text}Z`);
  return Number.isNaN(instant.getTime())
    ? '—'
    : instant.toLocaleString('zh-CN', { hour12: false });
};

export const formatDate = (value) => {
  if (!value) return '—';
  const text = String(value);
  return /^\d{4}-\d{2}-\d{2}$/.test(text) ? text : formatTime(text);
};

export const formatDuration = (seconds) => {
  const value = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const remainder = value % 60;
  return hours ? `${hours}时${minutes}分` : `${minutes}分${remainder}秒`;
};

export const formatTicks = (ticks) => formatDuration(Number(ticks || 0) / 10000000);

export const formatBytes = (bytes) => {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const amount = value / (1024 ** index);
  return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
};
