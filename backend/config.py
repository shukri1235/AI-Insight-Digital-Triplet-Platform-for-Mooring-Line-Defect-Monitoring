"""
Central configuration.

EVERY path in the backend is resolved relative to THIS file, never to os.getcwd(), so
launching from another folder or an IDE never creates a fresh empty database.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# --- Optional settings file ----------------------------------------------------------------
# config.toml next to the app folder (or FYP_CONFIG=<path>) sets the site's defaults. Precedence for every setting:
#   environment variable  >  config.toml  >  built-in default
# Only the settings in SPEC below can be set there; config.example.toml lists them all with explanations
# (System drawer -> Settings file -> "Create config.toml" writes it). The Gemini API key stays in .env / the
# environment, never in this file. Choices made in the app (e.g. the Gemini model) are kept in settings.json and
# win over config.toml, which only provides the starting value.
CONFIG_FILE = Path(os.getenv("FYP_CONFIG", BASE_DIR.parent / "config.toml"))
CONFIG_STATUS: dict = {"file": str(CONFIG_FILE), "exists": CONFIG_FILE.exists(), "loaded": False, "error": None,
                       "unknown": [], "used": {}}
_TOML: dict = {}
if CONFIG_FILE.exists():
    try:
        import tomllib
        _TOML = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        CONFIG_STATUS["loaded"] = True
    except Exception as _e:                                     # noqa: BLE001 - a bad file must not stop the app
        CONFIG_STATUS["error"] = f"{type(_e).__name__}: {_e}"
        print(f"[CONFIG] {CONFIG_FILE} could not be read ({CONFIG_STATUS['error']}) - using defaults")

# Every setting an operator can change is described ONCE, in config_spec.py (what it does, when to change it, the
# allowed range). config.example.toml, docs/CONFIG.md and the System drawer's settings list are generated from it.
import config_spec as _spec  # noqa: E402
from config_spec import SETTINGS, SETTINGS_BY_ENV, SETTINGS_BY_KEY, SECTIONS  # noqa: E402,F401

APP_DIR = BASE_DIR.parent


def _default_of(s) -> object:
    return str(APP_DIR / s.default) if s.type == "path" else s.default


# (environment variable, config.toml key, default, title) - kept for older imports
SPEC = [(s.env, s.key, _default_of(s), s.title) for s in SETTINGS]
_SPEC = {env: (key, default) for env, key, default, _ in SPEC}
_known = {s.key for s in SETTINGS}
_PATH_KEYS = {s.key for s in SETTINGS if s.type == "path"}
CONFIG_STATUS.update({"problems": [], "sources": {}, "values": {}, "suggest": {}})
CONFIG_STATUS["unknown"] = [f"{sec}.{k}" for sec, vals in _TOML.items() if isinstance(vals, dict)
                            for k in vals if f"{sec}.{k}" not in _known] + \
                           [sec for sec, vals in _TOML.items() if not isinstance(vals, dict)]
if CONFIG_STATUS["unknown"]:                                  # a typo is the usual reason: suggest the real name
    import difflib
    for _u in CONFIG_STATUS["unknown"]:
        _m = difflib.get_close_matches(_u, sorted(_known), n=1, cutoff=0.6) or \
            difflib.get_close_matches(_u.split(".")[-1], [k.split(".", 1)[1] for k in sorted(_known)], n=1, cutoff=0.75)
        if _m:
            CONFIG_STATUS["suggest"][_u] = next((k for k in sorted(_known) if k == _m[0] or k.endswith("." + _m[0])), _m[0])


def _toml_value(key):
    sec, name = key.split(".", 1)
    vals = _TOML.get(sec)
    if isinstance(vals, dict) and name in vals:
        return True, vals[name]
    return False, None


def _cfg(env: str, default=None):
    """Environment variable > config.toml > default, unconverted (kept for code outside this file)."""
    v = os.environ.get(env)
    if v not in (None, ""):
        return v
    key = _SPEC.get(env, (None, None))[0]
    if key:
        found, val = _toml_value(key)
        if found:
            if key in _PATH_KEYS and isinstance(val, str) and not Path(val).is_absolute():
                val = str((CONFIG_FILE.parent / val).resolve())
            return val
    return default


def _flag(env: str, default: bool) -> bool:
    v = _cfg(env, default)
    return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")


_TRUE, _FALSE = ("1", "true", "yes", "on"), ("0", "false", "no", "off")


def _convert(s, raw):
    """(value, problem). A problem means the value cannot be used as given."""
    t = s.type
    if t == "bool":
        if isinstance(raw, bool):
            return raw, None
        txt = str(raw).strip().lower()
        if txt in _TRUE or txt in _FALSE:
            return txt in _TRUE, None
        return None, "must be true or false"
    if t == "number":
        if isinstance(raw, bool):
            return None, "must be a number, not true/false"
        try:
            num = float(str(raw).strip()) if not isinstance(raw, (int, float)) else float(raw)
        except ValueError:
            return None, "must be a number"
        if isinstance(s.default, int) and not num.is_integer():
            return None, "must be a whole number"
        if (s.lo is not None and num < s.lo) or (s.hi is not None and num > s.hi):
            return None, f"must be {s.range_text()}" + (f" {s.unit}" if s.unit and not s.unit.startswith("confidence") else "")
        return (int(num) if isinstance(s.default, int) else num), None
    txt = "" if raw is None else str(raw).strip()
    if s.key == "model.device":
        import re as _re
        return (txt.lower(), None) if _re.fullmatch(r"auto|cpu|cuda(:\d+)?", txt.lower()) else \
            (None, 'must be "auto", "cpu" or "cuda:0" (cuda:1 for a second GPU)')
    if s.choices:
        return (txt.lower(), None) if txt.lower() in s.choices else (None, f"must be {s.range_text()}")
    if t in ("url", "secret_url"):
        if txt and "://" not in txt:
            return None, "must be empty or an address like postgresql+psycopg://user:password@host:5432/dbname"
        return txt, None
    if t == "mavlink":
        if not txt.lower().startswith(("udpin:", "udpout:", "udp:", "tcp:", "tcpin:", "com", "/dev/")):
            return None, "must look like udpin:0.0.0.0:14551 (or tcp:host:port, a COM port or /dev/tty...)"
        return txt, None
    if t == "path" and not txt:
        return None, "must not be empty"
    return txt, None


def redact_url(url: str) -> str:
    """postgresql+psycopg://user:secret@host/db -> postgresql+psycopg://user:***@host/db (for logs and the UI)."""
    import re as _re
    return _re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", url or "")


