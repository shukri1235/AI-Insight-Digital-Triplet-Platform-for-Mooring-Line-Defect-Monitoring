import { useState } from 'react';

const PRESETS = [0.25, 0.4, 0.5, 0.6];

/** Required threshold choice (no default): the operator decides before processing. */
export default function ThresholdPicker({ value, onChange, label = 'Threshold (required)', hint = true }: {
  value: number | null; onChange: (v: number) => void; label?: string; hint?: boolean;
}) {
  const [custom, setCustom] = useState('');
  return (
    <div role="group" aria-label="Detection threshold" className="flex flex-col gap-1.5" data-testid="threshold-picker">
      <span className="text-xs text-mut">{label}</span>
      <div className="flex flex-wrap gap-1.5">
        {PRESETS.map((p) => (
          <button type="button" key={p} onClick={() => { setCustom(''); onChange(p); }} data-testid={`thr-${p}`} aria-pressed={value === p && !custom}
                  className={`px-3 py-2 rounded-lg border font-bold cursor-pointer ${value === p && !custom
                    ? 'bg-acc-bg border-acc text-tx' : 'bg-ink border-edge text-tx2'}`}>
            {p.toFixed(2)}
          </button>
        ))}
        <input type="number" min={0.1} max={0.99} step={0.01} placeholder="custom" value={custom} aria-label="Custom threshold"
               data-testid="thr-custom"
               onChange={(e) => { setCustom(e.target.value); const v = parseFloat(e.target.value); if (v >= 0.1 && v <= 0.99) onChange(v); }}
               className={`w-[96px] bg-ink border border-dashed text-tx rounded-lg px-2.5 py-2 font-bold outline-none focus:border-acc ${custom ? 'border-acc' : 'border-[#3A4E63]'}`} />
      </div>
      {hint && (
        <span className="text-xs text-dim">
          {value == null ? 'Recorded with the analysis; choose before processing.' : `Detections ≥ ${value.toFixed(2)} will be recorded.`}
        </span>
      )}
    </div>
  );
}
