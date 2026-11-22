import { useEffect, useState } from 'react';
import { FileText } from 'lucide-react';
import { apiUrl, getJSON } from '../api';
import { Card, CardTitle } from './shared';

/* Where the event log is kept: one text file per app session in the logs folder (backend/event_log.py). */
interface Trail { current: string; folder: string; sessions: { name: string; sizeKb: number; current: boolean }[] }

export default function LogTrailCard() {
  const [t, setT] = useState<Trail | null>(null);
  useEffect(() => { getJSON<Trail>('/api/logs').then(setT).catch(() => setT(null)); }, []);
  if (!t) return null;
  const older = t.sessions.filter((s) => !s.current).slice(0, 8);
  const link = (name: string, label: string, download = false) => (
    <a href={apiUrl(`/api/logs/${encodeURIComponent(name)}${download ? '?download=1' : ''}`)} target={download ? undefined : '_blank'} rel="noreferrer"
       className="text-acc-tx underline cursor-pointer">{label}</a>
  );
  return (
    <Card className="flex flex-col gap-1.5" testid="log-trail">
      <CardTitle icon={<FileText className="w-3.5 h-3.5" />} title="Event log paper trail" />
      <p className="text-xs text-mut leading-relaxed -mt-0.5">
        Every event-log entry of this session is saved as plain text, together with the backend's own record of live dives,
        incident clips, batch analyses and approved reports. Files are only added to, never changed.
      </p>
      <div className="text-xs font-mono text-tx2 break-all" data-testid="log-trail-file">{t.folder}{t.folder.includes('\\') ? '\\' : '/'}{t.current}</div>
      <div className="text-xs flex gap-3">{link(t.current, 'View')}{link(t.current, 'Download', true)}</div>
      {older.length > 0 && (
        <details className="text-xs text-tx2">
          <summary className="cursor-pointer text-mut">Earlier sessions ({t.sessions.length - 1})</summary>
          <ul className="mt-1 flex flex-col gap-0.5 font-mono">
            {older.map((s) => <li key={s.name}>{link(s.name, s.name)} <span className="text-dim">· {s.sizeKb} kB</span></li>)}
          </ul>
        </details>
      )}
    </Card>
  );
}
