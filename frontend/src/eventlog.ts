/* Event log entries: time | type tag | message.
 * Call sites log plain sentences, optionally prefixed with the area ("LIVE: …", "INGEST: …");
 * the prefix becomes the tag, so the log can be skimmed by type. */

export type Tone = '' | 'crit' | 'ok' | 'warn' | 'info' | 'test';
import { apiUrl, postJSON } from './api';

export interface LogEntry { id: number; at: string; ts: number; kind: string; tone: Tone; text: string }

const PREFIXES: [RegExp, string, Tone][] = [
  [/^LIVE ERROR:\s*/, 'Live', 'crit'],
  [/^INCIDENT ERROR:\s*/, 'Clip', 'crit'],
  [/^REPORT ERROR:\s*/, 'Report', 'crit'],
  [/^YOLO ERROR:\s*/, 'Model', 'crit'],
  [/^ERROR:\s*/, 'Error', 'crit'],
  [/^CLIP:\s*/, 'Clip', 'crit'],
  [/^LIVE:\s*/, 'Live', 'info'],
  [/^ZONE:\s*/, 'Zone', 'info'],
  [/^ROV:\s*/, 'Link', ''],
  [/^INGEST:\s*/, 'Import', ''],
  [/^BATCH:\s*/, 'Batch', 'info'],
  [/^REVIEW:\s*/, 'Review', ''],
  [/^REPORT:\s*/, 'Report', 'ok'],
  [/^\[Token saver\]\s*/, 'Report', 'warn'],
  [/^AI WARN:\s*/, 'Report', 'warn'],
  [/^TEST MODE(?: ON| OFF)?:\s*/, 'Test', 'test'],
  [/^User command:\s*/, 'Config', ''],
  [/^CONFIG:\s*/, 'Config', ''],
  [/^YOLO(?: WEIGHTS CHANGED)?:\s*/, 'Model', ''],
  [/^SYSTEM:\s*/, 'System', ''],
];

export function classify(raw: string): { kind: string; tone: Tone; text: string } {
  let kind = 'Info', tone: Tone = '', text = raw.trim();
  for (const [re, k, t] of PREFIXES) {
    if (re.test(text)) { kind = k; tone = t; text = text.replace(re, ''); break; }
  }
  if (!tone) {
    if (/\b(failed|offline|lost|stale|missing|unreachable|duplicate|cancelled)\b/i.test(text)) tone = 'warn';
    else if (/\b(online|loaded|initialised|completed|approved|attached|connected)\b/i.test(text)) tone = 'ok';
  }
  return { kind, tone, text: text.charAt(0).toUpperCase() + text.slice(1) };
}

let seq = 0;
export function makeEntry(raw: string, kind?: string, tone?: Tone): LogEntry {
  const c = classify(raw);
  const now = new Date();
  const at = now.toLocaleTimeString([], { hour12: false });
  return { id: ++seq, at, ts: now.getTime(), kind: kind || c.kind, tone: tone ?? c.tone, text: c.text };
}

/* Paper trail: every entry is also sent to the backend, which appends it to this session's text file
 * (logs/session_<date>_<time>.txt). Sent in small batches; kept and retried while the backend is unreachable. */
const CLIENT = Math.random().toString(16).slice(2, 6);     // tells two browser tabs apart in the file
let pending: { ts: number; kind: string; text: string }[] = [];
let timer: ReturnType<typeof setTimeout> | undefined;

export function persist(e: LogEntry) {
  pending.push({ ts: e.ts, kind: e.kind, text: e.text });
  if (pending.length > 1000) pending = pending.slice(-1000);
  if (timer === undefined) timer = setTimeout(flush, 800);
}

async function flush() {
  timer = undefined;
  if (!pending.length) return;
  const batch = pending.slice(0, 200);
  try {
    await postJSON('/api/logs', { entries: batch, client: CLIENT });
    pending = pending.slice(batch.length);
    if (pending.length) timer = setTimeout(flush, 200);
  } catch {
    timer = setTimeout(flush, 5000);                         // backend offline: keep them, try again
  }
}

if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', () => {                // closing the tab: send what is left
    if (!pending.length) return;
    try {
      navigator.sendBeacon(apiUrl('/api/logs'), new Blob([JSON.stringify({ entries: pending, client: CLIENT })], { type: 'application/json' }));
      pending = [];
    } catch { /* nothing more can be done while the page closes */ }
  });
}

/** Splits a message so numbers with units ("88%", "120.5 m", "0.60") and quoted names stand out. */
const EMPH = /("[^"]+"|\b\d+(?:\.\d+)?\s?(?:%|m\b|s\b|ms\b|Hz\b)|\b0\.\d{2}\b)/g;
export function emphasise(text: string): { s: string; b: boolean }[] {
  const out: { s: string; b: boolean }[] = [];
  let last = 0;
  for (const m of text.matchAll(EMPH)) {
    if (m.index! > last) out.push({ s: text.slice(last, m.index), b: false });
    out.push({ s: m[0].replace(/^"|"$/g, ''), b: true });
    last = m.index! + m[0].length;
  }
  if (last < text.length) out.push({ s: text.slice(last), b: false });
  return out;
}
