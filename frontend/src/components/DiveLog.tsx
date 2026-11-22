import { useEffect, useId, useLayoutEffect, useMemo, useState } from 'react';
import { Activity, AlertTriangle, ArrowLeft, Download, FileText, RotateCcw, Siren } from 'lucide-react';
import { apiUrl, fmtTime, getJSON } from '../api';
import type { DiveLog as Log, DiveSample, DiveSighting } from '../types';
import { btn, Card, cmpBy, SortHeader, TestBadge, useSort } from './shared';

/* Live dive log: the whole dive as the app recorded it - depth over time with the depth zones, incident clips,
 * sightings and vehicle / operator events on one time axis, plus tables, a summary and downloads. The full video of
 * the dive belongs to the topside recorder (QGroundControl / DVR); this is the app's own record of the dive. */

const ZONE_FILL = ['rgba(79,209,197,0.10)', 'rgba(99,179,237,0.08)', 'rgba(160,174,192,0.10)', 'rgba(246,173,85,0.08)'];
const EVENT_LABEL: Record<string, string> = {
  CONNECTED: 'Link up', LOST: 'Link lost', PAUSED: 'App paused', ARMED: 'Armed', DISARMED: 'Disarmed', MODE: 'Mode', LEAK: 'LEAK', STATUSTEXT: 'Vehicle message',
};
const AXIS_L = 86;   // left gutter shared by the chart's depth axis and the lane labels, so both use one time axis
const typeName = (t: string) => (t === 'CORROSION' ? 'Pitting' : 'Deformation');
const fmtDur = (s: number) => (s >= 3600 ? `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min` : s >= 60 ? `${Math.floor(s / 60)} min ${Math.round(s % 60)} s` : `${Math.round(s)} s`);

/** width of an element that may mount later (the chart appears once the log has loaded) */
function useWidth<T extends HTMLElement>(): [(el: T | null) => void, number] {
  const [el, setEl] = useState<T | null>(null);
  const [w, setW] = useState(800);
  useLayoutEffect(() => {
    if (!el) return;
    const ro = new ResizeObserver(() => setW(el.clientWidth || 800));
    ro.observe(el);
    setW(el.clientWidth || 800);
    return () => ro.disconnect();
  }, [el]);
  return [setEl, w];
}

/** nearest sample at time t (series sorted by t) */
function sampleAt(series: DiveSample[], t: number): DiveSample | null {
  if (!series.length) return null;
  let lo = 0, hi = series.length - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (series[m].t < t) lo = m; else hi = m; }
  return Math.abs(series[lo].t - t) <= Math.abs(series[hi].t - t) ? series[lo] : series[hi];
}

