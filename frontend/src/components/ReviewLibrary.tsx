import { useEffect, useMemo, useRef, useState } from 'react';
import { Activity, ChevronRight, Film, FolderOpen, Pin, Search, Siren, X } from 'lucide-react';
import { fmtTime, getJSON, putJSON } from '../api';
import type { DiveSummary, MooringLine, VideoItem } from '../types';
import { fmtStamp } from '../util';
import { btn, Card, CardTitle, cmpBy, SortHeader, TestBadge, useSort, type SortDir } from './shared';

/* Review media library: every dive and incident clip that can be played, newest first,
 * with pinned items kept at the top. Pins are stored on the backend (videos.pinned_at), so
 * they survive restarts; the filter / sort choice is a per-browser convenience only. */

type Filter = 'all' | 'dives' | 'clips' | 'logs' | 'pinned';
type Col = 'name' | 'line' | 'created' | 'length' | 'defects';
const COL_LABEL: Record<Col, string> = { name: 'Name', line: 'Line', created: 'Created', length: 'Length', defects: 'Defect events' };
const FIRST_DIR: Partial<Record<Col, SortDir>> = { created: 'desc', length: 'desc', defects: 'desc' };

const PREF_KEY = 'review-library-prefs';
function loadFilter(): Filter {
  try {
    const p = JSON.parse(localStorage.getItem(PREF_KEY) || '{}');
    return ['all', 'dives', 'clips', 'logs', 'pinned'].includes(p.filter) ? p.filter : 'all';
  } catch { return 'all'; }
}

export const isReviewable = (v: VideoItem) =>
  !v.missing && v.playable && (v.sourceType === 'INCIDENT_CLIP' || v.status === 'COMPLETED');

/** Creation time in epoch ms (exact when the backend sends createdTs, else the minute string). */
export function createdMs(v: VideoItem): number {
  if (v.createdTs != null) return v.createdTs * 1000;
  const d = v.dateCreated ? new Date(v.dateCreated.replace(' ', 'T')) : null;
  return d && !Number.isNaN(d.getTime()) ? d.getTime() : 0;
}

const defectTotal = (v: VideoItem) =>
  v.defectEvents ? v.defectEvents.DEFORMATION + v.defectEvents.CORROSION : v.defectCount;

function dayLabel(ms: number): string {
  if (!ms) return 'Undated';
  const d = new Date(ms), today = new Date();
  const start = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((start(today) - start(d)) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short',
                                           ...(d.getFullYear() !== today.getFullYear() ? { year: 'numeric' } : {}) });
}


export function sortForPicker(list: VideoItem[]): VideoItem[] {
  return [...list].sort((a, b) => createdMs(b) - createdMs(a));
}

/** Live dive logs (the app's own record of each live dive: sensors, sightings, clips, events). Polls while a dive runs. */
function useDives(trigger: unknown): DiveSummary[] {
  const [dives, setDives] = useState<DiveSummary[]>([]);
  const live = dives.some((d) => d.isLive);
  useEffect(() => {
    let alive = true;
    const load = () => getJSON<DiveSummary[]>('/api/dives?limit=500').then((d) => { if (alive) setDives(d); }).catch(() => { /* backend offline */ });
    load();
    const timer = live ? setInterval(load, 4000) : undefined;
    return () => { alive = false; if (timer) clearInterval(timer); };
  }, [trigger, live]);
  return dives;
}

const LOGS_ON_ALL = 3;   // the "All" view shows the newest few dive logs; the chip lists every one

