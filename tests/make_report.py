"""
Regenerates tests/results/RESULTS.md (requirements traceability matrix with live
results) from tests/results/junit.xml, evidence.json and e2e.json.

    python -m pytest --junitxml=tests/results/junit.xml
    node tests/e2e/scenarios.mjs            (optional)
    python tests/make_report.py
"""
import json
import os
import platform
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

RES = Path(__file__).resolve().parent / "results"

# Requirement -> (scenario step / source, verification method, pytest tests, E2E checks, evidence keys)
REQS = [
    ("FR-1.1", "S1.1 Connect to the ROV video stream (RTSP / network / replay), auto-reconnect", "Test", ["test_tc_1_1_connects_and_streams", "test_tc_1_1_network_stream_and_reconnect"], ["E2E-1.1"], ["TC-1.1", "TC-1.1-network"]),
    ("FR-1.2", "S1.1 Live feed cannot be rewound (no scrubber / controls)", "Test + Inspection", ["test_tc_1_1_live_feed_cannot_be_rewound"], ["E2E-1.1"], []),
    ("FR-1.3", "S1.2 Telemetry comes from the BlueROV2 (MAVLink): depth, heading, Bar30 temp/pressure, sonar altitude, mode/armed", "Test", ["test_tc_1_2_digital_twin_follows_rov_depth"], ["E2E-1.0", "E2E-1.2"], ["TC-1.2"]),
    ("FR-1.3b", "No ROV → explicit offline placeholder, never simulated values; tether loss → STALE → offline → auto-recover; stale values never recorded with a clip", "Test", ["test_tc_1_2_no_rov_shows_offline_not_fake_values", "test_tc_1_2_link_loss_goes_stale_then_offline", "test_tc_1_2_stale_telemetry_never_recorded_with_a_clip"], ["E2E-0.2", "E2E-0.3", "E2E-1.9"], ["TC-1.2-offline", "TC-1.2-linkloss", "TC-1.2-stale-clip"]),
    ("FR-1.4", "S1.3 DEFORMATION @ 88 % labelled High confidence (certainty, not severity) at the ROV's real depth (120.5 m)", "Test", ["test_tc_1_3_deformation_detected_as_critical"], ["E2E-1.3"], ["TC-1.3"]),
    ("FR-1.5", "S1.4 5 s / 150-frame clip around the event, H.264, named by line/depth/date", "Test", ["test_tc_1_4_dvr_clip_is_5s_150_frames_named_and_logged", "test_tc_1_4_buffer_is_time_based_for_25fps_cameras"], [], ["TC-1.4", "TC-1.4-25fps"]),
    ("FR-1.6", "S1.4 Defect coordinates + per-frame ROV sensor readings logged with the clip", "Test", ["test_tc_1_4_defect_coordinates_logged_to_sqlite"], ["E2E-2.2b"], []),
    ("FR-1.7", "S1.5 Alert 'Deformation clipped' → 'Review Incident' bypasses ingest; alert on any tab", "Test", ["test_tc_1_5_alert_hands_off_clip_without_ingest_queue"], ["E2E-1.5a", "E2E-1.5b", "E2E-1.6"], []),
    ("FR-1.8", "Live slider = clip TRIGGER threshold (default 0.60); changes recorded with the dive", "Test", ["test_tc_1_6_live_slider_is_the_clip_trigger", "test_tc_1_6_trigger_default_is_0_60"], ["E2E-CB.1", "E2E-1.3"], ["TC-1.6"]),
    ("FR-1.9", "Detection ARMED / AUTO (depth-gated, disarms on stale link) / STANDBY; Freeze Frame is view-only", "Test", ["test_tc_1_7_detection_modes"], ["E2E-1.7", "E2E-1.8"], ["TC-1.7"]),
    ("FR-1.10", "Dive log: 5 Hz sensor samples in a separate sensor DB + raw MAVLink .tlog per dive; no rows (no repeated depth) while messages stop", "Test", ["test_tc_1_8_dive_log_sensor_db_and_tlog", "test_tc_1_8_no_repeated_depth_when_messages_stop"], [], ["TC-1.8", "TC-1.8-cut"]),
    ("FR-1.11", "Clip trigger must hold (3 of the last 5 inferred frames): one- or two-frame flickers never save a clip", "Test", ["test_tc_1_9_clip_trigger_must_hold"], [], ["TC-1.9"]),
    ("FR-2.1", "S2.1 Clip loads with DB-backed marker at the event", "Test", ["test_tc_2_1_clip_has_trigger_marker"], ["E2E-2.1"], ["TC-2.1"]),
    ("FR-2.2", "S2.2 Marker snaps to the exact frame", "Test", ["test_tc_2_2_marker_snaps_to_exact_frame"], ["E2E-2.2"], ["TC-2.2"]),
    ("FR-2.3", "S2.3 Review shows the RECORDED threshold (indicator, no slider)", "Test", ["test_tc_2_3_review_shows_recorded_threshold"], ["E2E-2.0", "E2E-3.3", "E2E-CB.2"], ["TC-2.3-indicator"]),
    ("FR-2.4", "S2.3 Re-analyse at 0.25 reveals secondary pitting on the same frame; original analysis kept", "Test", ["test_tc_2_3_reanalyse_at_0_25_reveals_pitting", "test_tc_2_3_threshold_is_required_and_bounded"], ["E2E-2.3a", "E2E-2.3b"], ["TC-2.3"]),
    ("FR-2.5", "Review never changes live settings (state isolation)", "Test", ["test_tc_2_3_review_never_changes_live_settings"], ["E2E-2.3c"], []),
    ("FR-2.6", "S2.4 Report assistant: facts from DB + sensor DB + key frames; claims checked in code", "Test", ["test_tc_2_4_report_is_grounded_in_db_and_key_frames", "test_tc_2_4_hallucination_is_caught_and_regenerated", "test_tc_2_4_persistent_hallucination_needs_review", "test_tc_2_4_rule_based_without_api_key"], ["E2E-2.4a", "E2E-2.4b", "E2E-2.4d"], ["TC-2.4", "TC-2.4-hallucination"]),
    ("FR-2.7", "S2.4 Segment comes from ROV depth, not the operator's selection ('Touchdown' vs 120 m)", "Test", ["test_tc_2_4_segment_comes_from_rov_depth_not_selection"], ["E2E-2.4c"], ["TC-2.4-segment"]),
    ("FR-2.8", "S2.5 30 s token-saver, enforced server-side; concurrent requests spend once", "Test", ["test_tc_2_5_token_saver_enforced_server_side", "test_tc_2_5_concurrent_requests_only_one_spends_tokens"], ["E2E-2.5a", "E2E-2.5b"], ["TC-2.5"]),
    ("FR-2.9", "Engineer edit / approve (locks) / export", "Test", ["test_tc_2_5_engineer_edit_approve_lock_and_export"], ["E2E-2.5c"], ["TC-2.5-signoff"]),
    ("FR-3.1", "S3.1 Threshold mandatory before processing; SHA-256 de-duplication (+ pre-upload fingerprint)", "Test", ["test_tc_3_1_upload_returns_before_processing_and_dedupes"], ["E2E-3.1a", "E2E-3.1c"], ["TC-3.1"]),
    ("FR-3.2", "S3.1 Import in place from any folder (no copy, background hash, offline media, file kept on delete); local-only browser", "Test", ["test_tc_3_1_in_place_import_no_copy", "test_tc_3_1_file_browser_is_local_only"], ["E2E-3.1b"], ["TC-3.1-inplace"]),
    ("FR-3.3", "S3.1 Upload from another device in one pass (hash while writing)", "Test", ["test_tc_3_1_streaming_upload_hashes_in_one_pass"], [], ["TC-3.1-stream"]),
    ("FR-3.4", "Historical dive: BlueROV2 .tlog attached and aligned to the video timeline", "Test", ["test_tc_3_1b_tlog_attached_and_aligned"], [], ["TC-3.1b"]),
    ("FR-3.5", "S3.2 HASHING/QUEUED → PROCESSING → COMPLETED with progress / ETA", "Test", ["test_tc_3_2_state_machine_progress_and_eta"], ["E2E-3.2"], ["TC-3.2-state"]),
    ("FR-3.6", "S3.2 Results match ground truth at the chosen threshold; non-defects never logged", "Test", ["test_tc_3_2_results_match_ground_truth", "test_tc_3_2_threshold_is_applied_and_recorded"], [], ["TC-3.2-accuracy", "TC-3.2-threshold"]),
    ("FR-3.7", "S3.3 Completed dive reviewable (markers + range streaming)", "Test", ["test_tc_3_3_completed_dive_is_reviewable"], ["E2E-3.3", "E2E-3.3b"], []),
    ("FR-3.8", "Robustness: crash mid-job resumes from checkpoint", "Test", ["test_tc_3_4_crash_mid_job_resumes_from_checkpoint"], [], ["TC-3.4"]),
    ("FR-UI.1", "To-do: empty-state boot, contextual control board per tab", "Test (E2E)", [], ["E2E-0.1", "E2E-0.4", "E2E-CB.1", "E2E-CB.2", "E2E-CB.3", "E2E-JS"], []),
    ("TM-1", "Class mapping: deformation/breakage + pitting variants; no COCO class can become a defect", "Test", ["test_tm_1_class_mapping"], [], []),
    ("TM-2", "Non-defect detections recorded in the object log (sensor DB), never in the defect DB; shown in Review", "Test", ["test_tm_2_general_detections_go_to_object_log_only"], ["E2E-T.4"], ["TM-2"]),
    ("TM-3", "Test Mode refuses to start while a real vehicle is connected", "Test", ["test_tm_3_refuses_while_real_vehicle_connected"], [], []),
    ("TM-4", "Test Mode: video streamed as the ROV camera (UDP) + simulated BlueROV2; dive/clips/sensors tagged TEST/SIM", "Test", ["test_tm_4_simulated_rov_camera_and_telemetry"], ["E2E-T.2", "E2E-T.3", "E2E-T.5", "E2E-T.6"], ["TM-4"]),
    ("TM-5", "Stand-in classes rehearse the defect workflow; mapping recorded and fixed per dive", "Test", ["test_tm_5_stand_in_rehearses_defects"], ["E2E-T.1"], ["TM-5"]),
    ("TM-6", "Batch ingest during Test Mode is tagged and uses the stand-in mapping", "Test", ["test_tm_6_batch_during_test_mode"], [], ["TM-6"]),
    ("TM-7", "Registry + reports never mix test and real data; test reports marked", "Test", ["test_tm_7_reports_keep_test_and_real_apart"], [], ["TM-7"]),
    ("TM-8", "Stopping Test Mode ends the test dive and the simulator", "Test", ["test_tm_8_stop_ends_test_dive_and_simulator"], ["E2E-T.7"], ["TM-8"]),
    ("SZ-1..3", "Registry by depth zone: events placed by ROV depth; unknown depth kept out of zones; zones sum to line totals", "Test", ["test_sz_1_and_2_events_follow_rov_depth", "test_sz_3_no_line_means_no_zones"], [], ["SZ-2"]),
    ("GA-1..3", "GPU arbiter: batch gets the whole GPU when alone; live latency bounded while batch runs; results unchanged", "Test + Measurement", ["test_ga_1_batch_gets_whole_gpu_without_live", "test_ga_2_live_latency_bounded_while_batch_runs", "test_ga_3_results_unchanged_by_scheduling"], [], ["GA-2"]),
    ("UI-1..5", "Redesign support: Auto default + wait reason; Locate file (fingerprint-checked); per-type event counts and clip incident summary; dives per zone; cancel clears object log", "Test", ["test_ui_1_auto_is_the_default_mode", "test_ui_2_locate_file_checks_identity", "test_ui_3_event_counts_and_clip_incident", "test_ui_4_history_lists_dives_per_zone", "test_ui_5_cancel_clears_object_log"], ["E2E-UI.1", "E2E-UI.2", "E2E-UI.3", "E2E-UI.4"], ["UI-2", "UI-3"]),
    ("PDF-1..3", "Real PDF export styled by client templates (logo, colours, company, sections, sign-off); template editor; default template", "Test", ["test_pdf_1_report_exports_as_pdf", "test_pdf_2_client_templates", "test_pdf_3_approved_report"], ["E2E-UI.21", "E2E-UI.22"], ["PDF-1", "PDF-2"]),
    ("CFG-1..3", "Optional config.toml: env > file > default, relative paths, broken file tolerated, example generated from the same list", "Test", ["test_cfg_1_precedence_paths_errors_unknown", "test_cfg_2_example_is_generated_from_the_spec", "test_cfg_3_app_choice_beats_file_model", "test_cfg_4_database_address"], ["E2E-UI.20"], ["CFG-1"]),
    ("CFG-5..6", "Operator-proof settings: bad values reported with the reason and the default used, contradictions flagged, typos get a suggestion; reference doc and recipes generated from the same list", "Test", ["test_cfg_5_bad_values_are_reported_and_defaults_used", "test_cfg_6_reference_doc_and_recipes"], ["E2E-UI.23"], ["CFG-5"]),
    ("UI-6..13", "Field fixes: Gemini errors explained (rule-based draft kept); key found outside the process; retired model switch; pins; report jobs + directory; bounded, timed Gemini calls; busy Gemini -> retry -> Flash-Lite -> rules in one deadline; redraft with Gemini in place", "Test", ["test_ui_6_gemini_failure_is_explained_and_never_loses_the_report", "test_ui_7_key_found_outside_the_process_environment", "test_ui_8_retired_model_suggests_the_new_one_and_switching_works", "test_ui_9_pin_media_for_the_review_library", "test_ui_10_report_jobs_show_progress_and_feed_the_directory", "test_ui_11_gemini_call_is_bounded_fast_and_timed", "test_ui_12_busy_gemini_falls_back_to_flash_lite_then_rules", "test_ui_13_redraft_with_gemini_in_place", "test_ui_14_backend_serves_the_built_ui"], ["E2E-UI.5", "E2E-UI.19"], ["UI-11", "UI-12", "UI-13"]),
    ("LOG-1..3", "Live dive log: the whole dive on one time axis (depth, zones, sightings vs trigger, clips, vehicle + operator events), readable while live and after; thinned chart keeps every depth extreme; CSV + .tlog downloads", "Test", ["test_log_1_dive_log_live_and_after", "test_log_2_thinning_keeps_the_extremes", "test_log_2b_thinning_a_four_hour_dive", "test_log_3_downloads"], ["E2E-LOG.1", "E2E-LOG.2", "E2E-LOG.3", "E2E-LOG.4"], ["LOG-1"]),
    ("LOG-4..5", "A dive left open with no data (PC asleep, Stop pressed hours later) ends at its last data, the idle time reported apart (old dives trimmed too); the app being suspended is logged with the dive", "Test", ["test_log_4_a_dive_left_open_is_not_counted", "test_log_5_app_pause_is_logged_with_the_dive"], [], ["LOG-4"]),
    ("TIME-1", "Batch processing time per analysis (decode + detection, x real time, frames/s, queue wait, proxy, resumes, GPU shared with live), in the Batch table and as CSV; a crash-resumed run adds up without the downtime", "Test + Measurement", ["test_time_1_batch_processing_time_recorded", "test_tc_3_4_crash_mid_job_resumes_from_checkpoint"], ["E2E-3.2c"], ["TIME-1"]),
    ("TIME-2", "Read-ahead: the next frames are decoded while the GPU runs detection; identical results; less time waiting for video", "Test + Measurement", ["test_time_2_read_ahead_overlaps_decoding_and_detection"], [], ["TIME-2"]),
    ("LAT-1..2", "Live picture delay: the network stream is decoded with 2 threads instead of one per CPU (each thread adds a frame of delay)", "Test + Measurement", ["test_lat_1_few_decoder_threads_cut_the_picture_delay", "test_lat_2_live_network_streams_open_with_few_decoder_threads"], [], ["LAT-1"]),
    ("LOGS-1..4", "Event log paper trail: one text file per session (browser entries + the backend's own record of dives, clips, batch runs, approvals); a dive is closed once, so a later stop cannot overwrite its end; a byte-identical clip is still saved", "Test", ["test_logs_1_browser_entries_are_appended", "test_logs_2_and_3_backend_record_and_dive_closed_once", "test_logs_4_identical_clip_is_still_saved"], ["E2E-UI.24"], ["LOGS-1", "LOGS-2"]),
    ("LBL-1..3", "Dissertation alignment (3.6.8): ML-01 splash zone 0-50 m (API RP 2I), existing databases migrated; labels High / Low confidence state certainty, not severity (one rule for deformation and pitting, old labels relabelled, Gemini facts carry confidence_label); CORROSION shown as Pitting (grade 6-7); basis of the defect classes on every report", "Test", ["test_lbl_1_splash_zone_is_50m", "test_lbl_2_labels_are_certainty_not_severity", "test_lbl_3_reports_name_pitting_and_carry_the_basis"], ["E2E-1.5a"], ["LBL-1", "LBL-2", "LBL-3"]),
    ("NFR-1", "Clip writes never stall the live loop", "Measurement", ["test_nfr_1_no_capture_stall_while_clip_is_written"], [], ["NFR-1"]),
    ("NFR-1b", "Slow inference never slows capture (latest-frame-wins)", "Measurement", ["test_nfr_1b_slow_inference_never_slows_capture"], [], ["NFR-1b"]),
    ("NFR-2", "Live keeps priority while batch runs on the same GPU (end-to-end)", "Measurement", ["test_nfr_2_live_keeps_priority_while_batch_runs"], [], ["NFR-2"]),
    ("NFR-3", "SQLite WAL (both DBs), no lock errors under concurrent writers", "Test", ["test_nfr_3_sqlite_wal_and_no_lock_errors"], [], []),
    ("NFR-4", "RAM buffer footprint", "Measurement", ["test_nfr_4_ram_buffer_footprint"], [], ["NFR-4"]),
    ("NFR-5", "Review/registry/report queries interactive at 4-hour scale", "Measurement", ["test_nfr_5_review_queries_at_4_hour_scale"], [], ["NFR-5"]),
    ("NFR-6", "Batch throughput (decode ceiling) and 4-hour estimate", "Measurement", ["test_nfr_6_batch_throughput_and_4h_estimate"], [], ["NFR-6"]),
    ("MA-1..3", "Real best.pt on CUDA, < 33 ms/frame, mAP/recall targets", "Test on target HW", ["test_ma_1_loads_on_gpu", "test_ma_2_realtime_latency", "test_ma_3_accuracy_targets"], [], []),
]


