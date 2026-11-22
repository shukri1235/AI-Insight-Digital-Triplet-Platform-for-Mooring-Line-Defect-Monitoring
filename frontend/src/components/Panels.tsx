import { Fragment, useEffect, useState } from 'react';
import { AlertTriangle, ArrowDown, Brain, Crosshair, Cpu, Database, FileText, RefreshCw } from 'lucide-react';
import { getJSON, localTime, postJSON, putJSON } from '../api';
import type {
  AiError, AiStatus, DetectionMode, HistorySummary, LiveState, MooringLine, ModelStatus, RovStatus, Run, SystemStats, Telemetry, VideoItem,
} from '../types';
import { btn, Card, CardTitle } from './shared';

export const LIVE_TRIGGER_DEFAULT = 0.6;
export const IOU_DEFAULT = 0.45;
export const ALL_ZONES = 'ALL';
const PRESETS = [0.25, 0.4, 0.5, 0.6];

const zoneShort = (name: string) => name.replace(/\s*Zone$/i, '');

/* =========================================================================
 * DETECTION (Live): mode, clip trigger, dive log
 * ========================================================================= */
const MODES: { id: DetectionMode; label: string; help: string }[] = [
  { id: 'ARMED', label: 'Armed', help: 'Detects and clips all the time.' },
  { id: 'AUTO', label: 'Auto', help: 'Detects only while the ROV is below {d} m, so deck and surface footage never clip.' },
  { id: 'STANDBY', label: 'Standby', help: 'Video only: no detection, no clips. For launch and recovery.' },
];

export function DetectionCard({ live, active, detectionMode, onDetectionMode, triggerConf, setTriggerConf, onToggleRec, logConf, depth }: {
  live: LiveState | null; active: boolean; detectionMode: DetectionMode; onDetectionMode: (m: DetectionMode) => void;
  triggerConf: number; setTriggerConf: (v: number) => void; onToggleRec: () => void; logConf: number; depth: number | null | undefined;
}) {
  const armDepth = live?.autoArmDepth ?? 2;
  const mode = MODES.find((m) => m.id === detectionMode)!;
  let state: string, waiting = false;
  if (detectionMode === 'ARMED') state = 'Armed';
  else if (detectionMode === 'STANDBY') state = 'Standby';
  else if (!active) state = 'Auto · arms when the ROV is below ' + armDepth + ' m';
  else if (live?.armed) state = `Auto · armed${depth != null ? ` (ROV at ${depth.toFixed(1)} m)` : ''}`;
  else if (live?.armWait === 'SHALLOW') { state = `Auto · waiting (ROV at ${depth?.toFixed(1) ?? '?'} m)`; waiting = true; }
  else { state = 'Auto · waiting for ROV depth'; waiting = true; }
  const recording = active && !!live?.recording;
  return (
    <Card className="flex flex-col gap-3" testid="threshold-panel" data-context="LIVE">
      <CardTitle icon={<Crosshair className="w-3.5 h-3.5" />} title="Detection"
                 right={<span data-testid="detection-state" className={waiting ? 'text-warn' : ''}>{state}</span>} />
      <div role="group" aria-label="Detection mode" className="grid grid-cols-3 gap-[3px] bg-ink rounded-[11px] p-[3px]">
        {MODES.map((m) => (
          <button key={m.id} onClick={() => onDetectionMode(m.id)} data-testid={`mode-${m.id}`} aria-pressed={detectionMode === m.id}
                  className={`py-2 rounded-lg text-sm cursor-pointer ${detectionMode === m.id ? 'bg-acc text-on-acc font-extrabold' : 'text-mut font-semibold hover:text-tx'}`}>
            {m.label}
          </button>
        ))}
      </div>
      <p className="text-xs text-mut font-semibold -mt-1">{mode.help.replace('{d}', String(armDepth))}</p>
      <label className="flex flex-col gap-1.5">
        <span className="flex justify-between text-[13px] font-bold">
          <span>Clip trigger</span><span data-testid="conf-value" className="text-acc">{triggerConf.toFixed(2)}</span>
        </span>
        <input type="range" min={0.3} max={0.95} step={0.05} value={triggerConf} data-testid="trigger-slider"
               onChange={(e) => setTriggerConf(parseFloat(e.target.value))} className="w-full accent-acc cursor-pointer" />
        <span className="flex justify-between text-[12.5px] text-dim font-semibold">
          <span data-testid="trigger-hold">
            Deformation ≥ trigger{(live?.triggerHold?.hits ?? 3) > 1 ? ` on ${live?.triggerHold?.hits ?? 3} of ${live?.triggerHold?.window ?? 5} frames` : ''} saves a 5 s clip · ≥ {logConf.toFixed(2)} is logged
            {active && (live?.triggerHold?.flickers ?? 0) > 0 && <span className="text-dim"> · {live?.triggerHold?.flickers} one-off hit{live?.triggerHold?.flickers === 1 ? '' : 's'} ignored</span>}
          </span>
          {Math.abs(triggerConf - (live?.triggerDefault ?? LIVE_TRIGGER_DEFAULT)) > 1e-6 && (
            <button onClick={() => setTriggerConf(live?.triggerDefault ?? LIVE_TRIGGER_DEFAULT)} className="text-acc-tx underline cursor-pointer">
              reset {(live?.triggerDefault ?? LIVE_TRIGGER_DEFAULT).toFixed(2)}
            </button>
          )}
        </span>
      </label>
      <div className="flex items-center justify-between text-[13px]">
        <span className="flex items-center gap-2 text-tx2 font-semibold"
              title="Dive log = sensor samples, .tlog and logged detections for this session. Incident clips are always saved.">
          <span className={`w-[7px] h-[7px] rounded-full ${recording ? 'bg-def animate-live' : 'bg-dim'}`} />
          {recording ? 'Dive log recording' : active ? 'Dive log off (clips still saved)' : 'Dive log starts with the stream'}
        </span>
        <button onClick={onToggleRec} disabled={!active} data-testid="toggle-rec" className={`${btn.ghost} text-[13px]`}>
          {recording ? 'Stop log' : 'Record log'}
        </button>
      </div>
    </Card>
  );
}

