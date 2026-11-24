"""Self-healing engine: monitor -> detect -> decide -> act -> verify."""
import csv
import os
import subprocess
import sys
import time

import yaml

from controller import Controller
from decision import DecisionEngine
from detector import Detector
from monitor import Monitor

HEADER = ["epoch", "event", "container", "kind", "detail"]
SCALING = ("SCALE_UP", "SCALE_DOWN", "RECONCILE")

# Console columns: (header, width, alignment). DETAIL is last and unbounded.
COLUMNS = [("DATE", 10, "<"), ("TIME", 12, "<"), ("LEVEL", 5, "<"), ("EVENT", 9, "<"),
           ("ACTION", 14, "<"), ("REPLICAS", 8, "<"), ("APP", 10, "<"),
           ("STATUS", 9, "<"), ("CPU", 5, ">"), ("MEM", 5, ">")]
GAP = "  "
HEADER_EVERY = 40

# Default level for each event type
LEVELS = {"STARTED": "INFO", "RUNNING": "INFO", "DETECTED": "WARN", "CLEARED": "OK",
          "DECISION": "INFO", "EXECUTED": "INFO", "VERIFIED": "OK",
          "ALERT": "ERROR", "ERROR": "ERROR", "RECOVERED": "OK"}
UP_LABEL = {"CONTAINER_DOWN": "CONTAINER_UP", "HEALTH_FAIL": "HEALTH_OK",
            "MEM_HIGH": "MEM_NORMAL", "CPU_HIGH": "CPU_NORMAL", "CPU_LOW": "CPU_NORMAL"}
# The CSV keeps the original event names so evaluation scripts stay valid
CSV_EVENT = {"EXECUTED": "ACTION_DONE", "ALERT": "DECISION"}

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
CODES = {"red": "31", "green": "32", "yellow": "33", "cyan": "36", "dim": "2"}
COLORS = {"INFO": "cyan", "OK": "green", "WARN": "yellow", "ERROR": "red",
          "HEALTHY": "green", "UNHEALTHY": "red", "EXITED": "red",
          "STARTING": "yellow", "MISSING": "red", "PAUSED": "yellow"}


def paint(text, color):
    if not (USE_COLOR and color):
        return text
    return f"\033[{CODES[color]}m{text}\033[0m"


def stamp(ts):
    """Local date and time (with milliseconds) as two strings."""
    return (time.strftime("%Y-%m-%d", time.localtime(ts)),
            time.strftime("%H:%M:%S", time.localtime(ts)) + f".{int(ts % 1 * 1000):03d}")


def pct(v):
    return "-" if v is None else f"{v:.0f}%"


def app_state(c):
    """STATUS label for one container entry of a snapshot."""
    st = c["status"]
    if st == "running":
        return "HEALTHY" if c["health_ok"] else "UNHEALTHY"
    if st in ("exited", "dead"):
        return "EXITED"
    if st in ("restarting", "created"):
        return "STARTING"
    return st.upper()[:9]


def render(values, detail="", colors=None):
    """Builds one console line. Pads first, colours second, so columns stay aligned."""
    colors = colors or {}
    cells = []
    for (name, width, align), value in zip(COLUMNS, values):
        text = f"{str(value)[:width]:{align}{width}}"
        cells.append(paint(text, colors.get(name)))
    return GAP.join(cells) + GAP + detail


def header_lines():
    head = render([n for n, _, _ in COLUMNS], "DETAIL")
    rule = GAP.join("-" * w for _, w, _ in COLUMNS) + GAP + "-" * 24
    return head, rule


