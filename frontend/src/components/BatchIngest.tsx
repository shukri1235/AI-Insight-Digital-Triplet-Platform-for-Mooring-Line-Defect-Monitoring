import { useEffect, useState, type FormEvent, type MouseEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Download, FolderOpen, Plus, Trash2, X } from 'lucide-react';
import { apiUrl, del, fmtEta, postJSON } from '../api';
import type { LogEntry } from '../eventlog';
import type { HistorySummary, MooringLine, RunTiming, TestModeStatus, VideoItem } from '../types';
import FileBrowser from './FileBrowser';
import { ALL_ZONES, EventCounts, zoneShort } from './Panels';
import ThresholdPicker from './ThresholdPicker';
import { createdMs } from './ReviewLibrary';
import { fmtStamp } from '../util';
import { btn, Card, cmpBy, EventLog, SortHeader, StatusPill, TestBadge, useSort, type SortDir } from './shared';

type Filter = 'all' | 'dives' | 'INCIDENT_CLIP';
type Col = 'name' | 'line' | 'status' | 'threshold' | 'time' | 'defects' | 'log' | 'added';
const FIRST_DIR: Partial<Record<Col, SortDir>> = { added: 'desc', defects: 'desc', log: 'desc', time: 'desc' };

/** 3.2 s · 2 min 14 s · 1 h 02 min */
export function fmtSpent(sec: number): string {
  if (sec < 60) return `${sec.toFixed(sec < 10 ? 1 : 0)} s`;
  if (sec < 3600) return `${Math.floor(sec / 60)} min ${String(Math.round(sec % 60)).padStart(2, '0')} s`;
  return `${Math.floor(sec / 3600)} h ${String(Math.round((sec % 3600) / 60)).padStart(2, '0')} min`;
}

/** Hover text: the full breakdown of one batch analysis's processing time. */
function timingTitle(t: RunTiming): string {
  const lines = [
    `Processing: ${fmtSpent(t.processingSec)} (decode + detection) for ${fmtSpent(t.videoSec)} of video`,
    `Speed: ${t.speedX != null ? `${t.speedX.toFixed(1)}× real time · ` : ''}${t.framesPerSec.toFixed(1)} frames/s · ${t.frames.toLocaleString()} frames at ${t.sampleFps ?? '—'} fps`,
    t.detectSec != null && t.waitSec != null
      ? `Detection (GPU) ${fmtSpent(t.detectSec)} · waiting for decoded video ${fmtSpent(t.waitSec)}${t.waitSec > 0.1 * t.processingSec ? ' → video decoding is the bottleneck' : ' → detection (GPU) is the bottleneck'}${t.prefetch ? ` (read-ahead ${t.prefetch} frames)` : ''}`
      : '',
    t.proxySec ? `Browser copy (H.264 proxy): ${fmtSpent(t.proxySec)} → total ${fmtSpent(t.totalSec)}` : '',
    t.queuedSec ? `Waited in the queue: ${fmtSpent(t.queuedSec)} (not counted)` : '',
    t.resumes ? `Resumed ${t.resumes}× after a restart (time while the app was closed not counted)` : '',
    t.liveSharedSec > 0.5 ? `GPU shared with a live dive for ${fmtSpent(t.liveSharedSec)} (live goes first, so this run was slower)` : '',
    `${t.resolution ?? ''} ${t.codec ?? ''} · reader ${t.reader ?? '—'} · batch ${t.batchSize ?? '—'} · ${t.model ?? ''} on ${t.device ?? '—'}`,
  ];
  return lines.filter(Boolean).join('\n');
}
const STATUS_ORDER = ['PROCESSING', 'HASHING', 'QUEUED', 'UNPROCESSED', 'FAILED', 'COMPLETED', 'ENDED'];
const THR_PRESETS = [0.25, 0.4, 0.5, 0.6];

/** One file waiting to be imported in a multi-file batch; null = use the shared setting above. */
interface Staged { path: string; name: string; lineId: string | null; conf: number | null; result?: { tone: 'warn' | 'err'; text: string } }
const stem = (p: string) => (p.split(/[\\/]/).pop() || '').replace(/\.[^.]+$/, '');