/* =========================================================================
 * ROV DEPTH (Live): BlueROV2 MAVLink telemetry. Never invented: when no vehicle is
 * talking the card says so instead of showing numbers.
 * ========================================================================= */
const n1 = (v: number | null | undefined, d = 1) => (v == null ? null : v.toFixed(d));

export function RovDepthCard({ telemetry, rov, seabed }: { telemetry: Telemetry | null; rov: RovStatus | null; seabed?: number }) {
  const t = telemetry;
  const stale = !!t?.stale;
  let placeholder: { title: string; sub: string } | null = null;
  if (rov?.state === 'DRIVER_MISSING') placeholder = { title: 'MAVLink driver missing', sub: 'Install pymavlink on the backend PC: pip install pymavlink' };
  else if (!t) placeholder = { title: 'ROV offline', sub: `No BlueROV2 heartbeat on ${rov?.url?.replace('udpin:', 'udp ') || 'udp :14551'}. Power the vehicle and add a BlueOS MAVLink endpoint to this PC.` };
  const sim = !!(t?.simulated || t?.source === 'SIM');
  const toSeabed = t?.altitude != null ? t.altitude : t?.depth != null && (t.seabedDepth || seabed) ? (t.seabedDepth || seabed)! - t.depth : null;
  const modeLabel = t ? `${sim ? 'SIMULATED · ' : ''}${t.mode || '—'}${t.armed != null ? ` · ${t.armed ? 'armed' : 'disarmed'}` : ''}` : '';
  return (
    <Card testid="telemetry-card">
      <CardTitle icon={<ArrowDown className="w-3.5 h-3.5" />} title="ROV depth"
                 right={t ? <span className={sim ? 'text-tst' : ''} data-testid={sim ? 'tel-simulated' : undefined}>{modeLabel}</span> : null} />
      {placeholder ? (
        <div className="mt-3 rounded-xl border border-dashed border-edge bg-ink px-4 py-5 text-center" data-testid="rov-offline">
          <div className="text-sm font-bold text-tx2">{placeholder.title}</div>
          <div className="text-xs text-dim mt-1">{placeholder.sub}</div>
        </div>
      ) : (
        <>
          {t?.leak && <div className="mt-2 text-xs font-extrabold text-on-def bg-def rounded-lg px-3 py-1.5 animate-live">Leak detected in the vehicle — follow the recovery procedure</div>}
          <div className={`flex items-baseline gap-2 mt-1.5 ${stale ? 'opacity-50' : ''}`}>
            <span data-testid="tel-depth" className="text-[44px] font-extrabold tracking-[-0.03em] leading-none">{n1(t?.depth) ?? '—'}</span>
            <span className="text-mut font-bold">m</span>
            {t?.climb != null && Math.abs(t.climb) >= 0.05 && (
              <span className="text-xs text-dim font-semibold">{t.climb > 0 ? '▲' : '▼'} {Math.abs(t.climb).toFixed(2)} m/s</span>
            )}
            <span data-testid="tel-segment" className="ml-auto font-extrabold text-acc text-right">{t?.segment || '—'}</span>
          </div>
          <div className={`grid grid-cols-4 gap-2 mt-3 pt-2.5 border-t border-line ${stale ? 'opacity-50' : ''}`}>
            {[
              { k: 'Heading', v: t?.heading != null ? `${Math.round(t.heading)}°` : '—', id: 'tel-heading' },
              { k: 'Water', v: t?.temperature != null ? `${t.temperature.toFixed(1)} °C` : '—', id: 'tel-temp' },
              { k: 'To seabed', v: toSeabed != null ? `${toSeabed.toFixed(1)} m` : '—', id: 'tel-alt',
                title: t?.altitude != null ? 'Ping sonar altitude' : 'Chart seabed depth minus ROV depth (no sonar)' },
              { k: 'Battery', v: t?.battery_v != null ? `${t.battery_v.toFixed(1)} V` : '—' },
            ].map((x) => (
              <div key={x.k} title={x.title}>
                <div className="text-xs text-dim font-semibold">{x.k}</div>
                <div data-testid={x.id} className="text-[15px] font-bold">{x.v}</div>
              </div>
            ))}
          </div>
          {stale && <div className="text-xs text-warn font-semibold mt-2" data-testid="tel-stale">Telemetry stale · last message {t?.ageSec?.toFixed(0)} s ago</div>}
        </>
      )}
    </Card>
  );
}

