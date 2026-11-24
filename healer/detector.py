"""Detector: snapshot -> findings. Sustained thresholds, not single samples."""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Finding:
    container: Optional[str]   # None = cluster-wide finding
    kind: str                  # CONTAINER_DOWN | HEALTH_FAIL | MEM_HIGH | CPU_HIGH | CPU_LOW
    detail: str


class Detector:
    def __init__(self, cfg):
        self.need_fails = cfg["health"]["consecutive_failures"]
        self.grace = cfg["health"]["grace_after_action_sec"]
        self.t = cfg["thresholds"]
        self.fails = {}          # name -> consecutive failed probes
        self.grace_until = {}    # name -> epoch until which findings are ignored
        self.mem_since = {}      # name -> epoch when memory first exceeded the threshold
        self.cpu_hi_since = {}   # name -> epoch when CPU first exceeded the threshold
        self.cpu_low_since = None

    @staticmethod
    def _since(table, key, condition, now):
        """Seconds `condition` has been continuously true, or None if it is false."""
        if not condition:
            table.pop(key, None)
            return None
        return now - table.setdefault(key, now)

    def _clear(self, name):
        for table in (self.fails, self.mem_since, self.cpu_hi_since):
            table.pop(name, None)

    def note_action(self, name, ts):
        """A corrective action touched `name`: ignore it while it restarts."""
        self.grace_until[name] = ts + self.grace
        self._clear(name)

    def reset_cluster(self):
        """Scaling changed the replica count: sustained-CPU timers start over."""
        self.cpu_low_since = None
        self.cpu_hi_since.clear()

    def analyze(self, snap):
        now, t, out = snap["ts"], self.t, []
        names = {c["name"] for c in snap["containers"]}
        for table in (self.fails, self.mem_since, self.cpu_hi_since, self.grace_until):
            for gone in set(table) - names:
                table.pop(gone)

        running = [c for c in snap["containers"] if c["running"]]
        readings = [c["cpu_pct"] for c in running if c["cpu_pct"] is not None]

        for c in snap["containers"]:
            n = c["name"]
            if now < self.grace_until.get(n, 0):
                continue
            if not c["running"]:
                self._clear(n)
                out.append(Finding(n, "CONTAINER_DOWN", f"status={c['status']}"))
                continue

            self.fails[n] = 0 if c["health_ok"] else self.fails.get(n, 0) + 1
            if self.fails[n] >= self.need_fails:
                out.append(Finding(n, "HEALTH_FAIL",
                                   f"{self.fails[n]} consecutive failed /health probes"))

            mem = c["mem_pct"]
            d = self._since(self.mem_since, n, mem is not None and mem >= t["mem_high_pct"], now)
            if d is not None and d >= t["mem_sustain_sec"]:
                out.append(Finding(n, "MEM_HIGH",
                                   f"mem={mem:.0f}% >= {t['mem_high_pct']}% for {d:.0f}s"))

            cpu = c["cpu_pct"]
            d = self._since(self.cpu_hi_since, n, cpu is not None and cpu >= t["cpu_high_pct"], now)
            if d is not None and d >= t["cpu_high_sustain_sec"]:
                out.append(Finding(n, "CPU_HIGH",
                                   f"cpu={cpu:.0f}% >= {t['cpu_high_pct']}% for {d:.0f}s"))

        # Cluster-wide: every running replica is below the low threshold
        idle = bool(readings) and len(readings) == len(running) and max(readings) < t["cpu_low_pct"]
        if not idle:
            self.cpu_low_since = None
        else:
            self.cpu_low_since = self.cpu_low_since or now
            d = now - self.cpu_low_since
            if d >= t["cpu_low_sustain_sec"]:
                out.append(Finding(None, "CPU_LOW",
                                   f"all replicas < {t['cpu_low_pct']}% for {d:.0f}s"))
        return out
