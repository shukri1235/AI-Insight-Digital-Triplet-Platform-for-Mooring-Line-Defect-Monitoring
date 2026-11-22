"""
Runs the browser walkthrough (tests/e2e/scenarios.mjs, 76 checks) on this PC:

  1. starts the backend in TEST MODE (tests/e2e/serve_test_backend.py: temp databases and media, stand-in detector,
     stand-in Gemini; your real data is never touched) on port 8500, serving the built UI;
  2. drives Chromium through every scenario (Playwright);
  3. stops the backend and records which PC the walkthrough ran on (tests/results/e2e_host.json).

  python tools/run_e2e.py            (tools\\run_e2e.bat installs Playwright first)

Close the real app first: the walkthrough needs ports 8500 and udp 14551.
"""
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "tests" / "results"
PORT = 8500


def port_busy(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_health(proc, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def main():
    if not (ROOT / "frontend" / "dist" / "index.html").exists():
        sys.exit("The web UI is not built yet (frontend\\dist). Run start.bat once, close the app, then try again.")
    if port_busy(PORT):
        sys.exit(f"Port {PORT} is in use. Close the Mooring Twin app (and any earlier walkthrough), then try again.")
    RES.mkdir(parents=True, exist_ok=True)
    log = open(RES / "e2e_backend.log", "w", encoding="utf-8")
    env = dict(os.environ)
    env.setdefault("FYP_MAVLINK_URL", "udpin:127.0.0.1:14551")    # localhost only: no Windows firewall prompt
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"                                 # the log is complete even when the backend is stopped
    print("[E2E] starting the test backend (generating test footage takes a few seconds)...", flush=True)
    be = subprocess.Popen([sys.executable, str(ROOT / "tests" / "e2e" / "serve_test_backend.py")], cwd=ROOT,
                          stdout=log, stderr=subprocess.STDOUT, env=env)
    code = 1
    try:
        if not wait_health(be):
            sys.exit(f"The test backend did not start. See {RES / 'e2e_backend.log'}")
        print(f"[E2E] backend ready on http://localhost:{PORT}; running the walkthrough in Chromium...", flush=True)
        env.update({"E2E_UI": f"http://localhost:{PORT}", "E2E_API": f"http://localhost:{PORT}", "PYTHON": sys.executable})
        code = subprocess.call(["node", str(ROOT / "tests" / "e2e" / "scenarios.mjs")], cwd=ROOT, env=env,
                               shell=(os.name == "nt"))
    finally:
        be.terminate()
        try:
            be.wait(10)
        except subprocess.TimeoutExpired:
            be.kill()
        log.close()
    try:
        node = subprocess.run(["node", "--version"], capture_output=True, text=True, shell=(os.name == "nt")).stdout.strip()
    except OSError:
        node = "?"
    (RES / "e2e_host.json").write_text(json.dumps({
        "host": f"{platform.platform()}, Python {platform.python_version()}, Node {node}",
        "ran": datetime.now().isoformat(timespec="seconds"), "exit_code": code}, indent=2))
    print(f"[E2E] results: {RES / 'e2e.json'}; screenshots: {RES / 'e2e'}")
    sys.exit(code)


if __name__ == "__main__":
    main()
