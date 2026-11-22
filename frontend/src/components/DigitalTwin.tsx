import type { HistorySummary, MooringLine, Telemetry } from '../types';

/* Digital twin: the mooring line drawn through its depth zones, the ROV at its real
 * (MAVLink) depth, and the defect events found in each zone across all dives. */

const W = 340, H = 156, GAP = 2, MIN_BAND = 28;
const P0 = { x: 300, y: 16 }, C = { x: 240, y: 124 }, P2 = { x: 40, y: H - 2 };
const FILLS = ['#123A40', '#0E2834', '#0B1C27', '#0A1820'];

const bez = (t: number) => ({
  x: (1 - t) ** 2 * P0.x + 2 * (1 - t) * t * C.x + t ** 2 * P2.x,
  y: (1 - t) ** 2 * P0.y + 2 * (1 - t) * t * C.y + t ** 2 * P2.y,
});
function pointAtY(y: number) {
  let lo = 0, hi = 1;
  for (let i = 0; i < 24; i++) {
    const mid = (lo + hi) / 2;
    if (bez(mid).y < y) lo = mid; else hi = mid;
  }
  return bez((lo + hi) / 2);
}

export default function DigitalTwin({ line, telemetry, history }: {
  line: MooringLine | undefined;
  telemetry: Telemetry | null | undefined;
  history: HistorySummary | null;
}) {
  const segs = line?.segments || [];
  const seabed = line?.seabed_depth || 200;
  const avail = H - GAP * Math.max(0, segs.length - 1);
  const bands = segs.map((s) => ({ ...s, h: MIN_BAND + ((s.end_m - s.start_m) / seabed) * (avail - MIN_BAND * segs.length) }));
  let y = 0;
  const placed = bands.map((b) => { const top = y; y += b.h + GAP; return { ...b, top }; });
  const yOf = (d: number) => {
    const b = placed.find((p) => d >= p.start_m && d <= p.end_m) || (d < (placed[0]?.start_m ?? 0) ? placed[0] : placed[placed.length - 1]);
    if (!b) return P0.y;
    return b.top + Math.min(1, Math.max(0, (d - b.start_m) / (b.end_m - b.start_m))) * b.h;
  };
  const depth = telemetry?.depth;
  const rov = depth != null ? pointAtY(Math.min(P2.y, Math.max(P0.y, yOf(depth)))) : null;
  const here = telemetry?.segment;
  const desc = placed.map((b) => {
    const z = history?.segments?.[b.name]?.counts;
    return `${b.name}: ${z?.DEFORMATION ?? 0} deformation, ${z?.CORROSION ?? 0} pitting`;
  }).join('; ');

  // Zone tags first, so the ROV label can be placed clear of them
  type Box = { x: number; y: number; w: number; h: number };
  const zoneTags = placed.map((b, i) => {
    const z = history?.segments?.[b.name]?.counts;
    const tags = [
      z?.CORROSION ? { text: `${z.CORROSION} pitting`, fill: '#C4A3FF', ink: '#1B1030' } : null,
      z?.DEFORMATION ? { text: `${z.DEFORMATION} def`, fill: '#FF6B5E', ink: '#1A0503' } : null,
    ].filter(Boolean) as { text: string; fill: string; ink: string }[];
    // Tags go on the side of the band away from the line, so neither the line nor the ROV covers them
    const lineX = pointAtY(Math.min(P2.y, Math.max(P0.y, b.top + b.h / 2))).x;
    const fromLeft = lineX > W / 2;
    let x = fromLeft ? 10 : i === 0 ? 262 : W - 10;          // top band: keep clear of the FPSO box
    return tags.map((t) => {
      const w = t.text.length * 6 + 16;
      if (!fromLeft) x -= w;
      const box = { x, y: b.top + 5, w, h: 17 };
      x = fromLeft ? x + w + 4 : x - 4;
      return { ...t, box };
    });
  });
  const avoid: Box[] = [...zoneTags.flat().map((t) => t.box), { x: 268, y: 2, w: 58, h: 14 }];
  const hits = (a: Box) => avoid.some((o) => a.x < o.x + o.w + 2 && a.x + a.w + 2 > o.x && a.y < o.y + o.h + 2 && a.y + a.h + 2 > o.y);
  const rovText = depth != null ? `ROV ${depth.toFixed(1)} m` : '';
  const rovLabel = (() => {
    if (!rov) return null;
    const w = rovText.length * 6.3 + 10, h = 16;
    const spots: Box[] = [
      { x: rov.x - w - 9, y: rov.y - h / 2, w, h },        // left of the vehicle
      { x: rov.x - w - 9, y: rov.y + 7, w, h },            // below-left
      { x: rov.x - w - 9, y: rov.y - h - 7, w, h },        // above-left
      { x: rov.x + 9, y: rov.y + 7, w, h },                // below-right
      { x: rov.x - w / 2, y: rov.y + 9, w, h },            // below
    ].map((b) => ({ ...b, x: Math.min(W - b.w - 2, Math.max(2, b.x)), y: Math.min(H - b.h - 1, Math.max(1, b.y)) }));
    return spots.find((b) => !hits(b)) || spots[0];
  })();

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" data-testid="digital-twin"
         aria-label={`Mooring line ${line?.id ?? ''}${depth != null ? ` with the ROV at ${depth.toFixed(1)} metres` : ', ROV offline'}. ${desc}`}>
      {placed.map((b, i) => {
        return (
          <g key={b.name}>
            <rect x="0" y={b.top} width={W} height={b.h} rx="6" fill={FILLS[i % FILLS.length]}
                  stroke={here === b.name ? '#3DD6C3' : 'none'} strokeOpacity="0.55" />
            {zoneTags[i].map((t) => (
              <g key={t.text} data-testid="twin-zone-tag">
                <rect x={t.box.x} y={t.box.y} width={t.box.w} height={t.box.h} rx="8.5" fill={t.fill} />
                <text x={t.box.x + 8} y={t.box.y + 12} fill={t.ink} fontSize="10.5" fontWeight="800" fontFamily="Manrope, sans-serif">{t.text}</text>
              </g>
            ))}
          </g>
        );
      })}
      <rect x="268" y="2" width="58" height="14" rx="4" fill="#1A2733" stroke="#2A3A4B" />
      <text x="282" y="12.5" fill="#B6C3CF" fontSize="9" fontWeight="700" fontFamily="Manrope, sans-serif">FPSO</text>
      <path d={`M ${P0.x} ${P0.y} Q ${C.x} ${C.y} ${P2.x} ${P2.y}`} fill="none" stroke="#3DD6C3" strokeWidth="2.5" strokeOpacity="0.85" />
      {/* zone names above the line, with a dark outline so the line never cuts through them */}
      {placed.map((b) => {
        const ty = b.top + b.h - 9;
        const right = pointAtY(Math.min(P2.y, Math.max(P0.y, ty - 4))).x < W / 2;   // line on the left here: name on the right
        return (
        <text key={b.name} x={right ? W - 10 : 10} y={ty} textAnchor={right ? 'end' : 'start'} fill={here === b.name ? '#E8EEF3' : '#A9C3C9'} fontSize="11" fontWeight="700"
              fontFamily="Manrope, sans-serif" stroke="#0A1820" strokeWidth="3.5" strokeLinejoin="round" paintOrder="stroke">
          {b.name.replace(/\s*Zone$/i, '')} · {b.start_m.toFixed(0)}–{b.end_m.toFixed(0)} m
        </text>
        );
      })}
      {rov && telemetry && (
        <g data-testid="twin-rov" opacity={telemetry.stale ? 0.5 : 1}>
          {!telemetry.stale && <circle className="rov-ring" cx={rov.x} cy={rov.y} r="5" fill="none" stroke="#3DD6C3" strokeWidth="2" />}
          <circle cx={rov.x} cy={rov.y} r="5.5" fill={telemetry.stale ? '#93A3B3' : telemetry.simulated ? '#F2B33D' : '#3DD6C3'} stroke="#0A1016" strokeWidth="2" />
          {rovLabel && (
            <g data-testid="twin-rov-label">
              <rect x={rovLabel.x} y={rovLabel.y} width={rovLabel.w} height={rovLabel.h} rx="5" fill="#0A1016" fillOpacity="0.88" />
              <text x={rovLabel.x + 5} y={rovLabel.y + 11.5} fill="#E8EEF3" fontSize="11" fontWeight="800" fontFamily="Manrope, sans-serif">{rovText}</text>
            </g>
          )}
        </g>
      )}
      {!rov && (
        <g data-testid="twin-offline">
          <rect x="70" y={H / 2 - 14} width="200" height="26" rx="8" fill="#0A1016" fillOpacity="0.85" />
          <text x={W / 2} y={H / 2 + 3} textAnchor="middle" fill="#93A3B3" fontSize="11" fontWeight="700" fontFamily="Manrope, sans-serif">
            ROV offline · no position without depth
          </text>
        </g>
      )}
    </svg>
  );
}