/* =========================================================================
 * SYSTEM DRAWER CARDS
 * ========================================================================= */
export function ModelCard({ model, modelFiles, loadingModel, onLoadModel, iou, setIou }: {
  model: ModelStatus | null; modelFiles: string[]; loadingModel: boolean; onLoadModel: (name: string) => void;
  iou: number; setIou: (v: number) => void;
}) {
  const [choice, setChoice] = useState<string>('');
  useEffect(() => { if (model?.model_path && !choice) setChoice(model.model_path); }, [model?.model_path]); // eslint-disable-line
  const options = Array.from(new Set([...modelFiles, ...(model?.model_path ? [model.model_path] : [])]));
  const defectClasses = model ? Object.values(model.defect_classes) : [];
  return (
    <Card className="flex flex-col gap-2.5" testid="control-board">
      <CardTitle icon={<Cpu className="w-3.5 h-3.5" />} title="Detection model"
                 right={<span className="text-dim">{model?.half ? 'FP16' : 'FP32'} · {model?.device?.toUpperCase() || '—'}</span>} />
      <div className="flex gap-2">
        <select value={choice} onChange={(e) => setChoice(e.target.value)} aria-label="Model weights" data-testid="model-select"
                className={`${btn.input} flex-1 min-w-0 font-mono text-[13px]`}>
          {options.length === 0 && <option value="">No weights found</option>}
          {options.map((f) => <option key={f} value={f}>{f}</option>)}
        </select>
        <button onClick={() => choice && onLoadModel(choice)} disabled={loadingModel || !choice} data-testid="model-load" className={`${btn.secondary} min-w-[70px]`}>
          {loadingModel ? <RefreshCw className="w-4 h-4 animate-spin mx-auto" /> : 'Load'}
        </button>
      </div>
      <div className="text-xs text-mut flex justify-between gap-2">
        <span className="flex items-center gap-1.5">
          <span className={`w-2 h-2 rounded-full ${model?.is_loaded ? 'bg-acc' : 'bg-warn'}`} />
          {model?.is_loaded ? `${model.model_path} loaded` : model?.ultralytics_available === false ? 'Ultralytics not installed' : 'No model loaded'}
        </span>
        <span className="font-mono">{model?.warmup_ms != null ? `warm-up ${model.warmup_ms} ms` : ''}</span>
      </div>
      {model?.is_loaded && (
        <p className={`text-xs ${defectClasses.length ? 'text-dim' : 'text-warn'}`}>
          {defectClasses.length
            ? `Defect classes: ${defectClasses.join(', ')}`
            : `This model has no deformation / pitting classes (${Object.values(model.class_names).slice(0, 4).join(', ')}…). Its boxes are shown and saved to the general detection log, never as defects. To rehearse the defect workflow use Test Mode stand-in classes.`}
        </p>
      )}
      {model?.last_error && (
        <div className="text-xs text-def-soft bg-def-bg border border-def-edge rounded-lg px-3 py-2 flex gap-2">
          <AlertTriangle className="w-4 h-4 shrink-0" /><span className="break-words min-w-0">{model.last_error}</span>
        </div>
      )}
      {model?.diagnostics?.hint && !model.last_error && model.device === 'cpu' && <p className="text-xs text-warn">{model.diagnostics.hint}</p>}
      <details className="text-xs text-mut">
        <summary className="cursor-pointer font-semibold">Advanced · IoU (box merging) {iou.toFixed(2)}</summary>
        <input type="range" min={0.1} max={0.95} step={0.05} value={iou} onChange={(e) => setIou(parseFloat(e.target.value))}
               className="w-full accent-acc mt-2" aria-label="IoU NMS threshold" />
        <span className="text-dim">Live only. Default {IOU_DEFAULT.toFixed(2)}; lower merges overlapping boxes more aggressively.</span>
      </details>
    </Card>
  );
}