export default function ReviewLibrary({ videos, lines, onSelect, onOpenLog, onChanged, log }: {
  videos: VideoItem[];
  lines: MooringLine[];
  onSelect: (id: string) => void;
  onOpenLog: (diveId: string) => void;
  onChanged: () => void;
  log: (m: string) => void;
}) {
  const dives = useDives(videos);
  const [filter, setFilter] = useState<Filter>(loadFilter);
  const { sort, setSort, toggle } = useSort<Col>('review-library-sort', { key: 'created', dir: 'desc' }, FIRST_DIR);
  const [query, setQuery] = useState('');
  const [lineFilter, setLineFilter] = useState('');
  // Optimistic pin state until the next /api/videos refresh confirms it
  const [pinOverride, setPinOverride] = useState<Record<string, number | null>>({});
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => { try { localStorage.setItem(PREF_KEY, JSON.stringify({ filter })); } catch { /* private window */ } }, [filter]);
  useEffect(() => { setPinOverride({}); }, [videos]);
  useEffect(() => {   // "/" jumps to search, like most file browsers
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (e.key === '/' && !(t && /INPUT|SELECT|TEXTAREA/.test(t.tagName))) { e.preventDefault(); searchRef.current?.focus(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const pinnedTs = (v: VideoItem): number | null =>
    v.id in pinOverride ? pinOverride[v.id] : (v.pinned ? (v.pinnedTs ?? 1) : null);

  const all = useMemo(() => videos.filter(isReviewable), [videos]);
  const byId = useMemo(() => new Map(videos.map((v) => [v.id, v])), [videos]);
  const usedLines = useMemo(() => [...new Set([...all.map((v) => v.lineId), ...dives.map((d) => d.lineId)].filter(Boolean) as string[])].sort(), [all, dives]);
  const counts = {
    all: all.length,
    dives: all.filter((v) => v.sourceType !== 'INCIDENT_CLIP').length,
    clips: all.filter((v) => v.sourceType === 'INCIDENT_CLIP').length,
    logs: dives.length,
    pinned: all.filter((v) => pinnedTs(v) != null).length,
  };

  const q = query.trim().toLowerCase();
  const logsShown = dives.filter((d) => {
    if (lineFilter && d.lineId !== lineFilter) return false;
    if (q && !`${d.name} ${d.lineId || ''} ${d.startedAt || ''} live dive log ${d.isTest ? 'test' : ''} ${d.sightings.DEFORMATION ? 'deformation' : ''} ${d.sightings.CORROSION ? 'pitting' : ''}`.toLowerCase().includes(q)) return false;
    return true;
  }).sort((a, b) => (b.isLive ? 1 : 0) - (a.isLive ? 1 : 0) || (b.createdTs ?? 0) - (a.createdTs ?? 0));
  const logGroups: { label: string; items: DiveSummary[] }[] = [];
  for (const d of logsShown) {
    const label = d.isLive ? 'Running now' : dayLabel((d.createdTs ?? 0) * 1000);
    const last = logGroups[logGroups.length - 1];
    if (last?.label === label) last.items.push(d); else logGroups.push({ label, items: [d] });
  }
  const shown = filter === 'logs' ? [] : all.filter((v) => {
    if (filter === 'dives' && v.sourceType === 'INCIDENT_CLIP') return false;
    if (filter === 'clips' && v.sourceType !== 'INCIDENT_CLIP') return false;
    if (filter === 'pinned' && pinnedTs(v) == null) return false;
    if (lineFilter && v.lineId !== lineFilter) return false;
    if (q) {
      const parent = v.parentName || (v.parentId ? byId.get(v.parentId)?.name || '' : '');
      const hay = `${v.name} ${parent} ${v.lineId || ''} ${v.dateCreated || ''} ${v.incident?.type === 'CORROSION' ? 'pitting' : v.incident?.type || ''} ${v.defectEvents?.DEFORMATION ? 'deformation' : ''} ${v.defectEvents?.CORROSION ? 'pitting' : ''} ${v.isTest ? 'test' : ''}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  // Column sort; ties fall back to newest first. Sorting by line groups the list by line (newest first
  // inside each line), because "everything on ML-02" is what a line sort is for.
  const newest = cmpBy<VideoItem>(createdMs, 'desc');
  const primary = {
    name: cmpBy<VideoItem>((v) => v.name, sort.dir),
    line: cmpBy<VideoItem>((v) => v.lineId, sort.dir),
    created: cmpBy<VideoItem>(createdMs, sort.dir),
    length: cmpBy<VideoItem>((v) => v.durationSec, sort.dir),
    defects: cmpBy<VideoItem>(defectTotal, sort.dir),
  }[sort.key];
  const cmp = (a: VideoItem, b: VideoItem) => primary(a, b) || newest(a, b);

  // Pinned block first (most recently pinned on top), then the rest, grouped by day or by line
  const pinned = filter === 'pinned' ? [] : shown.filter((v) => pinnedTs(v) != null)
    .sort((a, b) => (pinnedTs(b)! - pinnedTs(a)!) || cmp(a, b));
  const rest = (filter === 'pinned' ? shown : shown.filter((v) => pinnedTs(v) == null)).sort(cmp);
  const groups: { label: string; items: VideoItem[] }[] = [];
  if (sort.key === 'created' || sort.key === 'line') {
    for (const v of rest) {
      const label = sort.key === 'created' ? dayLabel(createdMs(v)) : v.lineId ? `Line ${v.lineId}` : 'No line';
      const last = groups[groups.length - 1];
      if (last?.label === label) last.items.push(v); else groups.push({ label, items: [v] });
    }
  } else if (rest.length) {
    groups.push({ label: `By ${COL_LABEL[sort.key].toLowerCase()} · ${sort.dir === 'asc' ? (sort.key === 'name' ? 'A – Z' : 'smallest first') : (sort.key === 'name' ? 'Z – A' : 'largest first')}`, items: rest });
  }

  const togglePin = (v: VideoItem) => {
    const next = pinnedTs(v) == null;
    setPinOverride((o) => ({ ...o, [v.id]: next ? Date.now() / 1000 : null }));
    putJSON(`/api/videos/${v.id}/pin`, { pinned: next })
      .then(() => { log(`REVIEW: ${next ? 'pinned' : 'unpinned'} "${v.name}"`); onChanged(); })
      .catch((e) => {
        setPinOverride((o) => { const c = { ...o }; delete c[v.id]; return c; });
        log(`ERROR: could not ${next ? 'pin' : 'unpin'} "${v.name}" — ${e.message}`);
      });
  };

  const lineName = (id: string | null) => (id ? lines.find((l) => l.id === id)?.id || id : '—');

  const row = (v: VideoItem) => {
    const clip = v.sourceType === 'INCIDENT_CLIP';
    const isPinned = pinnedTs(v) != null;
    const parent = clip ? v.parentName || (v.parentId ? byId.get(v.parentId)?.name : undefined) : undefined;
    const ev = v.defectEvents;
    const inc = v.incident;
    const t = createdMs(v);
    return (
      <li key={v.id} data-testid="lib-row" data-id={v.id} data-pinned={isPinned ? '1' : '0'}
          className="group flex items-stretch border-t border-line first:border-t-0 hover:bg-sel/70">
        <button onClick={() => togglePin(v)} data-testid="lib-pin" aria-pressed={isPinned}
                aria-label={isPinned ? `Unpin ${v.name}` : `Pin ${v.name}`} title={isPinned ? 'Unpin' : 'Pin to the top'}
                className={`w-11 shrink-0 grid place-items-center cursor-pointer transition-colors ${isPinned ? 'text-acc' : 'text-edge2 group-hover:text-dim hover:!text-tx'}`}>
          <Pin className={`w-4 h-4 ${isPinned ? 'fill-current -rotate-45' : ''} transition-transform`} />
        </button>
        <button onClick={() => onSelect(v.id)} data-testid="review-pick"
                className="flex-1 min-w-0 grid items-center gap-x-4 gap-y-1 py-2.5 pr-3 text-left cursor-pointer
                           grid-cols-[30px_minmax(0,1fr)] md:grid-cols-[30px_minmax(0,1fr)_104px_118px_64px_200px_16px]">
          <span className={`w-[30px] h-[30px] rounded-lg grid place-items-center ${clip ? 'bg-def-bg text-def' : 'bg-acc-bg text-acc'}`}>
            {clip ? <Siren className="w-4 h-4" /> : <Film className="w-4 h-4" />}
          </span>
          <span className="min-w-0">
            <span className="flex items-center gap-2 min-w-0">
              <span className="text-[13.5px] font-bold text-tx truncate" title={v.name}>{v.name}</span>
              {v.isTest && <TestBadge standIn={v.testProfile?.standIn} />}
            </span>
            <span className="block text-xs text-mut truncate mt-0.5">
              {clip
                ? <>Incident clip{inc?.type ? ` · ${inc.type === 'CORROSION' ? 'pitting' : inc.type.toLowerCase()} ${Math.round((inc.confidence || 0) * 100)}%` : ''}{inc?.depth != null ? ` · ${inc.depth.toFixed(1)} m` : ''}{parent ? ` · from ${parent}` : ''}</>
                : <>{v.sourceType === 'LIVE_SESSION' ? 'Live dive' : 'Imported dive'}{v.tlog ? ' · with .tlog' : ''}{v.resolution !== 'Unknown' ? ` · ${v.resolution}` : ''}</>}
              <span className="md:hidden"> · {lineName(v.lineId)} · {fmtStamp(t)} · {v.duration}</span>
            </span>
          </span>
          <span className="hidden md:block text-xs font-semibold text-tx2 truncate" data-testid="lib-line">{lineName(v.lineId)}</span>
          <span className="hidden md:block font-mono text-xs text-tx2 whitespace-nowrap" data-testid="lib-time"
                title={`${clip ? 'Clipped' : 'Imported'} ${new Date(t).toLocaleString()}`}>{fmtStamp(t)}</span>
          <span className="hidden md:block font-mono text-xs text-tx2 text-right">{v.duration}</span>
          <span className="col-start-2 md:col-start-auto flex items-center gap-1.5 justify-start md:justify-end flex-wrap">
            {ev && ev.DEFORMATION > 0 && <span className="text-[12px] font-bold rounded-md px-1.5 py-0.5 bg-def-bg text-def-tx">Deformation {ev.DEFORMATION}</span>}
            {ev && ev.CORROSION > 0 && <span className="text-[12px] font-bold rounded-md px-1.5 py-0.5 bg-pit-bg text-pit-tx">Pitting {ev.CORROSION}</span>}
            {(!ev || ev.DEFORMATION + ev.CORROSION === 0) && <span className="text-[12px] font-semibold text-dim">{v.defectCount ? `${v.defectCount} detections` : 'No defects'}</span>}
          </span>
          <ChevronRight className="hidden md:block w-4 h-4 text-dim group-hover:text-tx" />
        </button>
      </li>
    );
  };

  const logRow = (d: DiveSummary) => (
    <li key={d.id} data-testid="lib-log-row" data-id={d.id} className="group flex items-stretch border-t border-line first:border-t-0 hover:bg-sel/70">
      <span className="w-11 shrink-0" />
      <button onClick={() => onOpenLog(d.id)} data-testid="lib-open-log"
              className="flex-1 min-w-0 grid items-center gap-x-4 gap-y-1 py-2.5 pr-3 text-left cursor-pointer
                         grid-cols-[30px_minmax(0,1fr)] md:grid-cols-[30px_minmax(0,1fr)_104px_118px_64px_200px_16px]">
        <span className="w-[30px] h-[30px] rounded-lg grid place-items-center bg-pit-bg text-pit"><Activity className="w-4 h-4" /></span>
        <span className="min-w-0">
          <span className="flex items-center gap-2 min-w-0">
            <span className="text-[13.5px] font-bold text-tx truncate" title={d.name}>{d.name}</span>
            {d.isTest && <TestBadge />}
            {d.isLive && <span className="text-[12px] font-extrabold uppercase rounded px-1.5 py-0.5 bg-def text-on-def">Live</span>}
          </span>
          <span className="block text-xs text-mut truncate mt-0.5">
            Live dive · log only · {d.clips} clip{d.clips === 1 ? '' : 's'}{d.maxDepth != null ? ` · max ${d.maxDepth.toFixed(1)} m` : ''}{d.samples ? ` · ${d.samples.toLocaleString()} sensor readings` : ' · no sensor log'}{(d.idleTailSec ?? 0) > 0 ? ` · then left open ${fmtTime(d.idleTailSec!)}` : ''}
            <span className="md:hidden"> · {lineName(d.lineId)} · {fmtStamp((d.createdTs ?? 0) * 1000)} · {fmtTime(d.durationSec)}</span>
          </span>
        </span>
        <span className="hidden md:block text-xs font-semibold text-tx2 truncate">{lineName(d.lineId)}</span>
        <span className="hidden md:block font-mono text-xs text-tx2 whitespace-nowrap">{fmtStamp((d.createdTs ?? 0) * 1000)}</span>
        <span className="hidden md:block font-mono text-xs text-tx2 text-right">{fmtTime(d.durationSec)}</span>
        <span className="col-start-2 md:col-start-auto flex items-center gap-1.5 justify-start md:justify-end flex-wrap">
          {d.sightings.DEFORMATION > 0 && <span className="text-[12px] font-bold rounded-md px-1.5 py-0.5 bg-def-bg text-def-tx">Deformation {d.sightings.DEFORMATION}</span>}
          {d.sightings.CORROSION > 0 && <span className="text-[12px] font-bold rounded-md px-1.5 py-0.5 bg-pit-bg text-pit-tx">Pitting {d.sightings.CORROSION}</span>}
          {d.sightings.DEFORMATION + d.sightings.CORROSION === 0 && <span className="text-[12px] font-semibold text-dim">No sightings</span>}
        </span>
        <ChevronRight className="hidden md:block w-4 h-4 text-dim group-hover:text-tx" />
      </button>
    </li>
  );
  const logSection = (label: string, items: DiveSummary[], testid?: string, more?: React.ReactNode) => (
    <div key={`log-${label}`} data-testid={testid}>
      <div className="sticky top-0 z-10 flex items-center gap-2 px-4 py-1.5 bg-bar/95 backdrop-blur border-y border-line first:border-t-0 text-[12px] font-bold uppercase tracking-[.08em] text-dim">
        <Activity className="w-3 h-3 text-pit" />{label}<span className="font-mono normal-case tracking-normal text-edge2">{items.length}</span>
        {more && <span className="ml-auto normal-case tracking-normal font-semibold">{more}</span>}
      </div>
      <ul>{items.map(logRow)}</ul>
    </div>
  );

  const section = (label: string, items: VideoItem[], icon?: React.ReactNode, testid?: string) => (
    <div key={label} data-testid={testid}>
      <div className="sticky top-0 z-10 flex items-center gap-2 px-4 py-1.5 bg-bar/95 backdrop-blur border-y border-line first:border-t-0 text-[12px] font-bold uppercase tracking-[.08em] text-dim">
        {icon}{label}<span className="font-mono normal-case tracking-normal text-edge2">{items.length}</span>
      </div>
      <ul>{items.map(row)}</ul>
    </div>
  );

  const chip = (k: Filter, label: string) => (
    <button key={k} onClick={() => setFilter(k)} data-testid={`lib-filter-${k}`} aria-pressed={filter === k}
            className={`px-3 py-1.5 rounded-full text-[12.5px] font-semibold cursor-pointer border transition-colors ${filter === k ? 'bg-acc-bg text-acc-tx border-acc/40' : 'bg-transparent text-tx2 border-edge hover:border-edge2'}`}>
      {label} <span className="font-mono text-[12px] opacity-70">{counts[k]}</span>
    </button>
  );

  const waiting = videos.filter((v) => !isReviewable(v) && v.sourceType !== 'INCIDENT_CLIP' && v.sourceType !== 'LIVE_SESSION' && !v.missing).length;

  return (
    <main className="flex-1 px-5 py-4 flex flex-col min-h-0">
      <Card pad={false} testid="review-library" className="flex-1 flex flex-col min-h-[520px]">
        <div className="px-4 pt-3.5 pb-3 flex flex-col gap-3 border-b border-line">
          <CardTitle icon={<FolderOpen className="w-3.5 h-3.5" />} title="Media library"
                     right={<span className="hidden sm:inline text-dim font-semibold">Pick a dive or clip to review — nothing plays until you do</span>} />
          <div className="flex flex-wrap items-center gap-2">
            <label className="relative flex-1 min-w-[220px] max-w-md">
              <Search className="w-4 h-4 text-dim absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
              <input ref={searchRef} value={query} onChange={(e) => setQuery(e.target.value)} data-testid="lib-search"
                     placeholder="Search name, line, date, defect…  ( / )" aria-label="Search media"
                     className={`${btn.input} w-full pl-9 pr-8 py-1.5 text-[13px]`} />
              {query && <button onClick={() => setQuery('')} aria-label="Clear search" className="absolute right-2 top-1/2 -translate-y-1/2 text-dim hover:text-tx cursor-pointer"><X className="w-3.5 h-3.5" /></button>}
            </label>
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="Show">
              {chip('all', 'All')}{chip('dives', 'Dives')}{chip('clips', 'Incident clips')}{chip('logs', 'Live dive logs')}{chip('pinned', 'Pinned')}
            </div>
            <div className="ml-auto flex items-center gap-2">
              {usedLines.length > 1 && (
                <select value={lineFilter} onChange={(e) => setLineFilter(e.target.value)} data-testid="lib-line-filter" aria-label="Mooring line"
                        className={`${btn.input} py-1.5 text-[13px]`}>
                  <option value="">All lines</option>
                  {usedLines.map((l) => <option key={l} value={l}>{l}</option>)}
                </select>
              )}
              <select value={`${sort.key}:${sort.dir}`} onChange={(e) => { const [key, dir] = e.target.value.split(':'); setSort({ key: key as Col, dir: dir as SortDir }); }}
                      data-testid="lib-sort" aria-label="Sort" className={`${btn.input} py-1.5 text-[13px] md:hidden`}>
                <option value="created:desc">Newest first</option>
                <option value="created:asc">Oldest first</option>
                <option value="line:asc">By line</option>
                <option value="defects:desc">Most defects</option>
                <option value="name:asc">Name A–Z</option>
              </select>
            </div>
          </div>
        </div>

        <div className="hidden md:grid grid-cols-[44px_30px_minmax(0,1fr)_104px_118px_64px_200px_16px] gap-x-4 pr-3 py-1.5 text-[12px] border-b border-line"
             data-testid="lib-header">
          <span /><span />
          {(['name', 'line', 'created'] as Col[]).map((k) => <SortHeader key={k} k={k} label={COL_LABEL[k]} sort={sort} onSort={toggle} />)}
          <SortHeader k="length" label="Length" sort={sort} onSort={toggle} align="right" />
          <SortHeader k="defects" label="Defect events" sort={sort} onSort={toggle} align="right" />
          <span />
        </div>

        <div className="flex-1 overflow-auto scroll-alert" data-testid="lib-list">
          {filter === 'logs' ? (
            logsShown.length === 0
              ? <Empty title={dives.length ? 'No dive log matches' : 'No live dives yet'}
                       text={dives.length ? 'Nothing matches these filters.' : 'Every live dive gets a log here: depth over time, sightings, incident clips, vehicle events and the .tlog. Start one on the Live tab.'} />
              : logGroups.map((g) => logSection(g.label, g.items))
          ) : all.length === 0 && !(filter === 'all' && logsShown.length) ? (
            <Empty title="Nothing to review yet" text="Import and process a dive in Batch, or run a live dive — incident clips land here automatically." />
          ) : shown.length === 0 && !(filter === 'all' && logsShown.length) ? (
            filter === 'pinned' && !q && !lineFilter
              ? <Empty title="Nothing pinned yet" text="Use the pin at the start of any row to keep a dive or clip at the top of this list." />
              : <Empty title="No media matches" text={q ? `Nothing matches “${query.trim()}”.` : 'Nothing matches these filters.'}
                       action={<button className={btn.ghost} onClick={() => { setQuery(''); setLineFilter(''); setFilter('all'); }}>Clear filters</button>} />
          ) : (
            <>
              {pinned.length > 0 && section('Pinned', pinned, <Pin className="w-3 h-3 text-acc fill-current -rotate-45" />, 'lib-pinned')}
              {filter === 'all' && logsShown.length > 0 && logSection('Live dive logs', logsShown.slice(0, LOGS_ON_ALL), 'lib-logs',
                logsShown.length > LOGS_ON_ALL ? <button onClick={() => setFilter('logs')} className="text-acc-tx hover:underline cursor-pointer" data-testid="lib-logs-all">all {logsShown.length} →</button> : undefined)}
              {groups.map((g) => section(g.label, g.items))}
            </>
          )}
        </div>

        <div className="px-4 py-2 border-t border-line text-xs text-dim flex flex-wrap gap-x-4 gap-y-1">
          <span>{filter === 'logs' ? `${logsShown.length} of ${dives.length} dive logs shown` : `${shown.length} of ${all.length} shown`}</span>
          {waiting > 0 && <span>{waiting} imported dive{waiting === 1 ? ' is' : 's are'} not analysed yet — process {waiting === 1 ? 'it' : 'them'} in Batch to review</span>}
          <span className="ml-auto">Pins are saved on the server and shared by everyone using this backend</span>
        </div>
      </Card>
    </main>
  );
}

function Empty({ title, text, action }: { title: string; text: string; action?: React.ReactNode }) {
  return (
    <div className="h-full min-h-[280px] flex flex-col items-center justify-center gap-2 text-center p-6" data-testid="lib-empty">
      <div className="w-12 h-12 rounded-full border border-edge bg-ink grid place-items-center text-dim mb-1"><FolderOpen className="w-5 h-5" /></div>
      <p className="text-[14px] font-bold text-tx">{title}</p>
      <p className="text-[13px] text-mut max-w-md">{text}</p>
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}
