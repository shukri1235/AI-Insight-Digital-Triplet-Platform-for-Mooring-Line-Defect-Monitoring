"""
BlueROV2 telemetry over MAVLink (BlueOS + ArduSub).

Setup on the ROV: BlueOS -> MAVLink Endpoints -> "+" -> UDP Client to the topside PC
(default 192.168.2.1) port 14551. QGroundControl keeps 14550; this app listens on 14551
(FYP_MAVLINK_URL, default "udpin:0.0.0.0:14551").

Messages decoded (ArduSub):
  HEARTBEAT         vehicle type / armed flag / flight mode
  VFR_HUD           depth (= -alt), heading, climb rate
  ATTITUDE          roll / pitch / yaw
  SCALED_PRESSURE2  Bar30 external pressure (hPa) + water temperature (cdegC)
  SYS_STATUS        battery voltage / current / remaining
  RANGEFINDER / DISTANCE_SENSOR   altitude above seabed (Ping sonar, if fitted)
  STATUSTEXT        warnings - "Leak Detected" raises the leak flag

Connection state machine:
  DRIVER_MISSING  pymavlink not installed
  NO_ROV          listening, no vehicle heartbeat for > ROV_LOST_SEC   (UI: offline placeholder)
  CONNECTED       vehicle heartbeat within ROV_STALE_SEC
  STALE           heartbeat older than ROV_STALE_SEC (values shown greyed, never as live)

No simulated values are ever substituted. For testing, run tools/bluerov_sim.py (or
ArduSub SITL): it speaks the same MAVLink, so the identical code path is exercised.
"""
from __future__ import annotations

import math
import os
import struct
import threading
import time
from pathlib import Path

from config import (APP_PAUSE_SEC, MAVLINK_URL, ROV_LOST_SEC, ROV_STALE_SEC, SENSOR_LOG_HZ, TLOG_DIR, WATER_DENSITY)
import sensors_db

os.environ.setdefault("MAVLINK20", "1")
try:
    from pymavlink import mavutil
    PYMAVLINK = True
except Exception as _e:  # pragma: no cover
    mavutil = None
    PYMAVLINK = False
    print(f"[ROV] pymavlink unavailable ({_e}) - install it with: pip install pymavlink")

ARDUSUB_MODES = {0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED", 7: "CIRCLE", 9: "SURFACE",
                 16: "POSHOLD", 19: "MANUAL", 20: "MOTOR_DETECT", 21: "SURFTRAK"}
MAV_TYPE_SUBMARINE = 12
MAV_TYPE_GCS = 6
STREAM_MSGS = ("VFR_HUD", "ATTITUDE", "SCALED_PRESSURE2", "SYS_STATUS", "RANGEFINDER", "DISTANCE_SENSOR")