export function GpuCard({ stats }: { stats: SystemStats | null }) {
  const g = stats?.gpu;
  const a = stats?.arbiter;
  const live = a?.liveSharePct ?? 0, batch = a?.batchSharePct ?? 0;
  const up = stats?.uptimeSec ?? 0;
  const hms = `${String(Math.floor(up / 3600)).padStart(2, '0')}:${String(Math.floor((up % 3600) / 60)).padStart(2, '0')}:${String(Math.floor(up % 60)).padStart(2, '0')}`;
  const rows: [string, string][] = [
    ['Temperature', g?.tempC != null ? `${g.tempC} °C` : 'n/a'],
    ['VRAM', g?.vramUsedGB != null ? `${g.vramUsedGB.toFixed(1)} / ${g.vramTotalGB?.toFixed(1)} GB` : 'n/a'],
    ['Power', g?.powerW != null ? `${g.powerW} W` : 'n/a'],
    ['Live inference', stats?.live.status === 'LIVE' ? `${stats.live.inferenceMs} ms` : 'idle'],
    ['Batch', stats?.batch.currentId ? `${stats.batch.framesPerSec} frames/s` : stats?.batch.queued ? `${stats.batch.queued} queued` : 'idle'],
    ['Uptime', hms],
  ];
  return (
    <Card className="flex flex-col gap-2.5" testid="hardware-card">
      <div className="flex items-center gap-2">
        <span className="font-bold text-[13px] text-mut">GPU sharing</span>
        <span className="ml-auto text-xs text-dim">{g?.name ? g.name.replace('NVIDIA GeForce ', '') : (stats?.device || 'offline').toUpperCase()}</span>
      </div>
      <div className="h-2.5 rounded-full overflow-hidden flex bg-ink" data-testid="arbiter">
        <div className="bg-def transition-all" style={{ width: `${live}%` }} title="Live" />
        <div className="bg-acc transition-all" style={{ width: `${batch}%` }} title="Batch" />
      </div>
      <div className="flex justify-between text-xs font-mono">
        <span className="text-def-tx">live {Math.round(live)}%</span>
        <span className="text-tx2">live wait p95 {a?.liveWaitMsP95 != null ? `${Math.round(a.liveWaitMsP95)} ms` : '—'}</span>
        <span className="text-acc-tx">batch {Math.round(batch)}%</span>
      </div>
      <p className="text-xs text-dim">Live frames always go first; batch work uses the idle GPU time and slows down while a dive is live.</p>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs border-t border-line pt-2">
        {rows.map(([k, v]) => <div key={k} className="flex justify-between"><span className="text-dim">{k}</span><span className="font-semibold">{v}</span></div>)}
      </div>
    </Card>
  );
}

export const AI_PRESETS = [
  { id: 'flash', model: 'gemini-3.8-flash', label: 'Flash · standard', note: 'best visual check of key frames' },
  { id: 'lite', model: 'gemini-3.5-flash-lite', label: 'Flash-Lite · bulk', note: '~3× cheaper and faster, weaker visual check' },
];
export const isBulkModel = (m?: string | null) => !!m && /lite/i.test(m);