export default function BatchIngest({ videos, lines, history, zone, onClearZone, onRefresh, onReview, log, logs, modelLoaded, testMode, registry }: {
  videos: VideoItem[];
  lines: MooringLine[];
  history: HistorySummary | null;
  zone: string;                      // registry zone filter (ALL_ZONES = none)
  onClearZone: () => void;
  onRefresh: () => void;
  onReview: (id: string) => void;
  log: (msg: string) => void;
  logs: LogEntry[];
  modelLoaded: boolean;
  testMode?: TestModeStatus | null;
  registry: ReactNode;
}) {
  const [name, setName] = useState('');
  const [lineId, setLineId] = useState(lines[0]?.id || 'ML-01-NORTH');
  const [conf, setConf] = useState<number | null>(null);
  const [path, setPath] = useState<string | null>(null);
  const [tlogPath, setTlogPath] = useState<string | null>(null);
  const [tlogOffset, setTlogOffset] = useState('');
  const [browser, setBrowser] = useState<null | { kind: 'video' | 'tlog'; forVideo?: string; locate?: boolean }>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: 'ok' | 'warn' | 'err'; text: string } | null>(null);
  const [filter, setFilter] = useState<Filter>('all');
  const [confirmRemove, setConfirmRemove] = useState<string | null>(null);
  // The row threshold panel is drawn at page level (portal): the table scrolls sideways, and a box that scrolls clips
  // anything that sticks out of it - so on the last rows the panel used to be cut off or scrolled inside the table.
  const [rowPicker, setRowPicker] = useState<{ id: string; action: 'process' | 'analyse'; anchor: HTMLElement } | null>(null);
  const [, setReflow] = useState(0);
  useEffect(() => {
    if (!rowPicker) return;
    const again = () => setReflow((n) => n + 1);        // follow the button when the page scrolls or resizes
    window.addEventListener('scroll', again, true);
    window.addEventListener('resize', again);
    return () => { window.removeEventListener('scroll', again, true); window.removeEventListener('resize', again); };
  }, [rowPicker]);
  const openPicker = (e: MouseEvent<HTMLButtonElement>, id: string, action: 'process' | 'analyse') => {
    setRowPicker({ id, action, anchor: e.currentTarget }); setRowConf(null);
  };
  const [rowConf, setRowConf] = useState<number | null>(null);
  const [staged, setStaged] = useState<Staged[]>([]);
  const { sort, toggle } = useSort<Col>('batch-table-sort', { key: 'added', dir: 'desc' }, FIRST_DIR);

  const reset = () => { setPath(null); setName(''); setTlogPath(null); setTlogOffset(''); };
  const offset = tlogOffset.trim() === '' ? null : Number(tlogOffset);

  const addStaged = (paths: string[]) => {
    setStaged((cur) => [...cur, ...paths.filter((p) => !cur.some((c) => c.path === p) && p !== path)
      .map((p) => ({ path: p, name: stem(p), lineId: null, conf: null }))]);
    if (path) { setStaged((cur) => (cur.some((c) => c.path === path) ? cur : [{ path, name: name || stem(path), lineId: null, conf: null }, ...cur])); setPath(null); setName(''); }
  };
  const effConf = (r: Staged) => r.conf ?? conf;
  const submitMany = async () => {
    if (staged.some((r) => effConf(r) == null)) return;
    setNotice(null);
    setBusy('importing');
    let ok = 0, dup = 0, err = 0;
    const left: Staged[] = [];
    for (const r of staged) {
      try {
        const res = await postJSON<any>('/api/videos/import', { path: r.path, name: r.name.trim() || stem(r.path), line_id: r.lineId ?? lineId, conf: effConf(r) });
        ok++;
        log(`INGEST: "${res.video.name}" imported in place · ${r.lineId ?? lineId} · threshold ${effConf(r)!.toFixed(2)} (batch of ${staged.length}).`);
      } catch (e: any) {
        if (e.status === 409) dup++; else err++;
        left.push({ ...r, result: { tone: e.status === 409 ? 'warn' : 'err', text: e.message } });
      }
    }
    setStaged(left);
    setNotice({ tone: left.length ? 'warn' : 'ok',
                text: `Registered ${ok} of ${ok + dup + err} file${ok + dup + err === 1 ? '' : 's'} in place and queued them${dup ? ` · ${dup} already registered` : ''}${err ? ` · ${err} failed` : ''}.${left.length ? ' The ones left below say why.' : ''}` });
    setBusy(null); onRefresh();
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (staged.length) { await submitMany(); return; }
    if (conf == null || !path) return;
    setNotice(null);
    setBusy('importing');
    try {
      const res = await postJSON<any>('/api/videos/import', { path, name, line_id: lineId, conf, tlog_path: tlogPath, tlog_offset_sec: offset });
      setNotice({ tone: 'ok', text: `Registered "${res.video.name}" in place (nothing copied). Fingerprinting, then processing at ${conf.toFixed(2)}.` });
      log(`INGEST: "${res.video.name}" imported in place · threshold ${conf.toFixed(2)}.`);
      reset(); onRefresh();
    } catch (err: any) {
      setNotice({ tone: err.status === 409 ? 'warn' : 'err', text: err.message });
      if (err.status === 409) { log(`INGEST: duplicate — ${err.message}`); reset(); }   // already registered: nothing left to do with it
    } finally { setBusy(null); }
  };

  const rowAction = async () => {
    if (!rowPicker || rowConf == null) return;
    const v = videos.find((x) => x.id === rowPicker.id);
    try {
      if (rowPicker.action === 'process') await postJSON(`/api/videos/${rowPicker.id}/process`, { conf: rowConf });
      else await postJSON(`/api/videos/${rowPicker.id}/analyse`, { conf: rowConf });
      log(`BATCH: ${rowPicker.action === 'process' ? 'queued' : 're-analysis queued for'} "${v?.name}" at ${rowConf.toFixed(2)}${rowPicker.action === 'analyse' ? ' (original kept)' : ''}.`);
    } catch (e: any) { setNotice({ tone: 'err', text: e.message }); }
    setRowPicker(null); setRowConf(null); onRefresh();
  };

  const action = async (fn: () => Promise<unknown>, msg: string) => {
    try { await fn(); log(msg); } catch (e: any) { setNotice({ tone: 'err', text: e.message }); }
    onRefresh();
  };

  const attachTlog = async (videoId: string, p: string) => {
    try {
      const r = await postJSON<any>(`/api/videos/${videoId}/tlog-import`, { path: p });
      setNotice({ tone: 'ok', text: `ROV log attached: ${r.samples} samples (${r.alignment}).` });
      log(`INGEST: .tlog attached to "${videos.find((v) => v.id === videoId)?.name}" · ${r.samples} samples.`);
    } catch (e: any) { setNotice({ tone: 'err', text: e.message }); }
    onRefresh();
  };
  const locate = async (videoId: string, p: string) => {
    const v = videos.find((x) => x.id === videoId);
    try {
      await postJSON(`/api/videos/${videoId}/locate`, { path: p });
      setNotice({ tone: 'ok', text: `Found "${v?.name}" at ${p}. Its records and results are unchanged.` });
      log(`INGEST: "${v?.name}" located at ${p} (fingerprint matches).`);
    } catch (e: any) { setNotice({ tone: e.status === 409 ? 'warn' : 'err', text: e.message }); }
    onRefresh();
  };

  const zoneVideos = zone !== ALL_ZONES ? new Set(Object.entries(history?.byVideo || {}).filter(([, z]) => (z[zone] ?? 0) > 0).map(([id]) => id)) : null;
  const newest = cmpBy<VideoItem>(createdMs, 'desc');
  const primary = {
    name: cmpBy<VideoItem>((v) => v.name, sort.dir),
    line: cmpBy<VideoItem>((v) => v.lineId, sort.dir),            // by line, newest first within each line
    status: cmpBy<VideoItem>((v) => STATUS_ORDER.indexOf(v.status), sort.dir),
    threshold: cmpBy<VideoItem>((v) => v.activeRun?.conf ?? v.pendingConf, sort.dir),
    time: cmpBy<VideoItem>((v) => v.elapsedSec ?? v.activeRun?.timing?.processingSec ?? null, sort.dir),
    defects: cmpBy<VideoItem>((v) => (v.defectEvents ? v.defectEvents.DEFORMATION + v.defectEvents.CORROSION : null), sort.dir),
    log: cmpBy<VideoItem>((v) => v.telemetrySamples || null, sort.dir),
    added: cmpBy<VideoItem>(createdMs, sort.dir),
  }[sort.key];
  const rows = videos.filter((v) => (filter === 'all' || (filter === 'dives' ? v.sourceType !== 'INCIDENT_CLIP' : v.sourceType === filter))
    && (!zoneVideos || zoneVideos.has(v.id))).sort((a, b) => primary(a, b) || newest(a, b));
  const counts = { all: videos.length, dives: videos.filter((v) => v.sourceType !== 'INCIDENT_CLIP').length,
                   INCIDENT_CLIP: videos.filter((v) => v.sourceType === 'INCIDENT_CLIP').length };
  const canSubmit = !busy && (staged.length ? staged.every((r) => effConf(r) != null) : conf != null && !!path);

  return (
    <main className="flex-1 px-5 py-4 flex flex-col gap-4 stagger">
      {browser && (
        <FileBrowser kind={browser.kind}
                     title={browser.locate ? 'Where is this dive now? (the same file, moved)' : browser.kind === 'tlog' ? 'Select BlueROV2 telemetry log (.tlog)' : 'Select dive video on this PC'}
                     onClose={() => setBrowser(null)}
                     onPickMany={browser.kind === 'video' && !browser.forVideo ? (ps) => { if (ps.length === 1 && !staged.length) { setPath(ps[0]); if (!name) setName(stem(ps[0])); } else addStaged(ps); setBrowser(null); } : undefined}
                     onPick={(p) => {
                       if (browser.locate && browser.forVideo) locate(browser.forVideo, p);
                       else if (browser.forVideo) attachTlog(browser.forVideo, p);
                       else if (browser.kind === 'tlog') setTlogPath(p);
                       else if (staged.length) addStaged([p]);
                       else { setPath(p); if (!name) setName(stem(p)); }
                       setBrowser(null);
                     }} />
      )}

      {/* ---- import bar ---- */}
      <Card>
        <form onSubmit={submit} aria-label="Import a dive" className="flex flex-wrap items-end gap-3.5">
          <div className="flex flex-col gap-1.5 flex-[2_1_300px] min-w-0">
            <span className="text-xs text-mut">Dive video on this PC</span>
            <button type="button" onClick={() => setBrowser({ kind: 'video' })} data-testid="browse-video" title="Any folder or external drive on this PC. The file stays where it is."
                    className="flex items-center gap-2 bg-ink border border-dashed border-[#3A4E63] text-tx rounded-[9px] px-3 py-2.5 text-left cursor-pointer min-w-0">
              <FolderOpen className="w-4 h-4 shrink-0" />
              <span className="truncate text-[13px]">{staged.length ? `${staged.length} files below — browse to add more…` : path || 'Browse this PC… (tick several files to import together)'}</span>
            </button>
          </div>
          {!staged.length && (
            <label className="flex flex-col gap-1.5 flex-[1_1_180px] text-xs text-mut">Name
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. ML-02 Dive 14" data-testid="ingest-name" className={`${btn.input} text-sm`} />
            </label>
          )}
          <label className="flex flex-col gap-1.5 flex-[1_1_160px] text-xs text-mut">{staged.length ? 'Line (all files)' : 'Line'}
            <select value={lineId} onChange={(e) => setLineId(e.target.value)} className={`${btn.input} text-sm`} data-testid="ingest-line">
              {lines.map((l) => <option key={l.id} value={l.id}>{l.id}</option>)}
            </select>
          </label>
          <ThresholdPicker value={conf} onChange={setConf} hint={false} label={staged.length ? 'Threshold (all files)' : undefined} />
          {!staged.length && <div className="flex flex-col gap-1.5">
            {tlogPath && (
              <input type="number" step="0.1" value={tlogOffset} onChange={(e) => setTlogOffset(e.target.value)} aria-label="Video start offset in the log"
                     placeholder="offset s (blank = auto)" title="The video starts this many seconds into the log; blank aligns it automatically"
                     className={`${btn.input} w-[170px] text-xs !py-1.5`} />
            )}
            <button type="button" onClick={() => (tlogPath ? setTlogPath(null) : setBrowser({ kind: 'tlog' }))} data-testid="ingest-tlog"
                    title="Optional BlueROV2 telemetry log (QGroundControl / BlueOS) for depth, heading and water temperature"
                    className={`${btn.secondary} text-[13px] flex items-center gap-1.5 max-w-[220px]`}>
              {tlogPath ? <><span className="truncate">{tlogPath.split(/[\\/]/).pop()}</span><X className="w-3.5 h-3.5 shrink-0" /></> : <><Plus className="w-3.5 h-3.5" /> ROV log (.tlog)</>}
            </button>
          </div>}
          <button type="submit" disabled={!canSubmit} data-testid="ingest-submit" className={`${btn.primary} px-[18px]`}>
            {busy ? 'Registering…' : staged.length ? `Import & queue ${staged.length}` : 'Import & queue'}
          </button>
        </form>
        {staged.length > 0 && (
          <div className="mt-3 border border-line rounded-xl overflow-hidden" data-testid="staged">
            <div className="flex items-center gap-3 px-3 py-2 bg-bar text-xs text-mut">
              <span className="font-bold text-tx2">{staged.length} files to import</span>
              <span>Line and threshold above apply to every file; change a row to override it. ROV logs can be attached per dive after import.</span>
              <button type="button" onClick={() => setStaged([])} className="ml-auto underline cursor-pointer hover:text-tx">Clear all</button>
            </div>
            <div className="max-h-64 overflow-auto">
              <table className="w-full text-[13px]">
                <thead><tr className="text-left text-[12px] uppercase tracking-[.08em] text-dim">
                  <th className="px-3 py-1.5 font-bold">File</th><th className="px-2 font-bold">Name</th><th className="px-2 font-bold">Line</th><th className="px-2 font-bold">Threshold</th><th />
                </tr></thead>
                <tbody>
                  {staged.map((r, i) => {
                    const upd = (patch: Partial<Staged>) => setStaged((cur) => cur.map((x, j) => (j === i ? { ...x, ...patch, result: undefined } : x)));
                    return (
                      <tr key={r.path} className="border-t border-line align-middle" data-testid="staged-row">
                        <td className="px-3 py-1.5 max-w-[260px]">
                          <div className="font-mono text-xs truncate" title={r.path}>{r.path.split(/[\\/]/).pop()}</div>
                          {r.result && <div className={`text-[12px] ${r.result.tone === 'warn' ? 'text-warn' : 'text-def-tx'}`}>{r.result.text}</div>}
                        </td>
                        <td className="px-2 py-1.5"><input value={r.name} onChange={(e) => upd({ name: e.target.value })} aria-label="Dive name"
                                                           className={`${btn.input} !py-1 text-[13px] w-full min-w-[160px]`} /></td>
                        <td className="px-2 py-1.5">
                          <select value={r.lineId ?? ''} onChange={(e) => upd({ lineId: e.target.value || null })} aria-label="Line" data-testid="staged-line"
                                  className={`${btn.input} !py-1 text-[13px] ${r.lineId ? '' : 'text-mut'}`}>
                            <option value="">Same ({lineId})</option>
                            {lines.map((l) => <option key={l.id} value={l.id}>{l.id}</option>)}
                          </select>
                        </td>
                        <td className="px-2 py-1.5">
                          <select value={r.conf ?? ''} onChange={(e) => upd({ conf: e.target.value ? Number(e.target.value) : null })} aria-label="Threshold" data-testid="staged-conf"
                                  className={`${btn.input} !py-1 text-[13px] ${r.conf != null ? '' : 'text-mut'}`}>
                            <option value="">Same ({conf != null ? conf.toFixed(2) : 'choose above'})</option>
                            {THR_PRESETS.map((p) => <option key={p} value={p}>{p.toFixed(2)}</option>)}
                          </select>
                        </td>
                        <td className="px-2 text-right">
                          <button type="button" onClick={() => setStaged((cur) => cur.filter((_, j) => j !== i))} aria-label={`Remove ${r.name}`}
                                  className="text-dim hover:text-def-tx p-1 cursor-pointer"><Trash2 className="w-3.5 h-3.5" /></button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}
        {(notice || !modelLoaded || testMode?.enabled || (path && conf == null) || staged.some((r) => effConf(r) == null)) && (
          <div className="flex flex-col gap-1.5 mt-3">
            {((path && conf == null) || staged.some((r) => effConf(r) == null)) && <span className="text-xs text-warn">Choose a detection threshold to continue — it is recorded with the analysis.</span>}
            {notice && (
              <p data-testid="ingest-notice" className={`text-xs rounded-lg px-3 py-2 ${notice.tone === 'ok' ? 'text-ok bg-[#0F2A1E]'
                : notice.tone === 'warn' ? 'text-warn bg-[#33270C]' : 'text-def-soft bg-def-bg'}`}>{notice.text}</p>
            )}
            {!modelLoaded && <p className="text-xs text-warn">No YOLO weights loaded — load a model (gear menu) or queued jobs will fail.</p>}
            {testMode?.enabled && (
              <p data-testid="batch-test-note" className="text-xs text-tst">
                Test Mode is on: imports are tagged TEST{Object.keys(testMode.standIn).length ? ` and stand-in classes apply (${Object.entries(testMode.standIn).map(([k, v]) => `${k} → ${v}`).join(', ')})` : ''}.
              </p>
            )}
          </div>
        )}
      </Card>

      {/* ---- dives & clips ---- */}
      <Card pad={false} testid="batch-note">
        <div className="flex flex-wrap items-center gap-2.5 px-4 py-3 border-b border-line">
          <span className="font-semibold text-[15px]">Dives &amp; clips</span>
          <div role="group" aria-label="Filter" className="flex gap-1 ml-2">
            {(['all', 'dives', 'INCIDENT_CLIP'] as Filter[]).map((f) => (
              <button key={f} onClick={() => setFilter(f)} aria-pressed={filter === f} data-testid={`filter-${f}`}
                      className={`rounded-[7px] px-2.5 py-1.5 text-[13px] cursor-pointer ${filter === f ? 'bg-[#1C2A39] text-tx' : 'text-mut'}`}>
                {f === 'all' ? 'All' : f === 'dives' ? 'Dives' : 'Incident clips'} {counts[f]}
              </button>
            ))}
          </div>
          {zone !== ALL_ZONES && (
            <span className="flex items-center gap-1.5 bg-acc-bg text-acc-tx text-[13px] font-semibold rounded-full pl-3 pr-1.5 py-1" data-testid="zone-filter">
              Defects in {zoneShort(zone)}
              <button onClick={onClearZone} aria-label="Clear zone filter" className="w-5 h-5 grid place-items-center rounded-full hover:bg-acc/20 cursor-pointer"><X className="w-3.5 h-3.5" /></button>
            </span>
          )}
          <span className="ml-auto text-[13px] text-mut">Batch uses the GPU only when no live dive needs it</span>
          <a href={apiUrl('/api/batch/timings.csv')} data-testid="timings-csv" title="Every batch analysis with its processing time, speed and settings — for benchmarking"
             className={`${btn.secondary} !py-1.5 text-[13px] inline-flex items-center gap-1.5`}><Download className="w-3.5 h-3.5" />Processing times (CSV)</a>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm min-w-[1120px]">
            <thead>
              <tr className="text-left text-[12px]" data-testid="batch-header">
                {([['name', 'Name', 'px-4'], ['line', 'Line', 'px-2'], ['added', 'Added', 'px-2'], ['status', 'Status', 'px-2'], ['threshold', 'Threshold', 'px-2'], ['time', 'Processing time', 'px-2'],
                   ['defects', 'Defects', 'px-2'], ['log', 'Depth log', 'px-2']] as [Col, string, string][]).map(([k, label, pad]) => (
                  <th key={k} scope="col" className={`${pad} py-2.5 font-medium`} aria-sort={sort.key === k ? (sort.dir === 'asc' ? 'ascending' : 'descending') : undefined}>
                    <SortHeader k={k} label={label} sort={sort} onSort={toggle}
                                title={k === 'added' ? 'When the dive was imported or the clip was recorded — click to sort'
                                  : k === 'time' ? 'How long the analysis took (decode + detection); hover a value for the breakdown — click to sort' : undefined} />
                  </th>
                ))}
                <th scope="col" className="px-4 py-2.5 font-bold uppercase tracking-[.08em] text-dim text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && (
                <tr className="border-t border-line"><td colSpan={9} className="px-4 py-8 text-center text-[13px] text-mut">
                  {zoneVideos ? `No processed dive has defects in ${zoneShort(zone)}.` : 'Nothing here yet. Import a dive above, or let Live clip an incident.'}
                </td></tr>
              )}
              {rows.map((v) => {
                const clip = v.sourceType === 'INCIDENT_CLIP';
                const jobBusy = ['QUEUED', 'PROCESSING', 'HASHING'].includes(v.status) || !!v.pendingRun;
                const reviewable = !v.missing && v.playable && v.status === 'COMPLETED';
                const prog = v.pendingRun && v.status === 'COMPLETED' ? v.pendingRun.progress : v.progress;
                const ev = v.defectEvents;
                const sub = clip
                  ? `Incident clip · ${v.duration}${v.incident?.depth != null ? ` · ${v.incident.depth.toFixed(1)} m` : ''}`
                  : `${v.duration} · ${v.resolution.replace('x', '×')} · ${v.isManaged ? 'library copy' : 'on this PC'}`;
                return (
                  <tr key={v.id} className="border-t border-line align-middle" data-testid="vault-row" data-name={v.name}>
                    <td className="px-4 py-3 max-w-[340px]">
                      <div className={`font-semibold truncate ${clip ? 'text-def-tx' : ''}`} title={v.path || v.name}>{v.name}</div>
                      <div className="text-xs text-dim flex flex-wrap items-center gap-x-2 gap-y-1 mt-0.5">
                        {v.mediaOffline ? <span className="text-warn">Media offline — drive or folder not found</span> : <span>{sub}</span>}
                        {v.isTest && <TestBadge standIn={v.testProfile?.standIn} />}
                      </div>
                      {v.error && !v.mediaOffline && <div className="text-xs text-def-tx mt-1 whitespace-normal">{v.error}</div>}
                    </td>
                    <td className="px-2 py-3 font-bold text-[13px]">{v.lineId?.split('-').slice(0, 2).join('-') || '—'}</td>
                    <td className="px-2 py-3 font-mono text-xs text-tx2 whitespace-nowrap" data-testid="added-cell"
                        title={`${clip ? 'Recorded' : 'Imported'} ${new Date(createdMs(v)).toLocaleString()}`}>{fmtStamp(createdMs(v))}</td>
                    <td className="px-2 py-3 min-w-[200px]">
                      {(v.status === 'PROCESSING' || v.status === 'HASHING' || (v.pendingRun && v.status === 'COMPLETED')) ? (
                        <>
                          <div className="flex justify-between text-[13px] gap-2">
                            <span className="text-acc-tx font-semibold">{v.status === 'HASHING' ? 'Fingerprinting' : v.pendingRun && v.status === 'COMPLETED' ? `Re-analysing ${v.pendingRun.conf.toFixed(2)}` : 'Processing'}</span>
                            <span className="text-mut">{prog.toFixed(0)}%{v.status === 'PROCESSING' && v.etaSec != null ? ` · ${fmtEta(v.etaSec)}` : ''}</span>
                          </div>
                          <div className="h-1.5 bg-ink rounded-full mt-1.5 overflow-hidden"><div className="h-full bg-acc transition-all duration-700" style={{ width: `${prog}%` }} /></div>
                        </>
                      ) : <StatusPill status={v.missing && !v.mediaOffline ? 'FAILED' : v.status} />}
                    </td>
                    <td className="px-2 py-3 font-bold">
                      {v.activeRun ? <>{v.activeRun.conf.toFixed(2)}{v.activeRun.triggerConf ? <span className="text-dim font-semibold"> / trig {v.activeRun.triggerConf.toFixed(2)}</span> : null}</>
                        : v.pendingConf != null ? <span className="text-mut">{v.pendingConf.toFixed(2)}</span> : <span className="text-dim">—</span>}
                    </td>
                    <td className="px-2 py-3 text-[13px] whitespace-nowrap" data-testid="time-cell">
                      {v.elapsedSec != null ? (
                        <span className="text-acc-tx font-semibold font-mono" title="Processing time so far">{fmtSpent(v.elapsedSec)}<span className="text-mut font-sans font-normal"> so far</span></span>
                      ) : v.activeRun?.timing ? (
                        <span title={timingTitle(v.activeRun.timing)} className="cursor-help" data-testid="time-value">
                          <span className="font-bold font-mono">{fmtSpent(v.activeRun.timing.processingSec)}</span>
                          <span className="block text-xs text-mut">
                            {v.activeRun.timing.speedX != null ? `${v.activeRun.timing.speedX.toFixed(1)}× real time` : ''}{v.activeRun.timing.liveSharedSec > 0.5 ? ' · shared GPU' : ''}{v.activeRun.timing.resumes ? ' · resumed' : ''}
                          </span>
                        </span>
                      ) : <span className="text-dim" title={v.activeRun?.kind === 'CLIP' || v.activeRun?.kind === 'LIVE' ? 'Analysed live while recording' : v.activeRun ? 'Analysed before processing times were recorded (3.6.5) — re-analyse to measure' : undefined}>
                          {v.activeRun?.kind === 'CLIP' || v.activeRun?.kind === 'LIVE' ? 'live' : '—'}</span>}
                    </td>
                    <td className="px-2 py-3 text-[13px]" data-testid="defect-cell">
                      {v.status === 'COMPLETED' && ev ? <EventCounts c={ev.CORROSION} d={ev.DEFORMATION} empty="none" />
                        : v.status === 'PROCESSING' ? <span className="text-mut">so far {v.defectCount}</span> : <span className="text-dim">—</span>}
                    </td>
                    <td className="px-2 py-3 text-[13px]">
                      {v.telemetrySamples > 0 ? <span className="text-ok">{clip ? (v.isTest ? 'sim ✓' : 'live ✓') : v.tlog ? '.tlog ✓' : 'live ✓'}</span>
                        : !clip ? <span className="text-mut">none · <button onClick={() => setBrowser({ kind: 'tlog', forVideo: v.id })} className="text-acc-tx underline cursor-pointer" data-testid="attach-tlog">attach .tlog</button></span>
                        : <span className="text-dim">none</span>}
                    </td>
                    <td className="px-4 py-3 text-right whitespace-nowrap">
                      <div className="inline-flex gap-1.5 relative">
                        {v.mediaOffline && (
                          <button onClick={() => setBrowser({ kind: 'video', forVideo: v.id, locate: true })} data-testid="locate-file"
                                  className="bg-[#3A2A0B] text-warn border border-[#6B4E12] rounded-lg px-3 py-[7px] text-[13px] font-bold cursor-pointer">Locate file…</button>
                        )}
                        {reviewable && (
                          <button onClick={() => onReview(v.id)} data-testid="row-review" className="bg-acc text-on-acc font-bold rounded-lg px-3 py-[7px] text-[13px] cursor-pointer">Review</button>
                        )}
                        {jobBusy && v.status !== 'HASHING' && (
                          <button onClick={() => action(() => postJSON(`/api/videos/${v.id}/cancel`), `BATCH: cancelled "${v.name}".`)} data-testid="row-cancel"
                                  className={`${btn.secondary} !py-[7px] text-[13px]`}>Cancel</button>
                        )}
                        {!jobBusy && v.status === 'COMPLETED' && (
                          <button onClick={(e) => openPicker(e, v.id, 'analyse')} disabled={v.mediaOffline}
                                  title={v.mediaOffline ? 'Locate the file first' : 'Re-analyse at a threshold of your choice (new analysis, original kept)'}
                                  data-testid="row-reanalyse" className={`${btn.secondary} !py-[7px] text-[13px]`}>Re-analyse</button>
                        )}
                        {!jobBusy && !v.missing && (v.status === 'UNPROCESSED' || v.status === 'FAILED') && (
                          <button onClick={(e) => openPicker(e, v.id, 'process')} data-testid="row-process"
                                  className={`${btn.secondary} !py-[7px] text-[13px]`}>Process</button>
                        )}
                        {/* records with results are kept for integrity; only never-analysed imports can be removed */}
                        {!jobBusy && !clip && !v.activeRun && (confirmRemove === v.id ? (
                          <button onClick={() => { setConfirmRemove(null); action(() => del(`/api/videos/${v.id}`), `INGEST: removed "${v.name}" (never analysed; ${v.isManaged ? 'library copy deleted' : 'your file was not touched'}).`); }}
                                  onMouseLeave={() => setConfirmRemove(null)} className="bg-def text-on-def rounded-lg px-3 py-[7px] text-[13px] font-bold cursor-pointer">Confirm remove</button>
                        ) : (
                          <button onClick={() => setConfirmRemove(v.id)} data-testid="remove-record" title="Remove this never-analysed import (your file is kept)"
                                  className="text-dim hover:text-def-tx px-1.5 cursor-pointer"><X className="w-4 h-4" /></button>
                        ))}
                        {rowPicker?.id === v.id && (() => {
                          const r = rowPicker.anchor.getBoundingClientRect();
                          const H = 240, up = r.bottom + 8 + H > window.innerHeight && r.top - 8 - H > 0;   // no room below: open upwards
                          const pos = { position: 'fixed' as const, right: Math.max(8, window.innerWidth - r.right),
                                        ...(up ? { bottom: window.innerHeight - r.top + 8 } : { top: r.bottom + 8 }) };
                          return createPortal(
                            <div style={pos} className="z-50 w-[340px] bg-card border border-edge2 rounded-xl p-3.5 shadow-2xl flex flex-col gap-2.5 text-left whitespace-normal" data-testid="row-threshold">
                              <span className="text-[13px] font-bold">{rowPicker.action === 'process' ? 'Process at threshold' : 'Re-analyse (new analysis, original kept)'}</span>
                              <ThresholdPicker value={rowConf} onChange={setRowConf} label="Threshold" />
                              <div className="flex gap-2 justify-end">
                                <button onClick={() => setRowPicker(null)} className="text-[13px] text-mut px-2 cursor-pointer">Cancel</button>
                                <button onClick={rowAction} disabled={rowConf == null} data-testid="row-queue" className={`${btn.primary} !py-1.5 text-[13px]`}>Queue</button>
                              </div>
                            </div>, document.body);
                        })()}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="flex flex-wrap gap-4 items-stretch">
        <div className="flex-[2_1_640px] min-w-0 flex">{registry}</div>
        <EventLog entries={logs} title="Event log" hint="scroll for more" fill className="flex-[1_1_320px] min-w-0 max-h-[330px]" />
      </div>
    </main>
  );
}
