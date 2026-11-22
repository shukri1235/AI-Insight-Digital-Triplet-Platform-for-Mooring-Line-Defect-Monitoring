import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { AlertTriangle, CircleCheck, Download, Lock, RefreshCw, Save, X } from 'lucide-react';
import { apiUrl, getJSON, localTime, postJSON } from '../api';
import type { AiError, AiStatus, Grade, LiveDive, MooringLine, Report, ReportJob, ReportSummary, ReportUsage, Tab, VideoItem, Visual } from '../types';
import { btn, Card } from './shared';
import { isBulkModel } from './Panels';
import { useTemplates } from './ReportTemplates';

/* =========================================================================
 * INSPECTION REPORT ASSISTANT
 * Gemini only writes prose about facts the backend computed (events, depths,
 * segments, thresholds) and the key frames it is shown. Every claim is checked
 * in code; a report that fails the check is marked NEEDS_REVIEW. Nothing is
 * final until an engineer approves it.
 * ========================================================================= */

/** Progress of a report being drafted in the background: stage, bar, elapsed time. */
export function JobProgress({ job, compact = false }: { job: ReportJob; compact?: boolean }) {
  const pct = Math.round((job.progress || 0) * 100);
  const typical = job.typicalAiSec && job.useAi ? ` · usually ~${job.typicalAiSec} s` : '';
  return (
    <div className="flex flex-col gap-1" data-testid="report-progress" data-stage={job.stage} data-pct={pct}>
      <div className="flex items-center gap-2 text-xs">
        <span className={`font-semibold ${job.status === 'error' ? 'text-def-tx' : 'text-tx2'} truncate`}>
          {job.status === 'error' ? job.error : job.label}{job.status === 'running' ? '…' : ''}
        </span>
        <span className="ml-auto font-mono text-dim whitespace-nowrap">{job.status === 'running' ? `${pct}% · ` : ''}{Math.round(job.elapsedSec)} s{!compact && job.status === 'running' ? typical : ''}</span>
      </div>
      <div className="h-1.5 bg-ink rounded-full overflow-hidden" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}
           aria-label="Report progress">
        <div className={`h-full rounded-full transition-[width] duration-500 ${job.status === 'error' ? 'bg-def' : 'bg-acc'} ${job.status === 'running' ? 'animate-pulse' : ''}`}
             style={{ width: `${Math.max(3, pct)}%` }} />
      </div>
    </div>
  );
}

const JOB_STEPS: { stage: ReportJob['stage'][]; label: string }[] = [
  { stage: ['queued', 'facts'], label: 'Facts' },
  { stage: ['keyframes'], label: 'Key frames' },
  { stage: ['ai', 'retry', 'rules'], label: 'Draft' },
  { stage: ['check', 'save'], label: 'Check' },
];

