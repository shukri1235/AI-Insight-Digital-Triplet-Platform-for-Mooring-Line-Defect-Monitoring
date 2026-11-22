import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Copy, FileCog, FileText, ImagePlus, Star, Trash2, X } from 'lucide-react';
import { apiUrl, del, getJSON, postJSON, putJSON } from '../api';
import { btn, Card, CardTitle } from './shared';

export interface PdfTemplate {
  id: string; name: string; title: string; company: string; company_lines: string[]; logo: string | null;
  accent: string; text: string; muted: string; paper: 'A4' | 'Letter'; font: string; font_bold: string | null; base_size: number;
  cover: boolean; header_text: string; footer_text: string; page_numbers: boolean; watermark_drafts: boolean;
  sections: Record<string, boolean>; sign_off: { prepared_label: string; approved_label: string; signature_lines: boolean };
  disclaimer: string; errors?: string[];
}
export interface TemplateList { templates: PdfTemplate[]; default: string; defaultSource: string; dir: string }

const SECTIONS: [string, string][] = [
  ['overview', 'Inspection overview'], ['summary', 'Condition and summary'], ['findings', 'Findings table'],
  ['evidence', 'Key frames'], ['actions', 'Recommended actions'], ['limitations', 'Limitations'],
  ['engineer_notes', 'Engineer notes'], ['grounding', 'Grounding check'], ['ai_usage', 'AI usage and timings'],
  ['sign_off', 'Sign-off block'],
];

export const useTemplates = () => {
  const [list, setList] = useState<TemplateList | null>(null);
  const reload = () => getJSON<TemplateList>('/api/report-templates').then(setList).catch(() => setList(null));
  useEffect(() => { reload(); }, []);
  return { list, reload };
};

/** System drawer card: which template PDFs use, and the editor. */
export function ReportTemplatesCard() {
  const { list, reload } = useTemplates();
  const [open, setOpen] = useState(false);
  const def = list?.templates.find((t) => t.id === list.default);
  return (
    <Card className="flex flex-col gap-2.5" testid="templates-card">
      <CardTitle icon={<FileText className="w-3.5 h-3.5" />} title="PDF report templates"
                 right={<span className="text-tx2">{list ? `${list.templates.length}` : '—'}</span>} />
      <p className="text-xs text-mut leading-relaxed -mt-0.5">
        Logo, company details, colours, paper, sections and sign-off of exported PDFs. One template per client.
      </p>
      <div className="text-[13px] text-tx2">
        Default: <b className="text-tx" data-testid="templates-default">{def?.name || list?.default || '—'}</b>
        {list?.defaultSource && <span className="text-dim"> · {list.defaultSource}</span>}
      </div>
      <button onClick={() => setOpen(true)} className={`${btn.secondary} text-[13px]`} data-testid="templates-manage">Manage templates</button>
      {open && <TemplateEditor onClose={() => { setOpen(false); reload(); }} />}
    </Card>
  );
}

