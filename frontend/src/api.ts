// Backend location. Override with VITE_API_BASE in frontend/.env if the API runs elsewhere.
// Default: the backend serves this UI itself (start.bat / start.sh), so the API is on the same address.
// Under `npm run dev` (port 3000) the API is the backend on port 8500 of the same machine.
const devServer = typeof window !== 'undefined' && window.location.port === '3000';
export const API_BASE: string =
  (import.meta as any).env?.VITE_API_BASE?.replace(/\/$/, '') ||
  (typeof window === 'undefined' || devServer || !/^https?:$/.test(window.location.protocol)
    ? `http://${typeof window !== 'undefined' && window.location.hostname ? window.location.hostname : 'localhost'}:8500`
    : window.location.origin);

export const WS_URL = API_BASE.replace(/^http/, 'ws') + '/ws/telemetry';

export const apiUrl = (path: string) => `${API_BASE}${path}`;

export async function getJSON<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(apiUrl(path), init);
  const text = await res.text();
  let data: any = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* non-JSON error page */ }
  if (!res.ok) {
    const err: any = new Error(data?.detail || data?.message || `HTTP ${res.status}`);
    err.status = res.status;
    err.retryAfter = Number(res.headers.get('Retry-After')) || data?.cooldownSec || 0;
    throw err;
  }
  return data as T;
}

export const postJSON = <T,>(path: string, body?: unknown) =>
  getJSON<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

export const del = <T,>(path: string) => getJSON<T>(path, { method: 'DELETE' });


export const fmtEta = (sec: number | null | undefined) => {
  if (sec == null || !isFinite(sec)) return '';
  if (sec < 60) return `${Math.ceil(sec)}s left`;
  const m = Math.round(sec / 60);
  return m < 60 ? `${m} min left` : `${Math.floor(m / 60)}h ${m % 60}m left`;
};

export const fmtTime = (sec: number | null | undefined) => {
  if (sec == null || !isFinite(sec)) return '--:--';
  const s = Math.max(0, Math.floor(sec));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}` : `${m}:${String(r).padStart(2, '0')}`;
};

export const stamp = () => new Date().toLocaleTimeString();

/** Backend timestamps are naive UTC ISO strings; show them in the operator's local time. */
export function localTime(iso: string | null | undefined, withYear = true): string {
  if (!iso) return '—';
  const d = new Date(/Z$|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, '0');
  return `${withYear ? `${d.getFullYear()}-` : ''}${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export const putJSON = <T,>(path: string, body?: unknown) =>
  getJSON<T>(path, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });
