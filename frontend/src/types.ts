export type Tab = 'live' | 'batch' | 'review' | 'reports';
export type Severity = 'Info' | 'Low confidence' | 'High confidence';   // certainty, not severity
export type DetectionMode = 'ARMED' | 'AUTO' | 'STANDBY';
export type RovState = 'DRIVER_MISSING' | 'NO_ROV' | 'CONNECTED' | 'STALE';

export interface Detection {
  id: string;
  type: string;
  rawClass?: string;
  isDefect: boolean;
  confidence: number;
  severity: Severity;
  triggering?: boolean;      // live: at/above the clip trigger threshold
  standIn?: boolean;         // Test Mode: a generic class rehearsing a defect (e.g. "person" as DEFORMATION)
  x: number; y: number; width: number; height: number;
}

/** BlueROV2 telemetry (MAVLink) + mooring-line context added by the backend. */
export interface Telemetry {
  depth?: number | null;
  heading?: number | null;
  roll?: number | null;
  pitch?: number | null;
  temperature?: number | null;
  pressure_hpa?: number | null;
  altitude?: number | null;
  climb?: number | null;
  battery_v?: number | null;
  battery_pct?: number | null;
  armed?: boolean | null;
  mode?: string | null;
  leak?: boolean;
  stale?: boolean;
  simulated?: boolean;       // Test Mode BlueROV2 simulator, not the vehicle
  source?: string;           // recorded rows: MAVLINK | SIM | TLOG
  ageSec?: number;
  segment?: string | null;
  lineId?: string;
  seabedDepth?: number;
  t?: number;
}

export interface RovStatus {
  state: RovState;
  url: string;
  error: string | null;
  heartbeatAgeSec: number | null;
  msgRateHz: number;
  vehicle: { sysid?: number; type?: number; isSubmarine?: boolean };
  recording: boolean;
  tlog: string | null;
  simulated?: boolean;
}

export interface TestProfile { standIn: Record<string, string>; video?: string; simulatedRov?: boolean; targetDepth?: number }

export interface TestModeStatus {
  enabled: boolean; video: string | null; videoPath: string | null; streamUrl: string; streaming: boolean;
  streamRestarts: number; standIn: Record<string, string>; targetDepth: number; descentRate: number;
  simDepth: number | null; simRunning: boolean; error: string | null; uptimeSec: number;
}

export type LiveStatus = 'IDLE' | 'CONNECTING' | 'LIVE' | 'RECONNECTING' | 'ERROR';

export interface LiveState {
  type: 'live_state';
  status: LiveStatus;
  error: string | null;
  sessionId: string | null;
  runId: string | null;
  source: string | null;
  sourceKind: 'webcam' | 'network' | 'replay' | null;
  isTest?: boolean;
  testProfile?: TestProfile | null;
  recording: boolean;
  detectionMode: DetectionMode;
  armed: boolean;
  /** AUTO only: why detection waits - no fresh ROV depth, or the ROV is above autoArmDepth */
  armWait?: 'NO_DEPTH' | 'SHALLOW' | null;
  autoArmDepth?: number;
  lineId: string;
  triggerConf: number;
  logConf: number;
  /** clip trigger must hold: hits of the last `window` inferred frames; flickers = runs that never held */
  triggerHold?: { hits: number; window: number; now: number; flickers: number };
  /** the site's default trigger (config.toml live.trigger_default) */
  triggerDefault?: number;
  iou: number;
  thresholdLog: [number, number][];
  engineStatus: 'ONLINE' | 'STANDBY';
  captureFps: number;
  sourceFps?: number;
  inferenceFps: number;
  inferenceMs: number;
  framesInferred: number;
  framesSkipped: number;
  lateFrames?: number;
  bufferFrames: number;
  bufferCapacity?: number;
  bufferSeconds: number;
  bufferTargetSeconds: number;
  incidentCooldown: number;
  clipsWritten?: number;
  frameWidth?: number;
  frameHeight?: number;
  detections?: Detection[];
  telemetry?: Telemetry | null;
  rov?: RovStatus;
  t?: number;
  frameSeq?: number;
}

export interface IncidentEvent {
  type: 'incident';
  seq: number;
  stage: 'triggered' | 'saved' | 'failed';
  /** frame that triggered the incident: shared by its triggered / saved / failed events */
  triggerSeq?: number;
  sessionTime: number;
  triggerConf: number;
  defect: Detection;
  telemetry: Telemetry | null;
  lineId: string;
  clipId?: string;
  clipName?: string;
  triggerOffset?: number | null;
  error?: string;
}

