import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Activity, AlertTriangle, FileText, Pause, Play, Radio, VideoOff, X } from 'lucide-react';
import { apiUrl, fmtTime, postJSON } from '../api';
import { useContainRect } from '../hooks';
import type { LogEntry } from '../eventlog';
import type { Detection, DetectionMode, HistorySummary, LiveSource, LiveState, MooringLine, RovStatus, Telemetry, TestModeStatus, VideoItem } from '../types';
import DigitalTwin from './DigitalTwin';
import { DetectionCard, RovDepthCard } from './Panels';
import { btn, Card, DetectionOverlay, EventLog, Standby, TestBadge, typeName } from './shared';

const STRIP_WINDOW_SEC = 120;

/** One incident of the current dive, for the alert stack. */
export interface AlertItem {
  key: string;
  stage: 'triggered' | 'saved' | 'failed';
  clipId?: string | null;
  type: string;
  confidence: number;
  depth: number | null;
  zone: string | null;
  sessionTime: number | null;
  error?: string;
}

interface Frozen { url: string; detections: Detection[]; t: number }

export default function LiveOps({
  live, rov, telemetry, wsConnected, sources, line, lineId, triggerConf, setTriggerConf, detectionMode, onDetectionMode, onToggleRec,
  logConf, history, testMode, alerts, alertsVisible, onDismissAlerts, onReviewClip, onReport, onOpenDiveLog, log, logs, onOpenSystem, videos = [],
}: {
  live: LiveState | null;
  rov: RovStatus | null;
  telemetry: Telemetry | null;
  wsConnected: boolean;
  sources: LiveSource[];
  line: MooringLine | undefined;
  lineId: string;
  triggerConf: number;
  setTriggerConf: (v: number) => void;
  detectionMode: DetectionMode;
  onDetectionMode: (m: DetectionMode) => void;
  onToggleRec: () => void;
  logConf: number;
  history: HistorySummary | null;
  testMode: TestModeStatus | null;
  alerts: AlertItem[];
  alertsVisible: boolean;
  onDismissAlerts: () => void;
  onReviewClip: (clipId: string, t?: number) => void;
  onOpenDiveLog?: (diveId: string) => void;
  videos?: VideoItem[];
  onReport: (scope: string | null) => void;
  log: (msg: string) => void;
  logs: LogEntry[];
  onOpenSystem: () => void;
}) {
  const status = live?.status ?? 'IDLE';
  const active = status === 'LIVE' || status === 'CONNECTING' || status === 'RECONNECTING';
  const [source, setSource] = useState('');
  const [customUrl, setCustomUrl] = useState('rtsp://192.168.2.2:8554/');
  const [busy, setBusy] = useState(false);
  const [imgKey, setImgKey] = useState(0);
  const [frozen, setFrozen] = useState<Frozen | null>(null);
  const { ref: frameRef, rect } = useContainRect(live?.frameWidth, live?.frameHeight);
  // Test Mode: pre-select the simulated ROV camera; drop it again when Test Mode ends
  const testSrc = sources.find((s) => s.kind === 'test')?.value;
  useEffect(() => {
    if (testMode?.enabled && testSrc && !source) setSource(testSrc);
    if (!testMode?.enabled && source && !sources.some((s) => s.value === source) && source !== '__custom') setSource('');
  }, [testMode?.enabled, testSrc]); // eslint-disable-line react-hooks/exhaustive-deps

  // Rolling sightings strip (read-only): defect sightings of the last 2 min, by type
  const [events, setEvents] = useState<{ t: number; type: string; trig: boolean }[]>([]);
  const sessionRef = useRef<string | null>(null);
  useEffect(() => {
    if (!live) return;
    if (live.sessionId !== sessionRef.current) { sessionRef.current = live.sessionId; setEvents([]); setFrozen(null); }
    const defects = (live.detections || []).filter((d) => d.isDefect);
    if (defects.length && live.t != null) {
      const now = live.t;
      const add = [...new Set(defects.map((d) => d.type))].map((type) => ({ t: now, type, trig: defects.some((d) => d.type === type && d.triggering) }));
      setEvents((prev) => [...prev.filter((e) => e.t > now - STRIP_WINDOW_SEC), ...add].slice(-900));
    }
  }, [live?.frameSeq, live?.sessionId]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (!active && frozen) { URL.revokeObjectURL(frozen.url); setFrozen(null); } }, [active]); // eslint-disable-line

  const connect = async () => {
    const src = source === '__custom' ? customUrl.trim() : source;
    if (!src) return;
    setBusy(true);
    try {
      const label = sources.find((s) => s.value === src)?.label || src;
      await postJSON('/api/live/start', { source: src, label, line_id: lineId, trigger_conf: triggerConf, recording: true, detection_mode: detectionMode });
      log(`LIVE: connecting to ${label} on ${lineId} (clip trigger ≥ ${triggerConf.toFixed(2)}).`);
    } catch (e: any) { log(`LIVE ERROR: ${e.message}`); } finally { setBusy(false); }
  };
  const disconnect = async () => {
    setBusy(true);
    try { await postJSON('/api/live/stop'); log('LIVE: stream disconnected, dive session closed.'); }
    catch (e: any) { log(`LIVE ERROR: ${e.message}`); } finally { setBusy(false); }
  };

  /** Freeze Frame: purely a VIEW feature. Capture, detection, recording and clipping keep running. */
  const freeze = async () => {
    try {
      const blob = await (await fetch(apiUrl(`/api/live/snapshot.jpg?ts=${Date.now()}`))).blob();
      setFrozen({ url: URL.createObjectURL(blob), detections: live?.detections || [], t: live?.t ?? 0 });
      log('User command: frame frozen for inspection (the live pipeline keeps running).');
    } catch (e: any) { log(`ERROR: could not freeze the frame (${e.message})`); }
  };
  const unfreeze = () => { if (frozen) URL.revokeObjectURL(frozen.url); setFrozen(null); };

  const tNow = live?.t ?? 0;
  const windowStart = Math.max(0, tNow - STRIP_WINDOW_SEC);
  // Saved incident clips of this dive, as spans of dive time (~3 s before the trigger to ~2 s after).
  // Alerts arrive at once over the websocket; the video list adds the exact clip length once it refreshes.
  const clipSpans = useMemo(() => {
    const out: { id: string; start: number; end: number; type: string }[] = [];
    for (const a of alerts) {
      if (!a.clipId || a.sessionTime == null) continue;
      const v = videos.find((x) => x.id === a.clipId);
      const pre = v?.triggerOffsetSec ?? 3, len = v?.durationSec ?? 5;
      out.push({ id: a.clipId, start: a.sessionTime - pre, end: a.sessionTime - pre + len, type: a.type });
    }
    return out;
  }, [alerts, videos]);
  const [stripNote, setStripNote] = useState<string | null>(null);
  const onStripClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const t = windowStart + ((e.clientX - r.left) / r.width) * STRIP_WINDOW_SEC;
    const tol = (6 / r.width) * STRIP_WINDOW_SEC;
    const hit = clipSpans.find((c) => t >= c.start - tol && t <= c.end + tol);
    if (hit) {
      const at = Math.max(0, Math.min(t - hit.start, hit.end - hit.start - 0.05));
      log(`REVIEW: opened the clip of the sighting at T+${fmtTime(t)} from the live strip.`);
      onReviewClip(hit.id, at);
    } else if (events.some((ev) => Math.abs(ev.t - t) <= tol + 0.5)) {
      setStripNote(`T+${fmtTime(t)}: logged sighting below the clip trigger (≥ ${(live?.triggerConf ?? triggerConf).toFixed(2)}) — no clip was saved. It is in the dive log.`);
    } else setStripNote(null);
  };
  useEffect(() => { if (!stripNote) return; const id = setTimeout(() => setStripNote(null), 6000); return () => clearTimeout(id); }, [stripNote]);
  const isTestFeed = !!live?.isTest || (!active && !!testMode?.enabled);
  const armDepth = live?.autoArmDepth ?? 2;
  const waitText = live?.armWait === 'NO_DEPTH' ? 'Detection waiting for ROV depth'
    : live?.armWait === 'SHALLOW' ? `Detection waiting · ROV above ${armDepth} m` : 'Detection standby';
  const sourceLabel = useMemo(() => {
    const s = live?.source || '';
    return s.length > 42 ? `${s.slice(0, 20)}…${s.slice(-14)}` : s;
  }, [live?.source]);

  return (
    <main className="flex-1 px-5 py-4 flex flex-col lg:flex-row gap-4 items-start">
      {/* ================= left: feed + log ================= */}
      <section aria-label="Live video" className="flex-1 min-w-0 w-full flex flex-col gap-3 stagger">
        <Card pad={false}>
          <div className="flex flex-wrap items-center gap-3 px-4 py-[11px]">
            {active ? (
              <>
                <span className={`flex items-center gap-1.5 text-xs font-extrabold px-2.5 py-1 rounded-[7px] ${status === 'LIVE' ? 'bg-[#3A1513] text-def-tx' : 'bg-[#33270C] text-warn'}`}
                      data-testid="live-status">
                  <span className={`w-[7px] h-[7px] rounded-full ${status === 'LIVE' ? 'bg-def' : 'bg-warn'} animate-live`} />
                  {status === 'LIVE' ? 'LIVE' : status === 'CONNECTING' ? 'CONNECTING' : 'RECONNECTING'}
                </span>
                {live?.isTest && <TestBadge standIn={live.testProfile?.standIn} />}
                <span className="font-bold text-[15px]">{live?.isTest ? 'Simulated ROV camera' : 'BlueROV2 camera'}</span>
                <span className="text-dim font-mono text-xs truncate max-w-[280px]" title={live?.source || ''}>{sourceLabel}</span>
                <span className="ml-auto flex gap-3.5 text-[13px] font-semibold text-tx2">
                  <span>T+{fmtTime(tNow)}</span>
                  <span className="text-dim">{live?.captureFps.toFixed(0)} fps</span>
                  <span className="text-dim" title={`${live?.inferenceMs ?? 0} ms per frame · buffer ${live?.bufferFrames}/${live?.bufferCapacity ?? 150} frames`}>
                    {live?.armed ? `YOLO ${live.inferenceFps.toFixed(0)}/s` : 'YOLO idle'}
                  </span>
                  {!live?.recording && <span className="text-dim" title="Dive log off; incident clips are still saved">log off</span>}
                </span>
                <button onClick={disconnect} disabled={busy} className={`${btn.secondary} text-[13px]`}>Disconnect</button>
              </>
            ) : (
              <>
                <span className="font-bold text-[15px]">{testMode?.enabled ? 'Simulated ROV camera' : 'BlueROV2 camera'}</span>
                {testMode?.enabled && <TestBadge standIn={testMode.standIn} />}
                <select value={source} onChange={(e) => setSource(e.target.value)} data-testid="live-source" aria-label="Video source"
                        className={`${btn.input} flex-1 min-w-[200px] text-[13px]`}>
                  <option value="">Select the ROV video source…</option>
                  <option value="__custom">RTSP / network URL…</option>
                  {sources.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
                </select>
                {(source === '__custom' || source.startsWith('rtsp://192.168.2.2')) && (
                  <input value={source === '__custom' ? customUrl : source} onChange={(e) => { setSource('__custom'); setCustomUrl(e.target.value); }}
                         placeholder="rtsp://192.168.2.2:8554/video_stream__dev_video2" aria-label="Stream URL"
                         className={`${btn.input} flex-1 min-w-[240px] font-mono text-xs`} />
                )}
                <button onClick={connect} disabled={busy || !source || (source === '__custom' && !customUrl.trim())} data-testid="connect"
                        className={`${btn.primary} !py-2 flex items-center gap-2 text-[13px]`}>
                  <Radio className="w-4 h-4" /> Connect
                </button>
              </>
            )}
          </div>
          {live?.error && (status === 'ERROR' || status === 'RECONNECTING') && (
            <p className="mx-4 mb-2 text-xs font-mono text-warn bg-[#33270C] rounded-lg px-3 py-2">{live.error}</p>
          )}
          {telemetry?.leak && (
            <div data-testid="leak-alarm" className="mx-4 mb-2 text-sm font-extrabold text-on-def bg-def rounded-lg px-4 py-2 animate-live">
              ROV leak detected — follow the recovery procedure
            </div>
          )}

          {/* video: no playback controls, ever */}
          <div ref={frameRef} className="relative w-full aspect-video bg-[#071419] overflow-hidden">
            {status === 'LIVE' || (status === 'RECONNECTING' && live?.frameSeq) ? (
              <>
                {frozen ? (
                  <img src={frozen.url} alt="Frozen frame" className="absolute inset-0 w-full h-full object-contain" data-testid="frozen-frame" />
                ) : (
                  <img key={`${live?.sessionId}-${imgKey}`} src={apiUrl(`/api/live/mjpeg?s=${live?.sessionId}&k=${imgKey}`)}
                       alt="Live ROV feed" draggable={false} onError={() => setTimeout(() => setImgKey((k) => k + 1), 1000)}
                       className="absolute inset-0 w-full h-full object-contain select-none" />
                )}
                <DetectionOverlay detections={frozen ? frozen.detections : live?.detections || []} rect={rect} minConf={0} live />
                <div className="absolute top-3 left-3 z-20 flex flex-wrap items-center gap-2 pointer-events-none">
                  {frozen && <span className="bg-[#0F3A5A] text-[#BFE3FF] text-xs font-extrabold px-2.5 py-1 rounded-md">Frozen · T+{fmtTime(frozen.t)}</span>}
                  {!live?.armed && <span data-testid="standby-badge" className="bg-[#33270C]/95 text-warn text-xs font-bold px-2.5 py-1 rounded-md">{waitText}</span>}
                  {live?.engineStatus !== 'ONLINE' && <span className="bg-[#33270C]/95 text-warn text-xs font-bold px-2.5 py-1 rounded-md">No YOLO weights loaded</span>}
                </div>
                <div className="absolute bottom-3.5 left-3.5 z-20">
                  {frozen ? (
                    <button onClick={unfreeze} data-testid="unfreeze" className={`${btn.primary} !py-2 text-[13px] flex items-center gap-1.5`}>
                      <Play className="w-3.5 h-3.5" /> Back to live (+{Math.max(0, tNow - frozen.t).toFixed(0)} s)
                    </button>
                  ) : (
                    <button onClick={freeze} data-testid="freeze" title="Freeze your view to inspect a frame — capture, detection and recording keep running"
                            className="bg-ink/85 text-tx border border-edge2 rounded-[9px] px-3 py-2 text-[13px] font-semibold flex items-center gap-1.5 cursor-pointer">
                      <Pause className="w-3.5 h-3.5" /> Freeze frame
                    </button>
                  )}
                </div>
                <div className="absolute bottom-3.5 right-3.5 z-20 hidden sm:flex gap-3 bg-ink/85 border border-edge2 rounded-[9px] px-3 py-[7px] text-xs text-tx2 font-semibold">
                  <span><span className="inline-block w-3.5 border-t-[2.5px] border-def align-middle mr-1.5" />triggers a clip (≥ {(live?.triggerConf ?? triggerConf).toFixed(2)})</span>
                  <span><span className="inline-block w-3.5 border-t-2 border-dashed border-pit align-middle mr-1.5" />logged</span>
                </div>
              </>
            ) : status === 'CONNECTING' || status === 'RECONNECTING' ? (
              <Standby icon={<Radio className="w-6 h-6 animate-live" />} title="Acquiring signal"
                       subtitle={`Opening ${live?.source}. RTSP streams from BlueOS can take a few seconds to negotiate.`} />
            ) : (
              <Standby icon={<VideoOff className="w-6 h-6" />} title={status === 'ERROR' ? 'Stream offline' : 'No signal · standby'}
                       subtitle={!wsConnected ? 'The backend telemetry channel is offline. Start the Python backend on port 8500.'
                         : testMode?.enabled ? 'Test Mode is on: the simulated ROV camera is selected above. Press Connect.'
                         : 'Select the BlueROV2 camera stream above and press Connect.'}>
                {wsConnected && !testMode?.enabled && (
                  <button onClick={onOpenSystem} className={`${btn.secondary} text-[13px]`} data-testid="open-test-mode">No vehicle yet? Open Test Mode</button>
                )}
              </Standby>
            )}
          </div>

          <div className="px-4 pt-3 pb-3.5" data-testid="live-strip">
            <div className="flex justify-between text-xs text-dim font-semibold mb-2">
              <span>Defect sightings · last 2 min{clipSpans.some((c) => c.end >= windowStart) ? ' · click a framed sighting to review its clip' : ''}</span>
              <span className="flex items-center gap-3">
                {active ? `T+${fmtTime(windowStart)} → T+${fmtTime(tNow)}` : '—'}
                {live?.sessionId && onOpenDiveLog && (
                  <button onClick={() => onOpenDiveLog(live.sessionId!)} data-testid="open-dive-log" title="This dive's log so far: depth over time, sightings, clips, events, downloads"
                          className="inline-flex items-center gap-1 text-acc-tx hover:underline cursor-pointer"><Activity className="w-3.5 h-3.5" />Dive log</button>
                )}
              </span>
            </div>
            <div className={`relative h-6 bg-ink rounded-md overflow-hidden ${active ? 'cursor-pointer' : ''}`} onClick={active ? onStripClick : undefined}>
              {active && clipSpans.filter((c) => c.end >= windowStart).map((c) => {
                const left = Math.max(0, (c.start - windowStart) / STRIP_WINDOW_SEC) * 100;
                const right = Math.min(1, (c.end - windowStart) / STRIP_WINDOW_SEC) * 100;
                return (
                  <div key={c.id} data-testid="strip-clip" data-clip={c.id} title={`Incident clip · T+${fmtTime(c.start)}–T+${fmtTime(c.end)} — click to review`}
                       className="absolute inset-y-0 rounded-[4px] border border-def/80 bg-def/15 hover:bg-def/30 transition-colors"
                       style={{ left: `${left}%`, width: `max(10px, ${right - left}%)` }} />
                );
              })}
              {active && events.filter((e) => e.t >= windowStart).map((e, i) => {
                const def = e.type === 'DEFORMATION';
                return (
                  <div key={i} className={`absolute rounded-sm pointer-events-none ${def ? 'bg-def' : 'bg-pit'} ${e.trig ? 'top-1 bottom-1 w-[5px]' : 'top-1.5 bottom-1.5 w-[3px]'}`}
                       style={{ left: `${((e.t - windowStart) / STRIP_WINDOW_SEC) * 100}%` }} />
                );
              })}
            </div>
            {stripNote && <p className="text-xs text-mut mt-1.5" data-testid="strip-note">{stripNote}</p>}
          </div>
        </Card>

        <EventLog entries={logs} height={160} />
      </section>

      {/* ================= right: alerts, ROV, twin, detection ================= */}
      <aside aria-label="Dive status" className="w-full lg:w-[380px] xl:w-[400px] shrink-0 flex flex-col gap-3 stagger">
        {alertsVisible && alerts.length > 0 && (
          <AlertStack alerts={alerts} onReview={onReviewClip} onReport={(id) => onReport(`INCIDENT:${id}`)} onDismiss={onDismissAlerts} />
        )}
        <RovDepthCard telemetry={telemetry} rov={rov} seabed={line?.seabed_depth} />
        <Card className="!px-3.5 !py-3">
          <div className="flex items-center gap-2 text-[13px] text-mut font-bold mb-2">
            <span>Mooring line</span>
            <span className="text-xs text-dim font-semibold">· defects per zone, all {testMode?.enabled ? 'TEST ' : ''}dives</span>
            <button onClick={() => onReport(null)} data-testid="live-report" className="ml-auto text-xs font-bold text-acc-tx flex items-center gap-1 cursor-pointer">
              <FileText className="w-3.5 h-3.5" /> Report…
            </button>
          </div>
          <DigitalTwin line={line} telemetry={telemetry} history={history} />
        </Card>
        <DetectionCard live={live} active={active} detectionMode={detectionMode} onDetectionMode={onDetectionMode}
                       triggerConf={triggerConf} setTriggerConf={setTriggerConf} onToggleRec={onToggleRec} logConf={logConf}
                       depth={telemetry?.depth} />
        {isTestFeed && !active && <p className="text-xs text-tst">Test Mode on — this dive will be tagged TEST.</p>}
      </aside>
    </main>
  );
}

