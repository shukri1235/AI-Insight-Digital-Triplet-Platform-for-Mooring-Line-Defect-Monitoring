import type { MooringLine } from './types';

/** Which mooring-line segment a depth falls in (nearest segment when between bands). */
export function segmentFor(line: MooringLine | undefined, depth: number | null | undefined): string | null {
  if (!line || depth == null) return null;
  const hit = line.segments.find((s) => depth >= s.start_m && depth <= s.end_m);
  if (hit) return hit.name;
  let best: string | null = null, bestD = Infinity;
  for (const s of line.segments) {
    const d = Math.min(Math.abs(depth - s.start_m), Math.abs(depth - s.end_m));
    if (d < bestD) { bestD = d; best = s.name; }
  }
  return best;
}

/** m:ss.mmm for the review player. */
export const fmtMs = (t: number) => {
  const m = Math.floor(t / 60), s = Math.floor(t % 60), ms = Math.floor((t % 1) * 1000);
  return `${m}:${String(s).padStart(2, '0')}.${String(ms).padStart(3, '0')}`;
};

/** Date and time for directory rows: "Today 14:02", "Yesterday 09:13", "23 Oct 14:02", "3 Jan 2025 14:02". */
export function fmtStamp(ms: number | null | undefined): string {
  if (!ms) return '—';
  const d = new Date(ms), now = new Date();
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86400000);
  const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
  if (diff === 0) return `Today ${hm}`;
  if (diff === 1) return `Yesterday ${hm}`;
  const date = d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', ...(d.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}) });
  return `${date} ${hm}`;
}

/** Backend timestamps: naive UTC ISO ("2026-10-25T06:02:11") -> epoch ms. */
export function isoMs(iso: string | null | undefined): number {
  if (!iso) return 0;
  const t = new Date(/Z$|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`).getTime();
  return Number.isNaN(t) ? 0 : t;
}
