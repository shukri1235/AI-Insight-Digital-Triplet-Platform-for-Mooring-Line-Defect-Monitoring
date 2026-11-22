"""
CFG-1  config.toml sets defaults: environment variable > config.toml > built-in default; relative paths are
       resolved from the file's folder; a broken file never stops the app; unknown keys are reported.
CFG-2  config.example.toml is generated from the same list the code reads (cannot drift) and parses.
CFG-3  The app's own choice of Gemini model wins over the file's starting value.
CFG-5  Operators can trust the file: a value the app cannot use (wrong type, out of range, unknown choice, rules that
       contradict each other) is reported with the reason and the default is used; a misspelt name gets a "did you
       mean"; docs/CONFIG.md is generated from the same list as the code and every recipe in it is valid.
CFG-4  The database address: empty = the SQLite files; a server URL (env or file) moves both databases there (sensor
       tables on the same server unless sensor_url is set); passwords never reach the UI or the logs.
"""
import json
import os
import subprocess
import sys
import textwrap

from conftest import ROOT

BACKEND = ROOT / "backend"


def _probe(tmp_path, toml_text, env=None, expr="{}"):
    cfg = tmp_path / "config.toml"
    if toml_text is not None:
        cfg.write_text(textwrap.dedent(toml_text), encoding="utf-8")
    e = {k: v for k, v in os.environ.items() if not k.startswith(("FYP_", "GEMINI_", "TRIGGER_"))}
    e.update({"FYP_CONFIG": str(cfg), "FYP_SETTINGS_FILE": str(tmp_path / "settings.json"),
              "FYP_MEDIA_DIR": str(tmp_path / "media"), "FYP_MODELS_DIR": str(tmp_path / "models"),
              "FYP_DB_PATH": str(tmp_path / "db.sqlite")})
    e.update(env or {})
    code = f"import json, config as c; print(json.dumps({expr}, default=str))"
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=e, capture_output=True, encoding="utf-8", errors="replace", check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_cfg_1_precedence_paths_errors_unknown(tmp_path, evidence):
    toml = """
        [live]
        trigger_default = 0.7
        trigger_hits = 2
        [gemini]
        thinking = "high"
        [report]
        templates_dir = "my_templates"
        [lvie]
        typo = 1
        """
    expr = ('{"trig": c.LIVE_TRIGGER_DEFAULT, "hits": c.TRIGGER_HITS, "win": c.TRIGGER_WINDOW, "think": c.GEMINI_THINKING,'
            ' "tpl": str(c.REPORT_TEMPLATES_DIR), "status": c.CONFIG_STATUS}')
    r = _probe(tmp_path, toml, expr=expr)
    assert r["trig"] == 0.7 and r["hits"] == 2 and r["win"] == 5 and r["think"] == "high"
    assert r["tpl"] == str((tmp_path / "my_templates").resolve()), "relative path resolved from the file's folder"
    assert r["status"]["loaded"] and r["status"]["unknown"] == ["lvie.typo"]
    # environment variable beats the file
    r2 = _probe(tmp_path, toml, env={"TRIGGER_HITS": "4", "GEMINI_THINKING": "low"}, expr=expr)
    assert r2["hits"] == 4 and r2["think"] == "low" and r2["trig"] == 0.7
    # no file -> built-in defaults
    (tmp_path / "config.toml").unlink()
    r3 = _probe(tmp_path, None, expr=expr)
    assert r3["trig"] == 0.6 and r3["hits"] == 3 and not r3["status"]["exists"]
    # broken file -> defaults + the error is reported, the app still starts
    r4 = _probe(tmp_path, "[live\ntrigger_default = = 0.7\n", expr=expr)
    assert r4["trig"] == 0.6 and not r4["status"]["loaded"] and r4["status"]["error"]
    evidence["CFG-1"] = {"file": r["trig"], "env_beats_file": r2["hits"], "broken_file_error": r4["status"]["error"][:80]}


def test_cfg_2_example_is_generated_from_the_spec(tmp_path):
    import tomllib
    r = _probe(tmp_path, None, expr='{"example": c.config_example(), "keys": [k for _, k, _, _ in c.SPEC]}')
    shipped = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    assert shipped == r["example"], "config.example.toml is out of date - run python tools/make_config_docs.py"
    # every setting is listed; uncommenting all of them gives a valid file with exactly the known keys
    import re
    uncommented = "\n".join(l[2:] if re.match(r"^# [a-z_]+ = ", l) else l for l in shipped.splitlines())
    data = tomllib.loads(uncommented)
    assert sorted(f"{s}.{k}" for s, v in data.items() for k in v) == sorted(r["keys"])
    assert "api_key" not in shipped.lower().replace("api key does not go here", "")


def test_cfg_3_app_choice_beats_file_model(tmp_path):
    toml = '[gemini]\nmodel = "gemini-3.6-flash"\n'
    r = _probe(tmp_path, toml, expr='{"m": c.gemini_model()}')
    assert r["m"][0] == "gemini-3.6-flash" and "config file" in r["m"][1]
    (tmp_path / "settings.json").write_text('{"gemini_model": "gemini-3.5-flash-lite"}', encoding="utf-8")
    r = _probe(tmp_path, toml, expr='{"m": c.gemini_model()}')
    assert r["m"] == ["gemini-3.5-flash-lite", "chosen in the app"]


