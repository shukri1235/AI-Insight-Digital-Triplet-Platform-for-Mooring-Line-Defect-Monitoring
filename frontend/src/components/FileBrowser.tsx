import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { Check, ChevronRight, File, Folder, HardDrive, X } from 'lucide-react';
import { getJSON } from '../api';

interface Listing { path: string; parent: string | null; dirs: { name: string; path: string }[]; files: { name: string; path: string; sizeBytes: number }[] }

const fmtSize = (n: number) => n > 1 << 30 ? `${(n / (1 << 30)).toFixed(2)} GB` : `${(n / (1 << 20)).toFixed(1)} MB`;

/** Server-side file browser: lists folders on the BACKEND PC so a file can be registered
 *  in place (no copy). Only answers requests from the backend PC itself. */
export default function FileBrowser({ kind, title, onPick, onClose, onPickMany }: {
  kind: 'video' | 'tlog'; title: string; onPick: (path: string) => void; onClose: () => void;
  /** when given, files can be ticked (also across folders) and added together */
  onPickMany?: (paths: string[]) => void;
}) {
  const [picked, setPicked] = useState<string[]>([]);
  const multi = !!onPickMany;
  const toggle = (p: string) => setPicked((x) => (x.includes(p) ? x.filter((y) => y !== p) : [...x, p]));
  const [roots, setRoots] = useState<{ name: string; path: string }[]>([]);
  const [listing, setListing] = useState<Listing | null>(null);
  const [error, setError] = useState<string | null>(null);

  const open = (path: string) => {
    setError(null);
    getJSON<Listing>(`/api/fs/list?path=${encodeURIComponent(path)}&kind=${kind}`).then(setListing).catch((e) => setError(e.message));
  };
  useEffect(() => {
    getJSON<{ roots: { name: string; path: string }[]; home: string }>('/api/fs/roots')
      .then((r) => {
        setRoots(r.roots);
        let last: string | null = null;
        try { last = localStorage.getItem(`fb-last-${kind}`); } catch { /* storage unavailable */ }
        open(last || r.home);
      })
      .catch((e) => setError(e.message));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const pick = (p: string) => {
    try { if (listing) localStorage.setItem(`fb-last-${kind}`, listing.path); } catch { /* ignore */ }
    onPick(p);
  };

  return createPortal(
    <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4" onClick={onClose}>
      <div className="w-full max-w-2xl max-h-[80vh] bg-slate-900 border border-slate-700 rounded-xl flex flex-col overflow-hidden" onClick={(e) => e.stopPropagation()}
           data-testid="file-browser">
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-800">
          <span className="text-sm font-bold uppercase tracking-widest text-slate-200">{title}</span>
          <button onClick={onClose} className="text-slate-400 hover:text-white cursor-pointer"><X className="w-4 h-4" /></button>
        </div>
        <div className="flex flex-wrap gap-1.5 px-4 py-2 border-b border-slate-800">
          {roots.map((r) => (
            <button key={r.path} onClick={() => open(r.path)}
                    className="flex items-center gap-1 text-[12px] font-mono text-slate-300 bg-slate-950 border border-slate-700 hover:border-indigo-500 px-2 py-1 rounded cursor-pointer">
              <HardDrive className="w-3 h-3" />{r.name}
            </button>
          ))}
        </div>
        {error && <p className="m-4 text-xs text-rose-300 bg-rose-950/40 border border-rose-900 rounded px-3 py-2">{error}</p>}
        {listing && (
          <>
            <div className="px-4 py-2 text-[12px] font-mono text-slate-400 truncate border-b border-slate-800" title={listing.path}>{listing.path}</div>
            <div className="flex-1 overflow-auto divide-y divide-slate-800/60">
              {listing.parent && (
                <button onClick={() => open(listing.parent!)} className="w-full text-left px-4 py-2 text-xs text-slate-400 hover:bg-slate-800/60 cursor-pointer">.. (up)</button>
              )}
              {listing.dirs.map((d) => (
                <button key={d.path} onClick={() => open(d.path)}
                        className="w-full flex items-center gap-2 text-left px-4 py-2 text-xs text-slate-200 hover:bg-slate-800/60 cursor-pointer">
                  <Folder className="w-4 h-4 text-amber-400" />{d.name}<ChevronRight className="w-3 h-3 ml-auto text-slate-600" />
                </button>
              ))}
              {multi && listing.files.length > 1 && (
                <div className="flex items-center gap-2 px-4 py-1.5 text-[12px] text-slate-400 bg-slate-950/50">
                  <span>Tick several files to import them together.</span>
                  <button onClick={() => setPicked((x) => [...new Set([...x, ...listing.files.map((f) => f.path)])])} data-testid="fb-select-all"
                          className="ml-auto text-indigo-300 hover:text-white underline cursor-pointer">Select all {listing.files.length} here</button>
                </div>
              )}
              {listing.files.map((f) => {
                const on = picked.includes(f.path);
                return (
                  <div key={f.path} className={`flex items-center ${on ? 'bg-indigo-950/60' : ''}`}>
                    {multi && (
                      <button onClick={() => toggle(f.path)} data-testid="fb-check" aria-pressed={on} aria-label={`${on ? 'Untick' : 'Tick'} ${f.name}`}
                              className="pl-4 pr-1 py-2 cursor-pointer">
                        <span className={`w-4 h-4 rounded border grid place-items-center ${on ? 'bg-indigo-500 border-indigo-400' : 'border-slate-600 hover:border-indigo-400'}`}>
                          {on && <Check className="w-3 h-3 text-white" />}
                        </span>
                      </button>
                    )}
                    <button onClick={() => (picked.length ? toggle(f.path) : pick(f.path))} data-testid="fb-file"
                            title={picked.length ? 'Tick / untick' : multi ? 'Pick this file (or tick several to import together)' : ''}
                            className={`flex-1 min-w-0 flex items-center gap-2 text-left ${multi ? 'pl-2' : 'pl-4'} pr-4 py-2 text-xs text-white hover:bg-indigo-900/50 cursor-pointer`}>
                      <File className="w-4 h-4 text-indigo-400 shrink-0" /><span className="truncate">{f.name}</span>
                      <span className="ml-auto font-mono text-slate-500">{fmtSize(f.sizeBytes)}</span>
                    </button>
                  </div>
                );
              })}
              {!listing.dirs.length && !listing.files.length && <p className="px-4 py-6 text-xs text-slate-500">No sub-folders or {kind === 'tlog' ? '.tlog' : 'video'} files here.</p>}
            </div>
          </>
        )}
        {multi && picked.length > 0 && (
          <div className="flex items-center gap-3 px-4 py-3 border-t border-slate-800 bg-slate-950/70" data-testid="fb-picked">
            <span className="text-xs text-slate-300">{picked.length} file{picked.length === 1 ? '' : 's'} ticked</span>
            <button onClick={() => setPicked([])} className="text-xs text-slate-400 hover:text-white underline cursor-pointer">Clear</button>
            <button onClick={() => { try { if (listing) localStorage.setItem(`fb-last-${kind}`, listing.path); } catch { /* ignore */ } onPickMany!(picked); }}
                    data-testid="fb-add" className="ml-auto bg-acc text-on-acc font-bold rounded-lg px-3.5 py-2 text-xs cursor-pointer">
              Add {picked.length} file{picked.length === 1 ? '' : 's'}
            </button>
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