export default function DiveLog({ id, onBack, onOpenClip, onReport, log: addLog }: {
  id: string;
  onBack: () => void;
  onOpenClip: (clipId: string, t?: number) => void;
  onReport: (diveId: string, name: string) => void;
  log: (m: string) => void;
}) {
  const [data, setData] = useState<Log | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [view, setView] = useState<[number, number] | null>(null);   // zoom window (s); null = whole dive
  const [tab, setTab] = useState<'sightings' | 'events' | 'settings' | 'summary'>('sightings');
  const [hover, setHover] = useState<number | null>(null);
  const [drag, setDrag] = useState<[number, number] | null>(null);
  const [dlOpen, setDlOpen] = useState(false);
  const [chartRef, width] = useWidth<HTMLDivElement>();

  useEffect(() => {
    let alive = true;
    const load = () => getJSON<Log>(`/api/dives/${id}/log?points=2400`).then((d) => { if (alive) { setData(d); setErr(null); } })
      .catch((e) => { if (alive) setErr(e.message); });
    load();
    const timer = setInterval(() => { if (data?.dive.isLive !== false) load(); }, 3000);   // a running dive keeps updating
    return () => { alive = false; clearInterval(timer); };
  }, [id, data?.dive.isLive]); // eslint-disable-line react-hooks/exhaustive-deps

  if (err && !data) return <main className="flex-1 px-5 py-4"><Card><p className="text-def-tx text-sm">Could not load the dive log: {err}</p>
    <button onClick={onBack} className={`${btn.secondary} mt-3 text-[13px]`}>Back to the library</button></Card></main>;
  if (!data) return <main className="flex-1 px-5 py-4"><Card><p className="text-mut text-sm">Loading the dive log…</p></Card></main>;

  const d = data.dive, s = data.summary;
  const dur = Math.max(d.durationSec, data.series.length ? data.series[data.series.length - 1].t : 0, 1);
  const [v0, v1] = view ?? [0, dur];
  const clock = (t: number) => (d.createdTs ? new Date((d.createdTs + t) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '');

  return (
    <main className="flex-1 px-5 py-4 flex flex-col gap-4 min-w-0" data-testid="dive-log">
      {/* header */}
      <Card className="flex flex-wrap items-center gap-x-5 gap-y-2.5">
        <button onClick={onBack} className={`${btn.ghost} flex items-center gap-1.5 text-[13px]`} data-testid="dive-log-back"><ArrowLeft className="w-4 h-4" /> Library</button>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Activity className="w-4 h-4 text-acc" />
            <h2 className="text-[16px] font-extrabold truncate" data-testid="dive-log-title">{d.name}</h2>
            {d.isTest && <TestBadge />}
            {d.isLive && <span className="text-[12px] font-extrabold uppercase rounded-md px-2 py-0.5 bg-def text-on-def animate-pulse" data-testid="dive-log-live">Live</span>}
          </div>
          <div className="text-xs text-mut mt-0.5">
            {d.lineId || 'no line'}{d.lineName ? ` · ${d.lineName}` : ''} · started {d.startedAt}{d.endedAt ? ((s.idleTailSec ?? 0) > 0 ? ` · last data ${clock(s.durationSec)} · stopped ${d.endedAt}` : ` · ended ${d.endedAt.slice(11)}`) : ''} · model {d.model || '—'} · logged ≥ {d.logConf?.toFixed(2) ?? '—'}
          </div>
        </div>
        <div className="flex flex-wrap gap-x-5 gap-y-1 text-[13px]" data-testid="dive-log-stats">
          <Stat label="Duration" value={fmtDur(s.durationSec)} tone={(s.idleTailSec ?? 0) > 0 ? 'text-warn' : ''} title={(s.idleTailSec ?? 0) > 0 ? `with data; open ${fmtDur(s.openSec ?? s.durationSec)} in total` : undefined} />
          <Stat label="Max depth" value={s.maxDepth != null ? `${s.maxDepth.toFixed(1)} m` : '—'} />
          <Stat label="Incident clips" value={String(s.clips)} tone={s.clips ? 'text-def-tx' : ''} />
          <Stat label="Sightings" value={`${s.sightings.DEFORMATION} def · ${s.sightings.CORROSION} pit`} />
          <Stat label="Link losses" value={String(s.linkLosses)} tone={s.linkLosses ? 'text-warn' : ''} />
        </div>
        <div className="ml-auto flex items-center gap-2 relative">
          <button onClick={() => onReport(d.id, d.name)} className={`${btn.secondary} flex items-center gap-1.5 text-[13px]`} data-testid="dive-log-report"><FileText className="w-4 h-4" /> Report on this dive</button>
          <button onClick={() => setDlOpen((o) => !o)} className={`${btn.secondary} flex items-center gap-1.5 text-[13px]`} data-testid="dive-log-download" aria-expanded={dlOpen}><Download className="w-4 h-4" /> Download</button>
          {dlOpen && (
            <div className="absolute right-0 top-full mt-1.5 z-30 bg-card border border-edge rounded-xl shadow-2xl py-1.5 min-w-[260px]" data-testid="dive-log-download-menu"
                 onMouseLeave={() => setDlOpen(false)}>
              {(['sensors', 'sightings', 'events'] as const).map((k) => (
                <a key={k} href={apiUrl(`/api/dives/${d.id}/export/${k}.csv`)} onClick={() => { setDlOpen(false); addLog(`DIVE LOG: ${k} CSV downloaded for ${d.name}.`); }}
                   className="block px-3.5 py-2 text-[13px] hover:bg-sel" data-testid={`dive-log-csv-${k}`}>
                  {k === 'sensors' ? `Sensor log (CSV, all ${s.samples.toLocaleString()} readings)` : k === 'sightings' ? 'Sightings (CSV)' : 'Vehicle + operator events (CSV)'}
                </a>
              ))}
              <div className="border-t border-line my-1" />
              {data.tlogs.length ? data.tlogs.map((f) => (
                <a key={f.name} href={apiUrl(`/api/dives/${d.id}/tlog/${encodeURIComponent(f.name)}`)} onClick={() => setDlOpen(false)}
                   className="block px-3.5 py-2 text-[13px] hover:bg-sel font-mono" data-testid="dive-log-tlog">{f.name} <span className="text-dim">· {f.sizeMb} MB</span></a>
              )) : <span className="block px-3.5 py-2 text-xs text-dim">No .tlog (dive log recording was off)</span>}
            </div>
          )}
        </div>
      </Card>

      {((s.idleTailSec ?? 0) > 0 || (s.pauses?.length ?? 0) > 0) && (
        <div className="bg-warn/10 border border-warn/40 rounded-xl px-4 py-2.5 text-[13px] flex gap-2.5 items-start" data-testid="dive-log-gap-note">
          <AlertTriangle className="w-4 h-4 text-warn shrink-0 mt-0.5" />
          <div className="flex flex-col gap-1">
            {(s.pauses ?? []).map((p, i) => (
              <span key={i}>At T+{fmtTime(p.t ?? 0)} ({clock(p.t ?? 0)}) the app resumed after <b>{/for (.+?) \(/.exec(p.text || '')?.[1] ?? 'a while'}</b> not running (PC asleep or the app suspended). Nothing was recorded in that gap.</span>
            ))}
            {(s.idleTailSec ?? 0) > 0 && (
              <span>Nothing was recorded after <b>T+{fmtTime(s.durationSec)}</b> ({clock(s.durationSec)}). The dive stayed open for another <b>{fmtDur(s.idleTailSec!)}</b> until it was stopped{d.endedAt ? ` (${d.endedAt})` : ''}; that time is not counted in the duration.
                <span className="text-mut"> Usually the PC went to sleep, or the dive was left running — press Stop or Disconnect when a dive ends. (A Test Mode clip loops until then.)</span></span>
            )}
          </div>
        </div>
      )}

      {/* chart + lanes */}
      <Card className="flex flex-col gap-2" pad={false}>
        <div className="px-4 pt-3 flex items-center gap-3 text-xs text-dim">
          <span className="font-bold text-mut">Depth over time</span>
          <span>drag to zoom · click a clip to open it</span>
          {data.seriesTotal > data.series.length && <span>· {data.series.length.toLocaleString()} of {data.seriesTotal.toLocaleString()} readings drawn (every peak kept)</span>}
          {view && <button onClick={() => setView(null)} className="ml-auto flex items-center gap-1 text-acc-tx underline cursor-pointer" data-testid="dive-log-unzoom"><RotateCcw className="w-3.5 h-3.5" /> whole dive</button>}
        </div>
        <div ref={chartRef} className="relative px-1 select-none">
          <Chart data={data} width={width} v0={v0} v1={v1} hover={hover} setHover={setHover} drag={drag} setDrag={setDrag}
                 onZoom={(a, b) => setView(b - a >= 2 ? [a, b] : null)} onOpenClip={onOpenClip} clock={clock} />
        </div>
        <Lanes data={data} v0={v0} v1={v1} width={width} onOpenClip={onOpenClip} onHover={setHover} />
      </Card>

      {/* tables */}
      <Card pad={false} className="flex flex-col min-h-[240px]">
        <div className="flex gap-1 px-3 pt-2.5 border-b border-line" role="tablist">
          {([['sightings', `Sightings (${data.sightings.length})`], ['events', `Vehicle events (${data.events.length})`],
             ['settings', `Settings changes (${s.settingsChanges})`], ['summary', 'Summary']] as const).map(([k, label]) => (
            <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)} data-testid={`dive-log-tab-${k}`}
                    className={`px-3 py-2 text-[13px] font-semibold border-b-2 -mb-px cursor-pointer ${tab === k ? 'border-acc text-tx' : 'border-transparent text-mut hover:text-tx'}`}>{label}</button>
          ))}
        </div>
        <div className="flex-1 overflow-auto max-h-[420px]">
          {tab === 'sightings' && <SightingsTable data={data} clock={clock} onZoom={(t) => setView([Math.max(0, t - 30), Math.min(dur, t + 30)])} onOpenClip={onOpenClip} />}
          {tab === 'events' && <SimpleTable testid="dive-log-events" empty="No vehicle events recorded (no ROV link during this dive?)."
            rows={data.events.map((e) => [e.t != null ? `T+${fmtTime(e.t)}` : '—', e.t != null ? clock(e.t) : '', EVENT_LABEL[e.kind] || e.kind, e.text || ''])}
            head={['Dive time', 'Clock', 'Event', 'Detail']} warn={(r) => r[2] === 'Link lost' || r[2] === 'LEAK' || r[2] === 'App paused'} />}
          {tab === 'settings' && <SimpleTable testid="dive-log-settings" empty="No changes."
            rows={data.settings.map((c) => [`T+${fmtTime(c.t)}`, clock(c.t), c.kind === 'TRIGGER' ? 'Clip trigger' : c.kind === 'MODE' ? 'Detection mode' : 'Dive-log recording',
              c.kind === 'TRIGGER' ? `≥ ${Number(c.value).toFixed(2)}` : c.kind === 'RECORDING' ? (c.value ? 'on' : 'off') : String(c.value).toLowerCase(), c.t === 0 ? 'at start' : 'changed'])}
            head={['Dive time', 'Clock', 'Setting', 'Value', '']} />}
          {tab === 'summary' && <Summary data={data} />}
        </div>
      </Card>
    </main>
  );
}

