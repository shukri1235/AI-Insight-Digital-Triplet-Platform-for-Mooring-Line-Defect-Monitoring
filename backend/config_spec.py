"""
Every setting an operator can change, written for the person tuning a site - not for a programmer.

This one list is the source for:
  * what the app reads (config.py),
  * config.example.toml (the commented file operators copy to config.toml),
  * docs/CONFIG.md (the reference with "common situations"),
  * System drawer -> Settings: each value in use, where it came from, and any problem with it.
So the explanation can never drift from what the code does. Tests check that all three stay in sync.

Fields of a Setting
  key      section.name in config.toml
  env      environment variable that overrides the file (useful for passwords and one-off tests)
  default  built-in value. Paths are relative to the app folder (the one with backend/ and frontend/)
  title    short name shown in the app
  what     what it does, in plain words
  tune     when to change it and what you give up - the "trade-off" line
  unit     how the number is read (s, m, frames, confidence 0-1 ...)
  lo, hi   allowed range (inclusive); values outside it are rejected and the default is used
  choices  allowed values for a text setting
  kind     number / bool / text / path / url / secret_url / mavlink
  advanced True = rarely needs changing (listed after the others)
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Setting:
    key: str
    env: str
    default: object
    title: str
    what: str
    tune: str = ""
    unit: str = ""
    lo: float | None = None
    hi: float | None = None
    choices: tuple = ()
    kind: str = ""
    advanced: bool = False

    @property
    def section(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def name(self) -> str:
        return self.key.split(".", 1)[1]

    @property
    def type(self) -> str:
        if self.kind:
            return self.kind
        if isinstance(self.default, bool):
            return "bool"
        if isinstance(self.default, (int, float)):
            return "number"
        return "text"

    def range_text(self) -> str:
        if self.choices:
            return " / ".join(f'"{c}"' for c in self.choices)
        if self.type == "bool":
            return "true / false"
        if self.lo is not None and self.hi is not None:
            return f"{_num(self.lo)} to {_num(self.hi)}"
        if self.lo is not None:
            return f"{_num(self.lo)} or more"
        return ""


def _num(v) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


SECTIONS: dict[str, tuple[str, str]] = {
    "paths": ("Folders and files",
              "Where the app keeps its data. Point these at a bigger or backed-up drive; existing files are not moved "
              "for you - copy them across first, then change the path and restart."),
    "database": ("Database",
                 "Empty = two local files next to the app (the normal setup on one PC). Set an address to keep the "
                 "inspection records on a database server instead (cloud / AWS RDS, several users)."),
    "model": ("Detection model", "Which trained weights are loaded and on which processor."),
    "live": ("Live dive",
             "How the live feed is analysed, when an incident clip is saved, and how much delay the picture has."),
    "rov": ("BlueROV2 link", "Where the vehicle's telemetry (depth, heading, battery ...) arrives."),
    "batch": ("Batch (imported dive videos)",
              "How recorded videos are analysed after the dive. None of these change live detection."),
    "gemini": ("Report assistant (Gemini)",
               "Limits for the AI draft. When Gemini is slow or busy the app falls back to a rule-based draft, so a "
               "report is never lost. The API key is NOT set here - it stays in the .env file."),
    "report": ("Reports and PDF", "What goes into a report and which PDF template it uses."),
    "server": ("Server and network", "Port and who may use the server from other PCs."),
}

S = Setting
SETTINGS: list[Setting] = [
    # ---------------------------------------------------------------- paths
    S("paths.media_dir", "FYP_MEDIA_DIR", "backend/media", "Media folder",
      "Incident clips, browser uploads, review copies, .tlog files and report images. Dive videos imported from this "
      "PC stay where they are and are not copied here.",
      "Move it to a larger drive if clips and .tlogs fill the system disk (roughly 1-3 MB per incident clip).",
      kind="path"),
    S("paths.models_dir", "FYP_MODELS_DIR", "backend/models", "Model folder",
      "Folder searched for detection weights (.pt files).", kind="path", advanced=True),
    S("paths.logs_dir", "FYP_LOGS_DIR", "logs", "Event log folder",
      "One text file per app session (session_<date>_<time>.txt): every event-log entry from the browser plus the "
      "backend's own record of live dives, incident clips, batch analyses and approved reports. A paper trail: "
      "files are only ever added to, never changed or deleted by the app.",
      "Point it at a backed-up or shared drive if the trail must be kept with the inspection records.", kind="path"),
    S("paths.database", "FYP_DB_PATH", "backend/fyp_database.db", "Database file",
      "The local inspection database (dives, detections, reports). The ROV sensor log is a second file next to it "
      "(fyp_sensors.db). Ignored when database.url is set.",
      "Keep it on a local drive, not a network share - SQLite over a network share can corrupt.", kind="path",
      advanced=True),
    # ---------------------------------------------------------------- database
    S("database.url", "FYP_DATABASE_URL", "", "Database server address",
      "Address of a PostgreSQL server for the inspection records, e.g. "
      "postgresql+psycopg://mooring:PASSWORD@my-db.xxxx.rds.amazonaws.com:5432/mooring . Empty = the local file.",
      "Needs: pip install \"psycopg[binary]\", and tools/migrate_db.py to copy existing data across once. Put the "
      "address in the FYP_DATABASE_URL environment variable rather than this file when it contains a password.",
      kind="secret_url"),
    S("database.sensor_url", "FYP_SENSOR_DATABASE_URL", "", "Sensor database address",
      "Where the 5 Hz ROV sensor log goes. Empty = the same server as database.url (or the local sensor file).",
      "Only set it to put the high-volume sensor log on a different server.", kind="secret_url", advanced=True),
    # ---------------------------------------------------------------- model
    S("model.default", "FYP_DEFAULT_MODEL", "best.pt", "Weights loaded at start",
      "File name in the model folder (or a full path). Another model can still be loaded from the System drawer.",
      "Point it at the newest training run after retraining (or copy that best.pt into the model folder)."),
    S("model.device", "FYP_DEVICE", "auto", "Processor",
      "\"auto\" uses the NVIDIA GPU when one works, otherwise the CPU.",
      "Force \"cpu\" only to test without a GPU - live detection on a CPU is several times slower.",
      choices=("auto", "cuda:0", "cpu")),
    S("model.half", "FYP_HALF", True, "Half precision on the GPU",
      "FP16 maths on the GPU: about twice as fast, same detections in practice.",
      "Turn off only if a GPU driver problem shows wrong or no detections.", advanced=True),
    # ---------------------------------------------------------------- live
    S("live.trigger_default", "FYP_LIVE_TRIGGER", 0.60, "Clip trigger (starting value)",
      "Deformation confidence that saves an incident clip and raises the alert. This is where the Live slider "
      "starts; the operator can still move it during a dive, and every change is recorded with the dive.",
      "0.60 was chosen for model v4 by the threshold sweep on the validation video (the lowest threshold with false "
      "alarms <= 1 % of clean frames; threshold_sweep.py). With a different model, re-run the sweep rather than guess. "
      "Raise it (e.g. 0.70) if clean chain keeps saving clips; lower it (e.g. 0.50) if real deformations are missed. "
      "Pitting never saves a clip, whatever this is.",
      unit="confidence 0-1", lo=0.10, hi=0.99),
    S("live.log_conf", "FYP_LIVE_LOG_CONF", 0.40, "Show and log from",
      "Detections at or above this are drawn on the live feed (dashed below the trigger) and saved as sightings.",
      "Lower = more weak sightings for the engineer to review (and more noise); higher = a cleaner log that may miss "
      "faint defects. Must be at or below the clip trigger.",
      unit="confidence 0-1", lo=0.10, hi=0.99),
    S("live.trigger_hits", "TRIGGER_HITS", 3, "Hold rule: hits needed",
      "A clip is saved only when the trigger is reached on this many of the last trigger_window analysed frames, so "
      "a one-frame flicker (e.g. a normal link seen edge-on) does not save a clip.",
      "More hits = fewer false clips but a defect must stay in view longer. 1 hit of 1 = any single frame (the old "
      "behaviour). At 60 fps use about twice the numbers (e.g. 5 of 8) to keep the same time span.",
      unit="frames", lo=1, hi=30),
    S("live.trigger_window", "TRIGGER_WINDOW", 5, "Hold rule: out of the last",
      "Window for trigger_hits: 3 hits out of the last 5 analysed frames means the defect must be seen for about 0.1-0.2 s at 30 fps.",
      "Must be at least trigger_hits.", unit="frames", lo=1, hi=60),
    S("live.incident_cooldown_sec", "FYP_INCIDENT_COOLDOWN", 10.0, "Gap between clips",
      "Minimum time between two incident clips, so one defect held in view does not save a clip every few seconds.",
      "Shorter if defects are close together along the chain and each needs its own clip.", unit="s", lo=0, hi=600),
    S("live.auto_arm_depth_m", "FYP_AUTO_ARM_DEPTH", 2.0, "Auto mode starts detecting below",
      "In Auto mode detection runs only while the ROV is deeper than this, so launch and recovery at the surface do "
      "not raise alerts.",
      "Use 0.3-0.5 in a test tank or shallow lake, or simply switch to Armed.", unit="m", lo=0, hi=100),
    S("live.critical_conf", "FYP_CRITICAL_CONF", 0.60, "High-confidence label from",
      "A deformation or pitting detection at or above this confidence is labelled High confidence in the app and the "
      "report facts (below: Low confidence). The label states how certain the model is, not how severe the defect "
      "is. The rule-based report proposes REPAIR when a deformation event reaches it.",
      "Keep it equal to live.trigger_default, so every deformation that saves a clip is High confidence.",
      unit="confidence 0-1", lo=0.10, hi=0.99, advanced=True),
    S("live.decoder_threads", "FYP_LIVE_DECODER_THREADS", 2, "Video decoder threads",
      "Threads that decode the camera stream. Each extra thread holds the picture back by one more frame "
      "(33 ms at 30 fps); 2 keeps the delay low and decodes 1080p30 easily.",
      "Raise to 4 only if the System drawer shows the capture rate below the camera's (4K or 60 fps cameras). "
      "0 = one per CPU core (OpenCV's default: about 0.5 s more delay on a 16-thread PC).",
      unit="threads", lo=0, hi=32),
    # ---------------------------------------------------------------- rov
    S("rov.mavlink_url", "FYP_MAVLINK_URL", "udpin:0.0.0.0:14551", "Telemetry address",
      "Where the app listens for the vehicle's MAVLink messages. In BlueOS -> MAVLink Endpoints add a UDP client "
      "to <this PC's IP>:14551. QGroundControl keeps its own port (14550), so both work at once.",
      "Change the port only if 14551 is already used on this PC.", kind="mavlink"),
    # ---------------------------------------------------------------- batch
    S("batch.sample_fps", "FYP_BATCH_SAMPLE_FPS", 2.0, "Frames analysed per second of video",
      "Batch looks at this many frames per second of footage (2 = every 15th frame of a 30 fps video). A defect "
      "in view for 1 s is seen on at least 2 frames.",
      "Higher finds short glimpses but takes longer (decoding is the slow part, so not proportionally); lower is "
      "faster but can miss defects that pass quickly.", unit="frames/s", lo=0.2, hi=60),
    S("batch.batch_size", "FYP_BATCH_SIZE", 8, "Frames per GPU batch",
      "Frames sent to the GPU together. While a live dive runs, batch automatically uses chunks of 2 so live goes "
      "first.", "Larger can be slightly faster on a big GPU; smaller if the GPU runs out of memory.",
      unit="frames", lo=1, hi=64, advanced=True),
    S("batch.ffmpeg_reader", "FYP_BATCH_FFMPEG", True, "Read videos with ffmpeg",
      "Uses ffmpeg (with GPU video decoding when available) to read dive videos; off = OpenCV.",
      "Turn off only if a video fails to import with an ffmpeg error.", advanced=True),
    S("batch.prefetch_batches", "FYP_BATCH_PREFETCH", 2, "Read-ahead (batches)",
      "Batches decoded in the background while the GPU analyses the current one, so the two overlap. Each batch "
      "holds batch_size frames in memory (about 2.7 MB per 1280x720 frame).",
      "0 = off (the old behaviour, for comparison). More than 2 rarely helps.", unit="batches", lo=0, hi=8,
      advanced=True),
    # ---------------------------------------------------------------- gemini
    S("gemini.model", "GEMINI_MODEL", "gemini-3.8-flash", "Gemini model (starting choice)",
      "The model used for AI drafts until someone picks another in the app (the app's choice then wins).",
      "Retired models are flagged in the System drawer with the suggested replacement."),
    S("gemini.fallback_model", "GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite", "Fallback model",
      "Tried when the main model is busy, times out or runs out of quota, before the rule-based draft.",
      "\"\" = no fallback (go straight to the rule-based draft)."),
    S("gemini.thinking", "GEMINI_THINKING", "low", "Thinking level",
      "How much Gemini 3 models reason before answering.",
      "\"low\" is much faster and enough for writing up given facts; \"high\" is slower - only to compare drafts.",
      choices=("low", "high")),
    S("gemini.timeout_sec", "GEMINI_TIMEOUT_SEC", 90.0, "Time limit per request", "Longest wait for one Gemini answer.",
      "Raise on a very slow connection; the overall deadline below still applies.", unit="s", lo=10, hi=600,
      advanced=True),
    S("gemini.retries", "GEMINI_RETRIES", 1, "Quick retries when busy",
      "Extra attempts on the same model when it answers \"busy\", before trying the fallback model.",
      unit="attempts", lo=0, hi=5, advanced=True),
    S("gemini.deadline_sec", "GEMINI_DEADLINE_SEC", 150.0, "Overall deadline",
      "Total time for an AI draft (all attempts and the fallback). After this the rule-based draft is used and can "
      "be redrafted with Gemini later.", unit="s", lo=20, hi=900),
    S("gemini.cooldown_sec", "GEMINI_COOLDOWN_SEC", 30, "Gap between AI reports",
      "Minimum time between two AI drafts, to save tokens / quota. Rule-based drafts are not limited.",
      unit="s", lo=0, hi=3600),
    # ---------------------------------------------------------------- report
    S("report.max_keyframes", "FYP_REPORT_MAX_KEYFRAMES", 4, "Events with key frames",
      "How many events per report get pictures (a full frame and a close-up each). The images are sent to Gemini "
      "and printed in the PDF.",
      "More = a fuller report but more tokens and a longer PDF.", unit="events", lo=0, hi=20),
    S("report.template", "FYP_REPORT_TEMPLATE", "default", "PDF template",
      "Template used when none is chosen at export (a file name in the templates folder, without .json).",
      "Templates are edited in System -> PDF report templates."),
    S("report.templates_dir", "FYP_REPORT_TEMPLATES_DIR", "report_templates", "Templates folder",
      "Folder of PDF templates (one .json and logo per client).", kind="path", advanced=True),
    # ---------------------------------------------------------------- server
    S("server.port", "FYP_PORT", 8500, "Port",
      "The app is opened at http://<this PC>:<port>.",
      "Change it if 8500 is used by another program. Other PCs also need this port allowed in the firewall.",
      lo=1024, hi=65535),
    S("server.allow_remote_fs", "FYP_ALLOW_REMOTE_FS", False, "Let other PCs browse this PC's folders",
      "Off: only someone at this PC can browse its drives to import dive videos. Others can still watch, review and "
      "write reports.",
      "Turn on only on a trusted network where other PCs must import videos from this PC's drives."),
    S("server.test_stream_port", "FYP_TEST_STREAM_PORT", 5600, "Test Mode camera port",
      "UDP port of Test Mode's simulated camera (the BlueROV2's camera uses 5600 too).",
      "Change it if QGroundControl on this PC is already receiving video on 5600 while you use Test Mode.",
      lo=1024, hi=65535, advanced=True),
]
SETTINGS_BY_KEY = {s.key: s for s in SETTINGS}
SETTINGS_BY_ENV = {s.env: s for s in SETTINGS}


@dataclass(frozen=True)
class Recipe:
    title: str
    situation: str
    changes: tuple            # ((key, value), ...) - checked by the tests against SETTINGS
    note: str = ""
    tags: tuple = field(default_factory=tuple)


RECIPES: list[Recipe] = [
    Recipe("Clean chain keeps saving incident clips",
           "False alarms: normal links seen at an angle, glare or marine growth reach the trigger.",
           (("live.trigger_hits", 4), ("live.trigger_window", 6)),
           "Or raise live.trigger_default to 0.70. Check afterwards that real deformations still clip (FT-10)."),
    Recipe("Real deformations pass without a clip",
           "The alert comes late or not at all although the deformation is visible.",
           (("live.trigger_hits", 2), ("live.trigger_default", 0.50)),
           "Expect more false clips; the defect log still records everything above live.log_conf either way."),
    Recipe("Live picture lags behind the camera",
           "The stopwatch test (FT-6) shows more than about 0.3 s delay.",
           (("live.decoder_threads", 1),),
           "Each decoder thread adds one frame of delay. If capture then drops below the camera's frame rate, go back "
           "to 2."),
    Recipe("60 fps or 4K camera",
           "The System drawer shows the capture rate below the camera's, or clips trigger on very brief glimpses.",
           (("live.decoder_threads", 4), ("live.trigger_hits", 5), ("live.trigger_window", 8)),
           "Twice the frames per second: twice the hold window keeps the same 0.1-0.2 s rule. Batch keeps analysing "
           "batch.sample_fps frames per second of video, but decoding takes about twice as long."),
    Recipe("Shallow test site (tank, pool, lake)",
           "Auto mode never starts detecting because the ROV stays above 2 m.",
           (("live.auto_arm_depth_m", 0.3),),
           "Or switch the Detection card to Armed for the session."),
    Recipe("Batch has to be faster",
           "Imported dives queue up after a long field day.",
           (("batch.sample_fps", 1.0),),
           "Halves the detection work but not the decoding, and a defect must stay in view for 2 s to be seen twice. "
           "Check the Processing time hover first: if it says decoding is the bottleneck, a faster disk or GPU video "
           "decoding helps more than this."),
    Recipe("Laptop without an NVIDIA GPU (rehearsal only)",
           "Trying the app on a laptop with no CUDA GPU.",
           (("model.device", "cpu"), ("batch.sample_fps", 1.0)),
           "Live detection will skip many frames on a CPU; use Test Mode to rehearse the workflow, not to judge speed."),
    Recipe("Records on a database server (cloud / AWS)",
           "Several people or a cloud deployment share one set of inspection records.",
           (("database.url", "postgresql+psycopg://mooring:PASSWORD@my-db.xxxx.rds.amazonaws.com:5432/mooring"),),
           "Put the address with its password in the FYP_DATABASE_URL environment variable instead of this file. "
           "Copy existing data once with tools/migrate_db.py. Clips and .tlogs stay in paths.media_dir."),
    Recipe("Gemini is slow or often busy",
           "Report drafts take minutes or keep falling back to the rule-based draft.",
           (("gemini.deadline_sec", 90.0), ("gemini.thinking", "low")),
           "A shorter deadline gets you the rule-based draft sooner; you can press Redraft with Gemini later."),
    Recipe("System disk is filling up",
           "Clips, .tlogs and report images grow over many dives.",
           (("paths.media_dir", "D:/MooringTwin/media"),),
           "Copy the existing media folder to the new place first, then change the path and restart."),
]


def toml_value(s: Setting, value=None) -> str:
    """A value written the way config.toml needs it."""
    v = s.default if value is None else value
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return repr(float(v)) if isinstance(s.default, float) else str(int(v))
    return '"' + str(v).replace("\\", "/") + '"'
