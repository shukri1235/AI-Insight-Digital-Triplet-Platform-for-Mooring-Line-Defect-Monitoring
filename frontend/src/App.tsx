import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { getJSON, postJSON } from './api';
import { makeEntry, persist, type LogEntry, type Tone } from './eventlog';
import LogTrailCard from './components/LogTrail';
import { useInterval, useLiveSocket } from './hooks';
import type {
  DetectionMode, HistorySummary, IncidentEvent, LiveSource, LiveState, ModelStatus, MooringLine, RovStatus,
  SystemStats, Tab, Telemetry, VideoItem,
} from './types';
import { segmentFor } from './util';
import LiveOps, { type AlertItem } from './components/LiveOps';
import BatchIngest, { fmtSpent } from './components/BatchIngest';
import ReviewPipeline from './components/ReviewPipeline';
import DiveLog from './components/DiveLog';
import { ALL_ZONES, GeminiCard, GpuCard, IOU_DEFAULT, LIVE_TRIGGER_DEFAULT, ModelCard, RegistryCard, ResetCard } from './components/Panels';
import { ReportDialog, ReportPanel } from './components/Reports';
import ReportsDirectory from './components/ReportsDirectory';
import { ReportTemplatesCard, SettingsFileCard } from './components/ReportTemplates';
import { TestModeCard } from './components/TestMode';
import { Header, SystemDrawer } from './components/Shell';
import { EventLog, typeName } from './components/shared';