/** Gemini set-up check: is the key visible to the backend, what kind is it, does a tiny request work? */
export function GeminiCard() {
  const [st, setSt] = useState<AiStatus | null>(null);
  const [test, setTest] = useState<null | ({ ok: boolean; ms?: number; reply?: string } & Partial<AiError>)>(null);
  const [busy, setBusy] = useState(false);
  const [models, setModels] = useState<string[]>([]);
  const [modelErr, setModelErr] = useState<string | null>(null);
  const load = () => getJSON<AiStatus>('/api/ai/status').then(setSt).catch(() => setSt(null));
  useEffect(() => { load(); }, []);
  useEffect(() => {
    if (st?.configured) getJSON<{ models: string[] }>('/api/ai/models').then((m) => setModels(m.models || [])).catch(() => {});
  }, [st?.configured]);
  const run = async () => {
    setBusy(true);
    try { setTest(await postJSON('/api/ai/test')); } catch (e: any) { setTest({ ok: false, message: e.message, hint: '' }); }
    finally { setBusy(false); load(); }
  };
  const chooseModel = async (m: string, thenTest = false) => {
    setModelErr(null);
    try {
      await putJSON('/api/ai/model', { model: m });
      setTest(null);
      await load();
      if (thenTest) await run();
    } catch (e: any) { setModelErr(e.message); }
  };
  const envModel = (st?.modelSource || '').startsWith('GEMINI_MODEL');
  const suggestion = (test && !test.ok && test.suggestedModel) || (!test && st?.lastError?.suggestedModel) || null;
  const rows: [string, string][] = [
    ['API key', st ? (st.configured ? `found · ${st.keyKind}` : 'not found') : '—'],
    ...(st?.keySource ? [['From', st.keySource] as [string, string]] : []),
    ['SDK', st?.sdk ? `google-genai ${st.sdk}` : st ? 'google-genai not installed' : '—'],
  ];
  return (
    <Card className="flex flex-col gap-2.5" testid="gemini-card">
      <CardTitle icon={<Brain className="w-3.5 h-3.5" />} title="Gemini report assistant"
                 right={st ? <span className={st.configured && !st.lastError ? '' : 'text-warn'}>{st.configured ? (st.lastError ? 'last request failed' : 'ready') : 'off'}</span> : null} />
      <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
        {rows.map(([k, v]) => <Fragment key={k}><span className="text-dim">{k}</span><span className="text-tx2 font-semibold">{v}</span></Fragment>)}
      </div>
      <div role="radiogroup" aria-label="Gemini model" className="grid grid-cols-2 gap-1.5" data-testid="ai-model-presets">
        {AI_PRESETS.map((p) => {
          const on = st?.model === p.model;
          return (
            <button key={p.model} role="radio" aria-checked={on} disabled={!st?.configured || envModel} onClick={() => !on && chooseModel(p.model)}
                    data-testid={`ai-preset-${p.id}`}
                    className={`text-left rounded-[10px] px-3 py-2 border cursor-pointer disabled:cursor-default disabled:opacity-60 ${on ? 'bg-acc-bg border-acc' : 'bg-ink border-edge'}`}>
              <div className="text-[13px] font-bold">{p.label}</div>
              <div className="text-xs text-dim">{p.note}</div>
            </button>
          );
        })}
      </div>
      <details className="text-xs text-dim -mt-1">
        <summary className="cursor-pointer">Other model{st?.model && !AI_PRESETS.some((p) => p.model === st.model) ? ` (in use: ${st.model})` : ''}</summary>
        <select value={st?.model || ''} disabled={!st?.configured || envModel} onChange={(e) => chooseModel(e.target.value)}
                data-testid="ai-model" className={`${btn.input} !py-1.5 w-full mt-1.5 text-[13px] font-semibold`}>
          {[...new Set([...(st?.model ? [st.model] : []), ...models])].map((m) => <option key={m} value={m}>{m}</option>)}
        </select>
      </details>
      <span className="text-xs text-dim -mt-1">
        {envModel ? 'Set by the GEMINI_MODEL environment variable (remove it to choose here).'
          : 'Use Flash normally. Switch to Flash-Lite only for many reports at once, then switch back: its visual check of the key frames is weaker. Each report shows its real token use.'}
      </span>
      {modelErr && <p className="text-xs text-warn">{modelErr}</p>}
      {st?.keyWarning && <p className="text-xs text-warn">{st.keyWarning}</p>}
      {st && !st.configured && (
        <div className="text-xs text-dim flex flex-col gap-1">
          <span>The backend looks for <code className="font-mono">GEMINI_API_KEY</code> in its environment, in a <code className="font-mono">.env</code> file
            and in your Windows user / system variables. Easiest: create a text file named <code className="font-mono">.env</code> in the app folder
            (next to <code className="font-mono">backend</code>) containing</span>
          <code className="font-mono bg-ink rounded px-2 py-1 text-tx2">GEMINI_API_KEY=your-key-here</code>
          <span>then restart the backend. Keep that file private (do not hand it in).</span>
        </div>
      )}
      {st?.configured && (
        <button onClick={run} disabled={busy} data-testid="ai-test" className={`${btn.secondary} self-start text-[13px]`}>
          {busy ? 'Testing…' : 'Test connection'}
        </button>
      )}
      {test && (test.ok
        ? <p className="text-xs text-ok" data-testid="ai-test-result">Gemini answered "{test.reply}" in {test.ms} ms — the key works.</p>
        : <div className="text-xs rounded-lg bg-[#33270C] text-warn px-3 py-2 flex flex-col gap-1" data-testid="ai-test-result">
            <span className="font-bold">Connection test failed.</span>
            {test.hint && <span className="text-tx2">{test.hint}</span>}
            <span className="font-mono text-dim break-words">{test.message}</span>
          </div>)}
      {suggestion && !envModel && (
        <button onClick={() => chooseModel(suggestion, true)} data-testid="ai-use-suggested" className={`${btn.primary} !py-2 text-[13px] self-start`}>
          Use {suggestion} and test again
        </button>
      )}
      {!test && st?.lastError && (
        <div className="text-xs rounded-lg bg-[#33270C] text-warn px-3 py-2 flex flex-col gap-1">
          <span className="font-bold">Last Gemini request failed{st.lastError.at ? ` (${st.lastError.at.slice(11, 16)})` : ''}.</span>
          <span className="text-tx2">{st.lastError.hint}</span>
        </div>
      )}
      <p className="text-xs text-dim">The test sends one short prompt (a few tokens). If Gemini fails, reports are still drafted by the rules.</p>
    </Card>
  );
}

