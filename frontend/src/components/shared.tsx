import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, ChevronsUpDown } from 'lucide-react';
import type { Rect } from '../hooks';
import type { Detection, Severity, VideoStatus } from '../types';
import { emphasise, type LogEntry } from '../eventlog';

/** The label is the detector's certainty, not the defect's severity. */
export const severityColor = (s: Severity | string) =>
  s === 'High confidence' ? 'rose' : s === 'Low confidence' ? 'amber' : 'slate';

/** Display name of a canonical defect class. */
export const typeName = (t: string) =>
  t === 'DEFORMATION' ? 'Deformation' : t === 'CORROSION' ? 'Pitting' : t.charAt(0) + t.slice(1).toLowerCase();

/* -------------------------------------------------------------------------
 * Live box smoothing: each box is matched to the same object in the previous update
 * (same class, overlapping) and its position / size / confidence eased towards the new
 * value, so boxes and labels stop jittering frame to frame. A box that disappears for one
 * update is held for 350 ms instead of flickering. Display only - nothing recorded changes.
 * ----------------------------------------------------------------------- */
interface Track { id: string; key: string; x: number; y: number; w: number; h: number; conf: number; seen: number; det: Detection }

const iou = (a: { x: number; y: number; w: number; h: number }, b: Detection) => {
  const ax0 = a.x - a.w / 2, ax1 = a.x + a.w / 2, ay0 = a.y - a.h / 2, ay1 = a.y + a.h / 2;
  const bx0 = b.x - b.width / 2, bx1 = b.x + b.width / 2, by0 = b.y - b.height / 2, by1 = b.y + b.height / 2;
  const iw = Math.max(0, Math.min(ax1, bx1) - Math.max(ax0, bx0)), ih = Math.max(0, Math.min(ay1, by1) - Math.max(ay0, by0));
  const inter = iw * ih;
  return inter / Math.max(1e-6, a.w * a.h + b.width * b.height - inter);
};

export function useStableDetections(dets: Detection[], enabled: boolean): Detection[] {
  const tracks = useRef<Track[]>([]);
  const seq = useRef(0);
  return useMemo(() => {
    if (!enabled) { tracks.current = []; return dets; }
    const now = performance.now();
    const used = new Set<Track>();
    const next: Track[] = [];
    for (const d of dets) {
      const key = `${d.type}|${d.isDefect ? 1 : 0}|${d.rawClass || ''}`;
      let best: Track | undefined, bestScore = 0;
      for (const t of tracks.current) {
        if (t.key !== key || used.has(t)) continue;
        const near = Math.hypot(t.x - d.x, t.y - d.y) / Math.max(t.w, t.h, d.width, d.height, 1e-6) < 0.5 ? 0.3 : 0;
        const score = Math.max(iou(t, d), near);
        if (score > bestScore) { best = t; bestScore = score; }
      }
      if (best && bestScore >= 0.2) {
        used.add(best);
        best.x += 0.4 * (d.x - best.x); best.y += 0.4 * (d.y - best.y);
        best.w += 0.3 * (d.width - best.w); best.h += 0.3 * (d.height - best.h);
        best.conf += 0.3 * (d.confidence - best.conf);
        best.seen = now; best.det = d;
        next.push(best);
      } else {
        next.push({ id: `trk-${++seq.current}`, key, x: d.x, y: d.y, w: d.width, h: d.height, conf: d.confidence, seen: now, det: d });
      }
    }
    for (const t of tracks.current) if (!used.has(t) && now - t.seen < 350) next.push(t);
    tracks.current = next;
    return next.map((t) => ({ ...t.det, id: t.id, x: t.x, y: t.y, width: t.w, height: t.h, confidence: t.conf }));
  }, [dets, enabled]);
}

/* -------------------------------------------------------------------------
 * Bounding boxes, positioned on the real rendered frame rect (useContainRect).
 *  Deformation: coral · Pitting: lavender · anything else: grey "not a defect".
 *  Solid = at/above the clip trigger (or a recorded analysis); dashed = logged only.
 * ----------------------------------------------------------------------- */