function Stat({ label, value, tone = '', title }: { label: string; value: string; tone?: string; title?: string }) {
  return <div title={title}><div className="text-[12px] font-bold uppercase tracking-wide text-dim">{label}</div><div className={`font-bold ${tone}`}>{value}</div></div>;
}

function Chart({ data, width, v0, v1, hover, setHover, drag, setDrag, onZoom, onOpenClip, clock }: {
  data: Log; width: number; v0: number; v1: number; hover: number | null; setHover: (t: number | null) => void;
  drag: [number, number] | null; setDrag: (d: [number, number] | null) => void; onZoom: (a: number, b: number) => void;
  onOpenClip: (id: string, t?: number) => void; clock: (t: number) => string;
}) {
  const clipId = `dl-plot-${useId().replace(/:/g, '')}`;
  const H = 280, L = AXIS_L, R = 14, T = 14, B = 26;
  const W = Math.max(320, width - 8);
  const pts = data.series.filter((p) => p.depth != null && p.t >= v0 - 1 && p.t <= v1 + 1);
  const maxD = Math.max(5, ...data.series.filter((p) => p.depth != null).map((p) => p.depth!), ...data.sightings.map((x) => x.depth ?? 0));
  const yMax = Math.ceil((maxD * 1.12 + 1) / 5) * 5;
  const x = (t: number) => L + ((t - v0) / Math.max(1e-6, v1 - v0)) * (W - L - R);
  const y = (dep: number) => T + (dep / yMax) * (H - T - B);
  const tOf = (px: number) => v0 + ((px - L) / (W - L - R)) * (v1 - v0);
  const path = useMemo(() => {
    let p = '', pen = false, last = -1;
    for (const s of pts) {
      if (last >= 0 && s.t - last > 10) pen = false;          // a gap in the log (recording off, link lost) breaks the line
      p += `${pen ? 'L' : 'M'}${x(s.t).toFixed(1)},${y(s.depth!).toFixed(1)}`;
      pen = true; last = s.t;
    }
    return p;
  }, [pts, v0, v1, W, yMax]); // eslint-disable-line react-hooks/exhaustive-deps
  const ticks = useMemo(() => {
    const span = v1 - v0, steps = [10, 30, 60, 120, 300, 600, 900, 1800, 3600];
    const step = steps.find((st) => span / st <= 8) || 7200;
    const out: number[] = [];
    for (let t = Math.ceil(v0 / step) * step; t <= v1; t += step) out.push(t);
    return out;
  }, [v0, v1]);
  const dTicks = Array.from({ length: 6 }, (_, i) => Math.round((yMax / 5) * i));
  const hs = hover != null ? sampleAt(data.series, hover) : null;
  const rel = (e: React.MouseEvent) => { const r = (e.currentTarget as SVGElement).getBoundingClientRect(); return e.clientX - r.left; };

  return (
    <div className="relative">
      <svg width={W} height={H} data-testid="dive-log-chart" role="img" aria-label="Depth over time"
           onMouseMove={(e) => { const px = rel(e); if (px >= L && px <= W - R) { setHover(tOf(px)); if (drag) setDrag([drag[0], tOf(px)]); } }}
           onMouseLeave={() => { setHover(null); setDrag(null); }}
           onMouseDown={(e) => { const px = rel(e); if (px >= L) setDrag([tOf(px), tOf(px)]); }}
           onMouseUp={() => { if (drag) { const [a, b] = [Math.min(...drag), Math.max(...drag)]; if (b - a >= 2) onZoom(Math.max(0, a), b); } setDrag(null); }}>
        {/* zones */}
        {data.zones.map((z, i) => z.from < yMax && (
          <g key={z.name}>
            <rect x={L} width={W - L - R} y={y(Math.max(0, z.from))} height={Math.max(0, y(Math.min(yMax, z.to)) - y(Math.max(0, z.from)))} fill={ZONE_FILL[i % ZONE_FILL.length]} />
            <text x={W - R - 6} y={y(Math.max(0, z.from)) + 13} textAnchor="end" className="fill-dim" fontSize={11}>{z.name}</text>
          </g>
        ))}
        {/* grid */}
        {dTicks.map((dv) => <g key={dv}><line x1={L} x2={W - R} y1={y(dv)} y2={y(dv)} stroke="var(--color-line)" /><text x={L - 8} y={y(dv) + 4} textAnchor="end" fontSize={11} className="fill-dim">{dv} m</text></g>)}
        {ticks.map((t) => <g key={t}><line x1={x(t)} x2={x(t)} y1={T} y2={H - B} stroke="var(--color-line)" strokeDasharray="2 4" /><text x={x(t)} y={H - 8} textAnchor="middle" fontSize={11} className="fill-dim">{fmtTime(t)}</text></g>)}
        <defs><clipPath id={clipId}><rect x={L} y={T - 1} width={W - L - R} height={H - T - B + 2} /></clipPath></defs>
        <g clipPath={`url(#${clipId})`}>
        {/* clips */}
        {data.clips.filter((c) => c.start != null && c.end! >= v0 && c.start! <= v1).map((c) => (
          <g key={c.id} className="cursor-pointer" onClick={() => onOpenClip(c.id, c.trigger != null && c.start != null ? c.trigger - c.start : undefined)} data-testid="dive-log-clip">
            <rect x={x(c.start!)} y={T} width={Math.max(3, x(c.end!) - x(c.start!))} height={H - T - B} fill="rgba(229,62,62,0.16)" />
            <path d={`M${x(c.trigger ?? c.start!) - 7},${T} L${x(c.trigger ?? c.start!) + 7},${T} L${x(c.trigger ?? c.start!)},${T + 11} Z`} fill="var(--color-def)" data-testid="dive-log-clip-marker" data-clip={c.id} />
            <title>{`Incident clip ${c.name} — click to review`}</title>
          </g>
        ))}
        {/* depth */}
        <path d={path} fill="none" stroke="var(--color-acc)" strokeWidth={1.8} data-testid="dive-log-depth" pointerEvents="none" />
        {/* sightings */}
        {data.sightings.filter((sg) => sg.depth != null && sg.peakT >= v0 && sg.peakT <= v1).map((sg) => (
          <circle key={sg.id} cx={x(sg.peakT)} cy={y(sg.depth!)} r={sg.aboveTrigger ? 5.5 : 4} strokeWidth={2}
                  stroke={sg.type === 'DEFORMATION' ? 'var(--color-def)' : 'var(--color-pit)'}
                  fill={sg.aboveTrigger ? (sg.type === 'DEFORMATION' ? 'var(--color-def)' : 'var(--color-pit)') : 'var(--color-card)'} data-testid="dive-log-sighting">
            <title>{`${typeName(sg.type)} ${Math.round(sg.peak * 100)}% at ${sg.depth} m (${sg.zone || '—'})${sg.aboveTrigger ? ' — reached the clip trigger' : ''}`}</title>
          </circle>
        ))}
        </g>
        {/* drag + hover */}
        {drag && <rect pointerEvents="none" x={x(Math.min(...drag))} y={T} width={Math.abs(x(drag[1]) - x(drag[0]))} height={H - T - B} fill="rgba(79,209,197,0.15)" stroke="var(--color-acc)" />}
        {hover != null && hs && <>
          <line x1={x(hover)} x2={x(hover)} y1={T} y2={H - B} stroke="var(--color-tx2)" strokeWidth={1} pointerEvents="none" />
          {hs.depth != null && <circle cx={x(hs.t)} cy={y(hs.depth)} r={3.5} fill="var(--color-tx)" pointerEvents="none" />}
        </>}
      </svg>
      {hover != null && hs && (
        <div className="absolute top-2 pointer-events-none bg-bar/95 border border-edge rounded-lg px-3 py-2 text-xs shadow-xl"
             style={{ left: Math.min(Math.max(0, x(hover) + 12), W - 200) }} data-testid="dive-log-readout">
          <div className="font-mono font-bold">T+{fmtTime(hs.t)} <span className="text-dim font-normal">{clock(hs.t)}</span></div>
          <div>Depth <b>{hs.depth?.toFixed(1) ?? '—'} m</b> · heading {hs.heading?.toFixed(0) ?? '—'}°</div>
          <div>Water {hs.temperature?.toFixed(1) ?? '—'} °C · battery {hs.battery_v?.toFixed(1) ?? '—'} V</div>
          <div className="text-dim">{hs.mode || '—'}{hs.armed != null ? (hs.armed ? ' · armed' : ' · disarmed') : ''}</div>
        </div>
      )}
    </div>
  );
}

