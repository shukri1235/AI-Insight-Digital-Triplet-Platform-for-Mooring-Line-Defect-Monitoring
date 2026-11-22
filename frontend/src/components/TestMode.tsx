import { useEffect, useMemo, useState } from 'react';
import { FlaskConical, FolderOpen, Plus, Square, Trash2 } from 'lucide-react';
import { postJSON, putJSON } from '../api';
import type { ModelStatus, MooringLine, TestModeStatus, VideoItem } from '../types';
import { Card, CardTitle } from './shared';
import FileBrowser from './FileBrowser';

const TYPES = ['DEFORMATION', 'CORROSION'] as const;

/** TEST MODE: run the whole system without the BlueROV2 or a trained model.
 *  A chosen video is broadcast like the ROV camera, the BlueROV2 simulator feeds the real
 *  MAVLink link, and every record made meanwhile is tagged TEST. */
export function TestModeCard({ status, videos, model, line, liveActive, liveIsTest, onChanged, log }: {
  status: TestModeStatus | null;
  videos: VideoItem[];
  model: ModelStatus | null;
  line: MooringLine | undefined;
  liveActive: boolean;
  liveIsTest: boolean;
  onChanged: () => void;
  log: (m: string) => void;
}) {
  const on = !!status?.enabled;
  const [videoKey, setVideoKey] = useState('');         // "id:<videoId>" | "path:<abs path>"
  const [browser, setBrowser] = useState(false);
  const [target, setTarget] = useState('120.5');
  const [rate, setRate] = useState('2');
  const [standIn, setStandIn] = useState<{ cls: string; type: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [depth, setDepth] = useState<number | null>(null);

  const library = videos.filter((v) => v.sourceType !== 'INCIDENT_CLIP' && !v.missing);
  const genericClasses = useMemo(() => {
    const defects = new Set(Object.values(model?.defect_classes || {}));
    return Object.values(model?.class_names || {}).filter((c) => !defects.has(c));
  }, [model?.class_names, model?.defect_classes]);
  const seabed = line?.seabed_depth ?? 200;
  const modelLoaded = !!model?.is_loaded && Object.keys(model?.class_names || {}).length > 0;
  const realDefects = Object.values(model?.defect_classes || {});
  // a trained defect model (e.g. best.pt: deformation, pitting) has no generic class to stand in - and needs none
  const ownDefectModel = modelLoaded && genericClasses.length === 0 && realDefects.length > 0;
  const modelName = (model?.model_path || '').split(/[\\/]/).pop() || 'this model';

  useEffect(() => { if (on && status) { setDepth(status.targetDepth); setStandIn(Object.entries(status.standIn).map(([cls, type]) => ({ cls, type }))); } }, [on]); // eslint-disable-line

  // drag the simulated vehicle up/down (debounced)
  useEffect(() => {
    if (!on || depth == null || Math.abs(depth - (status?.targetDepth ?? depth)) < 0.05) return;
    const id = setTimeout(() => {
      postJSON('/api/test-mode/depth', { depth }).then(onChanged).catch((e) => setErr(e.message));
    }, 300);
    return () => clearTimeout(id);
  }, [depth]); // eslint-disable-line react-hooks/exhaustive-deps

  const mapping = () => Object.fromEntries(standIn.filter((r) => r.cls).map((r) => [r.cls, r.type]));
  const start = async () => {
    if (!videoKey) return;
    setBusy(true); setErr(null);
    try {
      const body: Record<string, unknown> = { target_depth: Number(target), descent_rate: Number(rate), stand_in: mapping() };
      if (videoKey.startsWith('id:')) body.video_id = videoKey.slice(3); else body.path = videoKey.slice(5);
      const st = await postJSON<TestModeStatus>('/api/test-mode/start', body);
      log(`TEST MODE ON: streaming "${st.video}" as the ROV camera, simulated BlueROV2 descending to ${st.targetDepth} m` +
          `${Object.keys(st.standIn).length ? ` · stand-in ${Object.entries(st.standIn).map(([k, v]) => `${k}→${v}`).join(', ')}` : ''}. Records are tagged TEST.`);
      setDepth(st.targetDepth);
      onChanged();
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };
  const stop = async () => {
    setBusy(true); setErr(null);
    try { await postJSON('/api/test-mode/stop'); log('TEST MODE OFF: simulated camera and ROV stopped.'); onChanged(); }
    catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };
  const applyStandIn = async () => {
    setErr(null);
    try {
      const data = await putJSON<TestModeStatus>('/api/test-mode/stand-in', { stand_in: mapping() });
      log(`TEST MODE: stand-in classes ${Object.keys(data.standIn).length ? Object.entries(data.standIn).map(([k, v]) => `${k}→${v}`).join(', ') : 'cleared'}.`);
      onChanged();
    } catch (e: any) { setErr(e.message); }
  };

  // Only shown for a generic model (e.g. yolo11n) that has no defect classes; a trained defect model needs none.
  const standInEditor = ownDefectModel ? null : (
    <div className="flex flex-col gap-2" data-testid="stand-in">
      <span className="text-xs uppercase font-bold tracking-wider text-slate-400">Stand-in defects (optional)</span>
      <p className="text-xs text-slate-500 -mt-1 leading-relaxed">
        For a generic model with no defect classes: treat one of its classes as a defect. Test Mode only.
      </p>
      {standIn.map((r, i) => (
        <div key={i} className="flex gap-1.5 items-center">
          <select value={r.cls} onChange={(e) => setStandIn(standIn.map((x, j) => (j === i ? { ...x, cls: e.target.value } : x)))}
                  data-testid="stand-in-class"
                  className="flex-1 min-w-0 bg-slate-950 border border-slate-700 text-xs font-mono text-slate-200 px-2 py-1.5 rounded outline-none">
            <option value="">class…</option>
            {genericClasses.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          <span className="text-slate-500 text-xs">as</span>
          <select value={r.type} onChange={(e) => setStandIn(standIn.map((x, j) => (j === i ? { ...x, type: e.target.value } : x)))}
                  data-testid="stand-in-type"
                  className="bg-slate-950 border border-slate-700 text-xs font-mono text-slate-200 px-2 py-1.5 rounded outline-none">
            {TYPES.map((t) => <option key={t} value={t}>{t === 'CORROSION' ? 'PITTING CORROSION' : 'DEFORMATION'}</option>)}
          </select>
          <button onClick={() => setStandIn(standIn.filter((_, j) => j !== i))} data-testid="stand-in-remove" className="text-slate-500 hover:text-rose-300 cursor-pointer"><Trash2 className="w-3.5 h-3.5" /></button>
        </div>
      ))}
      <div className="flex gap-2">
        <button onClick={() => setStandIn([...standIn, { cls: genericClasses[0] || '', type: 'DEFORMATION' }])} disabled={!genericClasses.length}
                data-testid="stand-in-add"
                title={!modelLoaded ? 'Load a model first (System → Model)' : ownDefectModel ? `${modelName} has no generic classes to stand in` : ''}
                className="flex items-center gap-1 text-xs font-bold uppercase text-slate-300 border border-slate-700 hover:border-amber-500 rounded px-2 py-1 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:border-slate-700">
          <Plus className="w-3 h-3" /> Add
        </button>
        {on && (
          <button onClick={applyStandIn} disabled={liveActive && liveIsTest} data-testid="stand-in-apply"
                  title={liveActive && liveIsTest ? 'Disconnect the test stream first — each dive records the mapping it used' : ''}
                  className="text-xs font-bold uppercase text-amber-300 border border-amber-700 rounded px-2 py-1 cursor-pointer disabled:opacity-40">
            Apply to next dive
          </button>
        )}
      </div>
      {!modelLoaded && <span className="text-xs text-slate-500" data-testid="stand-in-no-model">Load a model first to pick its classes.</span>}
      {modelLoaded && standIn.some((r) => r.cls && !genericClasses.includes(r.cls)) && (
        <span className="text-xs text-warn" data-testid="stand-in-stale">
          {standIn.filter((r) => r.cls && !genericClasses.includes(r.cls)).map((r) => r.cls).join(', ')} is not a class of {modelName} — remove it.
        </span>
      )}
    </div>
  );

  return (
    <Card className={`flex flex-col gap-3.5 ${on ? '!border-tst/70' : ''}`} testid="test-mode-card">
      {browser && <FileBrowser kind="video" title="Video for the simulated ROV camera" onClose={() => setBrowser(false)}
                               onPick={(p) => { setVideoKey(`path:${p}`); setBrowser(false); }} />}
      <CardTitle icon={<FlaskConical className="w-3.5 h-3.5" />} title="Test Mode"
                 right={<span data-testid="test-mode-state" className={`px-2 py-0.5 rounded-md ${on ? 'bg-tst text-on-tst' : 'text-dim'}`}>{on ? 'ON' : 'Off'}</span>} />
      {!on ? (
        <>
          <p className="text-xs text-mut leading-relaxed -mt-1">
            Stream a video as the <b className="text-slate-200">BlueROV2 camera</b> and simulate its <b className="text-slate-200">telemetry</b>.
            Everything made meanwhile is tagged <b className="text-amber-300">TEST</b> and kept out of the real records.
          </p>
          <div className="flex flex-col gap-1.5">
            <span className="text-xs uppercase font-bold tracking-wider text-slate-400">Camera video</span>
            <div className="flex gap-2">
              <select value={videoKey.startsWith('id:') ? videoKey : ''} onChange={(e) => setVideoKey(e.target.value)} data-testid="test-video"
                      className="flex-1 min-w-0 bg-slate-950 border border-slate-700 text-xs text-slate-200 px-2 py-2 rounded outline-none">
                <option value="">{videoKey.startsWith('path:') ? videoKey.slice(5).split(/[\\/]/).pop() : 'Choose from the vault…'}</option>
                {library.map((v) => <option key={v.id} value={`id:${v.id}`}>{v.name}</option>)}
              </select>
              <button onClick={() => setBrowser(true)} title="Browse this PC" data-testid="test-browse"
                      className="px-2.5 border border-slate-700 hover:border-amber-500 rounded text-slate-300 cursor-pointer"><FolderOpen className="w-4 h-4" /></button>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <label className="flex flex-col gap-1 text-xs uppercase font-bold tracking-wider text-slate-400">
              Dive to (m)
              <input type="number" min={0} max={seabed} step={0.5} value={target} onChange={(e) => setTarget(e.target.value)} data-testid="test-target"
                     className="bg-slate-950 border border-slate-700 text-sm font-mono text-slate-200 px-2 py-1.5 rounded outline-none normal-case" />
            </label>
            <label className="flex flex-col gap-1 text-xs uppercase font-bold tracking-wider text-slate-400">
              Descent (m/s)
              <input type="number" min={0.1} step={0.5} value={rate} onChange={(e) => setRate(e.target.value)} data-testid="test-rate"
                     className="bg-slate-950 border border-slate-700 text-sm font-mono text-slate-200 px-2 py-1.5 rounded outline-none normal-case" />
            </label>
          </div>
          {standInEditor}
          <button onClick={start} disabled={!videoKey || busy || (liveActive && !liveIsTest)} data-testid="test-start"
                  title={liveActive && !liveIsTest ? 'Disconnect the real live dive first' : ''}
                  className="py-2.5 bg-tst disabled:bg-btn disabled:text-dim text-on-tst font-bold text-xs uppercase tracking-widest rounded-lg flex items-center justify-center gap-2 cursor-pointer">
            <FlaskConical className="w-4 h-4" /> {busy ? 'Starting…' : 'Start Test Mode'}
          </button>
        </>
      ) : (
        <>
          <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[13px] font-mono">
            <span className="text-slate-500">Camera</span>
            <span className="text-slate-200 truncate" title={status?.videoPath || ''}>{status?.video}</span>
            <span className="text-slate-500">Stream</span>
            <span data-testid="test-streaming" className={status?.streaming ? 'text-emerald-300' : 'text-amber-300'}>{status?.streaming ? 'broadcasting (UDP)' : 'starting…'}</span>
            <span className="text-slate-500">Sim ROV</span>
            <span className="text-slate-200">{status?.simRunning ? `${status?.simDepth?.toFixed(1)} m → ${status?.targetDepth} m` : 'unavailable'}</span>
          </div>
          {status?.simRunning && (
            <div className="flex flex-col gap-1.5">
              <div className="flex justify-between text-xs uppercase font-bold tracking-wider text-slate-400">
                <span>Move the ROV to</span><span className="font-mono text-amber-300">{(depth ?? 0).toFixed(1)} m</span>
              </div>
              <input type="range" min={0} max={seabed} step={0.5} value={depth ?? 0} onChange={(e) => setDepth(parseFloat(e.target.value))}
                     data-testid="test-depth" className="w-full h-1.5 bg-slate-700 rounded-lg appearance-none cursor-pointer accent-amber-500" />
              <div className="flex justify-between text-[12px] font-mono text-slate-400">
                {(line?.segments || []).map((s) => <span key={s.name}>{s.name.split(' ')[0]} ≤{s.end_m.toFixed(0)}</span>)}
              </div>
            </div>
          )}
          {standInEditor}
          <button onClick={stop} disabled={busy} data-testid="test-stop"
                  className="py-2 border border-amber-700 text-amber-300 hover:bg-amber-950/40 font-bold text-xs uppercase tracking-widest rounded-lg flex items-center justify-center gap-2 cursor-pointer">
            <Square className="w-4 h-4" /> Stop Test Mode
          </button>
        </>
      )}
      {(err || status?.error) && <p className="text-xs text-rose-300 bg-rose-950/40 border border-rose-900 rounded px-3 py-2" data-testid="test-error">{err || status?.error}</p>}
    </Card>
  );
}