/* ------------------------------------------------------------------------- alert stack */
// The card itself is never re-mounted when a new incident arrives (that used to reset the list's
// scroll position every few seconds during long detections); only the headline animates.
function AlertStack({ alerts, onReview, onReport, onDismiss }: {
  alerts: AlertItem[]; onReview: (id: string) => void; onReport: (id: string) => void; onDismiss: () => void;
}) {
  const [top, ...earlier] = alerts;
  const [showAll, setShowAll] = useState(false);
  const where = (a: AlertItem) => (a.depth != null ? `${a.depth.toFixed(1)} m` : 'no ROV depth');
  const saved = alerts.filter((a) => a.stage === 'saved').length;
  return (
    <div role="alert" data-testid="incident-alert" data-stage={top.stage}
         className="bg-def-bg border border-def-edge rounded-2xl px-4 py-3.5 flex flex-col gap-2.5">
      <div key={top.key} className="flex flex-col gap-2.5 animate-[slide-in_.35s_ease-out_both]">
        <div className="flex items-center gap-2 font-extrabold text-def-soft">
          <AlertTriangle className="w-[18px] h-[18px]" />
          {top.stage === 'triggered' ? 'Clipping incident…' : top.stage === 'failed' ? 'Incident clip failed' : 'Deformation clipped'}
          {saved > 1 && <span className="ml-auto bg-def text-on-def rounded-full text-[12px] font-extrabold px-2 py-0.5" data-testid="incident-count">{saved} this dive</span>}
        </div>
        <div className="text-[15px] leading-snug">
          {typeName(top.type)} <b>{Math.round(top.confidence * 100)}%</b> at <b>{where(top)}</b>
          {top.zone ? `, ${top.zone.replace(/\s*Zone$/i, '')}` : ''}{top.sessionTime != null ? ` · T+${fmtTime(top.sessionTime)}` : ''}
        </div>
        {top.stage === 'triggered' && <div className="text-xs text-def-soft/80">Encoding the 5 s buffer in the background; the stream is unaffected.</div>}
        {top.stage === 'failed' && <div className="text-xs text-def-soft/80">{top.error || 'Encoder error.'}</div>}
        <div className="flex gap-2">
          <button onClick={() => top.clipId && onReview(top.clipId)} disabled={!top.clipId} data-testid="review-incident"
                  className="flex-1 bg-def text-on-def font-extrabold py-2.5 rounded-[10px] cursor-pointer disabled:opacity-50 disabled:cursor-wait">
            Review incident
          </button>
          <button onClick={() => top.clipId && onReport(top.clipId)} disabled={!top.clipId} data-testid="report-incident"
                  className="text-def-soft border border-def-edge rounded-[10px] px-3 font-bold cursor-pointer disabled:opacity-50">Report</button>
          <button onClick={onDismiss} aria-label="Dismiss" data-testid="dismiss-incident"
                  className="w-10 text-def-soft border border-def-edge rounded-[10px] grid place-items-center cursor-pointer"><X className="w-4 h-4" /></button>
        </div>
      </div>
      {earlier.length > 0 && (
        <div className="border-t border-[#4A1E1B] pt-2 flex flex-col gap-1">
          <div className="flex items-center text-xs font-bold text-[#C98C86]">
            <span>Earlier this dive · {earlier.length}</span>
            {earlier.length > 3 && (
              <button onClick={() => setShowAll(true)} data-testid="incident-all" className="ml-auto text-def-tx underline cursor-pointer">
                Show all {alerts.length}
              </button>
            )}
          </div>
          <div className="flex flex-col max-h-[172px] overflow-y-auto overscroll-contain scroll-alert pr-1" data-testid="incident-earlier">
            {earlier.map((a) => <IncidentRow key={a.key} a={a} onReview={onReview} onReport={onReport} />)}
          </div>
        </div>
      )}
      {showAll && <IncidentsDialog alerts={alerts} onClose={() => setShowAll(false)} onReview={(id) => { setShowAll(false); onReview(id); }}
                                   onReport={(id) => { setShowAll(false); onReport(id); }} />}
    </div>
  );
}

