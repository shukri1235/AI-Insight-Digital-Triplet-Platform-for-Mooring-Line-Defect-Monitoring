"""
BlueROV2 MAVLink simulator - for testing and demos without the vehicle.

It emits the same MAVLink messages ArduSub sends through BlueOS, so the backend's real
ingest path (rov_link.py) is exercised end to end:
  HEARTBEAT (MAV_TYPE_SUBMARINE, armed flag, ArduSub mode), VFR_HUD (depth = -alt,
  heading, climb), ATTITUDE, SCALED_PRESSURE2 (Bar30 pressure + water temperature),
  SYS_STATUS (battery), RANGEFINDER (Ping sonar altitude), STATUSTEXT ("Leak Detected").

    pip install pymavlink
    python tools/bluerov_sim.py                                  # descend to 120.5 m and hold
    python tools/bluerov_sim.py --target 190 --rate 1.5 --seabed 200
    python tools/bluerov_sim.py --leak-at 60                     # raise a leak alarm after 60 s

For a full physics simulation use ArduSub SITL instead (sim_vehicle.py -v ArduSub) and
point FYP_MAVLINK_URL at it.
"""
from __future__ import annotations

import argparse
import math
import os
import socket
import threading
import time

os.environ.setdefault("MAVLINK20", "1")
from pymavlink import mavutil  # noqa: E402

MODES = {"MANUAL": 19, "STABILIZE": 0, "ALT_HOLD": 2, "POSHOLD": 16}


class _UdpSender:
    """File-like UDP sender for pymavlink's MAVLink encoder (it only needs write())."""

    def __init__(self, host: str, port: int):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))           # any free local port

    def write(self, buf: bytes):
        try:
            self.sock.sendto(buf, self.addr)
        except OSError:                          # nobody listening yet: UDP just drops it
            pass

    def close(self):
        self.sock.close()


class BlueRovSim:
    def __init__(self, url="udpout:127.0.0.1:14551", start_depth=0.0, target=120.5, rate=1.0, seabed=200.0,
                 heading=135.0, armed=True, mode="ALT_HOLD", sonar=True, leak_at=None, hz=10.0, battery_v=16.2):
        self.url, self.depth, self.target, self.rate = url, start_depth, target, rate
        self.seabed, self.heading, self.armed, self.mode = seabed, heading, armed, mode
        self.sonar, self.leak_at, self.hz, self.battery_v = sonar, leak_at, hz, battery_v
        self._stop = threading.Event()
        self._th = None
        self.t0 = time.monotonic()
        self.silent = False   # True = stop sending (simulates tether loss)

    def start(self):
        if self.url.startswith("udpout:"):
            # Send through our own socket on a free local port. pymavlink's "udpout" socket binds to
            # the DESTINATION port on Windows (0.0.0.0:14551), which collides with the backend
            # listening on that same port on this PC: WinError 10048.
            host, port = self.url[len("udpout:"):].rsplit(":", 1)
            self._sock = _UdpSender(host, int(port))
            from pymavlink.dialects.v20 import ardupilotmega as dialect
            self.mav = dialect.MAVLink(self._sock, srcSystem=1, srcComponent=1)
        else:                                  # udpin / serial / tcp: pymavlink handles it
            self._sock = mavutil.mavlink_connection(self.url, source_system=1, source_component=1, dialect="ardupilotmega")
            self.mav = self._sock.mav
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()
        return self

    def stop(self):
        self._stop.set()
        if self._th:
            self._th.join(2)
        try:
            self._sock.close()
        except Exception:                       # noqa: BLE001
            pass

    def _run(self):
        mav = self.mav
        n, leak_sent = 0, False
        dt = 1.0 / self.hz
        while not self._stop.is_set():
            t = time.monotonic() - self.t0
            if self.depth < self.target:
                self.depth = min(self.target, self.depth + self.rate * dt)
                climb = -self.rate
            elif self.depth > self.target:
                self.depth = max(self.target, self.depth - self.rate * dt)
                climb = self.rate
            else:
                climb = 0.0
            if not self.silent:
                boot_ms = int(t * 1000)
                if n % int(self.hz) == 0:
                    base = mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED | (128 if self.armed else 0)
                    mav.heartbeat_send(12, 3, base, MODES.get(self.mode, 2), 4)
                    mav.sys_status_send(0, 0, 0, 500, int(self.battery_v * 1000), 1250, 78, 0, 0, 0, 0, 0, 0)
                hdg = (self.heading + 2 * math.sin(t / 7)) % 360
                mav.vfr_hud_send(0.0, 0.2, int(hdg), 30, -self.depth, climb)
                mav.attitude_send(boot_ms, math.radians(1.5 * math.sin(t / 3)), math.radians(-2 + math.sin(t / 5)),
                                  math.radians(hdg if hdg <= 180 else hdg - 360), 0, 0, 0)
                press = (101325 + 1025 * 9.80665 * self.depth) / 100.0
                temp = max(4.0, 29.0 - 0.02 * self.depth)
                mav.scaled_pressure2_send(boot_ms, press, 0.0, int(temp * 100))
                if self.sonar and n % 2 == 0:
                    mav.rangefinder_send(max(0.2, self.seabed - self.depth), 0)
                if self.leak_at is not None and t >= self.leak_at and not leak_sent:
                    mav.statustext_send(2, b"Leak Detected")
                    leak_sent = True
            n += 1
            self._stop.wait(dt)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="udpout:127.0.0.1:14551")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--target", type=float, default=120.5)
    ap.add_argument("--rate", type=float, default=1.0, help="vertical speed m/s")
    ap.add_argument("--seabed", type=float, default=200.0)
    ap.add_argument("--heading", type=float, default=135.0)
    ap.add_argument("--leak-at", type=float, default=None)
    ap.add_argument("--disarmed", action="store_true")
    a = ap.parse_args()
    sim = BlueRovSim(a.url, a.start, a.target, a.rate, a.seabed, a.heading, not a.disarmed, leak_at=a.leak_at).start()
    print(f"BlueROV2 sim -> {a.url}  (Ctrl+C to stop)")
    last = None
    try:
        while True:
            d = round(sim.depth, 1)
            if d != last:
                print(f"\rdepth {d:7.1f} m", end="", flush=True)
                last = d
            time.sleep(0.5)
    except KeyboardInterrupt:
        sim.stop()


if __name__ == "__main__":
    main()