function Lanes({ data, v0, v1, width, onOpenClip, onHover }: {
  data: Log; v0: number; v1: number; width: number; onOpenClip: (id: string, t?: number) => void; onHover: (t: number | null) => void;
}) {
  const L = AXIS_L, R = 14, W = Math.max(320, width - 8);
  const pos = (t: number) => `${((t - v0) / Math.max(1e-6, v1 - v0)) * 100}%`;
  const inView = (a: number, b: number) => b >= v0 && a <= v1;
  const lane = (label: string, color: string, children: React.ReactNode, testid: string) => (
    <div className="flex items-center h-6" data-testid={testid}>
      <span className="text-[12px] font-bold shrink-0 text-right pr-2.5" style={{ width: L, color }}>{label}</span>
      <div className="relative h-3 bg-ink rounded" style={{ width: W - L - R }}>{children}</div>
    </div>
  );
  const spans = (type: string) => data.sightings.filter((s) => s.type === type && inView(s.start, s.end)).map((s) => (
    <button key={s.id} title={`${typeName(s.type)} ${Math.round(s.peak * 100)}% · T+${fmtTime(s.peakT)}${s.clipId ? ' — open its clip' : ''}`}
            onClick={() => { const c = data.clips.find((x) => x.id === s.clipId); if (c) onOpenClip(c.id, c.start != null ? Math.max(0, s.peakT - c.start) : undefined); }}
            onMouseEnter={() => onHover(s.peakT)} onMouseLeave={() => onHover(null)}
            className={`absolute top-0 h-3 rounded-sm ${s.clipId ? 'cursor-pointer' : 'cursor-default'} ${s.aboveTrigger ? '' : 'opacity-60'}`}
            style={{ left: pos(Math.max(v0, s.start)), width: `max(4px, ${((Math.min(v1, s.end) - Math.max(v0, s.start)) / Math.max(1e-6, v1 - v0)) * 100}%)`,
                     background: type === 'DEFORMATION' ? 'var(--color-def)' : 'var(--color-pit)' }} />
  ));
  const ticks = (items: { t: number | null; label: string; warn?: boolean }[]) => items.filter((e) => e.t != null && e.t >= v0 && e.t <= v1).map((e, i) => (
    <span key={i} title={`T+${fmtTime(e.t!)} · ${e.label}`} onMouseEnter={() => onHover(e.t)} onMouseLeave={() => onHover(null)}
          className={`absolute -top-0.5 w-1 h-4 rounded-sm ${e.warn ? 'bg-warn' : 'bg-tx2'}`} style={{ left: pos(e.t!) }} />
  ));
  return (
    <div className="px-1 pb-3 flex flex-col gap-0.5">
      {lane('Deformation', 'var(--color-def-tx)', spans('DEFORMATION'), 'dive-lane-deformation')}
      {lane('Pitting', 'var(--color-pit-tx)', spans('CORROSION'), 'dive-lane-pitting')}
      {lane('Vehicle', 'var(--color-tx2)', ticks(data.events.map((e) => ({ t: e.t, label: `${EVENT_LABEL[e.kind] || e.kind} ${e.text || ''}`, warn: e.kind === 'LOST' || e.kind === 'LEAK' || e.kind === 'PAUSED' }))), 'dive-lane-vehicle')}
      {lane('Operator', 'var(--color-tx2)', ticks(data.settings.filter((c) => c.t > 0).map((c) => ({ t: c.t, label: `${c.kind} → ${String(c.value)}` }))), 'dive-lane-operator')}
    </div>
  );
}