def _shown(s, v):
    return redact_url(v) if s.type == "secret_url" and isinstance(v, str) else v


def _get(env: str):
    """The value of one setting: environment variable > config.toml > built-in default, converted and checked.
    A value that cannot be used is reported (System drawer -> Settings) and the default is used instead -
    a typo in the file must never stop the app or silently change its behaviour."""
    s = SETTINGS_BY_ENV[env]
    default = _default_of(s)
    raw_env = os.environ.get(env)
    found, raw_file = _toml_value(s.key)
    if raw_env not in (None, ""):
        source, raw = "env", raw_env
    elif found:
        source, raw = "file", raw_file
        CONFIG_STATUS["used"][s.key] = raw_file
    else:
        source, raw = "default", default
    value, problem = _convert(s, raw)
    if problem:
        CONFIG_STATUS["problems"].append({
            "key": s.key, "env": s.env, "title": s.title, "level": "error", "source": source,
            "value": _shown(s, raw), "problem": problem, "using": _shown(s, default),
            "text": f"{s.key} = {_shown(s, raw)!r} ({'environment variable ' + s.env if source == 'env' else 'config file'}) "
                    f"{problem} - using the default {_shown(s, default)!r}"})
        value, source = _convert(s, default)[0], "default"
    if s.type == "path" and source == "file" and not Path(value).is_absolute():
        value = str((CONFIG_FILE.parent / value).resolve())      # relative paths: from the config file's folder
    CONFIG_STATUS["sources"][s.key] = source
    CONFIG_STATUS["values"][s.key] = _shown(s, value)
    return value