export type VideoStatus = 'HASHING' | 'UNPROCESSED' | 'QUEUED' | 'PROCESSING' | 'COMPLETED' | 'FAILED' | 'DUPLICATE';
export type SourceType = 'LOCAL_UPLOAD' | 'LOCAL_IMPORT' | 'INCIDENT_CLIP' | 'LIVE_SESSION';

export interface RunTiming {
  processingSec: number; videoSec: number; frames: number; framesPerSec: number; speedX: number | null;
  queuedSec: number | null; proxySec: number | null; totalSec: number; resumes: number; liveSharedSec: number;
  detectSec?: number; waitSec?: number; prefetch?: number;
  reader: string | null; sampleFps: number | null; batchSize: number | null; model: string | null; device: string | null;
  resolution: string | null; codec: string | null; videoFps: number | null; startedAt: string | null; finishedAt: string | null;
}

export interface Run {
  id: string;
  kind: 'LIVE' | 'CLIP' | 'BATCH' | 'REANALYSIS' | 'LEGACY';
  conf: number;
  triggerConf: number | null;
  thresholdLog: [number, number][];
  model: string | null;
  status: string;
  progress: number;
  sampleFps: number | null;
  framesAnalyzed: number;
  framesWithDefects: number;
  /** batch / re-analysis: how long the processing took (null for live, clips and older runs) */
  timing?: RunTiming | null;
  error: string | null;
  parentRunId: string | null;
  createdAt: string | null;
  completedAt: string | null;
  defectCount?: number;
}

export interface VideoItem {
  id: string;
  name: string;
  sourceType: SourceType;
  status: VideoStatus;
  progress: number;
  etaSec: number | null;
  /** processing time so far of the running batch job */
  elapsedSec?: number | null;
  error: string | null;
  lineId: string | null;
  parentId: string | null;
  /** incident clips: name of the live dive they were cut from */
  parentName?: string | null;
  triggerOffsetSec: number | null;
  durationSec: number | null;
  duration: string;
  resolution: string;
  fps: number | null;
  codec: string | null;
  playable: boolean;
  fileSize: string;
  path: string | null;
  isManaged: boolean;
  missing: boolean;
  mediaOffline: boolean;
  framesAnalyzed: number;
  defectCount: number;
  /** events per defect type in the active analysis (same grouping as the registry) */
  defectEvents?: { DEFORMATION: number; CORROSION: number } | null;
  /** incident clips: what triggered the clip */
  incident?: ClipIncident | null;
  activeRun: Run | null;
  pendingRun: Run | null;
  pendingConf: number | null;
  telemetrySamples: number;
  tlog: { path: string; offsetSec: number; samples: number } | null;
  dateCreated: string | null;
  /** epoch seconds, for exact newest-first sorting */
  createdTs?: number | null;
  /** pinned to the top of the Review library (stored on the backend) */
  pinned?: boolean;
  pinnedTs?: number | null;
  streamUrl: string | null;
  isTest?: boolean;
  testProfile?: TestProfile | null;
}

export interface ClipIncident {
  type: string; confidence: number; depth: number | null; triggerConf: number | null; sessionTime: number | null;
}

export interface DefectRow {
  id: number;
  defect_type: string;
  raw_class: string | null;
  timestamp_sec: number;
  confidence: number;
  severity: Severity;
  x: number | null; y: number | null; width: number | null; height: number | null;
}

export interface SensorRow extends Telemetry { t: number; source?: string }

/** General (non-defect) detection from the sensor-DB object log. */
export interface ObjectRow { t: number; cls: string; conf: number; x: number; y: number; w: number; h: number }

export interface HistorySummary {
  counts: { CORROSION: number; DEFORMATION: number };
  detections: { CORROSION: number; DEFORMATION: number };
  framesAnalyzed: number;
  framesWithDefects: number;
  framesClean: number;
  inspections: number;
  eventGapSec: number;
  /** per depth zone (event assigned by ROV depth at its peak frame); present when a line is selected */
  segments?: Record<string, ZoneCounts>;
  unknownDepth?: ZoneCounts;
  /** video id -> {segment: events}: which dives have events in which zone */
  byVideo?: Record<string, Record<string, number>>;
}