class EventLog:
    """Events go to the CSV (data for the evaluation) and, aligned and coloured, to the console."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        new = not os.path.exists(path)
        self.fh = open(path, "a", newline="")
        self.w = csv.writer(self.fh)
        if new:
            self.w.writerow(HEADER)
        self.snap = None
        self.dec = None
        self.running_override = None
        self.rows = 0

    def attach(self, dec):
        self.dec = dec

    def set_running(self, n):
        """Replica count measured right after an action (the snapshot is older)."""
        self.running_override = n

    def _replicas(self):
        if self.running_override is not None:
            running = self.running_override
        elif self.snap:
            running = sum(c["running"] for c in self.snap["containers"])
        else:
            return "-"
        return f"{running}/{self.dec.desired if self.dec else '-'}"

    def _lookup(self, name):
        """(status, cpu, mem) for a container from the latest snapshot."""
        if self.snap and name:
            for c in self.snap["containers"]:
                if c["name"] == name.split(",")[0]:
                    return app_state(c), pct(c["cpu_pct"]), pct(c["mem_pct"])
            return "MISSING", "-", "-"
        return "-", "-", "-"

    def print_header(self):
        head, rule = header_lines()
        print(head, flush=True)
        print(rule, flush=True)

    def _print(self, ts, level, event, action, app, status, cpu, mem, detail):
        if self.rows and self.rows % HEADER_EVERY == 0:
            self.print_header()
        self.rows += 1
        date, clock = stamp(ts)
        colors = {"LEVEL": COLORS.get(level), "STATUS": COLORS.get(status)}
        print(render([date, clock, level, event, action, self._replicas(), app or "-",
                      status, cpu, mem], detail, colors), flush=True)

    def log(self, event, container, kind, detail="", ts=None, level=None, status=None):
        ts = ts or time.time()
        self.w.writerow([f"{ts:.6f}", CSV_EVENT.get(event, event), container or "",
                         kind, detail])
        self.fh.flush()
        st, cpu, mem = self._lookup(container)
        if status:                      # explicit status (for example after recovery)
            st, cpu, mem = status, "-", "-"
        self._print(ts, level or LEVELS.get(event, "INFO"), event, kind or "-",
                    container, st, cpu, mem, detail)

    def running(self, snap):
        """One RUNNING row per container each cycle (console only, not in the CSV)."""
        self.snap = snap
        self.running_override = None
        if not snap["containers"]:
            self._print(snap["ts"], "WARN", "RUNNING", "-", "-", "MISSING", "-", "-",
                        "no replicas found")
        for c in snap["containers"]:
            st = app_state(c)
            if not c["running"]:
                detail = "probe=none"
            else:
                detail = "probe=200" if c["health_ok"] else "probe=failed"
            if not snap["prom_ok"]:
                detail += " prometheus=unreachable"
            self._print(snap["ts"], "INFO" if st == "HEALTHY" else "WARN", "RUNNING", "-",
                        c["name"], st, pct(c["cpu_pct"]), pct(c["mem_pct"]), detail)


def load_config(path):
    path = os.path.abspath(path)
    base = os.path.dirname(path)
    with open(path) as fh:
        cfg = yaml.safe_load(fh)
    cfg["compose_file"] = os.path.abspath(os.path.join(base, cfg["compose_file"]))
    cfg["project_dir"] = os.path.dirname(cfg["compose_file"])
    cfg["log_file"] = os.path.abspath(os.path.join(base, cfg["log_file"]))
    return cfg


def main():
    default = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else default)
    mon, det = Monitor(cfg), Detector(cfg)
    ctl, events = Controller(cfg, mon), EventLog(cfg["log_file"])
    s = cfg["scaling"]
    existing = len(mon.list_containers())
    dec = DecisionEngine(cfg, min(max(existing, s["min_replicas"]), s["max_replicas"]))
    events.attach(dec)
    events.print_header()
    events.log("STARTED", None, "-", f"desired={dec.desired} existing={existing}")

    active = set()
    while True:
        cycle_start = time.time()
        try:
            snap = mon.snapshot()
            events.running(snap)
            findings = det.analyze(snap)

            current = {(f.container, f.kind): f for f in findings}
            for key, f in current.items():
                if key not in active:
                    events.log("DETECTED", f.container, f.kind, f.detail, ts=snap["ts"])
            for key in active - current.keys():
                events.log("CLEARED", key[0], key[1], f"resolved: {key[1]} no longer present")
                events.log("RECOVERED", key[0], UP_LABEL.get(key[1], "RECOVERED"),
                           "replica back in service" if key[1] == "CONTAINER_DOWN"
                           else "metric back to normal")
            active = set(current)

            for a in dec.decide(findings, snap, snap["ts"]):
                if a.type == "ALERT":
                    events.log("ALERT", a.container, a.type, a.reason)
                    continue
                events.log("DECISION", a.container, a.type, a.reason)
                t0 = time.time()
                try:
                    targets = ctl.execute(a, dec.desired)
                except subprocess.CalledProcessError as exc:
                    events.log("ERROR", a.container, a.type,
                               (exc.stderr or str(exc)).strip()[:300])
                    continue
                events.set_running(len(mon.running_names()))
                events.log("EXECUTED", a.container, a.type, f"took={time.time() - t0:.1f}s")
                for name in (targets or ([a.container] if a.container else [])):
                    det.note_action(name, time.time())
                if a.type in SCALING:
                    det.reset_cluster()
                if targets:
                    ok, secs = ctl.verify(targets, cfg["recovery"]["verify_timeout_sec"])
                    events.set_running(len(mon.running_names()))
                    if ok:
                        events.log("VERIFIED", ",".join(targets), a.type,
                                   f"result=ok recovery={secs:.1f}s", status="HEALTHY")
                    else:
                        events.log("VERIFIED", ",".join(targets), a.type,
                                   f"result=FAILED after={secs:.1f}s",
                                   level="ERROR", status="UNHEALTHY")
        except Exception as exc:  # the healer itself must never die
            events.log("ERROR", None, type(exc).__name__, str(exc)[:300])
        time.sleep(max(0.0, cfg["poll_interval_sec"] - (time.time() - cycle_start)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("healer stopped")
