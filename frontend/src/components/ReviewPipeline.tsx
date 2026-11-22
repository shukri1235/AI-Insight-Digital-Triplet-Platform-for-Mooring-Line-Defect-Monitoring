import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { ChevronLeft, ChevronRight, FolderOpen, Maximize2, Minimize2, Pause, Play } from 'lucide-react';
import { apiUrl, fmtTime, getJSON } from '../api';
import { useContainRect } from '../hooks';
import type { DefectRow, Detection, MooringLine, ObjectRow, Run, SensorRow, VideoItem } from '../types';
import { fmtMs, segmentFor } from '../util';
import { AnalysisCard } from './Panels';
import ReviewLibrary, { isReviewable, sortForPicker } from './ReviewLibrary';
import { btn, Card, DetectionOverlay, TestBadge } from './shared';


const EVENT_GAP = 2.0;     // detections closer than this are one event (matches backend)
const OVERLAY_HOLD = 0.6;  // show a logged box for this long after its timestamp
const SNAP_PX = 8;         // clicking within this many px of a marker snaps to it

interface DefectEvent { type: string; start: number; end: number; peak: number; severity: string; n: number }

function clusterEvents(rows: DefectRow[]): DefectEvent[] {
  const byType: Record<string, DefectRow[]> = {};
  for (const r of rows) (byType[r.defect_type] ||= []).push(r);
  const out: DefectEvent[] = [];
  for (const [type, list] of Object.entries(byType)) {
    let cur: DefectEvent | null = null;
    for (const r of list) {
      if (cur && r.timestamp_sec - cur.end <= EVENT_GAP) {
        cur.end = r.timestamp_sec; cur.n++; cur.peak = Math.max(cur.peak, r.confidence);
        if (r.severity === 'High confidence') cur.severity = 'High confidence';
      } else {
        if (cur) out.push(cur);
        cur = { type, start: r.timestamp_sec, end: r.timestamp_sec, peak: r.confidence, severity: r.severity, n: 1 };
      }
    }
    if (cur) out.push(cur);
  }
  return out.sort((a, b) => a.start - b.start);
}

/** Logged detections whose timestamp is the latest one <= t (within OVERLAY_HOLD). */
export function detectionsAt(rows: DefectRow[], t: number): Detection[] {
  let lo = 0, hi = rows.length - 1, idx = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (rows[mid].timestamp_sec <= t + 1e-4) { idx = mid; lo = mid + 1; } else hi = mid - 1;
  }
  if (idx < 0 || t - rows[idx].timestamp_sec > OVERLAY_HOLD) return [];
  const ts = rows[idx].timestamp_sec;
  const out: Detection[] = [];
  for (let i = idx; i >= 0 && rows[i].timestamp_sec === ts; i--) {
    const r = rows[i];
    if (r.x == null) continue;
    out.push({ id: `db-${r.id}`, type: r.defect_type, rawClass: r.raw_class || undefined, isDefect: true,
               confidence: r.confidence, severity: r.severity, x: r.x, y: r.y!, width: r.width!, height: r.height! });
  }
  return out;
}

/** General (non-defect) detections logged at the latest timestamp <= t. */
function objectsAt(rows: ObjectRow[], t: number): Detection[] {
  let lo = 0, hi = rows.length - 1, idx = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (rows[mid].t <= t + 1e-4) { idx = mid; lo = mid + 1; } else hi = mid - 1;
  }
  if (idx < 0 || t - rows[idx].t > OVERLAY_HOLD) return [];
  const ts = rows[idx].t;
  const out: Detection[] = [];
  for (let i = idx; i >= 0 && rows[i].t === ts; i--) {
    const r = rows[i];
    out.push({ id: `obj-${i}`, type: r.cls.toUpperCase(), rawClass: r.cls, isDefect: false, confidence: r.conf,
               severity: 'Info', x: r.x, y: r.y, width: r.w, height: r.h });
  }
  return out;
}