export function ResetCard({ onReset, canReset }: { onReset: () => void; canReset: boolean }) {
  const [confirm, setConfirm] = useState(false);
  return (
    <Card className="flex flex-col gap-2">
      <span className="font-bold text-[13px] text-mut">Demo reset</span>
      <p className="text-xs text-dim">Removes every analysis and detection so dives can be re-processed. Media, sensor logs and approved reports are kept.</p>
      <button disabled={!canReset} onClick={() => { if (confirm) { setConfirm(false); onReset(); } else setConfirm(true); }}
              onMouseLeave={() => setConfirm(false)} data-testid="reset-analyses"
              title={canReset ? '' : 'Stop the live stream and let batch jobs finish first'}
              className={`self-start text-xs font-bold px-3 py-1.5 rounded-lg border cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed ${
                confirm ? 'bg-def border-def text-on-def' : 'border-edge2 text-mut hover:text-def-tx hover:border-def-edge'}`}>
        <Database className="w-3 h-3 inline mr-1.5 -mt-0.5" />{confirm ? 'Click again to wipe analyses' : 'Reset analyses'}
      </button>
    </Card>
  );
}

/* =========================================================================
 * DEFECT REGISTRY (Batch): per-zone totals; clicking a zone filters the dive table
 * ========================================================================= */
export function EventCounts({ c, d, empty = 'no events' }: { c: number; d: number; empty?: string }) {
  if (!c && !d) return <span className="text-mut">{empty}</span>;
  return (
    <span>
      {d > 0 && <span className="text-def-tx">{d} deformation</span>}
      {d > 0 && c > 0 && <span className="text-dim"> · </span>}
      {c > 0 && <span className="text-pit-tx">{c} pitting</span>}
    </span>
  );
}

