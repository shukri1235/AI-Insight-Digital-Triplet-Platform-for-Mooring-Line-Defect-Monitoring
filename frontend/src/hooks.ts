import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { WS_URL } from './api';
import type { IncidentEvent, LiveState, RovStatus, Telemetry } from './types';

/** Server-push telemetry channel with exponential-backoff reconnect.
 *  The backend drives the cadence (<=15 msgs/s); the UI never requests inference. */
export function useLiveSocket(onIncident: (e: IncidentEvent) => void) {
  const [state, setState] = useState<LiveState | null>(null);
  // ROV link status/telemetry when no video stream is running (pushed at 2 Hz)
  const [rovIdle, setRovIdle] = useState<(RovStatus & { telemetry: Telemetry | null }) | null>(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const incidentRef = useRef(onIncident);
  incidentRef.current = onIncident;

  useEffect(() => {
    let disposed = false;
    let retry = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;
      ws.onopen = () => { retry = 0; setConnected(true); };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === 'live_state') setState(msg);
          else if (msg.type === 'rov') setRovIdle(msg);
          else if (msg.type === 'incident') incidentRef.current(msg);
        } catch { /* ignore malformed frame */ }
      };
      ws.onclose = () => {
        setConnected(false);
        if (!disposed) timer = setTimeout(connect, Math.min(10000, 1000 * 2 ** retry++));
      };
      ws.onerror = () => ws.close();
    };
    connect();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      if (wsRef.current) { wsRef.current.onclose = null; wsRef.current.close(); }
    };
  }, []);

  const send = useCallback((msg: unknown) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(JSON.stringify(msg));
  }, []);

  return { state, rovIdle, connected, send };
}

export interface Rect { left: number; top: number; width: number; height: number }

/** Where a media element with `object-fit: contain` actually draws its pixels inside
 *  its box. Bounding boxes are % of the FRAME, so the overlay must be positioned on
 *  this rect - not on the letterboxed container - or boxes drift at the edges. */
export function useContainRect(mediaW?: number | null, mediaH?: number | null) {
  const ref = useRef<HTMLDivElement>(null);
  const [rect, setRect] = useState<Rect | null>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const compute = () => {
      const cw = el.clientWidth, ch = el.clientHeight;
      if (!mediaW || !mediaH || !cw || !ch) { setRect({ left: 0, top: 0, width: cw, height: ch }); return; }
      const scale = Math.min(cw / mediaW, ch / mediaH);
      const width = mediaW * scale, height = mediaH * scale;
      setRect({ left: (cw - width) / 2, top: (ch - height) / 2, width, height });
    };
    compute();
    const ro = new ResizeObserver(compute);
    ro.observe(el);
    return () => ro.disconnect();
  }, [mediaW, mediaH]);

  return { ref, rect };
}

/** setInterval that always calls the latest callback; pass null to pause. */
export function useInterval(cb: () => void, ms: number | null) {
  const saved = useRef(cb);
  saved.current = cb;
  useEffect(() => {
    if (ms === null) return;
    const id = setInterval(() => saved.current(), ms);
    return () => clearInterval(id);
  }, [ms]);
}