export default function App() {
  // ---------------------------------------------------------------- core state
  const [activeTab, setActiveTab] = useState<Tab>('live');
  const [lines, setLines] = useState<MooringLine[]>([]);
  const [lineId, setLineId] = useState('ML-01-NORTH');
  // Registry / report depth-zone filter: ALL_ZONES or one segment (Batch registry click)
  const [segment, setSegment] = useState<string>(ALL_ZONES);
  // Live-only controls. Batch thresholds are chosen per dive; Review only displays the recorded ones.
  const [triggerConf, setTriggerConf] = useState(LIVE_TRIGGER_DEFAULT);
  const [liveIou, setLiveIou] = useState(IOU_DEFAULT);
  const [detectionMode, setDetectionMode] = useState<DetectionMode>('AUTO');
  const liveConfigReady = useRef(false);
  const [logs, setLogs] = useState<LogEntry[]>(() => [makeEntry('SYSTEM: dashboard loaded, connecting to the backend.')]);
  useEffect(() => { persist(logs[logs.length - 1]); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const log = useCallback((msg: string, kind?: string, tone?: Tone) => {
    const e = makeEntry(msg, kind, tone);
    persist(e);                                              // paper trail: logs/session_<date>_<time>.txt
    setLogs((p) => [e, ...p.slice(0, 199)]);
  }, []);

  const [apiOnline, setApiOnline] = useState<boolean | null>(null);
  const [model, setModel] = useState<ModelStatus | null>(null);
  const [modelFiles, setModelFiles] = useState<string[]>([]);
  const [loadingModel, setLoadingModel] = useState(false);
  const [videos, setVideos] = useState<VideoItem[]>([]);
  const [history, setHistory] = useState<HistorySummary | null>(null);
  const [stats, setStats] = useState<SystemStats | null>(null);
  const [sources, setSources] = useState<LiveSource[]>([]);
  const [systemOpen, setSystemOpen] = useState(false);
  const [reportDialog, setReportDialog] = useState<{ tab: Tab; scope: string | null; label?: string } | null>(null);

  // Review state is fully separate from Live state. Nothing is auto-selected on boot.
  const [reviewId, setReviewId] = useState<string | null>(null);
  const [seekRequest, setSeekRequest] = useState<{ t: number; nonce: number } | null>(null);
  // Live dive log open in the Review tab (a clip opened from it returns to it)
  const [diveLogId, setDiveLogId] = useState<string | null>(null);

  // ---------------------------------------------------------------- data fetchers
  const fetchVideos = useCallback(() =>
    getJSON<VideoItem[]>('/api/videos').then((v) => setVideos(Array.isArray(v) ? v : [])).catch(() => {}), []);
  const [testOn, setTestOn] = useState(false);
  // Real inspections and Test Mode data are never counted together
  const fetchHistory = useCallback(() =>
    getJSON<HistorySummary>(`/api/history?line_id=${encodeURIComponent(lineId)}&test=${testOn ? 1 : 0}`).then(setHistory).catch(() => {}), [lineId, testOn]);
  useEffect(() => { fetchHistory(); }, [fetchHistory]);
  const fetchModel = useCallback(() => {
    getJSON<ModelStatus>('/api/model/status').then(setModel).catch(() => {});
    getJSON<{ files: string[] }>('/api/model/files').then((d) => setModelFiles(d.files || [])).catch(() => {});
  }, []);
  const apiWas = useRef<boolean | null>(null);
  const fetchStats = useCallback(() =>
    getJSON<SystemStats>('/api/system/stats')
      .then((s) => {
        setStats(s); setApiOnline(true);
        if (apiWas.current !== true) log('SYSTEM: backend online (FastAPI :8500).', 'System', 'ok');
        apiWas.current = true;
      })
      .catch(() => {
        setApiOnline(false);
        if (apiWas.current !== false) log('SYSTEM: backend unreachable on :8500.', 'System', 'crit');
        apiWas.current = false;
      }), [log]);
  const fetchSources = useCallback(() =>
    getJSON<LiveSource[]>('/api/live/sources').then(setSources).catch(() => {}), []);

  useEffect(() => {
    getJSON<MooringLine[]>('/api/assets').then((a) => {
      setLines(a);
      if (a[0]) { setLineId(a[0].id); setSegment(ALL_ZONES); }
    }).catch(() => {});
    // The live worker owns its settings (they survive a page refresh); adopt them before pushing any.
    getJSON<LiveState>('/api/live/state').then((s) => {
      if (s.triggerConf != null) setTriggerConf(s.triggerConf);
      if (s.iou != null) setLiveIou(s.iou);
      if (s.detectionMode) setDetectionMode(s.detectionMode);
    }).catch(() => {}).finally(() => { setTimeout(() => { liveConfigReady.current = true; }, 0); });
    fetchStats(); fetchModel(); fetchVideos(); fetchSources();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const testMode = stats?.testMode ?? null;
  useEffect(() => {
    const on = !!testMode?.enabled;
    if (on !== testOn) { setTestOn(on); fetchSources(); }
  }, [testMode?.enabled]); // eslint-disable-line react-hooks/exhaustive-deps
  const batchBusy = videos.some((v) => ['HASHING', 'QUEUED', 'PROCESSING'].includes(v.status) || !!v.pendingRun);
  useInterval(fetchStats, 2000);
  useInterval(fetchHistory, 4000);
  useInterval(fetchVideos, batchBusy || activeTab === 'batch' ? 2000 : 8000);
  useInterval(fetchModel, model?.is_loaded ? 15000 : 3000);
  useEffect(() => { if (activeTab === 'live') fetchSources(); }, [activeTab, videos.length, fetchSources]);

  // Log batch state transitions (HASHING -> QUEUED -> PROCESSING -> COMPLETED/FAILED)
  const prevStatus = useRef<Record<string, string>>({});
  useEffect(() => {
    videos.forEach((v) => {
      const prev = prevStatus.current[v.id];
      if (prev && prev !== v.status && v.sourceType !== 'INCIDENT_CLIP') {
        const ev = v.defectEvents;
        const tone: Tone = v.status === 'COMPLETED' ? 'ok' : v.status === 'FAILED' ? 'warn' : 'info';
        const tm = v.activeRun?.timing;
        log(`"${v.name}" ${v.status === 'COMPLETED' ? `completed${tm ? ` in ${fmtSpent(tm.processingSec)} (${tm.speedX?.toFixed(1)}× real time)` : ''}${ev ? ` · ${ev.DEFORMATION} deformation, ${ev.CORROSION} pitting` : ''}`
          : v.status.toLowerCase()}${v.error ? ` — ${v.error}` : ''}`, v.status === 'COMPLETED' ? 'Done' : 'Batch', tone);
        if (v.status === 'COMPLETED') fetchHistory();
      }
      prevStatus.current[v.id] = v.status;
    });
  }, [videos, log]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---------------------------------------------------------------- live channel + incidents
  const [incidentEvents, setIncidentEvents] = useState<IncidentEvent[]>([]);
  const [bannerClip, setBannerClip] = useState<string | null>(null);
  const onIncident = useCallback((ev: IncidentEvent) => {
    setIncidentEvents((p) => [...p.filter((x) => x.seq !== ev.seq), ev].slice(-50));
    const depth = ev.telemetry?.depth != null ? `${ev.telemetry.depth.toFixed(1)} m` : 'unknown depth (no ROV)';
    const what = `${typeName(ev.defect?.type || '')} ${Math.round((ev.defect?.confidence || 0) * 100)}%`;
    if (ev.stage === 'triggered') log(`CLIP: ${what} ≥ trigger ${ev.triggerConf?.toFixed(2)} at ${depth} — saving the 5 s buffer.`);
    if (ev.stage === 'saved') { log(`CLIP: incident clip saved · ${what} at ${depth}`); setBannerClip(ev.clipId || null); fetchVideos(); }
    if (ev.stage === 'failed') log(`INCIDENT ERROR: clip failed — ${ev.error}`);
  }, [log, fetchVideos]);
  const { state: live, rovIdle, connected: wsConnected } = useLiveSocket(onIncident);
  const liveActive = live?.status === 'LIVE' || live?.status === 'CONNECTING' || live?.status === 'RECONNECTING';

  // A new dive starts with a clean alert slate (an old session's alert must not look current)
  const [dismissedAt, setDismissedAt] = useState(0);
  const prevSession = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (live?.sessionId && prevSession.current !== undefined && live.sessionId !== prevSession.current) {
      setIncidentEvents([]); setDismissedAt(0); setBannerClip(null);
    }
    prevSession.current = live?.sessionId ?? prevSession.current;
  }, [live?.sessionId]);
  const prevLive = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (live?.status && live.status !== prevLive.current) {
      if (prevLive.current !== undefined || live.status !== 'IDLE')
        log(`${live.status === 'ERROR' ? 'LIVE ERROR' : 'LIVE'}: stream ${live.status.toLowerCase()}${live.error ? ` — ${live.error}` : ''}`);
      prevLive.current = live.status;
    }
  }, [live?.status, live?.error, log]);

  // ROV link: from the live snapshot while streaming, from the 2 Hz idle push otherwise
  const rov: RovStatus | null = liveActive && live?.rov ? live.rov : rovIdle;
  const prevRov = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (rov?.state && rov.state !== prevRov.current) {
      if (prevRov.current !== undefined || rov.state === 'CONNECTED') {
        if (rov.state === 'CONNECTED') log(`BlueROV2 heartbeat${rov.simulated ? ' (simulator)' : ''} · telemetry online`, 'Link', 'ok');
        else if (rov.state === 'STALE') log('Telemetry stale (no messages for 3 s)', 'Link', 'warn');
        else if (rov.state === 'NO_ROV') log('ROV link lost · telemetry offline', 'Link', 'warn');
        else log(`ROV link ${rov.state}`, 'Link', 'warn');
      }
      prevRov.current = rov.state;
    }
  }, [rov?.state, log]); // eslint-disable-line react-hooks/exhaustive-deps

  // Push LIVE control changes to the worker (debounced). Nothing else ever changes them.
  const prevTrigger = useRef(triggerConf);
  useEffect(() => {
    if (!liveConfigReady.current) { prevTrigger.current = triggerConf; return; }
    const id = setTimeout(() => {
      postJSON('/api/live/config', { trigger_conf: triggerConf, iou: liveIou }).catch(() => {});
      if (Math.abs(prevTrigger.current - triggerConf) > 1e-6) log(`CONFIG: clip trigger changed ${prevTrigger.current.toFixed(2)} → ${triggerConf.toFixed(2)}`);
      prevTrigger.current = triggerConf;
    }, 400);
    return () => clearTimeout(id);
  }, [triggerConf, liveIou]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (liveActive && live?.detectionMode) setDetectionMode(live.detectionMode); }, [live?.detectionMode]); // eslint-disable-line
  const changeDetectionMode = (m: DetectionMode) => postJSON('/api/live/detection-mode', { mode: m })
    .then(() => { setDetectionMode(m); log(`CONFIG: detection ${m.toLowerCase()}${m === 'STANDBY' ? ' (no inference, no clips)' : m === 'AUTO' ? ` (arms below ${live?.autoArmDepth ?? 2} m)` : ''}`); })
    .catch((e) => log(`ERROR: ${e.message}`));
  const changeLine = (id: string) => {
    setLineId(id);
    setSegment(ALL_ZONES);
    postJSON('/api/live/config', { line_id: id }).catch(() => {});
    log(`CONFIG: mooring line ${id}`);
  };

  // ---------------------------------------------------------------- derived view state
  const reviewVideo = videos.find((v) => v.id === reviewId) || null;
  const line = lines.find((l) => l.id === lineId);
  const liveTelemetry: Telemetry | null = useMemo(() => {
    if (liveActive) return live?.telemetry || null;
    const t = rovIdle?.telemetry;
    if (!t) return null;
    return { ...t, lineId, seabedDepth: line?.seabed_depth, segment: segmentFor(line, t.depth) };
  }, [liveActive, live?.telemetry, rovIdle?.telemetry, line, lineId]);

  // Log zone crossings during a live dive (the registry filter stays where the operator put it)
  useEffect(() => {
    if (liveTelemetry?.segment && liveActive) log(`ZONE: ROV crossed into ${liveTelemetry.segment}`);
  }, [liveTelemetry?.segment]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (live?.lineId && liveActive && live.lineId !== lineId) setLineId(live.lineId); }, [live?.lineId]); // eslint-disable-line

  // This dive's incidents: saved clips from the database + in-flight events from the WebSocket
  const alerts = useMemo<AlertItem[]>(() => {
    const sid = live?.sessionId;
    if (!sid) return [];
    const lineOf = lines.find((l) => l.id === (live?.lineId || lineId));
    const items: AlertItem[] = videos.filter((v) => v.sourceType === 'INCIDENT_CLIP' && v.parentId === sid).map((v) => ({
      key: v.id, stage: 'saved', clipId: v.id, type: v.incident?.type || 'DEFORMATION', confidence: v.incident?.confidence ?? 0,
      depth: v.incident?.depth ?? null, zone: segmentFor(lineOf, v.incident?.depth), sessionTime: v.incident?.sessionTime ?? null,
    }));
    const known = new Set(items.map((i) => i.clipId));
    // one incident = one trigger frame; keep only its latest stage (triggered -> saved / failed)
    const latest = new Map<string, IncidentEvent>();
    for (const e of incidentEvents) {
      const k = String(e.triggerSeq ?? e.sessionTime);
      const prev = latest.get(k);
      if (!prev || prev.stage === 'triggered') latest.set(k, e);
    }
    for (const e of latest.values()) {
      if (e.stage === 'saved' && e.clipId && known.has(e.clipId)) continue;
      items.push({
        key: `ev-${e.triggerSeq ?? e.seq}`, stage: e.stage, clipId: e.clipId || null, type: e.defect?.type || 'DEFORMATION',
        confidence: e.defect?.confidence ?? 0, depth: e.telemetry?.depth ?? null,
        zone: e.telemetry?.segment ?? segmentFor(lineOf, e.telemetry?.depth), sessionTime: e.sessionTime ?? null, error: e.error,
      });
    }
    return items.sort((a, b) => (b.sessionTime ?? 0) - (a.sessionTime ?? 0));
  }, [videos, incidentEvents, live?.sessionId, live?.lineId, lines, lineId]);
  const alertsVisible = alerts.length > dismissedAt;
  const latestClipId = alerts.find((a) => a.clipId)?.clipId || null;

  // ---------------------------------------------------------------- actions
  const loadModel = async (name: string) => {
    setLoadingModel(true);
    log(`YOLO: loading weights ${name} (with GPU warm-up).`);
    try {
      const r = await postJSON<ModelStatus & { success: boolean; message: string }>('/api/model/load', { model_path: name });
      log(r.success ? `YOLO: ${r.message}` : `YOLO ERROR: ${r.message}`);
      setModel(r);
    } catch (e: any) {
      log(`YOLO ERROR: ${e.message}`);
    } finally {
      setLoadingModel(false);
      fetchModel();
    }
  };
  const toggleRec = () => postJSON('/api/live/recording', { enabled: !live?.recording })
    .then(() => log(`CONFIG: dive log ${live?.recording ? 'stopped (incident clips are still saved)' : 'recording'}`)).catch((e) => log(`ERROR: ${e.message}`));
  const resetDb = () => postJSON('/api/history/clear')
    .then(() => { log('SYSTEM: analyses and detections cleared (media, sensor logs and reports kept).', 'System', 'warn'); fetchHistory(); fetchVideos(); })
    .catch((e) => log(`ERROR: ${e.message}`));
  const openReview = (id: string, t?: number, fromLog = false) => {
    if (!fromLog) setDiveLogId(null);
    setReviewId(id);
    setActiveTab('review');
    setBannerClip(null);
    if (t != null) setSeekRequest({ t, nonce: Date.now() });
    fetchVideos();
  };
  const openDiveLog = (id: string) => {
    setDiveLogId(id);
    setReviewId(null);
    setActiveTab('review');
  };
  const cite = (videoId: string, t: number) => {
    setDiveLogId(null);
    setReviewId(videoId);
    setActiveTab('review');
    setReportDialog(null);
    setSeekRequest({ t, nonce: Date.now() });
    log(`REVIEW: jumped to ${t.toFixed(1)} s from a report citation.`);
  };

  const reportProps = {
    lineId, segment, lines, videos, reviewVideo, latestClipId, onCite: cite, log,
    liveSessionId: liveActive && live?.recording ? live.sessionId : null,
  };
  const bannerAlert = bannerClip ? alerts.find((a) => a.clipId === bannerClip) : undefined;

  return (
    <div className="w-full min-h-screen bg-ink text-tx font-sans text-sm flex flex-col overflow-x-hidden">
      <Header activeTab={activeTab} onTab={setActiveTab} liveActive={liveActive} batchBusy={batchBusy}
              lines={lines} lineId={lineId} onLineChange={changeLine} lineLocked={liveActive}
              rov={rov} model={model} stats={stats} apiOnline={apiOnline} wsConnected={wsConnected} testOn={testOn}
              onOpenSystem={() => setSystemOpen(true)} />

      {testMode?.enabled && (
        <div data-testid="test-banner" className="bg-tst text-on-tst px-5 py-2 font-bold text-[13px] flex flex-wrap items-center gap-x-3 gap-y-1">
          <span>Test Mode — simulated ROV camera ({testMode.video}) and telemetry. Everything recorded now is tagged TEST.
            {Object.keys(testMode.standIn).length > 0 && ` Stand-in defects: ${Object.entries(testMode.standIn).map(([k, v]) => `${k} → ${v}`).join(', ')}.`}</span>
          <button onClick={() => setSystemOpen(true)} className="ml-auto underline cursor-pointer">Test Mode controls</button>
        </div>
      )}
      {apiOnline === false && (
        <div className="bg-def-bg text-def-soft text-[13px] px-5 py-2.5 border-b border-def-edge">
          Backend offline. Start it with <code className="font-mono bg-black/40 px-1.5 py-0.5 rounded">python backend/main.py</code> — the dashboard reconnects automatically.
        </div>
      )}
      {bannerAlert && activeTab !== 'live' && (
        <div data-testid="global-incident" className="bg-def-bg border-b border-def-edge px-5 py-2 flex flex-wrap items-center gap-3 animate-alert">
          <span className="text-def-soft font-bold text-[13px]">
            Live: critical defect clipped — {typeName(bannerAlert.type)} {Math.round(bannerAlert.confidence * 100)}%{bannerAlert.depth != null ? ` at ${bannerAlert.depth.toFixed(1)} m` : ''}
          </span>
          <span className="ml-auto flex gap-2">
            {bannerAlert.clipId && reviewId !== bannerAlert.clipId && (
              <button onClick={() => openReview(bannerAlert.clipId!)} className="bg-def text-on-def text-xs font-extrabold px-3 py-1.5 rounded-lg cursor-pointer">Review incident</button>
            )}
            <button onClick={() => setBannerClip(null)} className="text-def-soft text-xs font-bold px-2 cursor-pointer">Dismiss</button>
          </span>
        </div>
      )}

      {activeTab === 'live' && (
        <LiveOps live={live} rov={rov} telemetry={liveTelemetry} wsConnected={wsConnected} sources={sources}
                 line={lines.find((l) => l.id === (live?.lineId || lineId))} lineId={lineId}
                 triggerConf={triggerConf} setTriggerConf={setTriggerConf} detectionMode={detectionMode} onDetectionMode={changeDetectionMode}
                 onToggleRec={toggleRec} logConf={live?.logConf ?? model?.liveLogConf ?? 0.4} history={history} testMode={testMode}
                 alerts={alerts} alertsVisible={alertsVisible} onDismissAlerts={() => setDismissedAt(alerts.length)}
                 onReviewClip={openReview} onReport={(scope) => setReportDialog({ tab: 'live', scope })} onOpenDiveLog={openDiveLog}
                 log={log} logs={logs} onOpenSystem={() => setSystemOpen(true)} videos={videos} />
      )}
      {activeTab === 'review' && diveLogId && !reviewId && (
        <DiveLog id={diveLogId} onBack={() => setDiveLogId(null)} log={log}
                 onOpenClip={(clipId, t) => { log(`REVIEW: opened incident clip from the dive log.`); openReview(clipId, t ?? 0, true); }}
                 onReport={(id, name) => setReportDialog({ tab: 'review', scope: `DIVE:${id}`, label: `Live dive · ${name}` })} />
      )}
      {activeTab === 'review' && !(diveLogId && !reviewId) && (
        <ReviewPipeline videos={videos} reviewId={reviewId} onSelect={(id) => { if (id) setDiveLogId(null); setReviewId(id); }} onOpenLog={openDiveLog} backTo={diveLogId ? 'Dive log' : undefined}
                        lines={lines} onRunsChanged={fetchVideos} log={log}
                        seekRequest={seekRequest}
                        reportPanel={<ReportPanel activeTab="review" {...reportProps} />} />
      )}
      {activeTab === 'batch' && (
        <BatchIngest videos={videos} lines={lines} history={history} zone={segment} onClearZone={() => setSegment(ALL_ZONES)}
                     onRefresh={fetchVideos} onReview={openReview} log={log} logs={logs} modelLoaded={!!model?.is_loaded} testMode={testMode}
                     registry={<RegistryCard history={history} line={line} segment={segment} onSegmentChange={setSegment}
                                             rovSegment={liveActive ? liveTelemetry?.segment : null} testData={testOn}
                                             onReport={() => setReportDialog({ tab: 'batch', scope: segment !== ALL_ZONES ? `SEGMENT:${lineId}|${segment}` : null })} />} />
      )}

      {activeTab === 'reports' && (
        <ReportsDirectory onCite={cite} log={log} newReport={<ReportPanel activeTab="reports" {...reportProps} />} />
      )}

      <SystemDrawer open={systemOpen} onClose={() => setSystemOpen(false)}>
        <ModelCard model={model} modelFiles={modelFiles} loadingModel={loadingModel} onLoadModel={loadModel} iou={liveIou} setIou={setLiveIou} />
        <TestModeCard status={testMode} videos={videos} model={model} line={line}
                      liveActive={liveActive} liveIsTest={!!live?.isTest}
                      onChanged={() => { fetchStats(); fetchSources(); }} log={log} />
        <GeminiCard />
        <ReportTemplatesCard />
        <SettingsFileCard />
        <GpuCard stats={stats} />
        <EventLog entries={logs} height={260} />
        <LogTrailCard />
        <ResetCard onReset={resetDb} canReset={!liveActive && !batchBusy} />
      </SystemDrawer>

      {reportDialog && (
        <ReportDialog activeTab={reportDialog.tab} initialScope={reportDialog.scope} initialScopeLabel={reportDialog.label} {...reportProps} onClose={() => setReportDialog(null)} />
      )}
    </div>
  );
}
