import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { ChevronRight, FileText, Search, X } from 'lucide-react';
import { getJSON } from '../api';
import type { ReportJob, ReportSummary } from '../types';
import { fmtStamp, isoMs } from '../util';
import { GRADE_CLS, JobProgress, ReportModal } from './Reports';
import { btn, Card, CardTitle, cmpBy, SortHeader, useSort, type SortDir } from './shared';

/* Every report ever drafted (current and past), with the jobs still running on top.
 * Opening a row shows the full report (edit, approve, export, jump to cited moments). */

type Status = 'all' | 'draft' | 'review' | 'approved';
type Col = 'report' | 'scope' | 'line' | 'grade' | 'status' | 'by' | 'created';
const FIRST_DIR: Partial<Record<Col, SortDir>> = { created: 'desc', grade: 'desc' };
const GRADE_RANK: Record<string, number> = { ACCEPTABLE: 1, MONITOR: 2, REPAIR: 3, REPLACE: 4 };
const STATUS_LABEL: Record<string, string> = { DRAFT: 'Draft', EDITED: 'Edited', NEEDS_REVIEW: 'Needs review', APPROVED: 'Approved' };
const STATUS_TONE: Record<string, string> = {
  DRAFT: 'text-tx2', EDITED: 'text-acc-tx', NEEDS_REVIEW: 'text-warn', APPROVED: 'text-ok',
};
const SCOPE_LABEL: Record<string, string> = { INCIDENT: 'Incident', DIVE: 'Dive', SEGMENT: 'Segment' };
const GRID = 'grid-cols-[minmax(0,1fr)_86px_104px_104px_108px_118px_124px_16px]';

const by = (r: ReportSummary) => (r.generator === 'RULE_BASED' ? 'Rules' : r.generator.replace(/^gemini-/i, 'Gemini ').replace(/-/g, ' '));