function telemetryAt(rows: SensorRow[], t: number): SensorRow | null {
  if (!rows.length) return null;
  let lo = 0, hi = rows.length - 1, idx = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (rows[mid].t <= t + 1e-4) { idx = mid; lo = mid + 1; } else hi = mid - 1;
  }
  const cands = [idx, idx + 1].filter((i) => i < rows.length);
  const best = cands.sort((x, y) => Math.abs(rows[x].t - t) - Math.abs(rows[y].t - t))[0];
  return Math.abs(rows[best].t - t) <= 2.0 ? rows[best] : null;   // no sample nearby = gap, not a stale value
}

/** Sightings -> bars: times less than EVENT_GAP apart are one stretch "in view" (same rule as events). */
interface LaneBar { left: number; width: number; start: number; end: number }

function laneBars(times: number[], duration: number): LaneBar[] {
  if (!duration || !times.length) return [];
  const ts = [...times].sort((a, b) => a - b);
  const out: LaneBar[] = [];
  let a = ts[0], b = ts[0];
  const push = () => out.push({ left: (a / duration) * 100, width: (Math.max(b - a, 0) / duration) * 100, start: a, end: b });
  for (const t of ts.slice(1)) {
    if (t - b <= EVENT_GAP) b = t; else { push(); a = b = t; }
  }
  push();
  return out;
}