WHY: dict = {}


def _why(el, how) -> str:
    """The lines that say why a test failed: the assertion / exception and where, kept short."""
    text = (el.text or "").splitlines()
    keep = [l for l in text if l.startswith("E ") or l.strip().startswith(("tests", "backend")) or ".py:" in l][-14:]
    msg = (el.get("message") or "").strip().splitlines()
    head = f"{how}{' in setup/teardown' if how == 'error' else ''}: {msg[0][:300] if msg else ''}"
    return "\n".join([head, *keep]) if keep else head


def load():
    junit = {}
    jx = RES / "junit.xml"
    if jx.exists():
        for tc in ET.parse(jx).getroot().iter("testcase"):
            st = "PASS"
            bad = tc.find("failure") if tc.find("failure") is not None else tc.find("error")
            if bad is not None:
                st = "FAIL"
                WHY[tc.get("name")] = _why(bad, "error" if bad.tag == "error" else "failed")
            elif tc.find("skipped") is not None:
                st = "SKIP"
            junit[tc.get("name")] = (st, float(tc.get("time") or 0))
    ev = json.loads((RES / "evidence.json").read_text()) if (RES / "evidence.json").exists() else {}
    e2e = {r["id"]: r for r in json.loads((RES / "e2e.json").read_text())} if (RES / "e2e.json").exists() else {}
    return junit, ev, e2e