def test_cfg_4_database_address(tmp_path, client):
    expr = '{"main": c.DATABASE_URL, "sens": c.SENSOR_DATABASE_URL, "red": c.redact_url(c.DATABASE_URL)}'
    r = _probe(tmp_path, None, expr=expr)
    assert r["main"].startswith("sqlite:///") and r["main"].endswith("db.sqlite") and r["sens"].endswith("fyp_sensors.db")
    url = "postgresql+psycopg://mooring:s3cret@db.example.rds.amazonaws.com:5432/mooring"
    r = _probe(tmp_path, f'[database]\nurl = "{url}"\n', expr=expr)
    assert r["main"] == url and r["sens"] == url and r["red"] == "postgresql+psycopg://mooring:***@db.example.rds.amazonaws.com:5432/mooring"
    r = _probe(tmp_path, None, env={"FYP_DATABASE_URL": url, "FYP_SENSOR_DATABASE_URL": "sqlite:////tmp/s.db"}, expr=expr)
    assert r["main"] == url and r["sens"] == "sqlite:////tmp/s.db"
    info = client.get("/api/config").json()
    assert info["database"]["dialect"] == "sqlite" and "***" not in info["database"]["main"]



def test_cfg_5_bad_values_are_reported_and_defaults_used(tmp_path, client, evidence):
    toml = """
        [live]
        trigger_default = 1.5
        log_conf = 0.7
        trigger_hits = 6
        trigger_hit = 2
        [batch]
        batch_size = "eight"
        sample_fps = "1.5"
        [model]
        device = "gpu"
        [rov]
        mavlink_url = "14551"
        [gemini]
        thinking = "medium"
        """
    expr = ('{"trig": c.LIVE_TRIGGER_DEFAULT, "log": c.LIVE_LOG_CONF, "hits": c.TRIGGER_HITS, "win": c.TRIGGER_WINDOW,'
            ' "bs": c.BATCH_SIZE, "fps": c.BATCH_SAMPLE_FPS, "dev": c.DEVICE, "mav": c.MAVLINK_URL, "think": c.GEMINI_THINKING,'
            ' "port": c.API_PORT, "status": c.CONFIG_STATUS}')
    r = _probe(tmp_path, toml, env={"FYP_PORT": "abc"}, expr=expr)
    st = r["status"]
    # unusable values -> default, nothing crashes
    assert r["trig"] == 0.6 and r["bs"] == 8 and r["dev"] == "auto" and r["mav"] == "udpin:0.0.0.0:14551"
    assert r["think"] == "low" and r["port"] == 8500
    assert r["fps"] == 1.5, "a number written as text is still read"
    errors = {p["key"]: p for p in st["problems"] if p["level"] == "error"}
    assert {"live.trigger_default", "batch.batch_size", "model.device", "rov.mavlink_url", "gemini.thinking",
            "server.port"} <= set(errors)
    assert "0.1 to 0.99" in errors["live.trigger_default"]["text"] and "using the default 0.6" in errors["live.trigger_default"]["text"]
    assert errors["server.port"]["source"] == "env" and "FYP_PORT" in errors["server.port"]["text"]
    # rules that contradict each other
    warn = " ".join(p["text"] for p in st["problems"] if p["level"] == "warning")
    assert r["hits"] == r["win"] == 5 and "could never be saved" in warn
    assert r["log"] == 0.7 and "live.log_conf (0.7) is above live.trigger_default" in warn
    # a typo gets a suggestion
    assert st["unknown"] == ["live.trigger_hit"] and st["suggest"]["live.trigger_hit"] == "live.trigger_hits"
    assert st["sources"]["batch.sample_fps"] == "file" and st["sources"]["live.trigger_default"] == "default"
    # the app's settings list explains each setting and says where its value came from
    info = client.get("/api/config").json()
    row = next(s for s in info["spec"] if s["key"] == "live.decoder_threads")
    assert row["title"] and row["what"] and row["tune"] and row["allowed"] == "0 to 32" and row["source"] in ("default", "env")
    assert len(info["sections"]) >= 8 and all(s["title"] and s["what"] for s in info["spec"])
    evidence["CFG-5"] = {"reported": sorted(errors), "warnings": len([p for p in st["problems"] if p["level"] == "warning"]),
                         "suggest": st["suggest"]}


def test_cfg_6_reference_doc_and_recipes(tmp_path):
    r = _probe(tmp_path, None, expr='{"md": c.config_reference_md()}')
    shipped = (ROOT / "docs" / "CONFIG.md").read_text(encoding="utf-8")
    assert shipped == r["md"], "docs/CONFIG.md is out of date - run python tools/make_config_docs.py"
    import sys
    sys.path.insert(0, str(BACKEND))
    import config as c
    import config_spec
    for rec in config_spec.RECIPES:
        for key, val in rec.changes:
            s = config_spec.SETTINGS_BY_KEY[key]                         # KeyError = recipe names a missing setting
            assert c._convert(s, val)[1] is None, (rec.title, key, val)
    for s in config_spec.SETTINGS:
        assert s.title and s.what and s.section in config_spec.SECTIONS, s.key
        assert c._convert(s, c._default_of(s))[1] is None, f"default of {s.key} fails its own check"