export function DetectionOverlay({ detections, rect, minConf, showNonDefects = true, source, live = false }: {
  detections: Detection[]; rect: Rect | null; minConf: number; showNonDefects?: boolean; source?: string; live?: boolean;
}) {
  const filtered = useMemo(() => detections.filter((d) => d.confidence >= minConf - 1e-9 && (showNonDefects || d.isDefect)),
    [detections, minConf, showNonDefects]);
  const visible = useStableDetections(filtered, live);
  if (!rect) return null;
  return (
    <div className="absolute pointer-events-none z-10" data-source={source}
         style={{ left: rect.left, top: rect.top, width: rect.width, height: rect.height }}>
      {visible.map((d) => {
        const def = d.isDefect && d.type === 'DEFORMATION';
        const pit = d.isDefect && d.type !== 'DEFORMATION';
        const logged = d.isDefect && d.triggering === false;
        const raw = d.standIn ? ` · stand-in: ${d.rawClass}`
          : d.isDefect && d.rawClass && ![d.type, typeName(d.type).toUpperCase()].includes(d.rawClass.toUpperCase().replace(/[^A-Z]/g, '')) ? ` · ${d.rawClass}` : '';
        const box = def
          ? `border-def ${logged ? 'border-2 border-dashed' : 'border-[2.5px] bg-def/8'}`
          : pit ? `border-pit border-2 ${logged ? 'border-dashed' : ''}`
          : 'border-dim border-[1.5px] border-dashed';
        const label = def
          ? (logged ? 'bg-def-bg text-def-soft' : 'bg-def text-on-def')
          : pit ? (logged ? 'bg-pit-bg text-pit-tx' : 'bg-pit text-on-pit')
          : 'bg-sel text-tx2';
        return (
          <div key={d.id} data-testid="det-box" data-type={d.type} data-conf={d.confidence} data-severity={d.severity}
               data-defect={d.isDefect ? '1' : '0'} data-raw={d.rawClass || ''} data-standin={d.standIn ? '1' : '0'}
               data-triggering={d.triggering ? '1' : '0'}
               className={`absolute rounded-[4px] ${live ? '' : 'transition-all duration-150'} ${box}`}
               style={{ left: `${d.x - d.width / 2}%`, top: `${d.y - d.height / 2}%`, width: `${d.width}%`, height: `${d.height}%` }}>
            <span className={`box-label absolute left-[-2px] ${d.y - d.height / 2 < 7 ? 'top-1 left-1' : 'bottom-full mb-1'} ${label} ${!d.isDefect ? 'font-semibold' : ''}`}
                  style={!d.isDefect ? { fontSize: 10 } : pit ? { fontSize: 10.5 } : undefined}>
              {d.isDefect ? typeName(d.type) : (d.rawClass || d.type)} <b>{live ? Math.round(d.confidence * 20) * 5 : Math.round(d.confidence * 100)}%</b>
              {raw}{!d.isDefect ? ' · not a defect' : live && d.triggering && def ? ' · clipped' : ''}
            </span>
          </div>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------------- status chips */
const STATUS_CLS: Record<string, string> = {
  UNPROCESSED: 'text-mut', QUEUED: 'text-acc-tx', PROCESSING: 'text-acc-tx', COMPLETED: 'text-ok',
  FAILED: 'text-def-tx', LIVE: 'text-def-tx', CONNECTING: 'text-warn', RECONNECTING: 'text-warn', ERROR: 'text-def-tx',
  IDLE: 'text-mut', HASHING: 'text-acc-tx', DUPLICATE: 'text-warn', CANCELLED: 'text-mut',
};
const STATUS_LABEL: Record<string, string> = {
  UNPROCESSED: 'Not processed', QUEUED: 'Queued', PROCESSING: 'Processing', COMPLETED: 'Completed', FAILED: 'Failed',
  LIVE: 'Live', CONNECTING: 'Connecting', RECONNECTING: 'Reconnecting', ERROR: 'Error', IDLE: 'Idle',
  HASHING: 'Fingerprinting', DUPLICATE: 'Duplicate', CANCELLED: 'Cancelled',
};

export function StatusPill({ status }: { status: VideoStatus | string }) {
  return (
    <span data-status={status} className={`inline-flex items-center gap-1.5 text-[13px] font-semibold ${STATUS_CLS[status] || 'text-mut'}`}>
      {(status === 'PROCESSING' || status === 'LIVE' || status === 'CONNECTING' || status === 'RECONNECTING' || status === 'HASHING') &&
        <span className="w-1.5 h-1.5 rounded-full bg-current animate-live" />}
      {STATUS_LABEL[status] || status}
    </span>
  );
}

/** Rounded header pill: tone picks the colours. */
export function Pill({ tone = 'mute', children, testid, title, dot = true, pulse = false, extra }: {
  tone?: 'ok' | 'mute' | 'warn' | 'bad' | 'test'; children: React.ReactNode; testid?: string; title?: string; dot?: boolean; pulse?: boolean;
  extra?: Record<string, string>;
}) {
  const cls = {
    ok: 'bg-acc-bg text-acc-tx', mute: 'bg-[#151F2A] text-tx2', warn: 'bg-[#33270C] text-warn',
    bad: 'bg-def-bg text-def-tx', test: 'bg-tst text-on-tst font-extrabold',
  }[tone];
  return (
    <span data-testid={testid} title={title} {...extra}
          className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[12.5px] font-semibold whitespace-nowrap ${cls}`}>
      {dot && <span className={`w-[7px] h-[7px] rounded-full bg-current ${pulse ? 'animate-live' : ''}`} />}
      {children}
    </span>
  );
}

/** BlueROV2 MAVLink link state. */
export function RovPill({ state, rate, simulated }: { state?: string; rate?: number; simulated?: boolean }) {
  const s = state || 'NO_ROV';
  const tone = s === 'CONNECTED' ? (simulated ? 'warn' : 'ok') : s === 'STALE' ? 'warn' : s === 'DRIVER_MISSING' ? 'bad' : 'mute';
  const label = s === 'NO_ROV' ? 'ROV offline' : s === 'DRIVER_MISSING' ? 'pymavlink missing' : s === 'STALE' ? 'ROV stale'
    : simulated ? 'ROV SIM' : 'ROV link';
  return (
    <Pill tone={tone} testid="rov-pill" extra={{ 'data-state': s, 'data-simulated': simulated ? '1' : '0' }}
          title={s === 'NO_ROV' ? 'No BlueROV2 heartbeat on the MAVLink port' : undefined}>
      {label}{s === 'CONNECTED' && rate ? ` · ${Math.round(rate)} Hz` : ''}
    </Pill>
  );
}

/** Marks Test Mode data everywhere it appears. */
export function TestBadge({ standIn }: { standIn?: Record<string, string> | null }) {
  const n = standIn ? Object.keys(standIn).length : 0;
  return (
    <span data-testid="test-badge" title={n ? `Stand-in: ${Object.entries(standIn!).map(([k, v]) => `${k} → ${v}`).join(', ')}` : 'Simulated ROV video and telemetry'}
          className="inline-flex items-center px-2 py-0.5 rounded-md bg-tst text-on-tst text-[12px] font-extrabold tracking-wide whitespace-nowrap">
      TEST{n ? ' · stand-in' : ''}
    </span>
  );
}

/* ------------------------------------------------------------------------- cards */
export function Card({ children, className = '', testid, pad = true, ...rest }: {
  children: React.ReactNode; className?: string; testid?: string; pad?: boolean;
} & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div data-testid={testid} {...rest}
         className={`bg-card border border-line rounded-2xl ${pad ? 'px-4 py-3.5' : 'overflow-hidden'} ${className}`}>
      {children}
    </div>
  );
}

/** Card title row: small icon tile, muted title, optional right-hand status. */
export function CardTitle({ icon, title, right, className = '' }: {
  icon?: React.ReactNode; title: React.ReactNode; right?: React.ReactNode; className?: string;
}) {
  return (
    <div className={`flex items-center gap-2 min-w-0 ${className}`}>
      {icon && <span className="w-[26px] h-[26px] rounded-lg bg-acc-bg text-acc grid place-items-center shrink-0">{icon}</span>}
      <span className="text-[13px] text-mut font-bold truncate">{title}</span>
      {right != null && <span className="ml-auto text-xs font-bold text-acc-tx text-right">{right}</span>}
    </div>
  );
}

export const btn = {
  primary: 'bg-acc text-on-acc font-bold rounded-[10px] px-3.5 py-2.5 cursor-pointer disabled:bg-btn disabled:text-dim disabled:cursor-not-allowed',
  secondary: 'bg-btn text-tx border border-edge2 font-semibold rounded-[9px] px-3 py-2 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed',
  ghost: 'bg-transparent text-tx border border-edge2 font-semibold rounded-[9px] px-3 py-1.5 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed',
  input: 'bg-ink border border-edge text-tx rounded-[9px] px-3 py-2 outline-none focus:border-acc',
};

/** Standby / offline placeholder used by both players before the operator picks media. */
export function Standby({ icon, title, subtitle, children }: {
  icon: React.ReactNode; title: string; subtitle: string; children?: React.ReactNode;
}) {
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center gap-4 text-center p-6 bg-[radial-gradient(ellipse_at_center,#0E2129_0%,#071419_70%)]">
      <div className="w-14 h-14 rounded-full border border-edge bg-card grid place-items-center text-dim">{icon}</div>
      <div>
        <p className="text-[15px] font-bold text-tx">{title}</p>
        <p className="text-[13px] text-mut mt-1.5 max-w-md">{subtitle}</p>
      </div>
      {children}
    </div>
  );
}

/* ------------------------------------------------------------------------- event log */
export function EventLog({ entries, height = 160, title = 'Event log', hint = 'newest first · scroll for more', className = '', fill = false }: {
  entries: LogEntry[]; height?: number; title?: string; hint?: string; className?: string; fill?: boolean;
}) {
  return (
    <Card className={`flex flex-col gap-1.5 !py-2.5 ${className}`} testid="event-log">
      <div className="flex items-center text-[13px] text-mut font-bold">
        <span>{title}</span><span className="ml-auto text-xs text-dim font-semibold">{hint}</span>
      </div>
      <div tabIndex={0} aria-label="Event log" className={`log overflow-y-auto -mx-2 ${fill ? 'flex-1 min-h-[190px]' : ''}`}
           style={fill ? undefined : { height }}>
        {entries.map((e) => (
          <div key={e.id} className={e.tone} data-kind={e.kind}>
            <span className="t">{e.at}</span>
            <span className="k">{e.kind}</span>
            <span className="break-words min-w-0">{emphasise(e.text).map((p, i) => (p.b ? <b key={i}>{p.s}</b> : <React.Fragment key={i}>{p.s}</React.Fragment>))}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}


/* ------------------------------------------------------------------------- sortable directories */
export type SortDir = 'asc' | 'desc';
export interface SortState<K extends string> { key: K; dir: SortDir }

/** Column sort shared by every directory: click a header to sort by it, click again to reverse.
 *  The choice is remembered per browser (a convenience only). firstDir: the direction a column
 *  starts in (dates and counts newest / largest first, text A-Z). */
export function useSort<K extends string>(storageKey: string, initial: SortState<K>, firstDir: Partial<Record<K, SortDir>> = {}) {
  const [sort, setSort] = useState<SortState<K>>(() => {
    try {
      const s = JSON.parse(localStorage.getItem(storageKey) || 'null');
      if (s && typeof s.key === 'string' && (s.dir === 'asc' || s.dir === 'desc')) return s as SortState<K>;
    } catch { /* storage unavailable */ }
    return initial;
  });
  useEffect(() => { try { localStorage.setItem(storageKey, JSON.stringify(sort)); } catch { /* ignore */ } }, [storageKey, sort]);
  const toggle = (key: K) => setSort((s) => (s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' }
                                                         : { key, dir: firstDir[key] ?? 'asc' }));
  return { sort, setSort, toggle };
}

/** Compare helper: numbers / strings, with empty values always last. */
export function cmpBy<T>(get: (x: T) => number | string | null | undefined, dir: SortDir) {
  const f = dir === 'asc' ? 1 : -1;
  return (a: T, b: T) => {
    const x = get(a), y = get(b);
    const ex = x == null || x === '', ey = y == null || y === '';
    if (ex || ey) return ex === ey ? 0 : ex ? 1 : -1;
    if (typeof x === 'number' && typeof y === 'number') return (x - y) * f;
    return String(x).localeCompare(String(y), undefined, { numeric: true, sensitivity: 'base' }) * f;
  };
}

export function SortHeader<K extends string>({ label, k, sort, onSort, align = 'left', title, className = '' }: {
  label: React.ReactNode; k: K; sort: SortState<K>; onSort: (k: K) => void; align?: 'left' | 'right'; title?: string; className?: string;
}) {
  const on = sort.key === k;
  const Icon = !on ? ChevronsUpDown : sort.dir === 'asc' ? ArrowUp : ArrowDown;
  return (
    <button type="button" onClick={() => onSort(k)} data-testid={`sort-${k}`} aria-sort={on ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}
            title={title || `Sort by ${typeof label === 'string' ? label.toLowerCase() : 'this column'}${on ? ` (${sort.dir === 'asc' ? 'ascending' : 'descending'} — click to reverse)` : ''}`}
            className={`group/sort inline-flex items-center gap-1 uppercase tracking-[.08em] font-bold cursor-pointer select-none whitespace-nowrap
                        ${align === 'right' ? 'justify-end w-full' : ''} ${on ? 'text-acc-tx' : 'text-dim hover:text-tx2'} ${className}`}>
      <span>{label}</span>
      <Icon className={`w-3 h-3 shrink-0 ${on ? '' : 'opacity-40 group-hover/sort:opacity-80'}`} />
    </button>
  );
}