export default function ReviewPipeline({ videos, reviewId, onSelect, onOpenLog, backTo = 'Library', lines, onRunsChanged, log, seekRequest, reportPanel }: {
  videos: VideoItem[];
  reviewId: string | null;
  onSelect: (id: string | null) => void;
  onOpenLog: (diveId: string) => void;
  /** where the close button goes back to ('Library', or 'Dive log' for a clip opened from a dive log) */
  backTo?: string;
  lines: MooringLine[];
  onRunsChanged: () => void;
  log: (m: string) => void;
  seekRequest: { t: number; nonce: number } | null;
  reportPanel: ReactNode;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);  // isolated from the live player
  const [defects, setDefects] = useState<DefectRow[]>([]);
  const [telemetry, setTelemetry] = useState<SensorRow[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [objects, setObjects] = useState<ObjectRow[]>([]);
  const [objCounts, setObjCounts] = useState<Record<string, number>>({});
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [dims, setDims] = useState<{ w: number; h: number } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const { ref: frameRef, rect } = useContainRect(dims?.w, dims?.h);

  const video = videos.find((v) => v.id === reviewId) || null;
  const reviewable = sortForPicker(videos.filter(isReviewable));
  const pinnedList = reviewable.filter((v) => v.pinned);
  const fps = video?.fps || 30;
  const trigger = video?.triggerOffsetSec ?? null;
  const isClip = video?.sourceType === 'INCIDENT_CLIP';
  const line = lines.find((l) => l.id === video?.lineId);

  useEffect(() => {
    setTelemetry([]); setTime(0); setDims(null); setLoadError(null); setPlaying(false);
    setDuration(video?.durationSec || 0);
    if (!reviewId) return;
    let cancelled = false;
    getJSON<SensorRow[]>(`/api/videos/${reviewId}/telemetry${(video?.durationSec ?? 0) > 3600 ? '?max_hz=1' : ''}`).then((t) => { if (!cancelled) setTelemetry(t || []); }).catch(() => {});
    return () => { cancelled = true; };
  }, [reviewId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Detections of ONE analysis run (the video's active run). A different threshold is a new
  // run (Re-analyse), never a slider over the recorded data.
  const activeRunId = video?.activeRun?.id;
  useEffect(() => {
    setDefects([]); setRun(null); setObjects([]); setObjCounts({});
    if (!reviewId) return;
    let cancelled = false;
    getJSON<{ objects: ObjectRow[]; counts: Record<string, number> }>(`/api/videos/${reviewId}/objects`)
      .then((o) => { if (!cancelled) { setObjects(o.objects || []); setObjCounts(o.counts || {}); } }).catch(() => {});
    getJSON<{ defects: DefectRow[]; run: Run | null }>(`/api/videos/${reviewId}/defects`)
      .then((d) => { if (!cancelled) { setDefects(d.defects || []); setRun(d.run); } })
      .catch((e) => !cancelled && setLoadError(e.message));
    return () => { cancelled = true; };
  }, [reviewId, activeRunId]);

  const events = useMemo(() => clusterEvents(defects), [defects]);
  const current = useMemo(() => detectionsAt(defects, time), [defects, time]);
  const currentObjects = useMemo(() => objectsAt(objects, time), [objects, time]);
  const curTel = useMemo(() => telemetryAt(telemetry, time), [telemetry, time]);
  const lanes = useMemo(() => {
    const by = (type: string) => defects.filter((d) => (type === 'DEFORMATION' ? d.defect_type === 'DEFORMATION' : d.defect_type !== 'DEFORMATION')).map((d) => d.timestamp_sec);
    return {
      def: laneBars(by('DEFORMATION'), duration),
      pit: laneBars(by('CORROSION'), duration),
      obj: laneBars(objects.map((o) => o.t), duration),
    };
  }, [defects, objects, duration]);

  /** Seek so that the frame DISPLAYED is the one analysed at timestamp t: browsers show
   *  frame i for currentTime in [i/fps, (i+1)/fps), so aim a quarter-frame inside it. */
  const seekExact = (t: number) => {
    const v = videoRef.current;
    if (!v) return;
    v.pause();
    v.currentTime = Math.max(0, Math.min(t + 0.25 / fps, (duration || t) - 0.001));
    setTime(t);
  };
  // Report citations ("EV-3 at 00:08") ask the player to jump; nonce makes repeats work
  const pendingSeek = useRef<number | null>(null);
  useEffect(() => {
    if (!seekRequest) return;
    if ((videoRef.current?.readyState ?? 0) >= 1) seekExact(seekRequest.t);
    else { pendingSeek.current = seekRequest.t; setTime(seekRequest.t); }   // show it now; the video follows once loaded
  }, [seekRequest?.nonce]); // eslint-disable-line react-hooks/exhaustive-deps
  const seekFree = (t: number) => {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = Math.max(0, Math.min(t, duration || t));
    setTime(v.currentTime);
  };
  const snapTargets = useMemo(() => [...(trigger != null ? [trigger] : []), ...events.map((e) => e.start)], [trigger, events]);
  const onTrackClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const t = ((e.clientX - r.left) / r.width) * duration;
    const tol = (SNAP_PX / r.width) * duration;
    const near = snapTargets.reduce<number | null>((best, s) => (Math.abs(s - t) <= tol && (best == null || Math.abs(s - t) < Math.abs(best - t)) ? s : best), null);
    if (near != null) seekExact(near); else seekFree(t);
  };
  const prevEvent = [...events].reverse().find((e) => e.start < time - 0.05);
  const nextEvent = events.find((e) => e.start > time + 0.05);
  const jump = (dir: 1 | -1) => {
    const target = dir > 0 ? nextEvent : prevEvent;
    if (target) seekExact(target.start);
  };
  // Full screen = the player box (video + boxes + a small control bar), not the bare <video>,
  // which would leave the bounding boxes behind
  const [fullscreen, setFullscreen] = useState(false);
  useEffect(() => {
    const on = () => setFullscreen(document.fullscreenElement === frameRef.current && !!frameRef.current);
    document.addEventListener('fullscreenchange', on);
    return () => document.removeEventListener('fullscreenchange', on);
  }, [frameRef]);
  const toggleFullscreen = () => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else frameRef.current?.requestFullscreen?.().catch(() => {});
  };
  const togglePlay = () => {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) v.play().catch(() => {}); else v.pause();
  };

  if (!video) {
    return <ReviewLibrary videos={videos} lines={lines} onSelect={onSelect} onOpenLog={onOpenLog} onChanged={onRunsChanged} log={log} />;
  }

  const pct = (t: number) => `${duration ? (t / duration) * 100 : 0}%`;
  const inc = video.incident;
  const desc = isClip
    ? `Incident clip${inc?.sessionTime != null ? ` · T+${fmtTime(inc.sessionTime)} into the live dive` : ''}${inc?.depth != null ? ` · ${inc.depth.toFixed(1)} m` : ''}`
    : `Dive · ${video.duration} · ${video.lineId || 'no line'}${video.dateCreated ? ` · imported ${video.dateCreated.slice(5, 16)}` : ''}`;
  const runLabel = run ? `${run.kind === 'LIVE' || run.kind === 'CLIP' ? 'Recorded live' : run.kind === 'REANALYSIS' ? 'Re-analysis' : 'Batch'} · ≥ ${run.conf.toFixed(2)}${run.triggerConf != null ? ` · trigger ${run.triggerConf.toFixed(2)}` : ''}` : '';
  const sensorSrc = telemetry[0]?.source === 'TLOG' ? 'imported .tlog' : telemetry[0]?.source === 'SIM' ? 'Test Mode simulator' : 'live MAVLink';
  const sensorSpan = telemetry.length > 1 ? telemetry[telemetry.length - 1].t - telemetry[0].t : 0;
  const sensorRate = sensorSpan > 0 ? (telemetry.length - 1) / sensorSpan : 0;

  return (
    <main className="flex-1 px-5 py-4 flex flex-col lg:flex-row gap-4 items-start">
      <section aria-label="Player" className="flex-1 min-w-0 w-full flex flex-col gap-3 stagger">
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-[13px] text-mut font-bold min-w-0">Reviewing
            <select value={video.id} onChange={(e) => onSelect(e.target.value)} data-testid="review-select"
                    className={`${btn.input} font-bold max-w-[440px] truncate`}>
              {pinnedList.length > 0 && (
                <optgroup label="Pinned">{pinnedList.map((v) => <option key={`p-${v.id}`} value={v.id}>{v.name}</option>)}</optgroup>
              )}
              <optgroup label="Dives">{reviewable.filter((v) => v.sourceType !== 'INCIDENT_CLIP' && !v.pinned).map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}</optgroup>
              <optgroup label="Incident clips · newest first">{reviewable.filter((v) => v.sourceType === 'INCIDENT_CLIP' && !v.pinned).map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}</optgroup>
              {!reviewable.some((v) => v.id === video.id) && <option value={video.id}>{video.name}</option>}
            </select>
          </label>
          <span className="text-[13px] text-tx2 font-semibold">{desc}</span>
          {video.isTest && <TestBadge standIn={video.testProfile?.standIn} />}
          {run && <span data-testid="threshold-indicator" className="text-xs font-bold text-acc-tx bg-acc-bg rounded-full px-2.5 py-1">{runLabel}</span>}
          <button onClick={() => onSelect(null)} data-testid="review-library-back" title={`Close this media and go back to the ${backTo.toLowerCase()}`}
                  className={`${btn.ghost} ml-auto inline-flex items-center gap-1.5 text-[12.5px]`}><FolderOpen className="w-3.5 h-3.5" />{backTo}</button>
        </div>

        <Card pad={false}>
          <div ref={frameRef} className="relative aspect-video bg-[#071419]">
            <video
              key={video.id}
              ref={videoRef}
              src={apiUrl(`/api/videos/stream/${video.id}`)}
              className="absolute inset-0 w-full h-full object-contain cursor-pointer"
              playsInline
              preload="auto"
              data-testid="review-video"
              onClick={togglePlay}
              onPlay={() => setPlaying(true)}
              onPause={() => setPlaying(false)}
              onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
              onSeeked={(e) => setTime(e.currentTarget.currentTime)}
              onLoadedMetadata={(e) => {
                const v = e.currentTarget;
                if (isFinite(v.duration)) setDuration(v.duration);
                setDims({ w: v.videoWidth, h: v.videoHeight });
                if (pendingSeek.current != null) {
                  const t = pendingSeek.current; pendingSeek.current = null;
                  v.currentTime = Math.max(0, Math.min(t + 0.25 / fps, v.duration - 0.001)); setTime(t);
                }
              }}
              onError={() => setLoadError(['h264', 'avc1'].includes((video.codec || '').toLowerCase())
                ? 'This browser cannot decode H.264 (e.g. open-source Chromium builds). Use Chrome, Edge or Firefox.'
                : `The browser cannot decode '${video.codec || 'unknown'}'. Install ffmpeg (or pip install imageio-ffmpeg) and re-process to create an H.264 proxy.`)}
            />
            <div className="absolute inset-0 pointer-events-none">
              <DetectionOverlay detections={[...currentObjects, ...current]} rect={rect} minConf={0} source="DB" />
            </div>
            {loadError && <div className="absolute top-3 left-3 right-3 z-20 text-xs text-def-soft bg-def-bg/95 border border-def-edge rounded-lg px-3 py-2">{loadError}</div>}
            {fullscreen && (
              <div className="absolute bottom-4 left-1/2 -translate-x-1/2 z-30 flex items-center gap-2 bg-ink/85 border border-edge2 rounded-xl px-3 py-2" data-testid="fs-controls">
                <button onClick={togglePlay} aria-label={playing ? 'Pause' : 'Play'} className="w-9 h-9 rounded-full bg-acc text-on-acc grid place-items-center cursor-pointer">
                  {playing ? <Pause className="w-4 h-4" /> : <Play className="w-4 h-4 ml-0.5" />}
                </button>
                <button onClick={() => jump(-1)} disabled={!prevEvent} className={`${btn.secondary} text-[13px]`}><ChevronLeft className="w-4 h-4" /></button>
                <button onClick={() => jump(1)} disabled={!nextEvent} className={`${btn.secondary} text-[13px]`}><ChevronRight className="w-4 h-4" /></button>
                <span className="text-tx2 font-bold text-[13px] px-1">{fmtMs(time)} <span className="text-dim">/ {fmtMs(duration)}</span></span>
                <button onClick={toggleFullscreen} aria-label="Exit full screen" className="text-mut hover:text-tx px-1 cursor-pointer"><Minimize2 className="w-4 h-4" /></button>
              </div>
            )}
          </div>
          <div className="px-4 pt-3 pb-3.5 flex flex-col gap-2.5">
            <div className="flex flex-wrap items-center gap-2.5">
              <button onClick={togglePlay} aria-label={playing ? 'Pause' : 'Play'} data-testid="review-play"
                      className="w-[42px] h-[42px] rounded-full bg-acc text-on-acc grid place-items-center cursor-pointer">
                {playing ? <Pause className="w-4 h-4" /> : <Play className="w-4 h-4 ml-0.5" />}
              </button>
              <button onClick={() => jump(-1)} disabled={!prevEvent} data-testid="prev-defect" title={prevEvent ? '' : 'No earlier defect'}
                      className={`${btn.secondary} text-[13px] flex items-center gap-1`}><ChevronLeft className="w-4 h-4" /> Previous defect</button>
              <button onClick={() => jump(1)} disabled={!nextEvent} data-testid="next-defect" title={nextEvent ? '' : 'No later defect'}
                      className={`${btn.secondary} text-[13px] flex items-center gap-1`}>Next defect <ChevronRight className="w-4 h-4" /></button>
              <button onClick={toggleFullscreen} aria-label="Full screen" title="Full screen (keeps the boxes)" data-testid="review-fullscreen"
                      className="text-dim hover:text-tx cursor-pointer px-1"><Maximize2 className="w-4 h-4" /></button>
              <span className="ml-auto text-tx2 font-bold"><span data-testid="review-time">{fmtMs(time)}</span> <span className="text-dim">/ {fmtMs(duration)}</span></span>
            </div>
            <div className="grid grid-cols-[96px_minmax(0,1fr)] gap-x-2.5 gap-y-1.5 items-center text-xs font-bold">
              <span />
              <div className="relative h-3.5">
                {trigger != null && (
                  <button data-testid="trigger-marker" onClick={() => seekExact(trigger)} title={`Incident trigger at ${trigger.toFixed(3)} s — click to snap`}
                          className="absolute -translate-x-1/2 text-def-tx font-extrabold whitespace-nowrap cursor-pointer hover:text-tx" style={{ left: pct(trigger) }}>
                    ▼ trigger
                  </button>
                )}
              </div>
              <span className="text-mut">Position</span>
              <div data-testid="review-track" onClick={onTrackClick} className="relative h-5 cursor-pointer flex items-center">
                <div className="relative w-full h-2.5 bg-ink rounded-full">
                  <div className="absolute inset-y-0 left-0 bg-acc rounded-full" style={{ width: pct(time) }} />
                  {trigger != null && <div className="absolute -top-1 -bottom-1 w-0.5 bg-def-tx" style={{ left: pct(trigger) }} />}
                  <div className="absolute top-1/2 w-3.5 h-3.5 -mt-[7px] -ml-[7px] rounded-full bg-tx" style={{ left: pct(time) }} />
                </div>
              </div>
              <span className="text-def-tx">Deformation</span>
              <Lane bars={lanes.def} cls="bg-def" time={pct(time)} testid="lane-deformation" duration={duration} onSnap={seekExact} onFree={seekFree} name="deformation" />
              <span className="text-pit-tx">Pitting</span>
              <Lane bars={lanes.pit} cls="bg-pit" time={pct(time)} testid="lane-pitting" duration={duration} onSnap={seekExact} onFree={seekFree} name="pitting" />
              {lanes.obj.length > 0 && <>
                <span className="text-dim" title="General detections from the object log (not defects)">Other objects</span>
                <Lane bars={lanes.obj} cls="bg-dim" time={pct(time)} testid="lane-objects" duration={duration} onSnap={seekExact} onFree={seekFree} name="object" />
              </>}
            </div>
            <p className="text-xs text-dim font-semibold">
              Each lane shows when that defect is in view — click a bar to jump to the start of that sighting.{isClip ? ' In a 5 s incident clip the defect usually stays in shot from the trigger to the end (2 s post-roll), so it fills the lane.' : ''}
            </p>
          </div>
        </Card>

        <Card pad={false}>
          {events.length > 0 ? (
            <div className="max-h-56 overflow-auto" data-testid="event-list">
              {events.map((ev, i) => {
                const activeEv = time >= ev.start - 0.05 && time <= ev.end + OVERLAY_HOLD;
                const def = ev.type === 'DEFORMATION';
                return (
                  <button key={i} onClick={() => seekExact(ev.start)}
                          className={`w-full flex items-center gap-2.5 px-4 py-[11px] text-left cursor-pointer ${i ? 'border-t border-line' : ''} ${activeEv ? 'bg-btn' : 'hover:bg-sel'}`}>
                    <span className={`w-2.5 h-2.5 rounded-full ${def ? 'bg-def' : 'bg-pit'}`} />
                    <b>{def ? 'Deformation' : 'Pitting (grade 6–7)'}</b>
                    <span className="text-mut font-semibold">{ev.severity}</span>
                    <span className="ml-auto text-tx2 font-semibold text-[13px]">
                      {fmtTime(ev.start)}{ev.end - ev.start >= 1 ? `–${fmtTime(ev.end)}` : ''} · peak {Math.round(ev.peak * 100)}% · {ev.n} frames
                    </span>
                  </button>
                );
              })}
            </div>
          ) : null}
          <p className={`text-xs text-dim px-4 py-2.5 ${events.length ? 'border-t border-line' : ''}`} data-testid="review-note">
            {run && run.status !== 'COMPLETED' && run.kind !== 'LIVE' && run.kind !== 'CLIP' ? `Analysis ${run.status.toLowerCase()} (${Math.round(run.progress)}%) — results so far. ` : ''}
            {video.status === 'COMPLETED' && run && events.length === 0 ? `No defects at or above ${run.conf.toFixed(2)} in this analysis. To look for weaker findings, re-analyse at a lower threshold. ` : ''}
            {telemetry.length === 0 ? 'No ROV depth / heading log for this media.'
              : `ROV depth / heading log: ${telemetry.length.toLocaleString()} readings over ${fmtTime(sensorSpan)}${sensorRate ? ` (about ${sensorRate.toFixed(0)} per second)` : ''} from the ${sensorSrc}; the depth, zone and heading at the playhead come from these.`}
            {Object.keys(objCounts).length > 0 && (
              <span data-testid="object-counts"> General detections (not defects): {Object.entries(objCounts).map(([k, n]) => `${k} ${n}`).join(', ')}.</span>
            )}
          </p>
        </Card>
      </section>

      <aside aria-label="Analysis and report" className="w-full lg:w-[380px] xl:w-[400px] shrink-0 flex flex-col gap-3 stagger">
        <AnalysisCard video={video} run={run} onRunsChanged={onRunsChanged} log={log} />
        <Card testid="telemetry-card">
          <div className="flex items-center text-[13px] text-mut font-bold">
            <span>ROV at {fmtMs(time)}</span>
            {telemetry.length > 0 && <span className={`ml-auto text-xs ${telemetry[0]?.source === 'SIM' ? 'text-tst' : 'text-dim'}`}>{telemetry[0]?.source === 'SIM' ? 'recorded · SIM' : `recorded · ${sensorSrc}`}</span>}
          </div>
          {telemetry.length === 0 ? (
            <p className="text-xs text-dim mt-2" data-testid="rov-offline">No ROV telemetry recorded for this media. Attach the dive's .tlog in Batch to add depth and heading.</p>
          ) : !curTel ? (
            <p className="text-xs text-dim mt-2" data-testid="rov-offline">No sensor sample at this moment (gap in the log).</p>
          ) : (
            <div className="grid grid-cols-3 gap-2 mt-2">
              <div><div className="text-[12px] text-dim font-semibold">Depth</div><div className="text-xl font-extrabold" data-testid="tel-depth">{curTel.depth != null ? `${curTel.depth.toFixed(1)} m` : '—'}</div></div>
              <div><div className="text-[12px] text-dim font-semibold">Zone</div><div className="text-base font-extrabold text-acc" data-testid="tel-segment">{segmentFor(line, curTel.depth)?.replace(/\s*Zone$/i, '') || '—'}</div></div>
              <div><div className="text-[12px] text-dim font-semibold">Heading</div><div className="text-xl font-extrabold" data-testid="tel-heading">{curTel.heading != null ? `${Math.round(curTel.heading)}°` : '—'}</div></div>
            </div>
          )}
        </Card>
        {reportPanel}
      </aside>
    </main>
  );
}

/** A defect lane. Clicking a bar (or within SNAP_PX of one) jumps to the start of that sighting;
 *  clicking an empty part of the lane moves the playhead there, like the position bar. */
function Lane({ bars, cls, time, testid, duration, onSnap, onFree, name }: {
  bars: LaneBar[]; cls: string; time: string; testid: string; duration: number;
  onSnap: (t: number) => void; onFree: (t: number) => void; name: string;
}) {
  const click = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!duration) return;
    const r = e.currentTarget.getBoundingClientRect();
    const t = ((e.clientX - r.left) / r.width) * duration;
    const tol = (SNAP_PX / r.width) * duration;
    const hit = bars.find((b) => t >= b.start - tol && t <= b.end + tol);
    if (hit) onSnap(hit.start); else onFree(t);
  };
  return (
    <div className="relative h-4 flex items-center cursor-pointer group/lane" onClick={click} data-testid={testid} data-bars={bars.length}>
      <div className="relative w-full h-2 bg-ink rounded-full">
        {bars.map((b, i) => (
          <div key={i} data-testid={`${testid}-bar`} data-start={b.start.toFixed(3)}
               title={`${name} from ${fmtMs(b.start)}${b.end - b.start >= 0.05 ? ` to ${fmtMs(b.end)}` : ''} — click to jump here`}
               className={`absolute inset-y-0 rounded-full ${cls} hover:brightness-125 hover:-inset-y-0.5 transition-[filter]`}
               style={{ left: `${b.left}%`, width: `max(6px, ${b.width}%)` }} />
        ))}
        <div className="absolute -top-1 -bottom-1 w-px bg-tx/60 pointer-events-none" style={{ left: time }} />
      </div>
    </div>
  );
}