def _warn(keys: tuple, text: str):
    CONFIG_STATUS["problems"].append({"key": keys[0], "keys": list(keys), "level": "warning", "text": text,
                                      "problem": text})


# Managed media library: browser uploads, incident clips, review proxies, report frames.
# Files imported "in place" from the PC are NOT copied here - only their path + hash is stored.
MEDIA_DIR = Path(os.getenv("FYP_UPLOAD_DIR") or _get("FYP_MEDIA_DIR"))
CLIPS_DIR = Path(os.getenv("FYP_CLIPS_DIR", MEDIA_DIR / "incidents"))
TLOG_DIR = Path(os.getenv("FYP_TLOG_DIR", MEDIA_DIR / "tlogs"))
REPORTS_DIR = Path(os.getenv("FYP_REPORTS_DIR", MEDIA_DIR / "reports"))
MODELS_DIR = Path(_get("FYP_MODELS_DIR"))
LOGS_DIR = Path(_get("FYP_LOGS_DIR"))          # event-log paper trail (event_log.py); created on first write
DB_PATH = Path(_get("FYP_DB_PATH"))
# High-rate ROV sensor time series live in their own SQLite file: separate write lock,
# separate lifecycle (archive / purge per dive) - see sensors_db.py.
SENSOR_DB_PATH = Path(os.getenv("FYP_SENSOR_DB_PATH", DB_PATH.with_name("fyp_sensors.db")))
UPLOAD_DIR = MEDIA_DIR  # backwards-compatible name
# Database addresses (SQLAlchemy URLs). Empty = the local SQLite files above.
DATABASE_URL = _get("FYP_DATABASE_URL") or f"sqlite:///{DB_PATH.as_posix()}"
SENSOR_DATABASE_URL = (_get("FYP_SENSOR_DATABASE_URL")
                       or (DATABASE_URL if not DATABASE_URL.startswith("sqlite") else f"sqlite:///{SENSOR_DB_PATH.as_posix()}"))