type SCol = 'time' | 'type' | 'peak' | 'depth' | 'zone';
function SightingsTable({ data, clock, onZoom, onOpenClip }: {
  data: Log; clock: (t: number) => string; onZoom: (t: number) => void; onOpenClip: (id: string, t?: number) => void;
}) {
  const { sort, toggle } = useSort<SCol>('dive-log-sightings-sort', { key: 'time', dir: 'asc' }, { peak: 'desc', depth: 'desc' });
  const get: Record<SCol, (s: DiveSighting) => string | number | null> = {
    time: (s) => s.peakT, type: (s) => s.type, peak: (s) => s.peak, depth: (s) => s.depth, zone: (s) => s.zone,
  };
  const rows = [...data.sightings].sort(cmpBy(get[sort.key], sort.dir));
  if (!rows.length) return <p className="p-4 text-[13px] text-mut">No sightings at or above {data.dive.logConf?.toFixed(2) ?? 'the log threshold'} during this dive.</p>;
  return (
    <table className="w-full text-[13px]" data-testid="dive-log-sightings">
      <thead className="sticky top-0 bg-bar text-[12px]">
        <tr className="text-left">
          {(['time', 'type', 'peak', 'depth', 'zone'] as SCol[]).map((k) => (
            <th key={k} className="px-3 py-1.5 font-normal"><SortHeader k={k} sort={sort} onSort={toggle}
              label={{ time: 'Dive time', type: 'Defect', peak: 'Peak', depth: 'Depth', zone: 'Zone' }[k]} /></th>
          ))}
          <th className="px-3 py-1.5 text-dim font-bold uppercase tracking-wide">Trigger</th><th className="px-3 py-1.5" />
        </tr>
      </thead>
      <tbody>
        {rows.map((s) => (
          <tr key={s.id} className="border-t border-line hover:bg-sel/60 cursor-pointer" onClick={() => onZoom(s.peakT)} data-testid="dive-log-sighting-row">
            <td className="px-3 py-1.5 font-mono">T+{fmtTime(s.peakT)} <span className="text-dim">{clock(s.peakT)}</span></td>
            <td className={`px-3 py-1.5 font-bold ${s.type === 'DEFORMATION' ? 'text-def-tx' : 'text-pit-tx'}`}>{typeName(s.type)}</td>
            <td className="px-3 py-1.5 font-mono">{Math.round(s.peak * 100)}% <span className="text-dim">· {s.detections} det.</span></td>
            <td className="px-3 py-1.5 font-mono">{s.depth != null ? `${s.depth.toFixed(1)} m` : '—'}</td>
            <td className="px-3 py-1.5">{s.zone || '—'}</td>
            <td className="px-3 py-1.5 text-xs">{s.type === 'DEFORMATION' ? (s.aboveTrigger ? <span className="text-def-tx font-bold">reached ≥ {s.trigger?.toFixed(2)}</span> : <span className="text-dim">below {s.trigger?.toFixed(2)}</span>) : <span className="text-dim">logged only</span>}</td>
            <td className="px-3 py-1.5 text-right">
              {s.clipId && <button onClick={(e) => { e.stopPropagation(); const c = data.clips.find((x) => x.id === s.clipId); onOpenClip(s.clipId!, c?.start != null ? Math.max(0, s.peakT - c.start) : undefined); }}
                                   className="inline-flex items-center gap-1 text-xs font-bold text-def-tx hover:underline cursor-pointer" data-testid="dive-log-open-clip"><Siren className="w-3.5 h-3.5" /> clip</button>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function SimpleTable({ head, rows, empty, testid, warn }: { head: string[]; rows: string[][]; empty: string; testid: string; warn?: (r: string[]) => boolean }) {
  if (!rows.length) return <p className="p-4 text-[13px] text-mut" data-testid={testid}>{empty}</p>;
  return (
    <table className="w-full text-[13px]" data-testid={testid}>
      <thead className="sticky top-0 bg-bar"><tr>{head.map((h, i) => <th key={i} className="text-left px-3 py-1.5 text-[12px] font-bold uppercase tracking-wide text-dim">{h}</th>)}</tr></thead>
      <tbody>{rows.map((r, i) => <tr key={i} className={`border-t border-line ${warn?.(r) ? 'text-warn' : ''}`}>{r.map((c, j) => <td key={j} className={`px-3 py-1.5 ${j < 2 ? 'font-mono' : ''}`}>{c}</td>)}</tr>)}</tbody>
    </table>
  );
}

function Summary({ data }: { data: Log }) {
  const s = data.summary;
  const zones = Object.entries(s.zoneTimeSec).sort((a, b) => b[1] - a[1]);
  const total = zones.reduce((a, [, v]) => a + v, 0) || 1;
  return (
    <div className="p-4 grid md:grid-cols-2 gap-6 text-[13px]" data-testid="dive-log-summary">
      <dl className="grid grid-cols-[170px_1fr] gap-y-1.5">
        <dt className="text-mut">Duration</dt><dd>{fmtDur(s.durationSec)}</dd>
        <dt className="text-mut">Sensor log</dt><dd>{s.samples.toLocaleString()} readings · {fmtDur(s.loggedSec)} logged</dd>
        <dt className="text-mut">Maximum depth</dt><dd>{s.maxDepth != null ? `${s.maxDepth.toFixed(1)} m` : '—'}</dd>
        <dt className="text-mut">Incident clips</dt><dd>{s.clips}</dd>
        <dt className="text-mut">Sightings</dt><dd>{s.sightings.DEFORMATION} deformation · {s.sightings.CORROSION} pitting · {s.aboveTrigger} reached the trigger</dd>
        <dt className="text-mut">One-off hits ignored</dt><dd>{s.flickersIgnored} <span className="text-dim">(above the trigger for 1–2 frames only)</span></dd>
        <dt className="text-mut">Battery</dt><dd>{s.battery ? `${s.battery.startV?.toFixed(1)} V → ${s.battery.endV?.toFixed(1)} V${s.battery.startPct != null && s.battery.endPct != null ? ` (${Math.round(s.battery.startPct)}% → ${Math.round(s.battery.endPct)}%)` : ''}` : '—'}</dd>
        <dt className="text-mut">Link losses</dt><dd className={s.linkLosses ? 'text-warn font-bold' : ''}>{s.linkLosses}</dd>
        <dt className="text-mut">Settings changed</dt><dd>{s.settingsChanges} time{s.settingsChanges === 1 ? '' : 's'}</dd>
      </dl>
      <div>
        <div className="text-[12px] font-bold uppercase tracking-wide text-dim mb-2">Time in each depth zone</div>
        {zones.length ? zones.map(([z, sec]) => (
          <div key={z} className="mb-1.5">
            <div className="flex justify-between"><span>{z}</span><span className="font-mono text-tx2">{fmtDur(sec)} · {Math.round((sec / total) * 100)}%</span></div>
            <div className="h-1.5 bg-ink rounded mt-0.5"><div className="h-full rounded bg-acc" style={{ width: `${(sec / total) * 100}%` }} /></div>
          </div>
        )) : <p className="text-mut">No depth readings.</p>}
      </div>
    </div>
  );
}
