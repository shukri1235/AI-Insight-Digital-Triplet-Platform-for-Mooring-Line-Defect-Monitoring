// Browser walkthrough of the three use-case scenarios (UI-level verification).
//   1) python tests/e2e/serve_test_backend.py     2) npm --prefix frontend run dev
//   3) node tests/e2e/scenarios.mjs               (npm i -D playwright, npx playwright install chromium)
// Starts tools/bluerov_sim.py (BlueROV2 MAVLink simulator) part-way through, so the
// "ROV offline" state is checked first. Writes tests/results/e2e.json + screenshots.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const UI = process.env.E2E_UI || 'http://localhost:3000';
const API = process.env.E2E_API || 'http://localhost:8500';
const MEDIA_DIR = path.join(HERE, '.media');
const MAV = process.env.E2E_MAVLINK || 'udpout:127.0.0.1:14551';
const OUT = path.join(ROOT, 'tests', 'results', 'e2e');
fs.mkdirSync(OUT, { recursive: true });

const results = [];
const check = (id, title, pass, detail = '') => {
  results.push({ id, title, pass: !!pass, detail: String(detail) });
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${id}  ${title}${detail ? `  — ${detail}` : ''}`);
};
const api = async (p, init) => (await fetch(API + p, init)).json();
const shot = (page, name) => page.screenshot({ path: path.join(OUT, `${name}.png`) });
const tid = (page, id) => page.locator(`[data-testid="${id}"]`);
const boxes = (page) => tid(page, 'det-box').evaluateAll((els) =>
  els.map((e) => ({ type: e.dataset.type, conf: Number(e.dataset.conf), text: e.textContent })));
const until = async (fn, ms = 15000, step = 250) => {
  const end = Date.now() + ms;
  while (Date.now() < end) { try { const r = await fn(); if (r) return r; } catch { /* retry */ } await new Promise((r) => setTimeout(r, step)); }
  return null;
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1400 } });
// the file browser opens in the "external drive" folder (it remembers the last folder per PC)
await page.addInitScript((dir) => { try { localStorage.setItem('fb-last-video', dir); } catch { /* */ } }, MEDIA_DIR);
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(e.message));
let sim;

try {
  // ------------------------------------------------------------ boot / empty state / no ROV
  await page.goto(UI);
  await page.waitForTimeout(3000);
  check('E2E-0.1', 'Live tab boots in standby (no stream auto-selected)',
    await page.getByText('No signal · standby').count() > 0 && await page.locator('img[alt="Live ROV feed"]').count() === 0);
  check('E2E-0.2', 'No ROV: telemetry card shows the offline placeholder, no numbers',
    await tid(page, 'rov-offline').count() === 1 && await tid(page, 'tel-depth').count() === 0 &&
    /ROV offline/i.test(await tid(page, 'rov-offline').innerText()));
  check('E2E-0.3', 'No ROV: digital twin shows "ROV offline" and no ROV node',
    await tid(page, 'twin-offline').count() === 1 && await tid(page, 'twin-rov').count() === 0);
  await shot(page, 's0_boot_no_rov');
  check('E2E-CB.1', 'Live detection card: trigger slider defaults to 0.60, detection defaults to Auto',
    (await tid(page, 'conf-value').innerText()) === '0.60' && await tid(page, 'mode-ARMED').count() === 1 &&
    (await tid(page, 'mode-AUTO').getAttribute('aria-pressed')) === 'true' && await tid(page, 'toggle-rec').count() === 1);
  await tid(page, 'open-system').click();
  await tid(page, 'system-drawer').waitFor();
  check('E2E-UI.1', 'Gear opens the System drawer: model, Test Mode, GPU sharing, event log',
    await tid(page, 'control-board').count() === 1 && await tid(page, 'test-mode-card').count() === 1 &&
    await tid(page, 'hardware-card').count() === 1 && await tid(page, 'system-drawer').locator('[data-testid="event-log"]').count() === 1);
  await tid(page, 'ai-test').click();
  const aiTest = await until(async () => (await tid(page, 'ai-test-result').innerText()), 8000);
  check('E2E-UI.5', 'System drawer: Gemini card tests the key with one tiny request and says the result',
    /the key works/.test(aiTest || ''), aiTest || '');
  check('E2E-UI.8', 'Gemini defaults to Flash (standard)', (await tid(page, 'ai-preset-flash').getAttribute('aria-checked')) === 'true');
  await tid(page, 'ai-preset-lite').click();
  const liteOn = await until(async () => (await tid(page, 'ai-preset-lite').getAttribute('aria-checked')) === 'true', 5000);
  const liteSaved = (await api('/api/ai/status')).model === 'gemini-3.5-flash-lite';
  await tid(page, 'ai-preset-flash').click();
  const backToFlash = await until(async () => (await api('/api/ai/status')).model === 'gemini-3.8-flash', 5000);
  check('E2E-UI.9', 'Flash-Lite (bulk) can be chosen and the backend saves it; switching back restores Flash', !!liteOn && liteSaved && !!backToFlash);
  await shot(page, 's0_system_drawer');
  // ---- 3.6.3: settings file + PDF templates
  check('E2E-UI.20', 'System drawer: settings file card says no config.toml is read (defaults) and offers to create one',
    (await tid(page, 'config-state').innerText()).trim() === 'defaults' && await tid(page, 'config-create').count() === 1);
  await tid(page, 'config-open').click();
  await tid(page, 'config-dialog').waitFor();
  const allRows = await tid(page, 'config-row').count();
  await tid(page, 'config-search').fill('delay');
  const delayRow = page.locator('[data-testid="config-row"][data-key="live.decoder_threads"]');
  const delayFound = await delayRow.count() === 1;
  if (delayFound && !(await delayRow.locator('[data-testid="config-detail"]').count())) await delayRow.locator('button').first().click();
  const detail = await delayRow.locator('[data-testid="config-detail"]').innerText().catch(() => '');
  await shot(page, 's0_settings_dialog');
  await tid(page, 'config-dialog-close').click();
  const trailCur = (await api('/api/logs')).current;
  const trailText = await until(async () => { const t = await (await fetch(`${API}/api/logs/${trailCur}`)).text(); return /\[ui \w+\] Dashboard loaded/.test(t) && t; }, 8000);
  check('E2E-UI.24', 'Event log paper trail: the browser\'s entries are saved to this session\'s text file, shown in the System drawer',
    !!trailText && (await tid(page, 'log-trail-file').innerText()).includes(trailCur), trailCur);
  check('E2E-UI.23', 'Settings: every setting in use is listed with its value and source; search finds it; each explains what it does, when to change it and the allowed range',
    allRows >= 20 && delayFound && /When to change/.test(detail) && /allowed 0 to 32/.test(detail) && /FYP_LIVE_DECODER_THREADS/.test(detail),
    `${allRows} rows`);
  await tid(page, 'templates-manage').click();
  await tid(page, 'template-editor').waitFor();
  await tid(page, 'tpl-company').fill('ACME Offshore Services');
  await tid(page, 'tpl-footer').fill('ACME confidential');
  await tid(page, 'template-save').click();
  const tplSaved = await until(async () => /Saved/.test(await tid(page, 'template-msg').innerText()), 5000);
  await tid(page, 'template-new-id').fill('acme');
  await tid(page, 'template-duplicate').click();
  const tplDup = await until(async () => (await api("/api/report-templates")).templates.some((t) => t.id === 'acme'), 5000);
  await tid(page, 'template-make-default').click();
  const madeDefault = await until(async () => (await api('/api/report-templates')).default === 'acme', 5000);
  const previewSrc = await tid(page, 'template-preview').getAttribute('src');
  const prev = await fetch(previewSrc);
  await shot(page, 's0_template_editor');
  check('E2E-UI.21', 'Template editor: edit company / footer, save, copy as a new client template, make it the default, PDF preview',
    !!tplSaved && !!tplDup && !!madeDefault && prev.headers.get('content-type') === 'application/pdf',
    `${previewSrc} -> ${prev.headers.get('content-type')}`);
  await tid(page, 'template-close').click();
  await tid(page, 'close-system').click();
  await tid(page, 'tab-review').click();
  check('E2E-0.4', 'Review tab boots on the media library (no file auto-selected)',
    await tid(page, 'review-library').count() === 1 && await page.locator('video').count() === 0);
  check('E2E-CB.2', 'Review has no threshold slider and no live controls',
    await tid(page, 'trigger-slider').count() === 0 && await tid(page, 'toggle-rec').count() === 0);

  // ------------------------------------------------------------ SCENARIO 3: historical dive, imported in place
  await tid(page, 'tab-batch').click();
  check('E2E-CB.3', 'Batch tab: no sliders or live controls; explains the per-dive threshold',
    await tid(page, 'trigger-slider').count() === 0 && await tid(page, 'toggle-rec').count() === 0 && await tid(page, 'batch-note').count() === 1);
  await tid(page, 'browse-video').click();
  await tid(page, 'file-browser').waitFor();
  await tid(page, 'fb-file').filter({ hasText: 'ML01_dive_last_month.mp4' }).click();
  await tid(page, 'ingest-name').fill('ML-01 Dive (last month)');
  await tid(page, 'ingest-line').selectOption('ML-01-NORTH');
  check('E2E-3.1a', 'Import is blocked until a detection threshold is chosen', await tid(page, 'ingest-submit').isDisabled());
  await tid(page, 'thr-0.25').click();
  await tid(page, 'ingest-submit').click();
  const note = await until(async () => (await tid(page, 'ingest-notice').innerText()).includes('in place') && tid(page, 'ingest-notice').innerText());
  check('E2E-3.1b', 'Registered in place (no copy), queued at the chosen threshold', !!note && /0\.25/.test(note), note);
  const seen = new Set();
  const row = tid(page, 'vault-row').filter({ hasText: 'ML-01 Dive (last month)' });
  for (let i = 0; i < 160; i++) {
    const t = await row.innerText().catch(() => '');
    [['HASHING', 'Fingerprinting'], ['QUEUED', 'Queued'], ['PROCESSING', 'Processing'], ['COMPLETED', 'Completed']]
      .forEach(([s, w]) => t.includes(w) && seen.add(s));
    if (t.includes('Completed')) break;
    await page.waitForTimeout(250);
  }
  check('E2E-3.2', 'Vault shows the state machine progressing to COMPLETED', seen.has('COMPLETED'), [...seen].join(' → '));
  await shot(page, 's3_batch_completed');
  await tid(page, 'browse-video').click();
  await tid(page, 'fb-file').filter({ hasText: 'ML01_dive_last_month.mp4' }).click();
  await tid(page, 'thr-0.25').click();
  await tid(page, 'ingest-submit').click();
  const dup = await until(async () => /already ingested/i.test(await tid(page, 'ingest-notice').innerText()) && tid(page, 'ingest-notice').innerText(), 8000);
  check('E2E-3.1c', 'Importing the same file again is rejected as a duplicate (SHA-256)', !!dup, dup || '');

  check('E2E-3.2b', 'Dive row shows its defect events per type', /deformation/.test(await row.locator('[data-testid="defect-cell"]').innerText()));
  const timeTxt = await row.locator('[data-testid="time-cell"]').innerText();
  const timeTitle = await row.locator('[data-testid="time-value"]').getAttribute('title').catch(() => '');
  const csvHrefT = await tid(page, 'timings-csv').getAttribute('href');
  const csvT = await (await fetch(csvHrefT.startsWith('http') ? csvHrefT : API + csvHrefT)).text();
  check('E2E-3.2c', 'Batch table shows how long the analysis took (speed × real time, breakdown on hover); all runs as CSV',
    /\d+(\.\d)? s|\d+ min/.test(timeTxt) && /× real time/.test(timeTxt) && /Processing: .* for .* of video/.test(timeTitle || '')
      && /ML-01 Dive \(last month\),.*,BATCH,/.test(csvT), timeTxt.replace(/\n/g, ' · '));
  await tid(page, 'zone-Mid-Water').click();
  const zoneChip = await tid(page, 'zone-filter').count() === 1;
  const filtered = await tid(page, 'vault-row').count();
  await tid(page, 'zone-Whole').click();
  check('E2E-UI.4', 'Registry zone click filters the dive table (dive without depth log is not in any zone)',
    zoneChip && filtered === 0 && await tid(page, 'vault-row').count() >= 1, `rows in Mid-Water: ${filtered}`);
  // ---- 3.6: several files in one batch, shared settings + a per-file override
  await tid(page, 'browse-video').click();
  await tid(page, 'file-browser').waitFor();
  await page.locator('[data-testid="fb-check"][aria-label="Tick ML02_dive_a.mp4"]').click();
  await page.locator('[data-testid="fb-check"][aria-label="Tick ML02_dive_b.mp4"]').click();
  await tid(page, 'fb-add').click();
  const stagedN = await tid(page, 'staged-row').count();
  await tid(page, 'ingest-line').selectOption('ML-02-WEST').catch(() => tid(page, 'ingest-line').selectOption({ index: 1 }));
  await tid(page, 'thr-0.5').click();
  await tid(page, 'staged-row').nth(1).locator('[data-testid="staged-conf"]').selectOption('0.6');
  await tid(page, 'ingest-submit').click();
  const multiNote = await until(async () => { const t = await tid(page, 'ingest-notice').innerText(); return /Registered 2 of 2/.test(t) && t; }, 10000);
  const vids = await api('/api/videos');
  const va = vids.find((v) => v.name === 'ML02_dive_a'), vb = vids.find((v) => v.name === 'ML02_dive_b');
  const confOf = (v) => v?.activeRun?.conf ?? v?.pendingConf;
  check('E2E-UI.17', 'Batch: tick several files, shared line + threshold with a per-file override, one click imports all',
    stagedN === 2 && !!multiNote && va && vb && va.lineId === vb.lineId && va.lineId !== 'ML-01-NORTH'
      && Math.abs(confOf(va) - 0.5) < 1e-9 && Math.abs(confOf(vb) - 0.6) < 1e-9 && await tid(page, 'staged').count() === 0,
    `${stagedN} staged · ${va?.lineId} ${confOf(va)} / ${confOf(vb)}`);
  await shot(page, 's3_multi_import');
  const names = async () => tid(page, 'vault-row').evaluateAll((els) => els.map((e) => e.getAttribute('data-name')));
  await tid(page, 'sort-name').click();
  const asc = await names();
  await tid(page, 'sort-name').click();
  const desc = await names();
  const sortedAsc = [...asc].sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }));
  await tid(page, 'sort-added').click();
  check('E2E-UI.18', 'Batch table: column headers sort (click again reverses); each row shows when it was added',
    JSON.stringify(asc) === JSON.stringify(sortedAsc) && JSON.stringify(desc) === JSON.stringify([...sortedAsc].reverse())
      && (await tid(page, 'added-cell').first().innerText()).trim().length > 4, asc.join(' | '));

  await row.locator('[data-testid="row-review"]').click();
  await page.waitForTimeout(1200);
  const ind = await tid(page, 'threshold-indicator').innerText().catch(() => '');
  check('E2E-3.3', 'Review opens the dive with its recorded threshold shown (indicator, not a slider)',
    /Batch · ≥ 0\.25/.test(ind) && await tid(page, 'event-list').locator('button').count() >= 3, ind);
  check('E2E-3.3b', 'Dive without a sensor log says so instead of showing values',
    /No ROV telemetry recorded/i.test(await tid(page, 'telemetry-card').innerText()));
  await shot(page, 's3_review_dive');

  // ------------------------------------------------------------ SCENARIO 1: live dive with the BlueROV2 (simulated over MAVLink)
  await tid(page, 'tab-live').click();
  sim = spawn(process.env.PYTHON || 'python3', [path.join(ROOT, 'tools', 'bluerov_sim.py'), '--url', MAV,
    '--start', '0', '--target', '120.5', '--rate', '60'], { stdio: 'ignore' });
  const linked = await until(async () => (await tid(page, 'rov-pill').first().getAttribute('data-state')) === 'CONNECTED' &&
    (await tid(page, 'tel-depth').count()) === 1 && (await tid(page, 'rov-offline').count()) === 0, 15000);
  check('E2E-1.0', 'BlueROV2 heartbeat → ROV link CONNECTED, offline placeholder replaced by telemetry', !!linked);
  await until(async () => /ROV 120\.5 m/.test(await tid(page, 'twin-rov').textContent()), 15000);
  check('E2E-1.2', 'Digital twin + telemetry follow the vehicle to 120.5 m (Mid-Water)',
    (await tid(page, 'tel-depth').innerText()) === '120.5' && /Mid-Water Zone/.test(await tid(page, 'tel-segment').innerText()));
  await shot(page, 's1_rov_connected');
  const src = tid(page, 'live-source');
  const opts = await src.locator('option').allTextContents();
  await src.selectOption({ label: opts.find((o) => o.includes('ML-01 Dive (last month)')) });
  await tid(page, 'connect').click();
  await page.locator('img[alt="Live ROV feed"]').waitFor({ timeout: 15000 });
  const liveCard = page.locator('img[alt="Live ROV feed"]').locator('xpath=ancestor::div[contains(@class,"rounded-2xl")][1]');
  check('E2E-1.1', 'Live feed is a broadcast: no <video>, no playback controls',
    await liveCard.locator('video').count() === 0 && await liveCard.locator('[data-testid="review-track"]').count() === 0
      && /last 2 min/.test(await tid(page, 'live-strip').innerText()));
  await page.locator('[data-testid="incident-alert"][data-stage="saved"]').waitFor({ timeout: 40000 });
  const alertText = (await tid(page, 'incident-alert').innerText()).replace(/\s+/g, ' ');
  await shot(page, 's1_alert');
  const clipLog = await tid(page, 'event-log').first().innerText();
  check('E2E-1.3', 'Alert: Deformation 88 % at 120.5 m (Mid-Water); the log records the ≥ 0.60 trigger',
    /Deformation 88% at 120\.5 m, Mid-Water/.test(alertText) && /trigger 0\.60/.test(clipLog), alertText);
  check('E2E-1.5a', 'Alert reads "Deformation clipped" (no severity claimed) with Review / Report buttons',
    /deformation clipped/i.test(alertText) && !/critical/i.test(alertText) && await tid(page, 'review-incident').count() === 1 && await tid(page, 'report-incident').count() === 1);
  await tid(page, 'report-incident').click();
  await tid(page, 'report-dialog').waitFor();
  check('E2E-UI.2', 'Report from the alert opens the report assistant scoped to that incident (live keeps running)',
    /incident ·/i.test(await tid(page, 'report-scope').locator('option:checked').innerText()) && (await api('/api/live/state')).status === 'LIVE');
  await shot(page, 's1_report_from_alert');
  await tid(page, 'close-report-dialog').click();
  await tid(page, 'review-incident').click();
  await page.waitForTimeout(1500);

  // ------------------------------------------------------------ SCENARIO 2: review the clip
  const clipName = await tid(page, 'review-select').locator('option:checked').innerText();
  check('E2E-1.5b', 'Hand-off lands in Review with the incident clip loaded',
    /^ML-01-NORTH_Incident_120_5m_\d{4}-\d{2}-\d{2}_\d{6}\.mp4$/.test(clipName), clipName);
  const clipInd = await tid(page, 'threshold-indicator').innerText().catch(() => '');
  check('E2E-2.0', 'Review shows how the clip was recorded: logged ≥ 0.40, triggered ≥ 0.60', /Recorded live · ≥ 0\.40 · trigger 0\.60/.test(clipInd), clipInd);
  check('E2E-2.1', 'Scrubber shows the trigger marker', await tid(page, 'trigger-marker').count() === 1);
  check('E2E-UI.6', 'Previous defect is disabled at the start of the clip; next defect is enabled',
    await tid(page, 'prev-defect').isDisabled() && !(await tid(page, 'next-defect').isDisabled()));
  await tid(page, 'review-fullscreen').click();
  const fs = await tid(page, 'fs-controls').waitFor({ timeout: 3000 }).then(() => true).catch(() => false);
  const fsBoxes = fs ? await page.evaluate(() => !!document.fullscreenElement?.querySelector('[data-source="DB"]')) : false;
  check('E2E-UI.7', 'Full screen shows the player with its bounding-box layer and controls', fs && fsBoxes);
  if (fs) await page.evaluate(() => document.exitFullscreen());
  await tid(page, 'trigger-marker').click();
  await page.waitForTimeout(600);
  const at40 = await boxes(page);
  check('E2E-2.2', 'Marker snaps to the trigger frame and shows the logged deformation box only',
    at40.length === 1 && at40[0].type === 'DEFORMATION', JSON.stringify(at40.map((b) => b.type)));
  check('E2E-2.2b', 'Review telemetry comes from the recorded sensor log (120.5 m)',
    /^120\.5 m$/.test(await tid(page, 'tel-depth').innerText()));
  check('E2E-2.2c', 'Clip lanes: deformation is drawn from the trigger to the end (post-roll)',
    Number(await tid(page, 'lane-deformation').getAttribute('data-bars')) >= 1);
  await shot(page, 's2_clip_recorded');
  const parseT = (s) => { const m = /(\d+):(\d+)\.(\d+)/.exec(s || ''); return m ? Number(m[1]) * 60 + Number(m[2]) + Number(m[3]) / 1000 : NaN; };
  await tid(page, 'review-track').click({ position: { x: 3, y: 10 } });
  await page.waitForTimeout(300);
  const laneBar = tid(page, 'lane-deformation-bar').first();
  const barStart = Number(await laneBar.getAttribute('data-start'));
  await laneBar.click();
  await page.waitForTimeout(500);
  const tLane = parseT(await tid(page, 'review-time').innerText());
  check('E2E-UI.13', 'Clicking a defect bar in a lane jumps to the start of that sighting', Math.abs(tLane - barStart) < 0.06, `bar ${barStart} → ${tLane}`);
  await tid(page, 'reanalyse').locator('[data-testid="thr-0.25"]').click();
  await tid(page, 'reanalyse-btn').click();
  const twoRuns = await until(async () => (await tid(page, 'run-item').count()) === 2 && (await tid(page, 'threshold-indicator').innerText()).includes('Re-analysis'), 30000);
  await tid(page, 'trigger-marker').click();
  await page.waitForTimeout(700);
  const at25 = await boxes(page);
  await shot(page, 's2_reanalysed_025');
  check('E2E-2.3a', 'Re-analyse at 0.25 → new analysis, original kept (2 analyses listed)', !!twoRuns);
  check('E2E-2.3b', 'Secondary pitting now appears on the same trigger frame',
    at25.some((b) => b.type === 'CORROSION' && /pitting/i.test(b.text)), JSON.stringify(at25.map((b) => b.text)));
  const liveTrig = (await api('/api/live/state')).triggerConf;
  check('E2E-2.3c', 'Review never changes the live trigger threshold', Math.abs(liveTrig - 0.6) < 1e-9, `live trigger ${liveTrig}`);

  // ---- report assistant
  check('E2E-2.4a', 'Report scope defaults to this incident', /Incident ·/.test(await tid(page, 'report-scope').locator('option:checked').innerText()));
  await tid(page, 'generate-report').click();
  const progressSeen = await tid(page, 'report-progress').first().waitFor({ timeout: 3000 }).then(() => true).catch(() => false);
  await tid(page, 'report-modal').waitFor({ timeout: 20000 });
  const stageAtEnd = await tid(page, 'report-progress').first().getAttribute('data-stage').catch(() => null);
  check('E2E-UI.15', 'Generating a report shows staged progress (facts → key frames → draft → check) until the draft opens',
    progressSeen && (stageAtEnd === 'done' || stageAtEnd === null), `last stage: ${stageAtEnd}`);
  await page.waitForTimeout(800);
  const grounded = await tid(page, 'grounding').getAttribute('data-passed');
  const imgsOk = await tid(page, 'keyframe').evaluateAll((els) => els.length >= 2 && els.every((i) => i.complete && i.naturalWidth > 0));
  await shot(page, 's2_report');
  check('E2E-2.4b', 'Draft report is grounded (claims checked against the DB) with key frames', grounded === 'true' && imgsOk);
  check('E2E-2.4c', 'Findings carry depth + segment from the ROV log, not the UI selection',
    /120\.5 m · Mid-Water Zone/.test(await tid(page, 'finding').first().innerText()));
  await tid(page, 'finding').filter({ hasText: 'DEFORMATION' }).first().locator('[data-testid="cite"]').click();
  await page.waitForTimeout(900);
  const tAfterCite = await tid(page, 'review-time').innerText();
  check('E2E-2.4d', 'Clicking a cited event jumps the review player to it', /^0?0:0[23]\./.test(tAfterCite), tAfterCite);
  await tid(page, 'report-item').first().click();
  await tid(page, 'report-modal').waitFor();
  await tid(page, 'engineer-name').fill('S. Ajed');
  await tid(page, 'approve-report').click();
  const approved = await tid(page, 'approved-by').waitFor({ timeout: 5000 }).then(() => true).catch(() => false);
  check('E2E-2.5c', 'Engineer approval locks the report', approved && await tid(page, 'save-report').count() === 0);
  const pdfHref = await tid(page, 'export-pdf').getAttribute('href');
  const pdfRes = await fetch(pdfHref);
  const pdfBytes = Buffer.from(await pdfRes.arrayBuffer());
  check('E2E-UI.22', 'Report view: "Download PDF" gives a real PDF file (default template) named after the scope',
    pdfRes.headers.get('content-type') === 'application/pdf' && pdfBytes.subarray(0, 5).toString() === '%PDF-' &&
    /attachment; filename=".+_approved\.pdf"/.test(pdfRes.headers.get('content-disposition') || '') &&
    (await tid(page, 'pdf-template').count()) === 1, `${pdfBytes.length} bytes, ${pdfRes.headers.get('content-disposition')}`);
  await shot(page, 's2_report_approved');
  await tid(page, 'close-report').click();
  const btn = await tid(page, 'generate-report').innerText();
  check('E2E-2.5a', 'Token-saver: AI generation locked for 30 s', /AI cooldown \((2\d|30)s\)/i.test(btn) && await tid(page, 'generate-report').isDisabled(), btn);
  await page.reload();
  await page.waitForTimeout(3000);
  await tid(page, 'live-report').click();
  const afterReload = (await until(async () => { const t = await tid(page, 'generate-report').innerText(); return /AI cooldown/i.test(t) && t; }, 5000))
    || await tid(page, 'generate-report').innerText();
  check('E2E-2.5b', 'Cooldown survives a page refresh (server-enforced)', /AI cooldown/i.test(afterReload), afterReload);
  await tid(page, 'close-report-dialog').click();

  // ---- 3.6: reports directory
  await tid(page, 'tab-reports').click();
  await tid(page, 'reports-directory').waitFor();
  const repRows = await until(async () => (await tid(page, 'rep-row').count()) >= 1 && tid(page, 'rep-row').count(), 6000);
  const apiReps = (await api('/api/reports?limit=500')).sort((a, b) => (b.createdAt || '').localeCompare(a.createdAt || ''));
  const firstDefault = await tid(page, 'rep-row').first().getAttribute('data-id');
  await tid(page, 'sort-created').click();
  const firstAscRep = await tid(page, 'rep-row').first().getAttribute('data-id');
  await tid(page, 'sort-created').click();
  const approvedListed = /Approved/.test(await tid(page, 'rep-list').innerText());
  await tid(page, 'rep-row').first().click();
  const repOpens = await tid(page, 'report-modal').waitFor({ timeout: 5000 }).then(() => true).catch(() => false);
  await shot(page, 's4_report_open_from_directory');
  if (repOpens) await tid(page, 'close-report').click();
  await shot(page, 's4_reports_directory');
  check('E2E-UI.16', 'Reports tab lists every report (newest first, sortable, dated) and opens one',
    repRows === apiReps.length && firstDefault === apiReps[0].id && firstAscRep === apiReps[apiReps.length - 1].id && approvedListed && repOpens
      && (await tid(page, 'rep-created').first().innerText()).trim().length > 4, `${repRows} rows`);
  // ---- 3.6.2: Gemini busy -> rule-based draft -> "Redraft with Gemini" replaces it in place
  const postJ = (p, body) => api(p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  const clipV = (await api('/api/videos')).find((v) => v.sourceType === 'INCIDENT_CLIP' && !v.missing);
  await postJ('/__e2e/fake-gemini', { busy: 'gemini-3.8-flash,gemini-3.5-flash-lite' });
  const busyRep = await postJ('/api/reports', { scope_type: 'INCIDENT', scope_ref: clipV.id, use_ai: true });
  await postJ('/__e2e/fake-gemini', { busy: '' });
  await tid(page, 'rep-search').fill('').catch(() => {});
  const busyRow = page.locator(`[data-testid="rep-row"][data-id="${busyRep.id}"]`);
  await busyRow.waitFor({ timeout: 8000 });
  await busyRow.click();
  await tid(page, 'report-modal').waitFor();
  const noteKind = await tid(page, 'ai-fallback-note').getAttribute('data-kind').catch(() => null);
  await shot(page, 's4_report_gemini_busy');
  await tid(page, 'redraft').click();
  const redrafted = await until(async () => (await tid(page, 'ai-fallback-note').count()) === 0
    && /Gemini 3\.8 Flash/.test(await tid(page, 'report-modal').innerText()), 15000);
  const after = await api(`/api/reports/${busyRep.id}`);
  await shot(page, 's4_report_redrafted');
  check('E2E-UI.19', 'Gemini busy: the rule-based draft offers "Redraft with Gemini", which replaces it in place when Gemini is back',
    busyRep.generator === 'RULE_BASED' && noteKind === 'rules' && !!redrafted && after.generator === 'gemini-3.8-flash' && after.id === busyRep.id,
    `${busyRep.generator} → ${after.generator}`);
  await tid(page, 'close-report').click();
  await tid(page, 'tab-review').click();

  const globalAlert = await tid(page, 'global-incident').waitFor({ timeout: 40000 }).then(() => true).catch(() => false);
  check('E2E-1.6', 'A new incident while on another tab still raises an alert (live keeps running)', globalAlert);
  await shot(page, 's2_global_alert');

  // ---- media library (3.5.5): newest first, filters, pins stored on the server
  if (await tid(page, 'review-library-back').count()) await tid(page, 'review-library-back').click();
  await tid(page, 'review-library').waitFor();
  await page.waitForTimeout(400);
  const expect = (await api('/api/videos')).filter((v) => !v.missing && v.playable && (v.sourceType === 'INCIDENT_CLIP' || v.status === 'COMPLETED'))
    .sort((a, b) => b.createdTs - a.createdTs).map((v) => v.id);
  const shownIds = await tid(page, 'lib-row').evaluateAll((els) => els.map((e) => e.getAttribute('data-id')));
  check('E2E-UI.10', 'Library lists every dive and clip, newest first, grouped by day',
    shownIds.length >= 3 && JSON.stringify(shownIds) === JSON.stringify(expect) && /today/i.test(await tid(page, 'lib-list').innerText()),
    `${shownIds.length} rows`);
  await tid(page, 'lib-filter-clips').click();
  const clipRows = await tid(page, 'lib-row').count();
  const clipsOnly = (await tid(page, 'review-pick').allInnerTexts()).every((t) => /Incident clip/.test(t));
  await tid(page, 'lib-filter-all').click();
  await tid(page, 'lib-search').fill('last month');
  const searched = await tid(page, 'lib-row').allInnerTexts();
  await tid(page, 'lib-search').fill('');
  check('E2E-UI.11', 'Library filters (incident clips only) and search narrow the list',
    clipRows >= 2 && clipsOnly && searched.length === 1 && /ML-01 Dive \(last month\)/.test(searched[0]), `${clipRows} clips, search → ${searched.length}`);
  const diveRow = tid(page, 'lib-row').filter({ hasText: 'ML-01 Dive (last month)' });
  await diveRow.locator('[data-testid="lib-pin"]').click();
  const pinnedTop = await until(async () => (await tid(page, 'lib-pinned').locator('[data-testid="lib-row"]').count()) === 1
    && /last month/.test(await tid(page, 'lib-row').first().innerText()), 5000);
  await shot(page, 's2_media_library');
  await page.reload();
  await tid(page, 'tab-review').click();
  await tid(page, 'review-library').waitFor();
  const kept = await until(async () => (await tid(page, 'lib-pinned').count()) === 1
    && (await tid(page, 'lib-row').first().getAttribute('data-pinned')) === '1', 5000);
  await tid(page, 'lib-row').first().locator('[data-testid="lib-pin"]').click();
  const unpinned = await until(async () => (await tid(page, 'lib-pinned').count()) === 0
    && !(await api('/api/videos')).some((v) => v.pinned), 5000);
  check('E2E-UI.12', 'Pinning moves a dive to the top, survives a page reload (saved on the server) and can be undone',
    !!pinnedTop && !!kept && !!unpinned);

  // ---- live controls
  await tid(page, 'tab-live').click();
  await page.waitForTimeout(500);
  const stackN = await tid(page, 'incident-count').innerText().catch(() => '');
  check('E2E-UI.3', 'Several incidents in one dive stack in one alert (count + earlier incidents with Review/Report)',
    /^\d+ this dive$/.test(stackN) && await tid(page, 'incident-earlier').locator('button:has-text("Report")').count() >= 1, stackN);
  await shot(page, 's1_alert_stack');
  const spans = await tid(page, 'strip-clip').count();
  let stripOpened = false, stripClip = '', stripT = NaN;
  if (spans) {
    const target = tid(page, 'strip-clip').last();
    stripClip = await target.getAttribute('data-clip');
    await target.click();
    await page.waitForTimeout(1500);
    stripOpened = await tid(page, 'review-video').count() === 1;
    stripT = parseT(await tid(page, 'review-time').innerText().catch(() => ''));
    const selected = await tid(page, 'review-select').inputValue().catch(() => '');
    stripOpened = stripOpened && selected === stripClip;
    await tid(page, 'tab-live').click();
    await page.waitForTimeout(500);
  }
  check('E2E-UI.14', 'Live sightings strip: a clipped sighting is framed and opens its clip in Review at that moment',
    spans >= 1 && stripOpened && stripT > 0.3, `${spans} framed · opened=${stripOpened} · t=${stripT}`);
  await tid(page, 'mode-STANDBY').click();
  const standby = await tid(page, 'standby-badge').waitFor({ timeout: 5000 }).then(() => true).catch(() => false);
  check('E2E-1.7', 'Standby: video keeps streaming, detection stops (badge shown)', standby && await page.locator('img[alt="Live ROV feed"]').count() === 1);
  await tid(page, 'mode-ARMED').click();
  await tid(page, 'freeze').click();
  const frozen = await tid(page, 'frozen-frame').waitFor({ timeout: 5000 }).then(() => true).catch(() => false);
  const stillLive = (await api('/api/live/state')).status === 'LIVE';
  check('E2E-1.8', 'Freeze Frame freezes the view only — capture keeps running', frozen && stillLive);
  await tid(page, 'unfreeze').click();

  // ---- live dive log (3.6.5): the running dive's own record, opened from the Live tab
  await tid(page, 'open-dive-log').click();
  await tid(page, 'dive-log').waitFor({ timeout: 10000 });
  const logReady = await until(async () => (await tid(page, 'dive-log-clip-marker').count()) >= 1
    && ((await tid(page, 'dive-log-depth').getAttribute('d')) || '').length > 40, 15000);
  const liveSid = (await api('/api/live/state')).sessionId;
  const box = await tid(page, 'dive-log-chart').boundingBox();
  await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.5);
  const readout = await tid(page, 'dive-log-readout').innerText().catch(() => '');
  check('E2E-LOG.1', 'Live tab "Dive log" opens the running dive: LIVE, depth over time, clip markers, sightings, hover readout',
    !!logReady && await tid(page, 'dive-log-live').count() === 1 && await tid(page, 'dive-log-sighting').count() >= 1
      && /Depth\s+\d+(\.\d)? m/.test(readout) && /\d+(\.\d)? V/.test(readout),
    `${await tid(page, 'dive-log-clip-marker').count()} clips · ${await tid(page, 'dive-log-sighting').count()} sightings · ${readout.replace(/\n/g, ' / ')}`);
  await tid(page, 'dive-log-download').click();
  const dl = await tid(page, 'dive-log-download-menu').innerText();
  const csvHref = await tid(page, 'dive-log-csv-sensors').getAttribute('href');
  const csvOk = (await (await fetch(csvHref.startsWith('http') ? csvHref : API + csvHref)).text()).startsWith('t_sec,depth');
  await tid(page, 'dive-log-download').click();
  await tid(page, 'dive-log-tab-settings').click();
  const settingsTxt = await tid(page, 'dive-log-settings').innerText();
  await tid(page, 'dive-log-tab-summary').click();
  const summaryTxt = await tid(page, 'dive-log-summary').innerText();
  await shot(page, 's1_dive_log_live');
  check('E2E-LOG.2', 'Dive log: downloads (sensor CSV of every reading, .tlog), settings changes and a summary',
    csvOk && /\.tlog/.test(dl) && /Detection mode/.test(settingsTxt) && /standby/i.test(settingsTxt) && /time in each depth zone/i.test(summaryTxt),
    `csv=${csvOk} · ${dl.replace(/\n/g, ' / ')} · settings: ${settingsTxt.replace(/\n/g, ' / ').slice(0, 300)}`);
  const marker = tid(page, 'dive-log-clip-marker').first();
  const markClip = await marker.getAttribute('data-clip');
  await marker.click();
  const fromLog = await until(async () => (await tid(page, 'review-video').count()) === 1
    && (await tid(page, 'review-select').inputValue().catch(() => '')) === markClip, 8000);
  await page.waitForTimeout(800);
  const logT = parseT(await tid(page, 'review-time').innerText().catch(() => ''));
  const backLabel = await tid(page, 'review-library-back').innerText();
  await tid(page, 'review-library-back').click();
  const backToLog = await tid(page, 'dive-log').waitFor({ timeout: 5000 }).then(() => true).catch(() => false);
  check('E2E-LOG.3', 'Clicking a clip on the dive log opens that clip in Review at the trigger; closing it returns to the dive log',
    !!fromLog && logT > 0.3 && /Dive log/.test(backLabel) && backToLog, `t=${logT} · back="${backLabel}"`);
  await tid(page, 'dive-log-back').click();
  await tid(page, 'review-library').waitFor();
  const logRow = await until(async () => (await page.locator(`[data-testid="lib-logs"] [data-testid="lib-log-row"][data-id="${liveSid}"]`).count()) === 1, 5000);
  await tid(page, 'lib-filter-logs').click();
  const logsOnly = (await tid(page, 'lib-row').count()) === 0 && (await tid(page, 'lib-log-row').count()) >= 1;
  await tid(page, 'lib-filter-all').click();
  check('E2E-LOG.4', 'The Review library lists live dive logs ("Live dive · log only") with their own filter',
    !!logRow && logsOnly && /log only/.test(await tid(page, 'lib-log-row').first().innerText()));
  await tid(page, 'tab-live').click();
  await page.waitForTimeout(400);

  // ---- tether loss
  sim.kill(); sim = null;
  const stale = await until(async () => (await tid(page, 'rov-pill').first().getAttribute('data-state')) === 'STALE', 8000);
  const offline = await until(async () => (await tid(page, 'rov-offline').count()) === 1, 15000);
  await shot(page, 's1_rov_lost');
  check('E2E-1.9', 'Vehicle silent → values marked STALE, then the offline placeholder returns', !!stale && !!offline);

  // ------------------------------------------------------------ TEST MODE (no vehicle, generic model)
  await page.click('button:has-text("Disconnect")');
  await page.waitForTimeout(1500);
  await tid(page, 'open-test-mode').click();
  await tid(page, 'test-mode-card').waitFor();
  await tid(page, 'test-video').selectOption({ label: 'ML-01 Dive (last month)' });
  await tid(page, 'stand-in-add').click();
  const classOpts = await tid(page, 'stand-in-class').first().locator('option').allTextContents();
  check('E2E-T.1', 'Stand-in editor offers the model\'s generic classes only (no defect classes)',
    classOpts.includes('mooringline') && !classOpts.includes('deformation') && !classOpts.includes('pitting'), classOpts.join(','));
  await tid(page, 'stand-in-remove').click();   // remove the row again
  await page.fill('[data-testid="test-rate"]', '400');
  await tid(page, 'test-start').click();
  const banner = await tid(page, 'test-banner').waitFor({ timeout: 10000 }).then(() => true).catch(() => false);
  await tid(page, 'close-system').click();
  const simPill = await until(async () => (await tid(page, 'rov-pill').first().getAttribute('data-simulated')) === '1' &&
    (await tid(page, 'rov-pill').first().getAttribute('data-state')) === 'CONNECTED', 15000);
  check('E2E-T.2', 'Test Mode on: banner across the app, ROV pill reads "ROV SIM", telemetry flagged simulated',
    banner && !!simPill && await tid(page, 'tel-simulated').count() === 1);
  const preselected = await tid(page, 'live-source').inputValue();
  check('E2E-T.3', 'The simulated ROV camera (UDP stream) is pre-selected as the source', preselected.startsWith('udp://'), preselected);
  await shot(page, 't1_test_mode_on');
  await tid(page, 'connect').click();
  await page.locator('img[alt="Live ROV feed"]').waitFor({ timeout: 20000 });
  const generic = await until(async () => (await page.locator('[data-testid="det-box"][data-defect="0"][data-raw="mooringline"]').count()) > 0, 30000, 100);
  check('E2E-T.4', 'Generic (non-defect) detections are drawn on the live feed', !!generic);
  await page.locator('[data-testid="incident-alert"][data-stage="saved"]').waitFor({ timeout: 40000 });
  check('E2E-T.5', 'The test dive is tagged TEST and still clips a real defect class', await tid(page, 'test-badge').count() >= 1);
  await shot(page, 't2_test_incident');
  await tid(page, 'review-incident').click();
  await page.waitForTimeout(1500);
  check('E2E-T.6', 'The test clip opens in Review with a TEST badge and simulated sensor log',
    await tid(page, 'test-badge').count() >= 1 && /Test Mode simulator/.test(await tid(page, 'review-note').innerText()));
  await shot(page, 't3_test_review');
  await tid(page, 'tab-live').click();
  await tid(page, 'open-system').click();
  await tid(page, 'test-stop').click();
  const bannerGone = await until(async () => (await tid(page, 'test-banner').count()) === 0, 10000);
  check('E2E-T.7', 'Stopping Test Mode removes the banner and ends the test dive',
    !!bannerGone && (await api('/api/live/state')).status === 'IDLE');
} catch (e) {
  check('E2E-ERR', 'Unexpected error', false, e.stack || e.message);
  await shot(page, 'error');
} finally {
  if (sim) sim.kill();
  await fetch(API + '/api/live/stop', { method: 'POST' }).catch(() => {});
  check('E2E-JS', 'No uncaught JavaScript errors', pageErrors.length === 0, pageErrors.join(' | '));
  fs.writeFileSync(path.join(ROOT, 'tests', 'results', 'e2e.json'), JSON.stringify(results, null, 2));
  await browser.close();
  const failed = results.filter((r) => !r.pass).length;
  console.log(`\n${results.length - failed}/${results.length} E2E checks passed`);
  process.exit(failed ? 1 : 0);
}