export function RegistryCard({ history, line, segment, onSegmentChange, rovSegment, testData, onReport }: {
  history: HistorySummary | null; line: MooringLine | undefined; segment: string; onSegmentChange: (s: string) => void;
  rovSegment?: string | null; testData: boolean; onReport: () => void;
}) {
  const segments = line?.segments || [];
  const unknown = history?.unknownDepth;
  const unknownEvents = (unknown?.counts.CORROSION ?? 0) + (unknown?.counts.DEFORMATION ?? 0);
  const zones = [
    { name: ALL_ZONES, label: 'Whole line', range: `0–${segments.length ? segments[segments.length - 1].end_m.toFixed(0) : '—'} m`,
      c: history?.counts.CORROSION ?? 0, d: history?.counts.DEFORMATION ?? 0 },
    ...segments.map((s) => ({ name: s.name, label: zoneShort(s.name), range: `${s.start_m.toFixed(0)}–${s.end_m.toFixed(0)} m`,
      c: history?.segments?.[s.name]?.counts.CORROSION ?? 0, d: history?.segments?.[s.name]?.counts.DEFORMATION ?? 0 })),
  ];
  return (
    <Card className="flex flex-col gap-3 w-full" testid="defect-registry">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-semibold text-[15px]">Defect registry · {line?.id || '—'}</span>
        {testData && <span data-testid="test-badge" className="px-2 py-0.5 rounded-md bg-tst text-on-tst text-[12px] font-extrabold">TEST dives</span>}
        <span className="text-[13px] text-mut">totals across {history?.inspections ?? 0} processed dive{history?.inspections === 1 ? '' : 's'} · click a zone to show only the dives with defects there</span>
        <button onClick={onReport} data-testid="registry-report" className={`${btn.ghost} ml-auto text-[13px] flex items-center gap-1.5`}>
          <FileText className="w-3.5 h-3.5" /> Report…
        </button>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5" data-testid="zone-selector">
        {zones.map((z) => {
          const on = segment === z.name;
          const here = z.name !== ALL_ZONES && rovSegment === z.name;
          return (
            <button key={z.name} onClick={() => onSegmentChange(z.name)} data-testid={`zone-${z.label.split(' ')[0]}`} data-active={on}
                    className={`text-left rounded-[10px] px-3 py-2.5 border cursor-pointer ${on ? 'bg-acc-bg border-acc' : 'bg-ink border-edge hover:border-edge2'}`}>
              <div className="font-semibold flex items-center gap-1.5">{here && <span className="w-2 h-2 rounded-full bg-acc animate-live" title="ROV is here" />}{z.label}</div>
              <div className="text-xs text-mut">{z.range}</div>
              <div className="mt-1.5 text-[13px]" data-testid="zone-count" data-n={z.c + z.d}><EventCounts c={z.c} d={z.d} /></div>
            </button>
          );
        })}
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-dim">
        <span data-testid="registry-events" data-deformation={history?.counts.DEFORMATION ?? 0} data-pitting={history?.counts.CORROSION ?? 0}>
          {history?.detections.DEFORMATION ?? 0} deformation and {history?.detections.CORROSION ?? 0} pitting detections behind these events
        </span>
        <span>clean frames {(history?.framesClean ?? 0).toLocaleString()} / {(history?.framesAnalyzed ?? 0).toLocaleString()}</span>
      </div>
      {unknownEvents > 0 && (
        <p className="text-xs text-warn" data-testid="unknown-depth">
          {unknownEvents} event{unknownEvents === 1 ? '' : 's'} with no ROV depth recorded {segment !== ALL_ZONES ? 'are not in any zone' : 'count in the line total only'} — attach the dive's .tlog to place them.
        </p>
      )}
      <p className="text-xs text-dim leading-relaxed">
        Each event (detections of one class less than {history?.eventGapSec ?? 2} s apart) goes to the zone of the ROV depth at its strongest
        frame. Counts use each dive's active analysis; incident clips are left out so they are not counted twice with their live dive.
      </p>
    </Card>
  );
}

/* =========================================================================
 * ANALYSIS SHOWN (Review): which run is displayed + re-analyse at another threshold
 * ========================================================================= */