def fmt_ev(v) -> str:
    if isinstance(v, dict):
        return ", ".join(f"{k}={fmt_ev(x)}" for k, x in v.items() if not isinstance(x, (dict, list)) or len(str(x)) < 80)
    return str(v)


def _gpu() -> str:
    try:
        import torch
        return torch.cuda.get_device_name(0) if torch.cuda.is_available() else "none (CPU only)"
    except Exception:  # noqa: BLE001 - torch is not needed for the report
        return "unknown (torch not installed)"


def main():
    junit, ev, e2e = load()
    lines = [f"# V&V results — generated {datetime.now():%Y-%m-%d %H:%M}",
             "", f"Host: {platform.platform()}, Python {platform.python_version()}, {os.cpu_count()} CPU threads, GPU: {_gpu()}"]
    for name in ("junit.xml", "e2e.json"):                 # when each part was last run (they can come from different PCs)
        f = RES / name
        if f.exists():
            where = ""
            if name == "e2e.json":
                host = RES / "e2e_host.json"
                info = json.loads(host.read_text()) if host.exists() else {}
                # e2e_host.json is written by tools/run_e2e.py together with e2e.json, so it names the PC it ran on
                if info and abs(f.stat().st_mtime - host.stat().st_mtime) < 600:
                    where = f" (browser walkthrough run on: {info['host']})"
                else:
                    where = " (browser walkthrough: re-run it on this PC with tools\\run_e2e.bat, or report it as run in the development environment)"
            lines.append(f"- `{name}` last written {datetime.fromtimestamp(f.stat().st_mtime):%Y-%m-%d %H:%M}{where}")
    lines.append("")
    tot = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for st, _ in junit.values():
        tot[st] += 1
    e2e_pass = sum(r["pass"] for r in e2e.values())
    lines += [f"**pytest:** {tot['PASS']} passed, {tot['FAIL']} failed, {tot['SKIP']} skipped  ·  "
              f"**browser E2E:** {e2e_pass}/{len(e2e)} checks passed", ""]
    lines += ["| Req | Scenario step / requirement | Method | Result | Tests | Evidence |", "|---|---|---|---|---|---|"]
    for rid, title, method, tests, e2e_ids, ev_keys in REQS:
        sts = [junit.get(t, ("NOT RUN", 0))[0] for t in tests] + [("PASS" if e2e[i]["pass"] else "FAIL") if i in e2e else "NOT RUN" for i in e2e_ids]
        ran = [s for s in sts if s != "NOT RUN"]
        if not ran or all(s == "SKIP" for s in ran):
            res = "⏳ pending (target HW)" if rid.startswith("MA") else "— not run"
        elif "FAIL" in ran:
            res = "❌ FAIL"
        else:
            res = "✅ PASS"
        evid = "<br>".join(f"**{k}**: {fmt_ev(ev[k])}" for k in ev_keys if k in ev)
        tlist = "<br>".join([t.replace("test_", "") for t in tests] + e2e_ids)
        lines.append(f"| {rid} | {title} | {method} | {res} | {tlist} | {evid} |")
    failed = [n for n, (s, _) in junit.items() if s == "FAIL"] + [i for i, r in e2e.items() if not r["pass"]]
    if failed:
        lines += ["", "## Failures"]
        for f in failed:
            lines.append(f"- {f}")
            if f in WHY:
                lines += ["", "```", WHY[f], "```", ""]
    (RES / "RESULTS.md").write_text("\n".join(lines) + "\n")
    print(f"wrote {RES / 'RESULTS.md'} ({tot}, e2e {e2e_pass}/{len(e2e)})")


if __name__ == "__main__":
    main()