function IncidentRow({ a, onReview, onReport }: { a: AlertItem; onReview: (id: string) => void; onReport: (id: string) => void }) {
  return (
    <div className="flex items-center gap-2 py-1 text-[13px] font-semibold text-[#F2C7C2]">
      <span className="text-[#C98C86] shrink-0 w-[58px]">{a.sessionTime != null ? `T+${fmtTime(a.sessionTime)}` : '—'}</span>
      <span className="truncate">{typeName(a.type)} {Math.round(a.confidence * 100)}% · {a.depth != null ? `${a.depth.toFixed(1)} m` : 'no depth'}</span>
      {a.clipId && <>
        <button onClick={() => onReview(a.clipId!)} className="ml-auto text-def-tx border border-[#5A2420] rounded-[7px] px-2 py-0.5 cursor-pointer">Review</button>
        <button onClick={() => onReport(a.clipId!)} className="text-def-soft border border-[#5A2420] rounded-[7px] px-2 py-0.5 cursor-pointer">Report</button>
      </>}
    </div>
  );
}

/** Every incident of this dive in one scrollable list (opened from the alert card). */
function IncidentsDialog({ alerts, onClose, onReview, onReport }: {
  alerts: AlertItem[]; onClose: () => void; onReview: (id: string) => void; onReport: (id: string) => void;
}) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [onClose]);
  return createPortal(
    <div className="fixed inset-0 z-40 bg-black/60 flex items-start justify-center p-4 md:pt-20" onClick={onClose}>
      <div className="w-full max-w-xl bg-card border border-def-edge rounded-2xl flex flex-col max-h-[80vh] animate-rise" onClick={(e) => e.stopPropagation()}
           role="dialog" aria-label="Incidents this dive" data-testid="incidents-dialog">
        <div className="flex items-center px-4 py-3 border-b border-line">
          <span className="font-extrabold">Incidents this dive · {alerts.length}</span>
          <span className="ml-3 text-xs text-dim">newest first · the live stream keeps running</span>
          <button onClick={onClose} aria-label="Close" className="ml-auto text-mut hover:text-tx cursor-pointer"><X className="w-5 h-5" /></button>
        </div>
        <div className="overflow-y-auto px-4 py-2">
          <table className="w-full text-[13px]">
            <thead><tr className="text-left text-xs text-dim"><th className="py-1.5 font-medium">Time</th><th className="font-medium">Defect</th>
              <th className="font-medium">Depth</th><th className="font-medium">Zone</th><th /></tr></thead>
            <tbody>
              {alerts.map((a) => (
                <tr key={a.key} className="border-t border-line">
                  <td className="py-2 text-mut">{a.sessionTime != null ? `T+${fmtTime(a.sessionTime)}` : '—'}</td>
                  <td className="font-semibold"><span className={a.type === 'DEFORMATION' ? 'text-def-tx' : 'text-pit-tx'}>{typeName(a.type)}</span> {Math.round(a.confidence * 100)}%</td>
                  <td>{a.depth != null ? `${a.depth.toFixed(1)} m` : '—'}</td>
                  <td className="text-tx2">{a.zone?.replace(/\s*Zone$/i, '') || '—'}</td>
                  <td className="text-right whitespace-nowrap">
                    {a.clipId ? <>
                      <button onClick={() => onReview(a.clipId!)} className="bg-def text-on-def font-bold rounded-lg px-2.5 py-1 mr-1.5 cursor-pointer">Review</button>
                      <button onClick={() => onReport(a.clipId!)} className={`${btn.ghost} !py-1 text-[13px]`}>Report</button>
                    </> : <span className="text-xs text-dim">{a.stage === 'failed' ? 'clip failed' : 'saving…'}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>,
    document.body,
  );
}