export const GRADE_CLS: Record<string, string> = {
  ACCEPTABLE: 'bg-emerald-950/60 border-emerald-700 text-emerald-300',
  MONITOR: 'bg-amber-950/60 border-amber-700 text-amber-300',
  REPAIR: 'bg-orange-950/60 border-orange-700 text-orange-300',
  REPLACE: 'bg-rose-950/70 border-rose-700 text-rose-300',
};
const STATUS_CLS: Record<string, string> = {
  DRAFT: 'text-slate-300 border-slate-600', EDITED: 'text-indigo-300 border-indigo-700',
  NEEDS_REVIEW: 'text-amber-300 border-amber-700', APPROVED: 'text-emerald-300 border-emerald-700',
};
const GRADES: Grade[] = ['ACCEPTABLE', 'MONITOR', 'REPAIR', 'REPLACE'];
const VISUALS: Visual[] = ['CONFIRMED', 'UNCERTAIN', 'NOT_VISIBLE', 'LIKELY_FALSE_POSITIVE', 'NOT_ASSESSED'];
const human = (s: string) => s.replace(/_/g, ' ').toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
const mmss = (t: number) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`;
const fmtUsage = (u: ReportUsage) =>
  `${u.input.toLocaleString()} in · ${u.output.toLocaleString()} out · ${u.thinking.toLocaleString()} thinking` +
  ` = ${u.total.toLocaleString()} tokens (${u.requests} request${u.requests === 1 ? '' : 's'}, ${u.images} images)`;

const prettyModel = (m: string) => m.split(/[-_]/).map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');

interface Scope { type: 'INCIDENT' | 'DIVE' | 'SEGMENT'; ref: string; label: string }

export interface ReportPanelProps {
  activeTab: Tab; lineId: string; segment: string; lines: MooringLine[]; videos: VideoItem[]; reviewVideo: VideoItem | null;
  liveSessionId: string | null; latestClipId: string | null;
  onCite: (videoId: string, t: number) => void; log: (m: string) => void;
  /** "TYPE:ref" to select first (e.g. the incident whose Report button was pressed) */
  initialScope?: string | null;
  /** label for initialScope when it is not one of the usual choices (e.g. a live dive opened from its dive log) */
  initialScopeLabel?: string | null;
  bare?: boolean;
}

/** The report assistant in a dialog (Live alerts, Batch registry). */
export function ReportDialog({ onClose, ...props }: ReportPanelProps & { onClose: () => void }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [onClose]);
  return createPortal(
    <div className="fixed inset-0 z-40 bg-black/60 flex items-start justify-center p-4 md:pt-24" onClick={onClose}>
      <div className="w-full max-w-md animate-rise" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Inspection report" data-testid="report-dialog">
        <Card className="flex flex-col gap-3 shadow-2xl">
          <div className="flex items-center">
            <span className="font-extrabold">Inspection report</span>
            <button onClick={onClose} aria-label="Close" data-testid="close-report-dialog" className="ml-auto text-mut hover:text-tx cursor-pointer"><X className="w-5 h-5" /></button>
          </div>
          <ReportPanel {...props} bare />
        </Card>
      </div>
    </div>,
    document.body,
  );
}

export function ReportPanel({ activeTab, lineId, segment, lines, videos, reviewVideo, liveSessionId, latestClipId, onCite, log, initialScope = null, initialScopeLabel = null, bare = false }: ReportPanelProps) {
  const [ai, setAi] = useState<AiStatus | null>(null);
  const [aiNote, setAiNote] = useState<AiError | null>(null);
  const [useAi, setUseAi] = useState(true);
  const [cooldown, setCooldown] = useState(0);
  const [busy, setBusy] = useState(false);
  const [job, setJob] = useState<ReportJob | null>(null);
  const [list, setList] = useState<ReportSummary[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [scopeKey, setScopeKey] = useState('');
  const [dives, setDives] = useState<LiveDive[]>([]);
  // finished live dives stay reportable after the stream stops
  useEffect(() => {
    if (activeTab !== 'live') return;
    getJSON<LiveDive[]>('/api/live/dives?limit=6').then(setDives).catch(() => setDives([]));
  }, [activeTab, liveSessionId, latestClipId]);

  useEffect(() => {
    getJSON<AiStatus>('/api/ai/status')
      .then((s) => { setAi(s); if (s.cooldownRemaining > 0) setCooldown(Math.ceil(s.cooldownRemaining)); })
      .catch(() => setAi({ configured: false, model: 'Gemini', cooldownRemaining: 0 }));
  }, []);
  useEffect(() => {
    if (cooldown <= 0) return;
    const id = setTimeout(() => setCooldown((c) => c - 1), 1000);
    return () => clearTimeout(id);
  }, [cooldown]);

  const scopes = useMemo<Scope[]>(() => {
    const out: Scope[] = [];
    const name = (id: string) => videos.find((v) => v.id === id)?.name || id.slice(0, 8);
    if (activeTab === 'review' && reviewVideo) {
      out.push(reviewVideo.sourceType === 'INCIDENT_CLIP'
        ? { type: 'INCIDENT', ref: reviewVideo.id, label: `Incident · ${reviewVideo.name}` }
        : { type: 'DIVE', ref: reviewVideo.id, label: `Dive · ${reviewVideo.name}` });
      if (reviewVideo.sourceType === 'INCIDENT_CLIP' && reviewVideo.parentId)
        out.push({ type: 'DIVE', ref: reviewVideo.parentId, label: 'Whole live dive (all its clips)' });
    }
    if (activeTab === 'live') {
      if (latestClipId) out.push({ type: 'INCIDENT', ref: latestClipId, label: `Latest incident · ${name(latestClipId)}` });
      if (liveSessionId) out.push({ type: 'DIVE', ref: liveSessionId, label: 'This live dive so far (snapshot at click)' });
      dives.filter((d) => d.id !== liveSessionId && d.status !== 'LIVE').slice(0, 3).forEach((d) => out.push({
        type: 'DIVE', ref: d.id,
        label: `Finished dive · ${d.startedAt ?? d.name} · ${mmss(d.durationSec || 0)} · ${d.defectCount} det.${d.clips ? ` · ${d.clips} clip${d.clips === 1 ? '' : 's'}` : ''}${d.isTest ? ' · TEST' : ''}`,
      }));
    }
    if (activeTab === 'reports') {
      const byNew = (a: VideoItem, b: VideoItem) => (b.createdTs ?? 0) - (a.createdTs ?? 0);
      videos.filter((v) => v.sourceType !== 'INCIDENT_CLIP' && v.status === 'COMPLETED').sort(byNew).slice(0, 8)
        .forEach((v) => out.push({ type: 'DIVE', ref: v.id, label: `Dive · ${v.name}` }));
      videos.filter((v) => v.sourceType === 'INCIDENT_CLIP').sort(byNew).slice(0, 8)
        .forEach((v) => out.push({ type: 'INCIDENT', ref: v.id, label: `Incident · ${v.name}` }));
    }
    if (activeTab === 'batch') {
      videos.filter((v) => v.sourceType !== 'INCIDENT_CLIP' && v.status === 'COMPLETED').slice(0, 5)
        .forEach((v) => out.push({ type: 'DIVE', ref: v.id, label: `Dive · ${v.name}` }));
    }
    const zones = segment === 'ALL' ? (lines.find((l) => l.id === lineId)?.segments.map((x) => x.name) || []) : [segment];
    zones.forEach((z) => out.push({ type: 'SEGMENT', ref: `${lineId}|${z}`, label: `Segment history · ${lineId} / ${z}` }));
    if (initialScope && !out.some((s) => `${s.type}:${s.ref}` === initialScope)) {
      const [type, ...rest] = initialScope.split(':');
      const ref = rest.join(':');
      if (type === 'INCIDENT' || type === 'DIVE' || type === 'SEGMENT')
        out.unshift({ type, ref, label: initialScopeLabel ? initialScopeLabel : type === 'INCIDENT' ? `Incident · ${name(ref)}` : type === 'DIVE' ? `Dive · ${name(ref)}` : `Segment history · ${ref.replace('|', ' / ')}` });
    }
    return out;
  }, [activeTab, reviewVideo, latestClipId, liveSessionId, videos, lineId, segment, lines, dives, initialScope, initialScopeLabel]);
  const scope = scopes.find((s) => `${s.type}:${s.ref}` === scopeKey) || scopes[0];
  // The most specific scope for what the operator is looking at is the default; a choice
  // never carries over to another tab or another media file.
  const firstKey = initialScope && scopes.some((s) => `${s.type}:${s.ref}` === initialScope) ? initialScope
    : scopes[0] ? `${scopes[0].type}:${scopes[0].ref}` : '';
  useEffect(() => { setScopeKey(firstKey); }, [activeTab, reviewVideo?.id, latestClipId, initialScope]); // eslint-disable-line
  useEffect(() => { if (!scopes.some((s) => `${s.type}:${s.ref}` === scopeKey)) setScopeKey(firstKey); }, [scopes]); // eslint-disable-line

  const refreshList = () => {
    if (!scope) return;
    getJSON<ReportSummary[]>(`/api/reports?scope_type=${scope.type}&scope_ref=${encodeURIComponent(scope.ref)}`).then(setList).catch(() => setList([]));
  };
  useEffect(refreshList, [scope?.type, scope?.ref]); // eslint-disable-line react-hooks/exhaustive-deps

  const aiOn = !!ai?.configured && useAi;
  // The report is drafted by a background job; poll it for stage + progress until it is done
  const finish = async (j: ReportJob) => {
    try {
      const r = await getJSON<Report>(`/api/reports/${j.reportId}`);
      log(`REPORT: ${r.generator === 'RULE_BASED' ? 'rule-based draft' : r.generator} → ${r.conditionGrade} (${r.status === 'NEEDS_REVIEW' ? 'failed grounding check — needs review' : 'grounded'})` +
          `${r.content.usage ? ` · ${fmtUsage(r.content.usage)}` : ' · no AI tokens used'} · ${Math.round(j.elapsedSec)} s.`);
      if (j.useAi) getJSON<{ cooldownRemaining: number }>('/api/ai/status').then((s) => setCooldown(Math.ceil(s.cooldownRemaining))).catch(() => setCooldown(30));
      if (r.content.ai_error) {
        setAiNote(r.content.ai_error);
        log(`AI WARN: Gemini failed (${r.content.ai_error.message.slice(0, 120)}) — made a rule-based draft instead. Open it and use Redraft with Gemini later.`);
      } else setAiNote(null);
      if (r.content.fallback) log(`AI WARN: ${prettyModel(r.content.fallback.from)} was ${r.content.fallback.reason} — ${prettyModel(r.content.fallback.to)} drafted the report.`);
      setOpenId(r.id);
      refreshList();
    } catch (e: any) { log(`REPORT ERROR: ${e.message}`); }
  };
  useEffect(() => {
    if (!job || job.status !== 'running') return;
    const id = setInterval(async () => {
      try {
        const j = await getJSON<ReportJob>(`/api/reports/jobs/${job.id}`);
        setJob(j);
        if (j.status === 'done') { setBusy(false); finish(j); setTimeout(() => setJob((cur) => (cur?.id === j.id ? null : cur)), 4000); }
        if (j.status === 'error') { setBusy(false); log(`REPORT ERROR: ${j.error}`); }
      } catch (e: any) { setBusy(false); setJob(null); log(`REPORT ERROR: lost track of the report job (${e.message})`); }
    }, 600);
    return () => clearInterval(id);
  }, [job?.id, job?.status]); // eslint-disable-line react-hooks/exhaustive-deps
  // A report started elsewhere (another tab, before a page reload) keeps showing its progress here
  useEffect(() => {
    getJSON<ReportJob[]>('/api/reports/jobs').then((js) => {
      const running = js.find((j) => j.status === 'running');
      if (running) { setJob(running); setBusy(true); }
    }).catch(() => {});
  }, []);
  const generate = async () => {
    if (!scope || busy || (aiOn && cooldown > 0)) return;
    setBusy(true);
    try {
      const j = await postJSON<ReportJob>('/api/reports/jobs', { scope_type: scope.type, scope_ref: scope.ref, use_ai: aiOn });
      setJob(j);
      log(`REPORT: drafting ${scope.label} with ${aiOn ? prettyModel(ai?.model || 'Gemini') : 'the rules'} — running in the background.`);
    } catch (e: any) {
      setBusy(false);
      if (e.status === 429) { setCooldown(e.retryAfter || 30); log(`[Token saver] AI cooldown — retry in ${e.retryAfter || 30}s.`); }
      else log(`REPORT ERROR: ${e.message}`);
    }
  };

  const inner = (
    <>
      <div className="flex justify-between items-center gap-2">
        {!bare && <span className="font-extrabold">Inspection report</span>}
        <span data-testid="ai-mode" data-bulk={aiOn && isBulkModel(ai?.model) ? '1' : '0'}
              title={aiOn && isBulkModel(ai?.model) ? 'Bulk model in use (cheaper, weaker visual check). Switch back in System → Gemini report assistant.' : undefined}
              className={`${bare ? '' : 'ml-auto'} text-xs font-bold px-2 py-0.5 rounded-full ${!aiOn ? 'bg-btn text-mut' : isBulkModel(ai?.model) ? 'bg-[#33270C] text-warn' : 'bg-acc-bg text-acc-tx'}`}>
          {aiOn ? `${prettyModel(ai?.model || 'Gemini')}${isBulkModel(ai?.model) ? ' · bulk' : ''}` : 'Rule-based'}
        </span>
        <button onClick={() => ai?.configured && setUseAi(!useAi)} data-testid="toggle-ai"
                title={ai?.configured ? 'Switch between Gemini and the rule-based draft (no AI)' : 'GEMINI_API_KEY is not set on the backend — rule-based drafts only'}
                className={`${bare ? 'ml-auto' : ''} text-xs font-semibold text-mut underline decoration-dotted cursor-pointer`}>
          {aiOn ? 'use rules instead' : ai?.configured ? 'use AI' : 'AI not set up'}
        </button>
      </div>
      <label className="flex flex-col gap-1.5 text-xs text-dim font-bold">Report on
        <select value={scope ? `${scope.type}:${scope.ref}` : ''} onChange={(e) => setScopeKey(e.target.value)} data-testid="report-scope"
                className={`${btn.input} text-[13px] font-semibold`}>
          {scopes.map((s) => <option key={`${s.type}:${s.ref}`} value={`${s.type}:${s.ref}`}>{s.label}</option>)}
        </select>
      </label>
      <button onClick={generate} disabled={!scope || busy || (aiOn && cooldown > 0)} data-testid="generate-report" className={btn.primary}>
        {busy ? `Drafting… ${Math.round((job?.progress || 0) * 100)}%` : aiOn && cooldown > 0 ? `AI cooldown (${cooldown}s)` : 'Generate draft report'}
      </button>
      {job && (
        <div className="flex flex-col gap-1.5 rounded-lg bg-ink border border-line px-3 py-2.5">
          <div className="flex gap-1">
            {JOB_STEPS.map((st, i) => {
              const at = JOB_STEPS.findIndex((x) => x.stage.includes(job.stage));
              const done = job.status === 'done' || (at >= 0 && i < at);
              const now = job.status === 'running' && i === at;
              return (
                <span key={st.label} className={`flex-1 text-center text-[12px] font-bold uppercase tracking-wide rounded px-1 py-0.5
                  ${done ? 'bg-acc-bg text-acc-tx' : now ? 'bg-sel text-tx' : 'text-dim'}`}>{st.label}</span>
              );
            })}
          </div>
          <JobProgress job={job} />
          {job.status === 'running' && <span className="text-xs text-dim">You can keep working — the draft opens when it is ready.</span>}
        </div>
      )}
      <p className="text-xs text-mut leading-relaxed">
        {aiOn ? 'Gemini sees only recorded facts and key frames; every claim is checked against the database.' : 'Drafted from the recorded facts by fixed rules (no AI tokens).'} You approve the grade.
      </p>
      {(aiNote || (aiOn && ai?.lastError)) && <AiErrorNote err={(aiNote || ai?.lastError)!} fallback={!!aiNote} />}
      {ai && !ai.configured && (
        <p className="text-xs text-dim" data-testid="ai-not-set">
          Gemini is off: no <code className="font-mono">GEMINI_API_KEY</code> found. See System (gear) → Gemini report assistant for how to add it.
        </p>
      )}
      {list.length > 0 && (
        <div className="flex flex-col gap-1 border-t border-line pt-2.5" data-testid="report-list">
          {list.slice(0, 5).map((r) => (
            <button key={r.id} onClick={() => setOpenId(r.id)} data-testid="report-item"
                    className="flex items-center gap-2 py-1.5 text-left text-[13px] rounded-md hover:bg-sel px-1 -mx-1 cursor-pointer">
              <span className={`px-2 py-0.5 rounded-md border text-[12px] font-extrabold ${GRADE_CLS[r.conditionGrade] || ''}`}>{r.conditionGrade}</span>
              <span className={r.status === 'APPROVED' ? 'font-semibold' : 'text-tx2'}>
                {human(r.status)}{r.engineer ? ` · ${r.engineer}` : ''}
              </span>
              <span className="ml-auto text-dim text-xs whitespace-nowrap">{r.generator === 'RULE_BASED' ? 'rules' : 'AI'} · {localTime(r.createdAt, false).slice(6)}</span>
            </button>
          ))}
        </div>
      )}
      {openId && <ReportModal id={openId} onClose={() => { setOpenId(null); refreshList(); }}
                              onCite={(v, t) => { setOpenId(null); onCite(v, t); }} log={log} />}
    </>
  );
  if (bare) return <div className="flex flex-col gap-2.5" data-testid="report-panel">{inner}</div>;
  return <Card className="flex flex-col gap-2.5" testid="report-panel">{inner}</Card>;
}

/** Why Gemini failed + what to do. */
export function AiErrorNote({ err, fallback }: { err: AiError; fallback?: boolean }) {
  return (
    <div className="text-xs rounded-lg bg-[#33270C] text-warn px-3 py-2 flex flex-col gap-1" data-testid="ai-error">
      <span className="font-bold">{fallback ? 'Gemini failed — this draft was made by the rules instead.' : 'Last Gemini request failed.'}</span>
      <span className="text-tx2">{err.hint}</span>
      <details className="text-dim"><summary className="cursor-pointer">Google's message</summary><span className="font-mono break-words">{err.message}</span></details>
    </div>
  );
}

const fmtDur = (sec: number | undefined) => {
  if (sec == null) return '—';
  if (sec < 60) return `${sec < 10 ? sec.toFixed(1) : Math.round(sec)} s`;
  return `${Math.floor(sec / 60)} min ${String(Math.round(sec % 60)).padStart(2, '0')} s`;
};

/** A textarea that grows with its text (no inner scroll bar); read-only reports render as plain text. */
function AutoText({ value, onChange, locked, className = '', minRows = 2, testid, placeholder }: {
  value: string; onChange: (v: string) => void; locked: boolean; className?: string; minRows?: number; testid?: string; placeholder?: string;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${el.scrollHeight + 2}px`;
  }, [value, locked]);
  if (locked) {
    return value.trim()
      ? <p className={`whitespace-pre-wrap ${className}`} data-testid={testid}>{value}</p>
      : <p className="text-dim italic text-[13px]" data-testid={testid}>—</p>;
  }
  return (
    <textarea ref={ref} value={value} rows={minRows} onChange={(e) => onChange(e.target.value)} data-testid={testid} placeholder={placeholder}
              className={`w-full resize-none overflow-hidden bg-ink border border-edge rounded-lg px-3 py-2.5 outline-none focus:border-acc ${className}`} />
  );
}