export interface ZoneCounts {
  counts: { CORROSION: number; DEFORMATION: number };
  detections: { CORROSION: number; DEFORMATION: number };
  inspections: number;
}

export interface Segment { name: string; start_m: number; end_m: number }
export interface MooringLine { id: string; name: string; seabed_depth: number; segments: Segment[] }

export interface ModelStatus {
  model_path: string | null;
  resolved_path: string | null;
  is_loaded: boolean;
  ultralytics_available: boolean;
  device: string;
  half: boolean;
  class_names: Record<string, string>;
  defect_classes: Record<string, string>;
  warmup_ms: number | null;
  last_error: string | null;
  diagnostics: { device_name?: string | null; hint?: string | null };
  liveLogConf?: number;
}

export interface ArbiterStats {
  device: string;
  liveActive: boolean;
  utilisationPct: number;
  liveSharePct: number;
  batchSharePct: number;
  liveWaitMsP50: number | null;
  liveWaitMsP95: number | null;
  liveWaitMsMax: number | null;
  liveInferMsP50: number | null;
  queueDepth: number;
}

export interface SystemStats {
  uptimeSec: number;
  device: string;
  gpu: { name: string | null; tempC: number | null; powerW: number | null; vramUsedGB: number | null; vramTotalGB: number | null; utilPct: number | null };
  arbiter: ArbiterStats;
  live: { status: LiveStatus; captureFps: number; inferenceFps: number; inferenceMs: number; framesSkipped: number; armed: boolean };
  batch: { currentId: string | null; queued: number; framesPerSec: number; lastJob?: Record<string, unknown> | null };
  rov: RovStatus;
  testMode?: TestModeStatus;
}

export interface LiveSource { value: string; label: string; kind: string }

// ---- reports ------------------------------------------------------------
export type Grade = 'ACCEPTABLE' | 'MONITOR' | 'REPAIR' | 'REPLACE';
export type Visual = 'CONFIRMED' | 'UNCERTAIN' | 'NOT_VISIBLE' | 'LIKELY_FALSE_POSITIVE' | 'NOT_ASSESSED';

export interface ReportEvent {
  id: string; video_id: string; type: string; raw_classes: string[]; t_start: number; t_end: number; t_peak: number;
  detections: number; peak_confidence: number; severity: string; depth_m: number | null; segment: string | null;
  water_temp_c: number | null; altitude_m: number | null; keyframe?: string; inspection?: string;
}

export interface ReportFinding {
  event_ref: string; defect_type: string; visual_assessment: Visual; description: string;
  depth_m: number | null; segment: string | null; event?: ReportEvent | null;
}

/** Actual Gemini token use reported by the API (summed over the draft and any regeneration). */
export interface ReportUsage {
  input: number; output: number; thinking: number; total: number; images: number; requests: number; seconds?: number;
  perRequest: { input: number; output: number; thinking: number; total: number; images: number; seconds?: number }[];
}

export interface LiveDive {
  id: string; name: string; status: string; lineId: string | null; startedAt: string | null;
  durationSec: number | null; defectCount: number; clips: number; isTest: boolean;
}

export interface ReportAction { action: string; priority: 'IMMEDIATE' | 'SHORT_TERM' | 'LONG_TERM'; justification: string; refs: string[] }

export interface ReportSummary {
  id: string; scopeType: 'INCIDENT' | 'DIVE' | 'SEGMENT'; scopeRef: string; title: string;
  status: 'DRAFT' | 'NEEDS_REVIEW' | 'EDITED' | 'APPROVED'; generator: string; engineer: string | null;
  approvedAt: string | null; createdAt: string | null; conditionGrade: Grade;
  scopeName?: string | null; lineId?: string | null; isTest?: boolean; findings?: number;
}

/** A report being generated in the background (POST /api/reports/jobs). */
export interface ReportJob {
  id: string; scopeType: string; scopeRef: string; useAi: boolean; model: string;
  status: 'running' | 'done' | 'error';
  stage: 'queued' | 'facts' | 'keyframes' | 'ai' | 'check' | 'retry' | 'rules' | 'save' | 'done';
  label: string; progress: number; elapsedSec: number; startedAt: string; typicalAiSec?: number | null;
  reportId?: string; generator?: string; conditionGrade?: Grade; reportStatus?: string;
  error?: string; errorStatus?: number;
  /** set when this job redrafts an existing report in place */
  redraftOf?: string;
}