for _d in (MEDIA_DIR, CLIPS_DIR, TLOG_DIR, REPORTS_DIR, MODELS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- YOLO -----------------------------------------------------------------
DEFAULT_MODEL = _get("FYP_DEFAULT_MODEL")
FALLBACK_MODEL = "yolo11n.pt"          # COCO weights, used only if best.pt is missing
DEVICE = _get("FYP_DEVICE")  # "auto" | "cuda:0" | "cpu"
USE_HALF = _get("FYP_HALF")  # FP16 on CUDA

# Canonical defect classes. Raw model class names are mapped onto these.
CLASS_ALIASES = {
    "CORROSION": ("CORROSION", "RUST", "PITTING", "PIT", "SURFACECORROSION"),
    "DEFORMATION": ("DEFORMATION", "DEFORM", "DEFORMED", "BENT", "CRACK", "BREAK", "BROKEN", "FRACTURE", "SNAPPED"),
}

# --- Detection thresholds ---------------------------------------------------
# Every analysis is an "analysis run" with a recorded threshold:
#   Live    : the operator's slider is the CLIP TRIGGER threshold (default 0.60);
#             detections >= LIVE_LOG_CONF are shown and logged.
#   Batch   : the operator must choose a threshold before processing.
#   Review  : read-only indicator of the run's threshold; "Re-analyse at..." creates a new run.
INFERENCE_CONF_FLOOR = 0.10     # absolute minimum any run may use
CRITICAL_CONF = _get("FYP_CRITICAL_CONF")   # label: a defect >= this is "High confidence" (certainty, not severity)
LIVE_TRIGGER_DEFAULT = _get("FYP_LIVE_TRIGGER")
LIVE_LOG_CONF = _get("FYP_LIVE_LOG_CONF")
# FFmpeg decodes several frames in parallel, one per thread, and hands each over only when the threads behind it are
# done: every thread adds a frame of delay. OpenCV uses one per CPU (16 on a 16-thread PC = ~0.5 s at 30 fps).
LIVE_DECODER_THREADS = _get("FYP_LIVE_DECODER_THREADS")
TRIGGER_CLASSES = ("DEFORMATION",)
# The clip trigger must HOLD: a trigger-class detection >= the trigger threshold on at least TRIGGER_HITS of the
# last TRIGGER_WINDOW inferred frames. One-frame flickers (e.g. a normal link seen edge-on for an instant) do not
# save a clip. 1 / 1 = the old behaviour (any single frame).
TRIGGER_HITS = _get("TRIGGER_HITS")
TRIGGER_WINDOW = _get("TRIGGER_WINDOW")
if TRIGGER_HITS > TRIGGER_WINDOW:
    _warn(("live.trigger_hits", "live.trigger_window"), f"live.trigger_hits ({TRIGGER_HITS}) is more than live.trigger_window "
          f"({TRIGGER_WINDOW}), so a clip could never be saved - using {TRIGGER_WINDOW} of {TRIGGER_WINDOW}")
    TRIGGER_HITS = TRIGGER_WINDOW
if LIVE_LOG_CONF > LIVE_TRIGGER_DEFAULT:
    _warn(("live.log_conf", "live.trigger_default"), f"live.log_conf ({LIVE_LOG_CONF}) is above live.trigger_default "
          f"({LIVE_TRIGGER_DEFAULT}): sightings between the two are not drawn or logged although they would save a clip")
REPORT_CONF = 0.25

# --- BlueROV2 / MAVLink -----------------------------------------------------
# BlueOS -> MAVLink Endpoints -> add a UDP endpoint to the topside PC (e.g. 192.168.2.1:14551).
# QGroundControl keeps 14550; this app listens on its own port.
MAVLINK_URL = _get("FYP_MAVLINK_URL")
ROV_STALE_SEC = 3.0             # no HEARTBEAT for this long -> STALE (values greyed)
ROV_LOST_SEC = 10.0             # ... and this long -> NO ROV (offline placeholder)
SENSOR_LOG_HZ = 5.0             # sensor rows per second while a dive log is recording
TELEMETRY_RECORD_MAX_AGE_SEC = 1.0  # a depth older than this is not stored with an incident (alert, clip name, clip rows)
DIVE_IDLE_TAIL_SEC = 60.0       # at Stop: no video frame and no sensor reading for this long -> the dive ends at its last data
APP_PAUSE_SEC = 15.0            # the clock jumped this far between 0.1 s ticks -> the app was not running (PC asleep / suspended)
AUTO_ARM_DEPTH_M = _get("FYP_AUTO_ARM_DEPTH")  # AUTO detection arms below this depth
WATER_DENSITY = 1025.0          # sea water, for depth-from-pressure fallback

# --- Pipeline A: live -------------------------------------------------------
CLIP_SECONDS = 5.0              # length of the rolling RAM buffer / incident clip
CLIP_BUFFER_NOMINAL_FRAMES = 150  # 30 fps x 5 s; sized by time for other frame rates
CLIP_BUFFER_MAX_FRAMES = 300
LIVE_MAX_WIDTH = 1280
LIVE_JPEG_QUALITY = 80
INCIDENT_COOLDOWN_SEC = _get("FYP_INCIDENT_COOLDOWN")
INCIDENT_POST_ROLL_SEC = 2.0    # ~3 s lead-up + ~2 s after the trigger in every 5 s clip
LIVE_DEFECT_LOG_INTERVAL = 0.5

# --- Pipeline B: batch ------------------------------------------------------
BATCH_SAMPLE_FPS = _get("FYP_BATCH_SAMPLE_FPS")
BATCH_SIZE = _get("FYP_BATCH_SIZE")
BATCH_IOU = 0.45
BATCH_USE_FFMPEG_READER = _get("FYP_BATCH_FFMPEG")
BATCH_PREFETCH = _get("FYP_BATCH_PREFETCH")   # x BATCH_SIZE frames held (~2.7 MB each at 1280x720)

# --- GPU arbiter -------------------------------------------------------------
# One thread owns the GPU. Live frames always run first; batch work is split into
# chunks of ARBITER_BATCH_CHUNK_WHEN_LIVE frames while a live dive is active, so a
# live frame never waits behind more than one small chunk.
ARBITER_BATCH_CHUNK_WHEN_LIVE = 2
ARBITER_LIVE_RECENT_SEC = 1.0

# --- File browser (in-place import) ------------------------------------------
# The server-side file browser exposes the backend PC's folders, so by default it only
# answers requests coming from this machine.
ALLOW_REMOTE_FS = _get("FYP_ALLOW_REMOTE_FS")
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm")

# --- Gemini report assistant ------------------------------------------------
GEMINI_DEFAULT_MODEL = "gemini-3.8-flash"   # Google's recommendation for new projects (Oct 2026)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", GEMINI_DEFAULT_MODEL)   # kept for imports; use gemini_model()
_toml_model = _toml_value("gemini.model")
GEMINI_CONFIG_MODEL = str(_toml_model[1]).strip() if _toml_model[0] and str(_toml_model[1]).strip() else None
_get("GEMINI_MODEL")                 # checked and listed like the others; gemini_model() decides which one is used
GEMINI_KNOWN_MODELS = ("gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash")
SETTINGS_FILE = Path(os.getenv("FYP_SETTINGS_FILE", BASE_DIR / "settings.json"))   # choices made in the app
GEMINI_COOLDOWN_SEC = _get("GEMINI_COOLDOWN_SEC")  # enforced server-side
REPORT_MAX_KEYFRAMES = _get("FYP_REPORT_MAX_KEYFRAMES")
REPORT_TEMPLATE_DEFAULT = _get("FYP_REPORT_TEMPLATE") or "default"
REPORT_TEMPLATES_DIR = Path(_get("FYP_REPORT_TEMPLATES_DIR"))
# Gemini call limits: a request that hangs or is retried for minutes (overloaded model) should fail
# over to the rule-based draft quickly instead of keeping the engineer waiting.
GEMINI_TIMEOUT_SEC = _get("GEMINI_TIMEOUT_SEC")
GEMINI_RETRIES = _get("GEMINI_RETRIES")           # extra quick attempts on the same model when it answers "busy" (5xx)
# When the chosen model is busy (503 high demand), times out or hits its quota, try this model before
# falling back to the rules. Empty = no fallback. Flash-Lite runs on separate capacity and quota.
GEMINI_FALLBACK_MODEL = _get("GEMINI_FALLBACK_MODEL")
# Overall time allowed for getting a first Gemini draft (all attempts, fallback included).
GEMINI_DEADLINE_SEC = _get("GEMINI_DEADLINE_SEC")
# Gemini 3 "thinking": low is much faster and enough for writing prose about given facts; set high to compare.
GEMINI_THINKING = _get("GEMINI_THINKING")

API_PORT = _get("FYP_PORT")

# --- Test Mode (no vehicle / no trained model yet) ---------------------------
# A chosen video is streamed like the BlueROV2 camera (BlueOS sends H.264 over UDP 5600) and
# the BlueROV2 MAVLink simulator feeds the real telemetry link. Everything recorded is tagged TEST.
TEST_STREAM_PORT = _get("FYP_TEST_STREAM_PORT")
TEST_STREAM_URL = f"udp://127.0.0.1:{TEST_STREAM_PORT}?overrun_nonfatal=1&fifo_size=5000000"


# --- Gemini API key ------------------------------------------------------------------------
# Windows only hands environment variables to a program when it starts, so a key set in
# "Environment Variables" is invisible to a backend launched from an older terminal (or from an
# IDE such as VS Code that has not been restarted). Look in more places than os.environ.
ENV_FILES = (BASE_DIR.parent / ".env", BASE_DIR / ".env")


def _clean(v):
    v = (v or "").strip().strip('"').strip("'").strip()
    return v or None


def _from_env_file(name):
    for f in ENV_FILES:
        try:
            for line in f.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    if k.strip() == name and _clean(v):
                        return _clean(v), f".env file ({f})"
        except OSError:
            continue
    return None


def _from_windows_registry(name):
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    for hive, path, label in ((winreg.HKEY_CURRENT_USER, r"Environment", "Windows user variables"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
                               "Windows system variables")):
        try:
            with winreg.OpenKey(hive, path) as k:
                v, _ = winreg.QueryValueEx(k, name)
                if _clean(v):
                    return _clean(v), f"{label} (read directly - the backend started before the key was set)"
        except OSError:
            continue
    return None


def gemini_key():
    """(key, where it was found) or (None, None). Order: process environment, .env file, Windows registry."""
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        v = _clean(os.environ.get(name))
        if v:
            return v, f"environment variable {name}"
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        hit = _from_env_file(name) or _from_windows_registry(name)
        if hit:
            return hit
    return None, None


def _settings() -> dict:
    try:
        import json
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def gemini_model() -> tuple[str, str]:
    """(model id, where it came from): GEMINI_MODEL env var > model chosen in the app > config.toml > default."""
    env = (os.environ.get("GEMINI_MODEL") or "").strip()
    if env:
        return env, "GEMINI_MODEL environment variable"
    saved = (_settings().get("gemini_model") or "").strip()
    if saved:
        return saved, "chosen in the app"
    if GEMINI_CONFIG_MODEL:
        return GEMINI_CONFIG_MODEL, f"config file ({CONFIG_FILE.name})"
    return GEMINI_DEFAULT_MODEL, "default"


def save_gemini_model(model: str):
    import json
    data = _settings()
    data["gemini_model"] = model
    SETTINGS_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")



def _wrap(text: str, prefix: str, width: int = 112) -> list[str]:
    import textwrap
    return textwrap.wrap(text, width=width, initial_indent=prefix, subsequent_indent=prefix,
                         break_long_words=False, break_on_hyphens=False) or [prefix.rstrip()]


def _ordered(section: str) -> list:
    items = [x for x in SETTINGS if x.section == section]
    return [x for x in items if not x.advanced] + [x for x in items if x.advanced]


def config_example() -> str:
    """config.example.toml - generated from config_spec.py, so it always lists exactly what the app reads."""
    bar = "# " + "=" * 110
    out = [bar, "#  MOORING TWIN SETTINGS", bar,
           "#  How to use this file",
           "#   1. Save it as config.toml in the app folder (the one with backend/ and frontend/), or press",
           "#      System drawer -> Settings -> Create config.toml.",
           "#   2. To change a setting, delete the \"# \" in front of its line and edit the value. Everything left",
           "#      commented keeps its built-in default.",
           "#   3. Save, then restart the app (start.bat / start.sh).",
           "#   4. Check System drawer -> Settings: it lists every value in use, where it came from (default, this",
           "#      file or an environment variable) and any value it could not use (it then keeps the default).",
           "#",
           "#  Text goes in quotes (\"cpu\"), numbers and true/false do not. Windows paths: use / or \\\\",
           "#  (\"D:/MooringTwin/media\"). Relative paths start from this file's folder.",
           "#  An environment variable with the name shown under a setting overrides this file.",
           "#",
           "#  Common situations - false clips, picture lag, 60 fps camera, shallow test site, cloud database,",
           "#  slow Gemini ...: see docs/CONFIG.md, which says which settings to change and what you give up.",
           "#",
           "#  Not in this file: the Gemini API key (.env file) and the PDF look (System -> PDF report templates).",
           bar]
    for sec, (title, intro) in SECTIONS.items():
        out += ["", "", "# " + "-" * 110, f"#  {title.upper()}"] + _wrap(intro, "#  ") + ["# " + "-" * 110, f"[{sec}]"]
        advanced_started = False
        for x in _ordered(sec):
            if x.advanced and not advanced_started:
                out += ["", "# ..... rarely needs changing " + "." * 81]
                advanced_started = True
            head = f"# {x.title}"
            facts = [f for f in ((x.unit if x.unit and not x.unit.startswith("confidence") else ""),
                                 (f"allowed {x.range_text()}" if x.range_text() else "")) if f]
            if x.unit.startswith("confidence"):
                facts.insert(0, "confidence 0-1")
            out += ["", head + (f"  ({', '.join(facts)})" if facts else "")]
            out += _wrap(x.what, "#     ")
            if x.tune:
                out += _wrap("When to change: " + x.tune, "#     ")
            out += [f"#     Environment variable: {x.env}", f"# {x.name} = {_spec.toml_value(x, _default_rel(x))}"]
    return "\n".join(out) + "\n"


def _default_rel(x):
    return x.default            # paths are written relative to the app folder, as in the spec


def config_reference_md() -> str:
    """docs/CONFIG.md - the operator's reference, generated from the same list as the code."""
    md = ["# Settings reference", "",
          "<!-- Generated from backend/config_spec.py - do not edit by hand (python tools/make_config_docs.py). -->", "",
          "The app runs with sensible defaults. A site changes them in **`config.toml`** in the app folder (next to "
          "`backend/` and `frontend/`). Start from `config.example.toml`, or press *System drawer → Settings → "
          "Create config.toml*.", "",
          "**How to change a setting**", "",
          "1. Open `config.toml` in a text editor (Notepad is fine).",
          "2. Find the setting under its `[section]`, delete the `# ` at the start of its line, and edit the value.",
          "3. Save and restart the app (`start.bat` / `start.sh`).",
          "4. Open *System drawer → Settings*. Every value in use is listed with where it came from. A value the app "
          "cannot use (a typo, out of range) is shown in orange with the reason, and the default is used instead, so a "
          "mistake never stops the app.", "",
          "**Which value wins:** environment variable › `config.toml` › built-in default. The environment variable "
          "(listed with each setting) is for passwords and one-off tests. A Gemini model picked in the app wins over "
          "the file.", "",
          "**Writing values:** text in quotes (`\"cpu\"`), numbers and `true`/`false` without. Windows paths with `/` "
          "(`\"D:/MooringTwin/media\"`). Relative paths start from the folder of `config.toml`.", "",
          "**Not here:** the Gemini API key (`.env` file) and the PDF look (*System → PDF report templates*).", "",
          "## Common situations", "",
          "Each recipe is a starting point - change one thing, test, and keep a note of what you changed and why.", ""]
    for r in _spec.RECIPES:
        md += [f"### {r.title}", "", f"*When:* {r.situation}", "", "```toml"]
        sec = None
        for key, val in r.changes:
            x = SETTINGS_BY_KEY[key]
            if x.section != sec:
                md.append(f"[{x.section}]")
                sec = x.section
            md.append(f"{x.name} = {_spec.toml_value(x, val)}")
        md += ["```", ""]
        if r.note:
            md += [r.note, ""]
    md += ["## All settings", ""]
    for sec, (title, intro) in SECTIONS.items():
        md += [f"### {title} — `[{sec}]`", "", intro, "",
               "| Setting | Default | Allowed | What it does | When to change it |", "|---|---|---|---|---|"]
        for x in _ordered(sec):
            cell = lambda t: t.replace("|", "\\|")   # noqa: E731
            name = f"**{x.title}**<br>`{x.name}`<br><sub>env `{x.env}`{' · rarely changed' if x.advanced else ''}</sub>"
            unit = f" {x.unit}" if x.unit and not x.unit.startswith("confidence") else ""
            md.append(f"| {name} | `{_spec.toml_value(x)}`{unit} | {cell(x.range_text()) or 'text'} | {cell(x.what)} | "
                      f"{cell(x.tune) or '—'} |")
        md.append("")
    md += ["## Fixed in the code", "",
           "Kept constant so results stay comparable between dives and sites: the 5 s clip length with ~2 s after the "
           "trigger, 5 Hz sensor logging, the ROV link timeouts (stale after 3 s, offline after 10 s), the live picture "
           "width (1280 px) and the batch overlap rule (IoU 0.45). Change them in `backend/config.py` only with a reason, "
           "and re-run the tests.", ""]
    return "\n".join(md)
