import { useEffect, type ReactNode } from 'react';
import { Settings, X } from 'lucide-react';
import type { ModelStatus, MooringLine, RovStatus, SystemStats, Tab } from '../types';
import { Pill, RovPill } from './shared';

/** Anchor-and-line mark used as the app logo. */
export function Logo() {
  return (
    <span className="w-8 h-8 rounded-[9px] bg-[#123532] grid place-items-center shrink-0">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#3DD6C3" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
        <circle cx="12" cy="5" r="2.2" /><path d="M12 7.2V21M5 13a7 7 0 0 0 14 0M8 11H5M19 11h-3" />
      </svg>
    </span>
  );
}

const TABS: { id: Tab; label: string }[] = [
  { id: 'live', label: 'Live' },
  { id: 'review', label: 'Review' },
  { id: 'batch', label: 'Batch' },
  { id: 'reports', label: 'Reports' },
];

export function Header({
  activeTab, onTab, liveActive, batchBusy, lines, lineId, onLineChange, lineLocked, rov, model, stats, apiOnline, wsConnected, testOn, onOpenSystem,
}: {
  activeTab: Tab; onTab: (t: Tab) => void; liveActive: boolean; batchBusy: boolean;
  lines: MooringLine[]; lineId: string; onLineChange: (id: string) => void; lineLocked: boolean;
  rov: RovStatus | null; model: ModelStatus | null; stats: SystemStats | null;
  apiOnline: boolean | null; wsConnected: boolean; testOn: boolean; onOpenSystem: () => void;
}) {
  const a = stats?.arbiter;
  const gpuText = liveActive ? 'live first' : stats?.batch.currentId ? 'batch' : 'idle';
  return (
    <header className="min-h-[60px] px-5 py-2.5 flex flex-wrap items-center gap-x-[18px] gap-y-2 border-b border-line bg-bar">
      <div className="flex items-center gap-2.5">
        <Logo />
        <span className="font-extrabold text-[17px] tracking-[-0.01em]">AI Insights Digital Triplet Platform</span>
      </div>
      <nav aria-label="Workspace" className="flex gap-0.5 bg-card border border-line rounded-[11px] p-[3px]">
        {TABS.map((t) => (
          <button key={t.id} onClick={() => onTab(t.id)} data-testid={`tab-${t.id}`} aria-current={activeTab === t.id ? 'page' : undefined}
                  className={`px-4 py-[7px] rounded-lg flex items-center gap-2 cursor-pointer ${activeTab === t.id ? 'bg-sel text-tx font-bold' : 'text-mut font-semibold hover:text-tx'}`}>
            {t.id === 'live' && liveActive && <span className="w-2 h-2 rounded-full bg-def animate-live" />}
            {t.label}
            {t.id === 'batch' && batchBusy && <span className="w-2 h-2 rounded-full bg-acc animate-live" title="Batch jobs running" />}
          </button>
        ))}
      </nav>
      <label className="flex items-center gap-2 text-mut text-[13px] font-semibold">
        Line
        <select value={lineId} onChange={(e) => onLineChange(e.target.value)} aria-label="Mooring line" data-testid="line-select"
                disabled={lineLocked} title={lineLocked ? 'The line is fixed for the running dive' : ''}
                className="bg-card text-tx border border-edge rounded-[9px] px-2.5 py-[7px] font-semibold outline-none focus:border-acc disabled:opacity-60">
          {lines.map((l) => <option key={l.id} value={l.id}>{l.id} · {l.name.match(/\(([^)]+)\)/)?.[1] ?? l.name}</option>)}
        </select>
      </label>
      <div className="ml-auto flex flex-wrap items-center gap-2">
        {apiOnline === false ? <Pill tone="bad" testid="api-pill">Backend offline</Pill>
          : !wsConnected ? <Pill tone="warn" testid="api-pill" pulse>Reconnecting…</Pill> : null}
        {testOn && <Pill tone="test" dot={false} testid="test-pill">TEST MODE</Pill>}
        <RovPill state={rov?.state} rate={rov?.msgRateHz} simulated={rov?.simulated} />
        <Pill tone={model?.is_loaded ? 'ok' : model?.last_error ? 'bad' : 'mute'} testid="yolo-pill">
          {model?.is_loaded ? `YOLO ${model.model_path}` : model?.last_error ? 'YOLO error' : 'No model'}
        </Pill>
        {stats && (
          <Pill tone="mute" dot={false} testid="gpu-pill" title={`GPU: live ${Math.round(a?.liveSharePct ?? 0)}% · batch ${Math.round(a?.batchSharePct ?? 0)}% (last 10 s)`}>
            GPU
            <span className="inline-flex w-12 h-1.5 rounded-full overflow-hidden bg-ink">
              <span className="bg-def" style={{ width: `${a?.liveSharePct ?? 0}%` }} />
              <span className="bg-acc" style={{ width: `${a?.batchSharePct ?? 0}%` }} />
            </span>
            {gpuText}
          </Pill>
        )}
        <button onClick={onOpenSystem} aria-label="System settings" data-testid="open-system"
                className="w-[38px] h-[38px] rounded-[10px] bg-card border border-edge text-tx2 grid place-items-center cursor-pointer">
          <Settings className="w-[19px] h-[19px]" />
        </button>
      </div>
    </header>
  );
}

/** Right-hand drawer for things that are set up once, not used every minute. */
export function SystemDrawer({ open, onClose, children }: { open: boolean; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    if (!open) return;
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-30" data-testid="system-drawer">
      <div className="absolute inset-0 bg-black/45" onClick={onClose} />
      <aside aria-label="System" className="absolute right-0 top-0 h-full w-full max-w-[420px] bg-[#0E151E] border-l border-[#263445] flex flex-col animate-drawer">
        <div className="flex items-center px-[18px] py-4 border-b border-line">
          <span className="font-bold text-base">System</span>
          <button onClick={onClose} aria-label="Close" data-testid="close-system"
                  className="ml-auto w-[34px] h-[34px] rounded-lg border border-[#263445] text-tx2 grid place-items-center cursor-pointer"><X className="w-4 h-4" /></button>
        </div>
        <div className="flex-1 overflow-auto p-[18px] flex flex-col gap-3.5 stagger">{children}</div>
      </aside>
    </div>
  );
}
