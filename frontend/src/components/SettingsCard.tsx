import { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { AlertTriangle, ChevronDown, ChevronRight, Search, Settings2, X } from 'lucide-react';
import { getJSON, postJSON } from '../api';
import { btn, Card, CardTitle } from './shared';

/* Settings (System drawer): which config.toml is read, what is wrong in it, and every setting in use with a plain
 * explanation - so an operator tuning a site sees the effect of an edit without reading code. The text comes from
 * backend/config_spec.py, the same list config.example.toml and docs/CONFIG.md are generated from. */

type Source = 'default' | 'file' | 'env' | 'app';
interface SettingInfo {
  env: string; key: string; section: string; name: string; title: string; what: string; tune: string; unit: string;
  allowed: string; advanced: boolean; default: unknown; value: unknown; source: Source; problem: boolean;
}
interface Problem { key: string; level: 'error' | 'warning'; text: string }
interface ConfigInfo {
  file: string; exists: boolean; loaded: boolean; error: string | null; unknown: string[]; suggest?: Record<string, string>;
  problems?: Problem[]; sections?: { key: string; title: string; intro: string }[]; spec: SettingInfo[];
  database?: { main: string; sensors: string; dialect: string };
}

const SOURCE: Record<Source, { label: string; cls: string; title: string }> = {
  default: { label: 'default', cls: 'text-dim border-edge', title: 'Built-in default' },
  file: { label: 'config.toml', cls: 'text-acc-tx border-acc/40 bg-acc-bg', title: 'Set in config.toml' },
  env: { label: 'env', cls: 'text-pit-tx border-pit/40 bg-pit-bg', title: 'Set by an environment variable (wins over config.toml)' },
  app: { label: 'app', cls: 'text-ok border-ok/40', title: 'Chosen in the app (wins over config.toml)' },
};
const show = (v: unknown) => (v === '' || v == null ? '(empty)' : typeof v === 'string' ? v : JSON.stringify(v));

export function SettingsFileCard() {
  const [cfg, setCfg] = useState<ConfigInfo | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const load = () => getJSON<ConfigInfo>('/api/config').then(setCfg).catch(() => setCfg(null));
  useEffect(() => { load(); }, []);
  const create = async () => {
    try { const r = await postJSON<{ file: string }>('/api/config/create'); setMsg(`Created ${r.file}. Every setting in it is commented out (defaults) and explained; edit it, then restart the app.`); load(); }
    catch (e: any) { setMsg(e.message); }
  };
  const changed = cfg ? cfg.spec.filter((s) => s.source !== 'default') : [];
  const problems = cfg?.problems ?? [];
  return (
    <Card className="flex flex-col gap-2" testid="config-card">
      <CardTitle icon={<Settings2 className="w-3.5 h-3.5" />} title="Settings"
                 right={<span className={cfg?.error || problems.length ? 'text-warn' : cfg?.loaded ? 'text-ok' : 'text-dim'} data-testid="config-state">
                   {cfg?.error ? 'error' : cfg?.loaded ? (problems.length ? `${problems.length} problem${problems.length === 1 ? '' : 's'}` : 'loaded') : 'defaults'}</span>} />
      <p className="text-xs text-mut leading-relaxed -mt-0.5">
        This site's settings are in <code className="font-mono">config.toml</code> (thresholds, clip rule, paths, Gemini limits, port).
        Environment variables win over the file. Restart the app after editing.
      </p>
      <div className="text-xs font-mono text-tx2 break-all" title={cfg?.file}>{cfg?.file}</div>
      {cfg?.database && (
        <p className="text-xs text-tx2" data-testid="config-db">
          Database: <b>{cfg.database.dialect === 'sqlite' ? 'local SQLite file' : cfg.database.dialect}</b>
          <span className="font-mono text-dim break-all"> · {cfg.database.main}</span>
        </p>
      )}
      {cfg?.error && <p className="text-xs text-warn">The file could not be read: {cfg.error}. Every setting is at its default until it is fixed.</p>}
      {(problems.length > 0 || (cfg?.unknown.length ?? 0) > 0) && (
        <div className="bg-warn/10 border border-warn/40 rounded-lg px-3 py-2 text-xs flex flex-col gap-1" data-testid="config-problems">
          {problems.map((p, i) => <span key={i} className="text-warn"><AlertTriangle className="inline w-3.5 h-3.5 -mt-0.5 mr-1" />{p.text}</span>)}
          {cfg!.unknown.map((u) => (
            <span key={u} className="text-warn"><AlertTriangle className="inline w-3.5 h-3.5 -mt-0.5 mr-1" />
              <code className="font-mono">{u}</code> is not a setting and is ignored{cfg!.suggest?.[u] ? <> - did you mean <code className="font-mono font-bold">{cfg!.suggest[u]}</code>?</> : ''}</span>
          ))}
        </div>
      )}
      {cfg && (
        <p className="text-xs text-tx2" data-testid="config-summary">
          {changed.length ? <>{changed.length} changed from the default: {changed.map((s) => s.key).join(', ')}</> : 'All settings at their defaults.'}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        {cfg && <button onClick={() => setOpen(true)} className={`${btn.secondary} text-[13px]`} data-testid="config-open">All settings ({cfg.spec.length})</button>}
        {cfg && !cfg.exists && <button onClick={create} className={`${btn.secondary} text-[13px]`} data-testid="config-create">Create config.toml</button>}
      </div>
      {msg && <p className="text-xs text-tx2">{msg}</p>}
      {open && cfg && <SettingsDialog cfg={cfg} onClose={() => setOpen(false)} />}
    </Card>
  );
}

function SettingsDialog({ cfg, onClose }: { cfg: ConfigInfo; onClose: () => void }) {
  const [q, setQ] = useState('');
  const [onlyChanged, setOnlyChanged] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [advanced, setAdvanced] = useState(false);
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [onClose]);
  const needle = q.trim().toLowerCase();
  const rows = useMemo(() => cfg.spec.filter((s) => {
    if (onlyChanged && s.source === 'default' && !s.problem) return false;
    if (!advanced && s.advanced && s.source === 'default' && !s.problem && !needle) return false;
    if (needle && !`${s.title} ${s.key} ${s.env} ${s.what} ${s.tune}`.toLowerCase().includes(needle)) return false;
    return true;
  }), [cfg, needle, onlyChanged, advanced]);
  const sections = cfg.sections ?? [...new Set(cfg.spec.map((s) => s.section))].map((k) => ({ key: k, title: k, intro: '' }));
  const hiddenAdvanced = cfg.spec.filter((s) => s.advanced && s.source === 'default' && !s.problem).length;

  return createPortal(
    <div className="fixed inset-0 z-50 bg-black/60 flex items-start justify-center p-4 md:pt-12" onClick={onClose}>
      <div className="w-full max-w-4xl max-h-[88vh] flex flex-col bg-card border border-edge rounded-2xl shadow-2xl" onClick={(e) => e.stopPropagation()}
           role="dialog" aria-label="Settings in use" data-testid="config-dialog">
        <div className="px-5 pt-4 pb-3 border-b border-line flex flex-col gap-2.5">
          <div className="flex items-center gap-2">
            <Settings2 className="w-4 h-4 text-acc" />
            <span className="font-extrabold">Settings in use</span>
            <button onClick={onClose} aria-label="Close" className="ml-auto text-mut hover:text-tx cursor-pointer" data-testid="config-dialog-close"><X className="w-5 h-5" /></button>
          </div>
          <p className="text-xs text-mut leading-relaxed">
            To change one: open <code className="font-mono">config.toml</code> (<span className="font-mono break-all">{cfg.file}</span>), remove the <code className="font-mono"># </code> in
            front of its line under its <code className="font-mono">[section]</code>, edit the value, save and restart the app. Common situations (false clips,
            picture lag, 60 fps camera, cloud database …) are in <code className="font-mono">docs/CONFIG.md</code>.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <label className="relative flex-1 min-w-[200px]">
              <Search className="w-4 h-4 text-dim absolute left-2.5 top-1/2 -translate-y-1/2 pointer-events-none" />
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search: trigger, delay, gemini, port …" aria-label="Search settings"
                     className={`${btn.input} w-full pl-8 py-1.5 text-[13px]`} data-testid="config-search" />
            </label>
            <label className="flex items-center gap-1.5 text-[13px] text-tx2 cursor-pointer">
              <input type="checkbox" checked={onlyChanged} onChange={(e) => setOnlyChanged(e.target.checked)} data-testid="config-only-changed" /> Only changed
            </label>
            <label className="flex items-center gap-1.5 text-[13px] text-tx2 cursor-pointer">
              <input type="checkbox" checked={advanced} onChange={(e) => setAdvanced(e.target.checked)} /> Show rarely changed ({hiddenAdvanced})
            </label>
          </div>
        </div>
        <div className="overflow-auto px-2 pb-3">
          {sections.map((sec) => {
            const items = rows.filter((s) => s.section === sec.key);
            if (!items.length) return null;
            return (
              <section key={sec.key} className="mt-3" data-testid={`config-section-${sec.key}`}>
                <div className="px-3 pb-1">
                  <div className="text-[12px] font-extrabold uppercase tracking-wide text-tx2">{sec.title} <span className="font-mono normal-case font-normal text-dim">[{sec.key}]</span></div>
                  {sec.intro && <div className="text-xs text-dim mt-0.5">{sec.intro}</div>}
                </div>
                <ul>
                  {items.map((s) => {
                    const isOpen = expanded === s.key || !!needle && items.length <= 3;
                    const src = SOURCE[s.source] ?? SOURCE.default;
                    return (
                      <li key={s.key} className={`rounded-lg ${s.problem ? 'bg-warn/10' : ''}`} data-testid="config-row" data-key={s.key}>
                        <button onClick={() => setExpanded(isOpen ? null : s.key)} aria-expanded={isOpen}
                                className="w-full grid grid-cols-[16px_minmax(0,1fr)_auto_auto] items-center gap-x-3 px-3 py-2 text-left hover:bg-sel/60 rounded-lg cursor-pointer">
                          {isOpen ? <ChevronDown className="w-4 h-4 text-dim" /> : <ChevronRight className="w-4 h-4 text-dim" />}
                          <span className="min-w-0">
                            <span className="text-[13px] font-semibold">{s.title}</span>
                            <span className="ml-2 font-mono text-[12px] text-dim">{s.name}</span>
                          </span>
                          <span className={`font-mono text-[13px] max-w-[260px] truncate ${s.problem ? 'text-warn font-bold' : s.source !== 'default' ? 'text-tx font-bold' : 'text-tx2'}`}
                                title={show(s.value)} data-testid="config-value">{show(s.value)}{s.unit && !s.unit.startsWith('confidence') && typeof s.value === 'number' ? ` ${s.unit}` : ''}</span>
                          <span className={`text-[12px] font-bold border rounded px-1.5 py-0.5 whitespace-nowrap ${src.cls}`} title={src.title}>{src.label}</span>
                        </button>
                        {isOpen && (
                          <div className="pl-10 pr-4 pb-3 text-[12.5px] leading-relaxed flex flex-col gap-1.5" data-testid="config-detail">
                            <p className="text-tx2">{s.what}</p>
                            {s.tune && <p><span className="font-bold text-tx">When to change: </span><span className="text-tx2">{s.tune}</span></p>}
                            <p className="text-xs text-dim">
                              Default <code className="font-mono text-tx2">{show(s.default)}</code>
                              {s.allowed && <> · allowed <code className="font-mono text-tx2">{s.allowed}</code></>}
                              {s.unit && <> · {s.unit}</>}
                              {' '}· in config.toml: <code className="font-mono text-tx2">[{s.section}] {s.name} = …</code>
                              {' '}· environment variable <code className="font-mono text-tx2">{s.env}</code>
                            </p>
                          </div>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </section>
            );
          })}
          {rows.length === 0 && <p className="p-6 text-center text-[13px] text-mut">No setting matches.</p>}
        </div>
      </div>
    </div>,
    document.body,
  );
}