export function AnalysisCard({ video, run, onRunsChanged, log }: { video: VideoItem | null; run: Run | null; onRunsChanged: () => void; log: (m: string) => void }) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [newConf, setNewConf] = useState<number | null>(null);
  const [custom, setCustom] = useState('');
  const [busy, setBusy] = useState(false);
  const pending = video?.pendingRun;
  useEffect(() => {
    if (!video) { setRuns([]); return; }
    getJSON<{ runs: Run[] }>(`/api/videos/${video.id}/runs`).then((r) => setRuns(r.runs || [])).catch(() => {});
  }, [video?.id, video?.activeRun?.id, pending?.status]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setNewConf(null); setCustom(''); }, [video?.id]);
  if (!video) return null;

  const activate = async (id: string) => {
    try { await postJSON(`/api/runs/${id}/activate`); onRunsChanged(); log(`REVIEW: "${video.name}" now shows analysis ${id.slice(0, 8)}.`); }
    catch (e: any) { log(`ERROR: ${e.message}`); }
  };
  const reanalyse = async () => {
    if (newConf == null) return;
    setBusy(true);
    try {
      await postJSON(`/api/videos/${video.id}/analyse`, { conf: newConf });
      log(`REVIEW: re-analysing "${video.name}" at ${newConf.toFixed(2)} (original kept).`);
      setNewConf(null); setCustom(''); onRunsChanged();
    } catch (e: any) { log(`ERROR: ${e.message}`); } finally { setBusy(false); }
  };
  const label = (r: Run) => (r.kind === 'LIVE' || r.kind === 'CLIP' ? 'Recorded live' : r.kind === 'REANALYSIS' ? 'Re-analysis' : 'Batch analysis');
  const shown = runs.filter((r) => r.status === 'COMPLETED' || r.id === video.activeRun?.id);
  const tl = run?.thresholdLog || [];

  return (
    <Card className="flex flex-col gap-2.5" testid="threshold-panel" data-context="REVIEW">
      <div className="text-[13px] text-mut font-bold">Analysis shown</div>
      <div role="radiogroup" aria-label="Analyses of this media" className="flex flex-col gap-1.5" data-testid="run-list">
        {(shown.length ? shown : run ? [run] : []).map((r) => {
          const on = r.id === (video.activeRun?.id ?? run?.id);
          return (
            <button key={r.id} role="radio" aria-checked={on} disabled={on || r.status !== 'COMPLETED'} onClick={() => activate(r.id)}
                    data-testid="run-item" data-active={on}
                    className={`flex justify-between gap-2 rounded-[10px] px-3 py-2.5 border text-left cursor-pointer disabled:cursor-default ${
                      on ? 'bg-acc-bg border-acc text-tx font-extrabold border-[1.5px]' : 'bg-ink border-edge text-tx2 font-semibold'}`}>
              <span>{label(r)} · ≥ {r.conf.toFixed(2)}</span>
              <span>{r.defectCount ?? (on ? video.defectCount : 0)} detections</span>
            </button>
          );
        })}
      </div>
      <div className="text-xs text-dim grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5" data-testid="analysis-record">
        {run ? (
          <>
            <span>Recorded at</span><span className="text-tx2 font-semibold" data-testid="record-conf">≥ {run.conf.toFixed(2)}</span>
            {run.triggerConf != null && <><span>Clip trigger</span><span className="text-def-tx font-semibold" data-testid="record-trigger">{run.triggerConf.toFixed(2)}</span></>}
            <span>Model</span><span className="text-tx2 truncate" title={run.model || ''}>{run.model || '—'}</span>
            <span>Frames</span><span className="text-tx2">{run.framesAnalyzed.toLocaleString()}{run.sampleFps ? ` @ ${run.sampleFps} fps` : ''} · {localTime(run.completedAt || run.createdAt)}</span>
            {tl.length > 1 && <><span>Trigger changes</span><span className="text-tx2">{tl.map(([t, c]) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')} → ${c.toFixed(2)}`).join(' · ')}</span></>}
          </>
        ) : <span className="col-span-2">No analysis recorded yet.</span>}
      </div>
      {pending ? (
        <div className="text-xs text-acc-tx bg-acc-bg border border-indigo-800 rounded-lg px-3 py-2" data-testid="pending-run">
          Re-analysis at {pending.conf.toFixed(2)} {pending.status.toLowerCase()} · {Math.round(pending.progress)}%
          <div className="mt-1.5 h-1 bg-ink rounded-full overflow-hidden"><div className="h-full bg-acc" style={{ width: `${pending.progress}%` }} /></div>
        </div>
      ) : video.status === 'COMPLETED' && !video.mediaOffline ? (
        <div className="flex flex-col gap-1.5 pt-1" data-testid="reanalyse">
          <span className="text-xs text-dim font-bold">Re-analyse (keeps both)</span>
          <div className="grid grid-cols-[repeat(4,minmax(0,1fr))_1.4fr] gap-1.5" data-testid="threshold-picker">
            {PRESETS.map((p) => (
              <button key={p} onClick={() => { setCustom(''); setNewConf(p); }} data-testid={`thr-${p}`}
                      className={`rounded-lg py-2 border font-bold cursor-pointer ${newConf === p && !custom ? 'bg-acc-bg border-acc text-tx' : 'bg-ink border-edge text-tx'}`}>
                {p.toFixed(2)}
              </button>
            ))}
            <input type="number" min={0.1} max={0.99} step={0.01} placeholder="custom" value={custom} aria-label="Custom threshold"
                   onChange={(e) => { setCustom(e.target.value); const v = parseFloat(e.target.value); setNewConf(v >= 0.1 && v <= 0.99 ? v : null); }}
                   className="w-full bg-ink border border-dashed border-[#3A4E63] text-tx rounded-lg px-2 py-2 font-bold outline-none focus:border-acc" />
          </div>
          {newConf != null && (
            <button onClick={reanalyse} disabled={busy} data-testid="reanalyse-btn" className={btn.primary}
                    title={newConf === run?.conf ? 'Same threshold as the analysis shown: useful with a newer model; both runs are kept' : undefined}>
              {busy ? 'Queuing…' : newConf === run?.conf ? `Re-analyse at ${newConf.toFixed(2)} again` : `Re-analyse at ${newConf.toFixed(2)}`}
            </button>
          )}
        </div>
      ) : video.mediaOffline ? <p className="text-xs text-warn">Media offline — locate the file in Batch to re-analyse.</p> : null}
    </Card>
  );
}

/** Zone filter labels without the trailing "Zone". */
export { zoneShort };