export interface AiError { message: string; hint: string; at?: string; suggestedModel?: string | null }

export interface AiStatus {
  configured: boolean; model: string; modelSource?: string; cooldownRemaining: number; cooldownSec?: number;
  keyKind?: string; keyWarning?: string | null; sdk?: string | null; keySource?: string | null; lastError?: AiError | null;
}

export interface Report extends ReportSummary {
  content: {
    summary: string; condition_grade: Grade; grade_rationale: string; findings: ReportFinding[];
    recommended_actions: ReportAction[]; limitations: string[]; engineer_notes: string;
    keyframes: { id: string; event_ref: string; t: number; files: string[] }[];
    second_opinion: Record<string, number>;
    scope: { type: string; name: string; coverage?: { live: boolean; until_s: number | null; snapshot_at: string; text: string } };
    usage?: ReportUsage | null;
    /** seconds per stage (facts, keyframes, ai, rules, check, total) */
    timings?: Record<string, number | boolean> | null;
    thinking?: string | null;
    analysis: { kind: string; threshold: number; trigger_threshold: number | null; model: string } | null;
    sensors: { source: string; samples: number | null; depth_range_m: number[] | null };
    test_data?: { note: string; stand_in_classes: Record<string, string> } | null;
    /** Gemini failed for this report: why, and what to do (the draft is rule-based) */
    ai_error?: AiError | null;
    /** failed Gemini attempts before the draft (busy / timeout / quota / fatal) */
    ai_attempts?: { model: string; kind: 'busy' | 'timeout' | 'quota' | 'fatal'; seconds: number; message: string }[];
    /** set when the chosen model was busy and the fallback model drafted the report */
    fallback?: { from: string; to: string; reason: string; message: string } | null;
    redrafts?: number;
  };
  grounding: { passed: boolean; errors: string[]; attempts: number; firstAttemptErrors: string[] };
}

/* ---- live dive log (3.6.5) ---- */
export interface DiveSummary {
  id: string; name: string; lineId: string | null; status: string; isLive: boolean; startedAt: string | null;
  createdTs: number | null; durationSec: number; clips: number;
  /** time the dive stayed open after its last data (PC asleep / Stop pressed much later); not in durationSec */
  idleTailSec?: number; openSec?: number; sightings: { DEFORMATION: number; CORROSION: number };
  samples: number; maxDepth: number | null; isTest: boolean; source: string | null;
}
export interface DiveSample {
  t: number; depth: number | null; heading: number | null; temperature: number | null; battery_v: number | null;
  battery_pct: number | null; mode: string | null; armed: boolean | null;
}
export interface DiveSighting {
  id: string; type: 'DEFORMATION' | 'CORROSION'; start: number; end: number; peakT: number; peak: number; detections: number;
  depth: number | null; zone: string | null; severity: string; trigger: number | null; aboveTrigger: boolean; clipId: string | null;
}
export interface DiveClip { id: string; name: string; start: number | null; end: number | null; trigger: number | null; durationSec: number | null; playable: boolean }
export interface DiveEvent { t: number | null; kind: string; text: string | null }
export interface DiveSetting { t: number; kind: 'TRIGGER' | 'MODE' | 'RECORDING'; value: number | string | boolean }
export interface DiveLog {
  dive: DiveSummary & { model: string | null; logConf: number | null; triggerConf: number | null; lineName: string | null; endedAt: string | null };
  zones: { name: string; from: number; to: number }[];
  seabed: number | null;
  series: DiveSample[]; seriesTotal: number;
  events: DiveEvent[]; clips: DiveClip[]; sightings: DiveSighting[]; settings: DiveSetting[];
  summary: {
    durationSec: number; samples: number; loggedSec: number; maxDepth: number | null; zoneTimeSec: Record<string, number>;
    clips: number; sightings: { DEFORMATION: number; CORROSION: number }; aboveTrigger: number; flickersIgnored: number;
    battery: { startV: number; endV: number; startPct: number | null; endPct: number | null } | null;
    linkLosses: number; settingsChanges: number;
    idleTailSec?: number; openSec?: number; pauses?: DiveEvent[];
  };
  tlogs: { name: string; sizeMb: number }[];
}