function TemplateEditor({ onClose }: { onClose: () => void }) {
  const { list, reload } = useTemplates();
  const [sel, setSel] = useState<string>('');
  const [t, setT] = useState<PdfTemplate | null>(null);
  const [dirty, setDirty] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [newId, setNewId] = useState('');
  const [previewKey, setPreviewKey] = useState(0);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => { if (list && !sel) setSel(list.default); }, [list]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const cur = list?.templates.find((x) => x.id === sel);
    if (cur) { setT(JSON.parse(JSON.stringify(cur))); setDirty(false); }
  }, [sel, list]);
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [onClose]);

  const upd = (patch: Partial<PdfTemplate>) => { setT((x) => (x ? { ...x, ...patch } : x)); setDirty(true); };
  const save = async () => {
    if (!t) return false;
    try {
      const { id, errors, ...body } = t; // eslint-disable-line @typescript-eslint/no-unused-vars
      await putJSON(`/api/report-templates/${t.id}`, body);
      setDirty(false); setMsg({ ok: true, text: `Saved "${t.name}".` }); await reload(); setPreviewKey((k) => k + 1);
      return true;
    } catch (e: any) { setMsg({ ok: false, text: e.message }); return false; }
  };
  const duplicate = async () => {
    const id = newId.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '');
    if (!t || !id) return;
    if (list?.templates.some((x) => x.id === id)) { setMsg({ ok: false, text: `A template "${id}" already exists.` }); return; }
    try {
      const { id: _old, errors, ...body } = t; // eslint-disable-line @typescript-eslint/no-unused-vars
      await putJSON(`/api/report-templates/${id}`, { ...body, name: `${t.name} (copy)`, logo: t.logo });
      setNewId(''); await reload(); setSel(id); setMsg({ ok: true, text: `Created "${id}" from "${t.name}".` });
    } catch (e: any) { setMsg({ ok: false, text: e.message }); }
  };
  const remove = async () => {
    if (!t || t.id === 'default') return;
    try { await del(`/api/report-templates/${t.id}`); await reload(); setSel('default'); setMsg({ ok: true, text: `Deleted "${t.name}".` }); }
    catch (e: any) { setMsg({ ok: false, text: e.message }); }
  };
  const makeDefault = async () => {
    if (!t) return;
    if (dirty && !(await save())) return;
    try { await postJSON('/api/report-templates/default', { id: t.id }); await reload(); setMsg({ ok: true, text: `"${t.name}" is now the default for PDF exports.` }); }
    catch (e: any) { setMsg({ ok: false, text: e.message }); }
  };
  const uploadLogo = async (f: File) => {
    if (!t) return;
    if (dirty && !(await save())) return;
    try {
      const res = await fetch(apiUrl(`/api/report-templates/${t.id}/logo`), { method: 'POST', body: f, headers: { 'X-Filename': f.name } });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
      await reload(); setPreviewKey((k) => k + 1); setMsg({ ok: true, text: 'Logo uploaded.' });
    } catch (e: any) { setMsg({ ok: false, text: e.message }); }
  };
  const removeLogo = async () => {
    if (!t) return;
    try { await del(`/api/report-templates/${t.id}/logo`); await reload(); setPreviewKey((k) => k + 1); }
    catch (e: any) { setMsg({ ok: false, text: e.message }); }
  };

  const fontChoice = useMemo(() => (t ? (['auto', 'helvetica', 'times'].includes(t.font) ? t.font : 'ttf') : 'auto'), [t]);
  const field = 'flex flex-col gap-1 text-[12.5px] font-bold uppercase tracking-wide text-dim';
  const input = `${btn.input} !py-1.5 text-[13px] normal-case font-normal tracking-normal`;

  return createPortal(
    <div className="fixed inset-0 z-50 bg-black/75 flex items-start justify-center p-2 md:p-6" onClick={onClose}>
      <div className="w-full max-w-6xl max-h-[calc(100vh-1rem)] md:max-h-[calc(100vh-3rem)] bg-card border border-edge rounded-2xl flex flex-col shadow-2xl animate-rise"
           onClick={(e) => e.stopPropagation()} data-testid="template-editor" role="dialog" aria-label="PDF report templates">
        <div className="px-5 py-3.5 border-b border-line flex flex-wrap items-center gap-3">
          <FileCog className="w-5 h-5 text-acc" />
          <h3 className="text-[17px] font-extrabold">PDF report templates</h3>
          <select value={sel} onChange={(e) => setSel(e.target.value)} data-testid="template-select" className={`${btn.input} !py-1.5 text-[13px]`}>
            {list?.templates.map((x) => <option key={x.id} value={x.id}>{x.name}{x.id === list.default ? ' ★' : ''}</option>)}
          </select>
          <span className="text-xs text-dim truncate" title={list?.dir}>Files: {list?.dir}</span>
          <button onClick={onClose} aria-label="Close" data-testid="template-close" className="ml-auto text-mut hover:text-tx cursor-pointer p-1"><X className="w-5 h-5" /></button>
        </div>

        {t && (
          <div className="flex-1 min-h-0 grid lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
            <div className="overflow-y-auto px-5 py-4 flex flex-col gap-4 border-r border-line">
              {(t.errors?.length ?? 0) > 0 && <p className="text-[13px] text-warn">{t.errors!.join('; ')}</p>}
              <div className="grid grid-cols-2 gap-3">
                <label className={field}>Template name<input value={t.name} onChange={(e) => upd({ name: e.target.value })} className={input} data-testid="tpl-name" /></label>
                <label className={field}>Report title<input value={t.title} onChange={(e) => upd({ title: e.target.value })} className={input} data-testid="tpl-title" /></label>
                <label className={`${field} col-span-2`}>Company<input value={t.company} onChange={(e) => upd({ company: e.target.value })} className={input} data-testid="tpl-company" /></label>
                <label className={`${field} col-span-2`}>Company details (one per line: address, phone, web)
                  <textarea rows={3} value={t.company_lines.join('\n')} onChange={(e) => upd({ company_lines: e.target.value.split('\n') })}
                            className={`${input} resize-y`} data-testid="tpl-lines" />
                </label>
              </div>

              <div className="flex items-center gap-3">
                {t.logo ? (
                  <img src={`${apiUrl(`/api/report-templates/${t.id}/logo`)}?v=${previewKey}`} alt="Logo" className="h-12 max-w-[160px] object-contain bg-white rounded p-1" data-testid="tpl-logo" />
                ) : <span className="h-12 w-28 grid place-items-center text-xs text-dim border border-dashed border-edge rounded">No logo</span>}
                <input ref={fileRef} type="file" accept="image/png,image/jpeg" className="hidden" data-testid="tpl-logo-file"
                       onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadLogo(f); e.target.value = ''; }} />
                <button onClick={() => fileRef.current?.click()} className={`${btn.secondary} text-[13px] flex items-center gap-1.5`}><ImagePlus className="w-4 h-4" /> {t.logo ? 'Replace logo' : 'Upload logo'}</button>
                {t.logo && <button onClick={removeLogo} className="text-xs text-mut underline cursor-pointer">remove</button>}
                <span className="text-xs text-dim">PNG or JPG, under 3 MB</span>
              </div>

              <div className="grid grid-cols-3 gap-3">
                <label className={field}>Accent colour
                  <span className="flex items-center gap-2">
                    <input type="color" value={t.accent} onChange={(e) => upd({ accent: e.target.value })} className="h-8 w-10 bg-transparent cursor-pointer" data-testid="tpl-accent" />
                    <span className="font-mono text-[12px] text-tx2 normal-case">{t.accent}</span>
                  </span>
                </label>
                <label className={field}>Paper
                  <select value={t.paper} onChange={(e) => upd({ paper: e.target.value as 'A4' | 'Letter' })} className={input}>
                    <option>A4</option><option>Letter</option>
                  </select>
                </label>
                <label className={field}>Text size (pt)
                  <input type="number" min={7} max={14} step={0.5} value={t.base_size} onChange={(e) => upd({ base_size: Number(e.target.value) })} className={input} />
                </label>
                <label className={`${field} col-span-3`}>Font
                  <span className="flex gap-2">
                    <select value={fontChoice} onChange={(e) => upd({ font: e.target.value === 'ttf' ? '' : e.target.value })} className={input}>
                      <option value="auto">Auto (Arial / DejaVu)</option><option value="helvetica">Helvetica</option>
                      <option value="times">Times</option><option value="ttf">Your own .ttf…</option>
                    </select>
                    {fontChoice === 'ttf' && (
                      <input value={t.font} onChange={(e) => upd({ font: e.target.value })} placeholder="C:\Windows\Fonts\calibri.ttf" className={`${input} flex-1`} />
                    )}
                  </span>
                </label>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <label className={field}>Page header text<input value={t.header_text} onChange={(e) => upd({ header_text: e.target.value })} placeholder="Client: North Bow FPSO" className={input} /></label>
                <label className={field}>Page footer text<input value={t.footer_text} onChange={(e) => upd({ footer_text: e.target.value })} className={input} data-testid="tpl-footer" /></label>
                <label className={`${field} col-span-2`}>Disclaimer (end of the report)
                  <textarea rows={2} value={t.disclaimer} onChange={(e) => upd({ disclaimer: e.target.value })} className={`${input} resize-y`} />
                </label>
                <label className={field}>Sign-off: prepared label<input value={t.sign_off.prepared_label} onChange={(e) => upd({ sign_off: { ...t.sign_off, prepared_label: e.target.value } })} className={input} /></label>
                <label className={field}>Sign-off: approved label<input value={t.sign_off.approved_label} onChange={(e) => upd({ sign_off: { ...t.sign_off, approved_label: e.target.value } })} className={input} /></label>
              </div>

              <div className="flex flex-wrap gap-x-5 gap-y-2 text-[13px]">
                {([['cover', 'Cover page'], ['page_numbers', 'Page numbers'], ['watermark_drafts', 'DRAFT watermark until approved']] as const).map(([k, label]) => (
                  <label key={k} className="flex items-center gap-2 cursor-pointer">
                    <input type="checkbox" checked={!!t[k]} onChange={(e) => upd({ [k]: e.target.checked } as Partial<PdfTemplate>)} className="accent-acc" /> {label}
                  </label>
                ))}
                <label className="flex items-center gap-2 cursor-pointer">
                  <input type="checkbox" checked={t.sign_off.signature_lines} onChange={(e) => upd({ sign_off: { ...t.sign_off, signature_lines: e.target.checked } })} className="accent-acc" /> Signature lines
                </label>
              </div>
              <div>
                <div className={field}>Sections</div>
                <div className="grid grid-cols-2 gap-x-5 gap-y-1.5 mt-1.5 text-[13px]">
                  {SECTIONS.map(([k, label]) => (
                    <label key={k} className="flex items-center gap-2 cursor-pointer">
                      <input type="checkbox" checked={!!t.sections[k]} onChange={(e) => upd({ sections: { ...t.sections, [k]: e.target.checked } })} className="accent-acc" /> {label}
                    </label>
                  ))}
                </div>
              </div>
            </div>

            <div className="flex flex-col min-h-[360px]">
              <div className="px-4 py-2 text-xs text-dim border-b border-line flex items-center gap-2">
                Preview on the newest report{dirty ? ' · save to update' : ''}
                <a href={`${apiUrl(`/api/report-templates/${t.id}/preview`)}?v=${previewKey}`} target="_blank" rel="noreferrer" className="ml-auto underline text-acc-tx">open in a new tab</a>
              </div>
              <iframe key={`${t.id}-${previewKey}`} title="PDF preview" src={`${apiUrl(`/api/report-templates/${t.id}/preview`)}?v=${previewKey}`}
                      className="flex-1 w-full bg-white" data-testid="template-preview" />
            </div>
          </div>
        )}

        <div className="px-5 py-3 border-t border-line bg-bar rounded-b-2xl flex flex-wrap items-center gap-2">
          {msg && <span className={`text-[13px] ${msg.ok ? 'text-ok' : 'text-def-tx'}`} data-testid="template-msg">{msg.text}</span>}
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <input value={newId} onChange={(e) => setNewId(e.target.value)} placeholder="new-template-id" data-testid="template-new-id"
                   className={`${btn.input} !py-1.5 w-40 text-[13px]`} />
            <button onClick={duplicate} disabled={!newId.trim()} data-testid="template-duplicate" className={`${btn.secondary} text-[13px] flex items-center gap-1.5`}><Copy className="w-4 h-4" /> Copy as new</button>
            <button onClick={remove} disabled={!t || t.id === 'default'} data-testid="template-delete" className={`${btn.secondary} text-[13px] flex items-center gap-1.5`}><Trash2 className="w-4 h-4" /> Delete</button>
            <button onClick={makeDefault} disabled={!t || t.id === list?.default} data-testid="template-make-default" className={`${btn.secondary} text-[13px] flex items-center gap-1.5`}><Star className="w-4 h-4" /> Use by default</button>
            <button onClick={save} disabled={!dirty} data-testid="template-save" className={`${btn.primary} !py-2 text-[13px]`}>Save</button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/** System drawer card: the optional config.toml. */
export { SettingsFileCard } from './SettingsCard';