export default function ReportsDirectory({ onCite, log, newReport, refreshKey }: {
  onCite: (videoId: string, t: number) => void;
  log: (m: string) => void;
  newReport: ReactNode;
  refreshKey?: number;
}) {
  const [reports, setReports] = useState<ReportSummary[]>([]);
  const [jobs, setJobs] = useState<ReportJob[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [status, setStatus] = useState<Status>('all');
  const [scopeType, setScopeType] = useState('');
  const [lineFilter, setLineFilter] = useState('');
  const [query, setQuery] = useState('');
  const [openId, setOpenId] = useState<string | null>(null);
  const { sort, toggle } = useSort<Col>('reports-dir-sort', { key: 'created', dir: 'desc' }, FIRST_DIR);
  const searchRef = useRef<HTMLInputElement>(null);

  const load = () => {
    getJSON<ReportSummary[]>('/api/reports?limit=2000').then((r) => { setReports(r); setLoaded(true); }).catch(() => setLoaded(true));
    getJSON<ReportJob[]>('/api/reports/jobs').then(setJobs).catch(() => setJobs([]));
  };
  useEffect(load, [refreshKey]);
  const running = jobs.filter((j) => j.status === 'running');
  useEffect(() => {                                 // follow running jobs closely; otherwise refresh now and then
    const id = setInterval(load, running.length ? 1000 : 4000);
    return () => clearInterval(id);
  }, [running.length]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (e.key === '/' && !(t && /INPUT|SELECT|TEXTAREA/.test(t.tagName))) { e.preventDefault(); searchRef.current?.focus(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const lines = useMemo(() => [...new Set(reports.map((r) => r.lineId).filter(Boolean) as string[])].sort(), [reports]);
  const counts = {
    all: reports.length,
    draft: reports.filter((r) => r.status === 'DRAFT' || r.status === 'EDITED').length,
    review: reports.filter((r) => r.status === 'NEEDS_REVIEW').length,
    approved: reports.filter((r) => r.status === 'APPROVED').length,
  };
  const q = query.trim().toLowerCase();
  const shown = reports.filter((r) => {
    if (status === 'draft' && !(r.status === 'DRAFT' || r.status === 'EDITED')) return false;
    if (status === 'review' && r.status !== 'NEEDS_REVIEW') return false;
    if (status === 'approved' && r.status !== 'APPROVED') return false;
    if (scopeType && r.scopeType !== scopeType) return false;
    if (lineFilter && r.lineId !== lineFilter) return false;
    if (q && !`${r.title} ${r.scopeName || ''} ${r.lineId || ''} ${r.conditionGrade} ${STATUS_LABEL[r.status]} ${r.engineer || ''} ${by(r)} ${r.createdAt || ''} ${r.isTest ? 'test' : ''}`
      .toLowerCase().includes(q)) return false;
    return true;
  });
  const newest = cmpBy<ReportSummary>((r) => isoMs(r.createdAt), 'desc');
  const primary = {
    report: cmpBy<ReportSummary>((r) => r.scopeName || r.title, sort.dir),
    scope: cmpBy<ReportSummary>((r) => r.scopeType, sort.dir),
    line: cmpBy<ReportSummary>((r) => r.lineId, sort.dir),
    grade: cmpBy<ReportSummary>((r) => GRADE_RANK[r.conditionGrade] ?? 0, sort.dir),
    status: cmpBy<ReportSummary>((r) => ['NEEDS_REVIEW', 'DRAFT', 'EDITED', 'APPROVED'].indexOf(r.status), sort.dir),
    by: cmpBy<ReportSummary>(by, sort.dir),
    created: cmpBy<ReportSummary>((r) => isoMs(r.createdAt), sort.dir),
  }[sort.key];
  const rows = [...shown].sort((a, b) => primary(a, b) || newest(a, b));

  const chip = (k: Status, label: string) => (
    <button key={k} onClick={() => setStatus(k)} data-testid={`rep-filter-${k}`} aria-pressed={status === k}
            className={`px-3 py-1.5 rounded-full text-[12.5px] font-semibold cursor-pointer border transition-colors ${status === k ? 'bg-acc-bg text-acc-tx border-acc/40' : 'bg-transparent text-tx2 border-edge hover:border-edge2'}`}>
      {label} <span className="font-mono text-[12px] opacity-70">{counts[k]}</span>
    </button>
  );
  const head = (k: Col, label: string, align: 'left' | 'right' = 'left') => <SortHeader key={k} k={k} label={label} sort={sort} onSort={toggle} align={align} />;

  return (
    <main className="flex-1 px-5 py-4 flex flex-col lg:flex-row gap-4 items-stretch min-h-0">
      <Card pad={false} testid="reports-directory" className="flex-1 min-w-0 flex flex-col min-h-[520px]">
        <div className="px-4 pt-3.5 pb-3 flex flex-col gap-3 border-b border-line">
          <CardTitle icon={<FileText className="w-3.5 h-3.5" />} title="Reports"
                     right={<span className="hidden sm:inline text-dim font-semibold">Every draft and approved report — click one to open it</span>} />
          <div className="flex flex-wrap items-center gap-2">
            <label className="relative flex-1 min-w-[220px] max-w-md">
              <Search className="w-4 h-4 text-dim absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
              <input ref={searchRef} value={query} onChange={(e) => setQuery(e.target.value)} data-testid="rep-search"
                     placeholder="Search dive, line, grade, engineer…  ( / )" aria-label="Search reports"
                     className={`${btn.input} w-full pl-9 pr-8 py-1.5 text-[13px]`} />
              {query && <button onClick={() => setQuery('')} aria-label="Clear search" className="absolute right-2 top-1/2 -translate-y-1/2 text-dim hover:text-tx cursor-pointer"><X className="w-3.5 h-3.5" /></button>}
            </label>
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="Status">
              {chip('all', 'All')}{chip('draft', 'Drafts')}{chip('review', 'Needs review')}{chip('approved', 'Approved')}
            </div>
            <div className="ml-auto flex items-center gap-2">
              <select value={scopeType} onChange={(e) => setScopeType(e.target.value)} aria-label="Scope" data-testid="rep-scope-filter" className={`${btn.input} py-1.5 text-[13px]`}>
                <option value="">All scopes</option><option value="INCIDENT">Incidents</option><option value="DIVE">Dives</option><option value="SEGMENT">Segments</option>
              </select>
              {lines.length > 1 && (
                <select value={lineFilter} onChange={(e) => setLineFilter(e.target.value)} aria-label="Mooring line" className={`${btn.input} py-1.5 text-[13px]`}>
                  <option value="">All lines</option>
                  {lines.map((l) => <option key={l} value={l}>{l}</option>)}
                </select>
              )}
            </div>
          </div>
        </div>

        {running.length > 0 && (
          <div className="px-4 py-2.5 border-b border-line bg-bar/60 flex flex-col gap-2" data-testid="rep-running">
            <span className="text-[12px] font-bold uppercase tracking-[.08em] text-dim">Drafting now · {running.length}</span>
            {running.map((j) => (
              <div key={j.id} className="flex items-center gap-3">
                <span className="text-[13px] font-semibold w-48 truncate">{SCOPE_LABEL[j.scopeType] || j.scopeType} · {j.redraftOf ? 'redraft with Gemini' : j.useAi ? 'Gemini' : 'rules'}</span>
                <div className="flex-1 min-w-0"><JobProgress job={j} compact /></div>
              </div>
            ))}
          </div>
        )}

        <div className="overflow-x-auto flex-1 flex flex-col min-h-0">
          <div className="min-w-[860px] flex-1 flex flex-col min-h-0">
            <div className={`grid ${GRID} gap-x-3 px-4 py-1.5 text-[12px] border-b border-line`} data-testid="rep-header">
              {head('report', 'Report')}{head('scope', 'Scope')}{head('line', 'Line')}{head('grade', 'Grade')}
              {head('status', 'Status')}{head('by', 'Drafted by')}{head('created', 'Created')}<span />
            </div>
            <ul className="flex-1 overflow-auto scroll-alert" data-testid="rep-list">
              {rows.map((r) => (
                <li key={r.id}>
                  <button onClick={() => setOpenId(r.id)} data-testid="rep-row" data-id={r.id}
                          className={`group w-full grid ${GRID} gap-x-3 items-center px-4 py-2.5 text-left border-b border-line hover:bg-sel/70 cursor-pointer`}>
                    <span className="min-w-0">
                      <span className="flex items-center gap-2 min-w-0">
                        <span className="text-[13.5px] font-bold truncate" title={r.title}>{r.scopeName || r.title}</span>
                        {r.isTest && <span className="text-[12px] font-extrabold bg-tst text-on-tst rounded px-1.5 py-0.5">TEST</span>}
                      </span>
                      <span className="block text-xs text-mut truncate mt-0.5">
                        {r.findings != null ? `${r.findings} finding${r.findings === 1 ? '' : 's'}` : ''}
                        {r.status === 'APPROVED' && r.engineer ? ` · approved by ${r.engineer}${r.approvedAt ? ` · ${fmtStamp(isoMs(r.approvedAt))}` : ''}` : ''}
                      </span>
                    </span>
                    <span className="text-xs font-semibold text-tx2">{SCOPE_LABEL[r.scopeType] || r.scopeType}</span>
                    <span className="text-xs font-semibold text-tx2 truncate">{r.lineId || '—'}</span>
                    <span><span className={`px-2 py-0.5 rounded-md border text-[12px] font-extrabold ${GRADE_CLS[r.conditionGrade] || ''}`}>{r.conditionGrade}</span></span>
                    <span className={`text-xs font-bold ${STATUS_TONE[r.status] || ''}`} data-testid="rep-status">{STATUS_LABEL[r.status] || r.status}</span>
                    <span className="text-xs text-tx2 truncate" title={r.generator}>{by(r)}</span>
                    <span className="font-mono text-xs text-tx2 whitespace-nowrap" data-testid="rep-created" title={r.createdAt ? new Date(isoMs(r.createdAt)).toLocaleString() : ''}>
                      {fmtStamp(isoMs(r.createdAt))}
                    </span>
                    <ChevronRight className="w-4 h-4 text-dim group-hover:text-tx" />
                  </button>
                </li>
              ))}
              {loaded && rows.length === 0 && (
                <li className="px-4 py-14 text-center text-[13px] text-mut" data-testid="rep-empty">
                  {reports.length === 0 ? 'No reports yet. Draft one from the panel on the right, a live alert, or Review.' : 'No report matches these filters.'}
                </li>
              )}
            </ul>
          </div>
        </div>
        <div className="px-4 py-2 border-t border-line text-xs text-dim flex flex-wrap gap-x-4">
          <span>{rows.length} of {reports.length} shown</span>
          <span className="ml-auto">Approved reports are locked; drafts stay editable</span>
        </div>
      </Card>

      <aside aria-label="New report" className="w-full lg:w-[380px] xl:w-[400px] shrink-0 flex flex-col gap-3">
        {newReport}
      </aside>

      {openId && <ReportModal id={openId} onClose={() => { setOpenId(null); load(); }}
                              onCite={(v, t) => { setOpenId(null); onCite(v, t); }} log={log} />}
    </main>
  );
}