class RovLink:
    def __init__(self, url: str = MAVLINK_URL):
        self.url = url
        self.state = "DRIVER_MISSING" if not PYMAVLINK else "NO_ROV"
        self.error: str | None = None
        self._lock = threading.Lock()
        self._v: dict = {}               # latest decoded values
        self._last_hb = 0.0              # monotonic time of last vehicle heartbeat
        self._last_msg = 0.0
        self._msg_count = 0
        self._rate_hz = 0.0
        self.vehicle: dict = {}          # sysid / type / autopilot
        self.simulated = False           # True while Test Mode's BlueROV2 simulator is the vehicle
        self.events: list[dict] = []
        self._conn = None
        self._stop = threading.Event()
        self._requested = False
        # dive-log session
        self._session: dict | None = None
        self._tlog = None
        self.last_sample_mono = 0.0       # monotonic time of the last dive-log reading

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if not PYMAVLINK:
            return
        threading.Thread(target=self._rx_loop, name="mavlink-rx", daemon=True).start()
        threading.Thread(target=self._housekeeping, name="mavlink-hk", daemon=True).start()

    def stop(self):
        self._stop.set()
        self.stop_session()

    def _open(self):
        self._conn = mavutil.mavlink_connection(self.url, source_system=255, source_component=191,
                                                autoreconnect=True, dialect="ardupilotmega")

    # ------------------------------------------------------------------ receive
    def _rx_loop(self):
        while not self._stop.is_set():
            if self._conn is None:
                try:
                    self._open()
                    self.error = None
                    print(f"[ROV] Listening for BlueROV2 MAVLink on {self.url}")
                except Exception as e:
                    self.error = f"Cannot open {self.url}: {e}"
                    self._stop.wait(3)
                    continue
            try:
                msg = self._conn.recv_match(blocking=True, timeout=0.5)
            except Exception as e:
                self.error = str(e)
                self._stop.wait(0.5)
                continue
            if msg is None:
                continue
            mtype = msg.get_type()
            if mtype == "BAD_DATA":
                continue
            self._write_tlog(msg)
            self._handle(msg, mtype)

    def _handle(self, msg, mtype: str):
        now = time.monotonic()
        with self._lock:
            self._last_msg = now
            self._msg_count += 1
            v = self._v
            if mtype == "HEARTBEAT":
                if msg.type in (MAV_TYPE_GCS,) or msg.autopilot == 8:  # ignore other ground stations
                    return
                first = self._last_hb == 0.0 or now - self._last_hb > ROV_LOST_SEC
                self._last_hb = now
                self.vehicle = {"sysid": msg.get_srcSystem(), "type": msg.type,
                                "isSubmarine": msg.type == MAV_TYPE_SUBMARINE}
                armed = bool(msg.base_mode & 128)
                mode = ARDUSUB_MODES.get(msg.custom_mode, str(msg.custom_mode))
                if v.get("armed") is not None and armed != v.get("armed"):
                    self._event("ARMED" if armed else "DISARMED", mode)
                if v.get("mode") is not None and mode != v.get("mode"):
                    self._event("MODE", mode)
                v["armed"], v["mode"] = armed, mode
                if first:
                    self.state = "CONNECTED"
                    self._requested = False
                    self._event("CONNECTED", f"sysid {msg.get_srcSystem()} type {msg.type}")
            elif mtype == "VFR_HUD":
                v["depth"] = round(max(0.0, -float(msg.alt)), 2)
                v["_depth_at"] = now
                v["heading"] = float(msg.heading) % 360
                v["climb"] = round(float(msg.climb), 2)
                v["_depth_src"] = "VFR_HUD"
            elif mtype == "ATTITUDE":
                v["roll"] = round(math.degrees(msg.roll), 1)
                v["pitch"] = round(math.degrees(msg.pitch), 1)
                if "heading" not in v:
                    v["heading"] = round(math.degrees(msg.yaw) % 360, 1)
            elif mtype == "SCALED_PRESSURE2":
                v["pressure_hpa"] = round(float(msg.press_abs), 1)
                v["temperature"] = round(msg.temperature / 100.0, 2)
                if v.get("_depth_src") != "VFR_HUD":  # depth from Bar30 pressure if VFR_HUD absent
                    v["depth"] = round(max(0.0, (msg.press_abs * 100 - 101325) / (WATER_DENSITY * 9.80665)), 2)
                    v["_depth_at"] = now
            elif mtype == "SYS_STATUS":
                if msg.voltage_battery not in (0, 65535):
                    v["battery_v"] = round(msg.voltage_battery / 1000.0, 2)
                if msg.battery_remaining >= 0:
                    v["battery_pct"] = float(msg.battery_remaining)
            elif mtype == "RANGEFINDER":
                v["altitude"] = round(float(msg.distance), 2)
            elif mtype == "DISTANCE_SENSOR":
                if msg.current_distance and msg.current_distance < msg.max_distance:
                    v["altitude"] = round(msg.current_distance / 100.0, 2)
            elif mtype == "STATUSTEXT":
                txt = msg.text if isinstance(msg.text, str) else msg.text.decode(errors="ignore")
                txt = txt.strip("\x00 ")
                if "leak" in txt.lower():
                    v["leak"] = True
                    self._event("LEAK", txt)
                else:
                    self._event("STATUSTEXT", txt)
            v["_at"] = now
        if mtype == "HEARTBEAT" and not self._requested:
            self._request_streams(msg)

    def _request_streams(self, hb):
        """Ask ArduSub for the messages we need at 5 Hz (harmless if QGC already did)."""
        try:
            sysid, compid = hb.get_srcSystem(), hb.get_srcComponent()
            self._conn.mav.request_data_stream_send(sysid, compid, mavutil.mavlink.MAV_DATA_STREAM_ALL, 5, 1)
            for name in STREAM_MSGS:
                mid = getattr(mavutil.mavlink, f"MAVLINK_MSG_ID_{name}", None)
                if mid is not None:
                    self._conn.mav.command_long_send(sysid, compid, mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                                                     0, mid, 200000, 0, 0, 0, 0, 0)
            self._requested = True
        except Exception as e:
            print(f"[ROV] stream request failed: {e}")

    def _housekeeping(self):
        """State transitions, message rate, our own GCS heartbeat, dive-log sampling."""
        last_count, last_t = 0, time.monotonic()
        next_sample = 0.0
        prev_wall = time.time()
        while not self._stop.is_set():
            now = time.monotonic()
            prev_wall = self._check_pause(prev_wall, time.time())
            with self._lock:
                if self._last_hb:
                    age = now - self._last_hb
                    if age > ROV_LOST_SEC and self.state != "NO_ROV":
                        self.state = "NO_ROV"
                        self._event("LOST", f"no heartbeat for {age:.0f}s")
                        self._v.clear()
                    elif ROV_STALE_SEC < age <= ROV_LOST_SEC:
                        self.state = "STALE"
                    elif age <= ROV_STALE_SEC:
                        self.state = "CONNECTED"
                if now - last_t >= 1.0:
                    self._rate_hz = (self._msg_count - last_count) / (now - last_t)
                    last_count, last_t = self._msg_count, now
            if self._conn is not None and int(now) != int(now - 0.1):
                try:  # 1 Hz GCS heartbeat (lets BlueOS / the vehicle see us)
                    self._conn.mav.heartbeat_send(MAV_TYPE_GCS, 8, 0, 0, 4)
                except Exception:
                    pass
            if self._session and now >= next_sample:
                next_sample = now + 1.0 / SENSOR_LOG_HZ
                self._sample(now)
            self._stop.wait(0.1)

    def _check_pause(self, prev_wall: float, wall: float) -> float:
        """This loop ticks every 0.1 s. A much longer jump means the whole app was not running - the PC slept or
        hibernated, or the process was suspended - so nothing (video, sensors, detection) was recorded meanwhile.
        Logged as an event so a dive log explains its gap instead of showing hours of nothing."""
        gap = wall - prev_wall
        if gap > APP_PAUSE_SEC:
            h, m, s = int(gap // 3600), int(gap % 3600 // 60), int(gap % 60)
            span = f"{h} h {m} min" if h else f"{m} min {s} s" if m else f"{s} s"
            print(f"[ROV] The app was not running for {span} (PC asleep or the process suspended)")
            with self._lock:
                self._event("PAUSED", f"app not running for {span} (PC asleep or suspended); nothing recorded meanwhile")
        return wall

    # ------------------------------------------------------------------ public read
    def status(self) -> dict:
        with self._lock:
            age = time.monotonic() - self._last_hb if self._last_hb else None
            return {"state": self.state, "url": self.url, "error": self.error,
                    "heartbeatAgeSec": round(age, 1) if age is not None else None,
                    "msgRateHz": round(self._rate_hz, 1), "vehicle": self.vehicle, "simulated": self.simulated,
                    "recording": self._session is not None,
                    "tlog": self._session.get("tlog_name") if self._session else None}

    @property
    def source(self) -> str:
        """Provenance stored with every sensor sample: real vehicle or Test Mode simulator."""
        return "SIM" if self.simulated else "MAVLINK"

    def telemetry(self) -> dict | None:
        """Latest sensor values, or None when no ROV (UI shows the offline placeholder)."""
        with self._lock:
            if self.state not in ("CONNECTED", "STALE") or not self._v:
                return None
            out = {k: v for k, v in self._v.items() if not k.startswith("_")}
            out["stale"] = self.state == "STALE"
            out["ageSec"] = round(time.monotonic() - self._v.get("_at", time.monotonic()), 1)
            out["depthAgeSec"] = (round(time.monotonic() - self._v["_depth_at"], 2)   # time since the last depth message
                                  if "_depth_at" in self._v else None)
            out.setdefault("leak", False)
            out["simulated"] = self.simulated
            return out

    def recent_events(self, n: int = 20) -> list[dict]:
        with self._lock:
            return list(self.events[-n:])

    def _event(self, kind: str, text: str):
        ev = {"kind": kind, "text": text, "wall_time": time.time()}
        self.events.append(ev)
        self.events = self.events[-200:]
        sess = self._session
        try:
            sensors_db.insert_event(media_id=sess["media_id"] if sess else None,
                                    t=(time.monotonic() - sess["t0"]) if sess else None,
                                    wall_time=ev["wall_time"], kind=kind, text=text)
        except Exception:
            pass

    # ------------------------------------------------------------------ dive log
    def start_session(self, media_id: str, t0_monotonic: float):
        """Starts logging decoded samples (sensors DB) and the raw .tlog for a dive."""
        self.stop_session()
        stamp = time.strftime('%Y%m%d-%H%M%S')
        name = f"dive-{stamp}-{media_id[:8]}.tlog"
        n = 1
        while (TLOG_DIR / name).exists():    # recording off/on within the same second: a new file, never overwrite
            n += 1
            name = f"dive-{stamp}{chr(96 + min(n, 26))}-{media_id[:8]}.tlog"
        path = TLOG_DIR / name
        with self._lock:
            self._tlog = open(path, "wb")
            self._session = {"media_id": media_id, "t0": t0_monotonic, "tlog_path": str(path), "tlog_name": name,
                             "buffer": []}
        return str(path)

    def stop_session(self):
        with self._lock:
            sess, self._session = self._session, None
            if self._tlog:
                self._tlog.close()
                self._tlog = None
        if sess and sess["buffer"]:
            sensors_db.insert_samples(sess["buffer"])
        return sess

    def _write_tlog(self, msg):
        tl = self._tlog
        if tl is None:
            return
        try:  # standard .tlog record: big-endian uint64 microseconds + raw MAVLink frame
            tl.write(struct.pack(">Q", int(time.time() * 1e6)) + msg.get_msgbuf())
        except Exception:
            pass

    def _sample(self, now: float):
        """One dive-log row - only for a depth reading that arrived since the previous row. When messages stop
        (tether cut), no row is written, from the first missed reading on: the log shows a gap, never the last
        good depth repeated (3.6.11; before, rows repeated it until the link turned STALE after 3 s)."""
        tel = self.telemetry()
        sess = self._session
        if not sess or tel is None or tel.get("stale"):
            return
        with self._lock:
            depth_at = self._v.get("_depth_at")
        if depth_at is None or depth_at == sess.get("last_depth_at"):
            return
        sess["last_depth_at"] = depth_at
        row = {"media_id": sess["media_id"], "t": round(now - sess["t0"], 3), "wall_time": time.time(),
               "source": self.source, **{f: tel.get(f) for f in sensors_db.FIELDS}}
        sess["buffer"].append(row)
        self.last_sample_mono = now
        if len(sess["buffer"]) >= int(SENSOR_LOG_HZ * 2):
            rows, sess["buffer"] = sess["buffer"], []
            threading.Thread(target=sensors_db.insert_samples, args=(rows,), daemon=True).start()


rov = RovLink()