function Meta({ label, children, testid, tone = '' }: { label: string; children: ReactNode; testid?: string; tone?: string }) {
  return (
    <div className="min-w-0" data-testid={testid}>
      <div className="text-[12px] font-bold uppercase tracking-[.08em] text-dim">{label}</div>
      <div className={`text-[13px] font-semibold mt-0.5 ${tone || 'text-tx2'}`}>{children}</div>
    </div>
  );
}

const SectionTitle = ({ children, right }: { children: ReactNode; right?: ReactNode }) => (
  <div className="flex items-baseline gap-3 mb-2.5">
    <h4 className="text-[12px] font-extrabold uppercase tracking-[.1em] text-mut">{children}</h4>
    {right && <span className="ml-auto text-xs text-dim">{right}</span>}
  </div>
);

const PRIORITY_CLS: Record<string, string> = {
  IMMEDIATE: 'bg-def text-on-def', SHORT_TERM: 'bg-[#33270C] text-warn', LONG_TERM: 'bg-btn text-tx2', ROUTINE: 'bg-btn text-tx2',
};

export function ReportModal({ id, onClose, onCite, log }: { id: string; onClose: () => void; onCite: (videoId: string, t: number) => void; log: (m: string) => void }) {
  const [r, setR] = useState<Report | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [edit, setEdit] = useState<{ summary: string; grade: Grade; rationale: string; notes: string; verdicts: Record<string, Visual> } | null>(null);
  const [dirty, setDirty] = useState(false);
  const [engineer, setEngineer] = useState(() => { try { return localStorage.getItem('engineer-name') || ''; } catch { return ''; } });
  const [redraft, setRedraft] = useState<ReportJob | null>(null);
  const { list: tpls } = useTemplates();
  const [pdfTpl, setPdfTpl] = useState('');
  const [redraftMsg, setRedraftMsg] = useState<string | null>(null);

  const load = (rep: Report) => {
    setR(rep);
    setEdit({ summary: rep.content.summary, grade: rep.content.condition_grade, rationale: rep.content.grade_rationale,
              notes: rep.content.engineer_notes || '', verdicts: Object.fromEntries(rep.content.findings.map((f) => [f.event_ref, f.visual_assessment])) });
    setDirty(false);
  };
  useEffect(() => { getJSON<Report>(`/api/reports/${id}`).then(load).catch((e) => setErr(e.message)); }, [id]);
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [onClose]);

  const locked = r?.status === 'APPROVED';
  const upd = (patch: Partial<NonNullable<typeof edit>>) => { setEdit((e) => (e ? { ...e, ...patch } : e)); setDirty(true); };
  const save = async () => {
    if (!edit || !r) return;
    try {
      const rep = await getJSON<Report>(`/api/reports/${r.id}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ summary: edit.summary, condition_grade: edit.grade, grade_rationale: edit.rationale, engineer_notes: edit.notes,
                               findings: Object.entries(edit.verdicts).map(([event_ref, visual_assessment]) => ({ event_ref, visual_assessment })) }),
      });
      load(rep); log('REPORT: engineer edits saved.');
    } catch (e: any) { setErr(e.message); }
  };
  const approve = async () => {
    if (!r || !engineer.trim()) return;
    try {
      if (dirty) await save();
      try { localStorage.setItem('engineer-name', engineer.trim()); } catch { /* ignore */ }
      load(await postJSON<Report>(`/api/reports/${r.id}/approve`, { engineer: engineer.trim() }));
      log(`REPORT: approved by ${engineer.trim()} — now locked.`);
    } catch (e: any) { setErr(e.message); }
  };

  const startRedraft = async () => {
    if (!r || redraft?.status === 'running') return;
    setRedraftMsg(null);
    try {
      if (dirty) await save();                         // keep the engineer's notes; the draft text is replaced
      const j = await postJSON<ReportJob>(`/api/reports/${r.id}/redraft`);
      setRedraft(j);
      log(`REPORT: asking Gemini again for ${c?.scope?.name || 'this report'} (redraft in place).`);
    } catch (e: any) {
      setRedraftMsg(e.status === 429 ? `Gemini cooldown — try again in ${e.retryAfter || 30} s.` : e.message);
    }
  };
  useEffect(() => {
    if (!redraft || redraft.status !== 'running') return;
    const t = setInterval(async () => {
      try {
        const j = await getJSON<ReportJob>(`/api/reports/jobs/${redraft.id}`);
        setRedraft(j);
        if (j.status === 'done') {
          const rep = await getJSON<Report>(`/api/reports/${id}`);
          load(rep);
          setRedraft(null);
          log(`REPORT: redrafted by ${prettyModel(rep.generator)} → ${rep.conditionGrade}${rep.content.usage ? ` · ${fmtUsage(rep.content.usage)}` : ''} · ${Math.round(j.elapsedSec)} s.`);
        } else if (j.status === 'error') {
          setRedraftMsg(j.error || 'Redraft failed.');
          log(`AI WARN: redraft failed — ${j.error}`);
        }
      } catch (e: any) { setRedraftMsg(e.message); setRedraft(null); }
    }, 700);
    return () => clearInterval(t);
  }, [redraft?.id, redraft?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const kf = useMemo(() => Object.fromEntries((r?.content.keyframes || []).map((k) => [k.event_ref, k])), [r]);
  const c = r?.content;
  const second = c?.second_opinion || {};
  const tm = (c?.timings || {}) as Record<string, number>;
  const aiSec = typeof tm.ai === 'number' ? tm.ai : undefined;
  const drafter = r ? (r.generator === 'RULE_BASED' ? 'Rules (no AI)' : prettyModel(r.generator)) : '';

  return createPortal(
    <div className="fixed inset-0 z-50 bg-black/75 flex items-start justify-center p-2 md:p-6" onClick={onClose}>
      <div className="w-full max-w-5xl max-h-[calc(100vh-1rem)] md:max-h-[calc(100vh-3rem)] bg-card border border-edge rounded-2xl flex flex-col shadow-2xl animate-rise font-log"
           onClick={(e) => e.stopPropagation()} data-testid="report-modal" role="dialog" aria-label="Inspection report">
        {/* ---------- header ---------- */}
        <div className="px-6 pt-5 pb-4 border-b border-line flex flex-col gap-4">
          <div className="flex items-start gap-3">
            <div className="min-w-0 flex-1">
              <div className="text-xs font-bold uppercase tracking-[.1em] text-dim font-sans">
                {r ? `${r.scopeType.toLowerCase()} report` : 'Report'}{c?.test_data ? ' · test data' : ''}
              </div>
              <h3 className="text-[19px] font-extrabold text-tx font-sans leading-snug break-words mt-0.5">
                {c?.scope?.name || r?.title || 'Report'}
              </h3>
            </div>
            {c?.test_data && <span className="shrink-0 text-[12px] font-extrabold bg-tst text-on-tst rounded-md px-2 py-1 font-sans">TEST</span>}
            {r && <span data-testid="report-status" className={`shrink-0 px-2.5 py-1 rounded-md border text-[12px] font-extrabold uppercase font-sans ${STATUS_CLS[r.status]}`}>{human(r.status)}</span>}
            <button onClick={onClose} data-testid="close-report" aria-label="Close" className="shrink-0 text-mut hover:text-tx cursor-pointer p-1"><X className="w-5 h-5" /></button>
          </div>
          {r && c && (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-3">
              <Meta label="Drafted by">{drafter}{c.thinking ? <span className="text-dim font-normal"> · {c.thinking} thinking</span> : null}</Meta>
              <Meta label="Created">{localTime(r.createdAt)}{c.redrafts ? <span className="text-dim font-normal"> · redrafted{c.redrafts > 1 ? ` ×${c.redrafts}` : ''}</span> : null}</Meta>
              <Meta label="Analysis">{c.analysis ? `${c.analysis.kind.toLowerCase()} · ≥ ${c.analysis.threshold.toFixed(2)}${c.analysis.trigger_threshold != null ? ` · trigger ${c.analysis.trigger_threshold.toFixed(2)}` : ''}` : '—'}</Meta>
              <Meta label="ROV sensors">{c.sensors ? `${c.sensors.source}${c.sensors.samples != null ? ` · ${c.sensors.samples} readings` : ''}` : '—'}</Meta>
              {c.scope?.coverage && <Meta label="Covers" testid="report-coverage" tone={c.scope.coverage.live ? 'text-warn' : ''}>{c.scope.coverage.text}</Meta>}
              <Meta label="Time taken" testid="report-time">
                {fmtDur(typeof tm.total === 'number' ? tm.total : undefined)}
                {aiSec != null && <span className="text-dim font-normal"> · Gemini {fmtDur(aiSec)}{tm.ai_failed ? ', no draft' : ''}{c.usage?.requests && c.usage.requests > 1 ? ` (${c.usage.requests} requests)` : ''}{(c.ai_attempts?.length ?? 0) > 0 && !tm.ai_failed && c.ai_attempts ? ` incl. ${c.ai_attempts.length} failed attempt${c.ai_attempts.length > 1 ? 's' : ''}` : ''}</span>}
              </Meta>
              <Meta label="AI usage" testid="report-usage">
                <span title={c.usage?.perRequest?.map((u, i) => `request ${i + 1}: ${u.input} in / ${u.output} out / ${u.thinking} thinking${u.seconds != null ? ` · ${u.seconds} s` : ''}`).join('\n')}>
                  {c.usage ? `${c.usage.total.toLocaleString()} tokens · ${c.usage.input.toLocaleString()} in / ${c.usage.output.toLocaleString()} out / ${c.usage.thinking.toLocaleString()} thinking` : 'none (rule-based draft)'}
                </span>
              </Meta>
            </div>
          )}
        </div>

        {/* ---------- body: one scroll for the whole report ---------- */}
        <div className="flex-1 overflow-y-auto overscroll-contain">
          {err && <p className="mx-6 mt-4 text-[13px] text-def-soft bg-def-bg border border-def-edge rounded-lg px-3 py-2">{err}</p>}
          {!r || !edit || !c ? <p className="p-8 text-[15px] text-mut">Loading…</p> : (
            <div className="px-6 py-5 flex flex-col gap-7 text-[15px] leading-relaxed text-tx">
              {!locked && ((r.generator === 'RULE_BASED' && c.ai_error) || c.fallback) && (
                <div data-testid="ai-fallback-note" data-kind={c.fallback ? 'fallback' : 'rules'}
                     className="rounded-xl border border-[#6B4E12] bg-[#2A210C] px-4 py-3.5 flex flex-col gap-2.5 text-[14px]">
                  <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
                    <div className="flex-1 min-w-[16rem] flex flex-col gap-1">
                      <span className="font-bold text-warn flex items-center gap-2"><AlertTriangle className="w-4 h-4 shrink-0" />
                        {c.fallback
                          ? `Drafted by ${prettyModel(c.fallback.to)} — ${prettyModel(c.fallback.from)} was ${c.fallback.reason}`
                          : 'Gemini was unavailable, so this is a rule-based draft'}
                      </span>
                      <span className="text-tx2">
                        {c.fallback
                          ? 'Its visual check is less thorough, so look at the key frames yourself, or ask the main model again.'
                          : `Tried: ${[...new Set((c.ai_attempts || []).map((a) => prettyModel(a.model)))].join(', ') || 'Gemini'}. ` +
                            'Busy spells usually pass within minutes — ask Gemini again to replace this draft.'}
                        {' '}Your engineer notes are kept.
                      </span>
                    </div>
                    <button onClick={startRedraft} disabled={redraft?.status === 'running'} data-testid="redraft"
                            className={`${btn.secondary} shrink-0 flex items-center gap-1.5 text-[13px]`}>
                      <RefreshCw className={`w-4 h-4 ${redraft?.status === 'running' ? 'animate-spin' : ''}`} />
                      {c.fallback ? `Redraft with ${prettyModel(c.fallback.from)}` : 'Redraft with Gemini'}
                    </button>
                  </div>
                  {redraft && redraft.status === 'running' && <JobProgress job={redraft} />}
                  {redraftMsg && <p className="text-[13px] text-def-tx" data-testid="redraft-error">{redraftMsg}</p>}
                </div>
              )}
              <div data-testid="grounding" data-passed={r.grounding.passed}
                   className={`rounded-xl border px-4 py-3 text-[14px] ${r.grounding.passed ? 'border-[#1E5A3E] bg-[#0F2A1E] text-ok' : 'border-[#6B4E12] bg-[#33270C] text-warn'}`}>
                {r.grounding.passed ? (
                  <span className="flex items-center gap-2.5"><CircleCheck className="w-5 h-5 shrink-0" />Every event, depth and segment in this draft matches the database
                    {r.grounding.attempts > 1 ? ` (the first AI draft failed ${r.grounding.firstAttemptErrors.length} check(s) and was regenerated)` : ''}.</span>
                ) : (
                  <div>
                    <span className="flex items-center gap-2.5 font-bold"><AlertTriangle className="w-5 h-5 shrink-0" />This draft makes claims the data does not support — check before use:</span>
                    <ul className="list-disc pl-8 mt-1.5 space-y-0.5">{r.grounding.errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
                  </div>
                )}
              </div>

              <div className="grid md:grid-cols-[minmax(0,1fr)_300px] gap-6">
                <section>
                  <SectionTitle>Summary</SectionTitle>
                  <AutoText value={edit.summary} onChange={(v) => upd({ summary: v })} locked={locked} minRows={3} className="text-[15px] leading-relaxed text-tx" />
                </section>
                <section>
                  <SectionTitle>Condition grade</SectionTitle>
                  {locked ? (
                    <div className={`px-4 py-2.5 rounded-xl border font-extrabold text-[17px] font-sans ${GRADE_CLS[edit.grade]}`} data-testid="report-grade">{edit.grade}</div>
                  ) : (
                    <select value={edit.grade} onChange={(e) => upd({ grade: e.target.value as Grade })} data-testid="report-grade"
                            className={`w-full px-4 py-2.5 rounded-xl border font-extrabold text-[17px] font-sans cursor-pointer ${GRADE_CLS[edit.grade]}`}>
                      {GRADES.map((g) => <option key={g} value={g}>{g}</option>)}
                    </select>
                  )}
                  <div className="mt-2.5">
                    <AutoText value={edit.rationale} onChange={(v) => upd({ rationale: v })} locked={locked} minRows={3} className="text-[14px] leading-relaxed text-tx2" />
                  </div>
                </section>
              </div>

              <section>
                <SectionTitle right={Object.keys(second).length > 0 && r.generator !== 'RULE_BASED'
                  ? <span data-testid="second-opinion">AI visual check: {Object.entries(second).map(([k, n]) => `${n} ${human(k).toLowerCase()}`).join(' · ')}</span> : undefined}>
                  Findings ({c.findings.length})
                </SectionTitle>
                {c.findings.length === 0 && <p className="text-[14px] text-mut">No defect events in this scope.</p>}
                <div className="flex flex-col gap-3">
                  {c.findings.map((f) => {
                    const k = kf[f.event_ref];
                    const ev = f.event;
                    const def = f.defect_type === 'DEFORMATION';
                    return (
                      <article key={f.event_ref} className="bg-ink border border-line rounded-xl p-4 flex flex-col lg:flex-row gap-4" data-testid="finding">
                        <div className="flex-1 min-w-0 flex flex-col gap-2.5">
                          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[13px] font-sans">
                            <button disabled={!ev} onClick={() => ev && onCite(ev.video_id, ev.t_start)} data-testid="cite" title={ev ? 'Open this moment in Review' : ''}
                                    className="font-mono font-bold text-acc-tx bg-acc-bg border border-acc/30 px-2 py-0.5 rounded-md hover:text-tx cursor-pointer disabled:cursor-default">
                              {f.event_ref}{ev ? ` · ${mmss(ev.t_start)}` : ''}
                            </button>
                            <span className={`font-extrabold text-[14px] ${def ? 'text-def-tx' : 'text-pit-tx'}`}>{def ? 'Deformation' : f.defect_type === 'CORROSION' ? 'Pitting (grade 6–7)' : f.defect_type}</span>
                            {ev && <span className="text-tx2 font-semibold">peak {Math.round(ev.peak_confidence * 100)}% · {ev.detections} detections</span>}
                            <span className="text-tx2 font-semibold">{f.depth_m != null ? `${f.depth_m} m` : 'no depth'}{f.segment ? ` · ${f.segment}` : ''}</span>
                          </div>
                          {ev?.inspection && <span className="text-xs text-dim truncate">{ev.inspection}</span>}
                          <p className="text-[15px] leading-relaxed text-tx">{f.description}</p>
                          <label className="flex items-center gap-2.5 text-[13px] text-mut font-semibold font-sans">
                            Visual check
                            <select value={edit.verdicts[f.event_ref]} disabled={locked} data-testid="verdict"
                                    onChange={(e) => upd({ verdicts: { ...edit.verdicts, [f.event_ref]: e.target.value as Visual } })}
                                    className={`${btn.input} !py-1.5 text-[13px] font-semibold cursor-pointer disabled:cursor-default disabled:opacity-90`}>
                              {VISUALS.map((v) => <option key={v} value={v}>{human(v)}</option>)}
                            </select>
                          </label>
                        </div>
                        {k && (
                          <div className="flex gap-2 shrink-0 lg:w-[360px]">
                            {k.files.map((fn) => (
                              <a key={fn} href={apiUrl(`/api/reports/${r.id}/frames/${fn}`)} target="_blank" rel="noreferrer" title="Open full size" className="flex-1 min-w-0">
                                <img src={apiUrl(`/api/reports/${r.id}/frames/${fn}`)} alt={`${f.event_ref} key frame`} data-testid="keyframe"
                                     className="w-full h-36 rounded-lg border border-edge object-cover bg-black hover:border-acc" />
                              </a>
                            ))}
                          </div>
                        )}
                      </article>
                    );
                  })}
                </div>
              </section>

              <section>
                <SectionTitle>Recommended actions</SectionTitle>
                <ol className="flex flex-col gap-2">
                  {c.recommended_actions.map((a, i) => (
                    <li key={i} className="bg-ink border border-line rounded-xl px-4 py-3 flex flex-col gap-1">
                      <div className="flex flex-wrap items-center gap-2.5 font-sans">
                        <span className={`text-[12px] font-extrabold uppercase tracking-wide rounded-md px-2 py-0.5 ${PRIORITY_CLS[a.priority] || 'bg-btn text-tx2'}`}>{human(a.priority)}</span>
                        <span className="font-extrabold text-[15px]">{human(a.action)}</span>
                        {a.refs.length > 0 && <span className="font-mono text-xs text-acc-tx">{a.refs.join(', ')}</span>}
                      </div>
                      <p className="text-[14.5px] text-tx2 leading-relaxed">{a.justification}</p>
                    </li>
                  ))}
                </ol>
              </section>

              {c.limitations.length > 0 && (
                <section>
                  <SectionTitle>Limitations</SectionTitle>
                  <ul className="list-disc pl-5 space-y-1 text-[14px] text-tx2 leading-relaxed">{c.limitations.map((l, i) => <li key={i}>{l}</li>)}</ul>
                </section>
              )}

              <section>
                <SectionTitle>Engineer notes</SectionTitle>
                <AutoText value={edit.notes} onChange={(v) => upd({ notes: v })} locked={locked} minRows={2} testid="engineer-notes"
                          placeholder="Your own observations, measurements, decisions…" className="text-[15px] leading-relaxed text-tx" />
              </section>
            </div>
          )}
        </div>

        {/* ---------- footer: always visible ---------- */}
        {r && (
          <div className="px-6 py-3.5 border-t border-line bg-bar rounded-b-2xl flex flex-wrap items-center gap-3 font-sans">
            <div className="flex items-center rounded-[9px] border border-edge2 overflow-hidden">
              <a href={apiUrl(`/api/reports/${r.id}/pdf${pdfTpl ? `?template=${encodeURIComponent(pdfTpl)}` : ''}`)} data-testid="export-pdf"
                 onClick={() => log(`REPORT: PDF downloaded (${tpls?.templates.find((x) => x.id === (pdfTpl || tpls?.default))?.name || 'default'} template).`)}
                 className="bg-transparent text-tx font-semibold px-3 py-1.5 flex items-center gap-1.5 text-[13px] hover:bg-btn">
                <Download className="w-4 h-4" /> Download PDF
              </a>
              {(tpls?.templates.length ?? 0) > 1 && (
                <select value={pdfTpl || tpls!.default} onChange={(e) => setPdfTpl(e.target.value)} data-testid="pdf-template" aria-label="PDF template"
                        className="bg-ink border-l border-edge2 text-[12.5px] text-tx2 px-2 py-1.5 outline-none cursor-pointer max-w-[170px]">
                  {tpls!.templates.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
                </select>
              )}
            </div>
            <a href={apiUrl(`/api/reports/${r.id}/html`)} target="_blank" rel="noreferrer" data-testid="export-html"
               className="text-xs text-mut underline hover:text-tx">printable page</a>
            {locked ? (
              <span className="ml-auto flex items-center gap-2 text-[14px] text-ok font-bold" data-testid="approved-by">
                <Lock className="w-4 h-4" /> Approved by {r.engineer} · {localTime(r.approvedAt)}
              </span>
            ) : (
              <div className="ml-auto flex flex-wrap items-center gap-2">
                {dirty && <span className="text-xs text-warn font-semibold">Unsaved edits</span>}
                <button onClick={save} disabled={!dirty} data-testid="save-report" className={`${btn.secondary} flex items-center gap-1.5 text-[13px]`}>
                  <Save className="w-4 h-4" /> Save edits
                </button>
                <input value={engineer} onChange={(e) => setEngineer(e.target.value)} placeholder="Engineer name" data-testid="engineer-name"
                       aria-label="Engineer name" className={`${btn.input} w-44 text-[13px]`} />
                <button onClick={approve} disabled={!engineer.trim()} data-testid="approve-report"
                        className="flex items-center gap-1.5 px-3.5 py-2 text-[13px] font-extrabold rounded-[9px] bg-ok text-[#06210F] disabled:opacity-40 cursor-pointer disabled:cursor-not-allowed">
                  <CircleCheck className="w-4 h-4" /> Approve &amp; lock
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
