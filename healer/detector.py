"""Rule-based fault detection with sustained conditions and stale-data protection."""

from collections import deque
from dataclasses import dataclass
from statistics import mean
from typing import Optional


@dataclass
class Finding:
    kind: str
    container: Optional[str]
    detail: str


class Sustained:
    """Track a condition over a continuous, sufficiently fresh time window."""

    def __init__(self, window, max_gap):
        self.window = float(window)
        self.max_gap = float(max_gap)
        self.buf = deque()

    def add(self, ts, condition):
        """Add a sample. Reset history if the monitoring gap is too large."""

        ts = float(ts)
        condition = bool(condition)

        # A gap means continuity can no longer be trusted.
        if self.buf and ts - self.buf[-1][0] > self.max_gap:
            self.clear()

        self.buf.append((ts, condition))

        # Retain enough history to validate the window plus gap tolerance.
        cutoff = ts - self.window - self.max_gap

        while self.buf and self.buf[0][0] < cutoff:
            self.buf.popleft()

    def holds(self, now):
        """True only when fresh, continuous samples cover the full window."""

        if not self.buf:
            return False

        now = float(now)
        cutoff = now - self.window

        # The latest sample must be recent.
        if now - self.buf[-1][0] > self.max_gap:
            return False

        # There must be enough history to cover the entire window.
        if self.buf[0][0] > cutoff:
            return False

        # Check samples inside the requested window.
        recent = [
            condition
            for ts, condition in self.buf
            if ts >= cutoff
        ]

        return bool(recent) and all(recent)

    def clear(self):
        self.buf.clear()


class Detector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.health_cfg = cfg["health"]
        self.t = cfg["thresholds"]

        self.need_fails = self.health_cfg["consecutive_failures"]
        self.grace = self.health_cfg["grace_after_action_sec"]

        # Allow gaps up to 1.5 polling intervals.
        self.max_gap = cfg["poll_interval_sec"] * 1.5

        # Container tracking
        self.fail_count = {}
        self.grace_until = {}

        self.mem_trk = {}
        self.cpu_trk = {}

        # Cluster tracking
        self.cluster_high = Sustained(
            self.t["cpu_high_sustain_sec"],
            self.max_gap,
        )
        self.cluster_low = Sustained(
            self.t["cpu_low_sustain_sec"],
            self.max_gap,
        )

        self.last_cluster = []
        self.last_cpu_ts = None

    # --------------------------------------------------------------
    # State management
    # --------------------------------------------------------------

    def _clear_container(self, name):
        """Clear all detector history for one container."""

        self.fail_count.pop(name, None)

        tracker = self.mem_trk.pop(name, None)
        if tracker:
            tracker.clear()

        tracker = self.cpu_trk.pop(name, None)
        if tracker:
            tracker.clear()

    def note_action(self, name, now):
        """Reset tracking after healing and apply a recovery grace period."""

        self.grace_until[name] = now + self.grace
        self._clear_container(name)

    def reset_cluster(self):
        """Reset cluster history after a scaling event."""

        self.cluster_high.clear()
        self.cluster_low.clear()
        self.last_cluster = []
        self.last_cpu_ts = None

    def _in_grace(self, name, now):
        return now < self.grace_until.get(name, 0)

    # --------------------------------------------------------------
    # Container detection
    # --------------------------------------------------------------

    def _check_container(self, c, now):
        """Analyze one container and return its findings."""

        name = c["name"]
        out = []

        in_grace = self._in_grace(name, now)

        # Recovery grace: don't diagnose transitional states.
        if in_grace:
            self._clear_container(name)
            return out

        # Docker state failure takes precedence.
        if not c["running"]:
            self._clear_container(name)

            out.append(
                Finding(
                    kind="CONTAINER_DOWN",
                    container=name,
                    detail=f"status={c['status']}",
                )
            )

            return out

        # ----------------------------------------------------------
        # Application health
        # ----------------------------------------------------------

        health_ok = c.get("health_ok")

        if health_ok is True:
            self.fail_count[name] = 0

        elif health_ok is False:
            self.fail_count[name] = (
                self.fail_count.get(name, 0) + 1
            )

        else:
            # Unknown health result interrupts a consecutive-failure streak.
            self.fail_count[name] = 0

        failures = self.fail_count.get(name, 0)

        if failures >= self.need_fails:
            out.append(
                Finding(
                    kind="HEALTH_FAIL",
                    container=name,
                    detail=(
                        f"{failures} consecutive failed /health probes; "
                        f"detail={c.get('health_detail', 'unknown')}"
                    ),
                )
            )

        # ----------------------------------------------------------
        # Memory sustained high
        # ----------------------------------------------------------

        mem = c.get("mem_pct")

        if mem is None:
            # Missing metric: continuity is broken.
            tracker = self.mem_trk.get(name)
            if tracker:
                tracker.clear()

        else:
            tracker = self.mem_trk.setdefault(
                name,
                Sustained(
                    self.t["mem_sustain_sec"],
                    self.max_gap,
                ),
            )

            tracker.add(
                now,
                mem >= self.t["mem_high_pct"],
            )

            if tracker.holds(now):
                out.append(
                    Finding(
                        kind="MEM_HIGH",
                        container=name,
                        detail=(
                            f"memory={mem:.1f}% >= "
                            f"{self.t['mem_high_pct']}% sustained "
                            f"for {self.t['mem_sustain_sec']}s"
                        ),
                    )
                )

        # ----------------------------------------------------------
        # CPU sustained high per container
        # ----------------------------------------------------------

        cpu = c.get("cpu_pct")

        if cpu is None:
            tracker = self.cpu_trk.get(name)
            if tracker:
                tracker.clear()

        else:
            tracker = self.cpu_trk.setdefault(
                name,
                Sustained(
                    self.t["cpu_high_sustain_sec"],
                    self.max_gap,
                ),
            )

            tracker.add(
                now,
                cpu >= self.t["cpu_high_pct"],
            )

            if tracker.holds(now):
                out.append(
                    Finding(
                        kind="CPU_HIGH",
                        container=name,
                        detail=(
                            f"cpu={cpu:.1f}% >= "
                            f"{self.t['cpu_high_pct']}% sustained "
                            f"for {self.t['cpu_high_sustain_sec']}s"
                        ),
                    )
                )

        return out

    # --------------------------------------------------------------
    # Cluster detection
    # --------------------------------------------------------------

    def _check_cluster(self, snap, now):
        """Detect cluster CPU high/low only with complete fresh metrics."""

        running = [
            c for c in snap["containers"]
            if c["running"]
        ]

        # If no running replicas, no valid cluster CPU reading.
        if not running:
            self.cluster_high.clear()
            self.cluster_low.clear()
            self.last_cluster = []
            self.last_cpu_ts = None
            return []

        # Newly restarted replicas are excluded from reliable
        # cluster-level analysis until their grace period ends.
        if any(self._in_grace(c["name"], now) for c in running):
            self.cluster_high.clear()
            self.cluster_low.clear()
            self.last_cluster = []
            self.last_cpu_ts = None
            return []

        cpus = [c.get("cpu_pct") for c in running]

        # Require CPU data from every running replica.
        if any(cpu is None for cpu in cpus):
            self.cluster_high.clear()
            self.cluster_low.clear()
            self.last_cluster = []
            self.last_cpu_ts = None
            return []

        avg = mean(cpus)

        self.cluster_high.add(
            now,
            avg >= self.t["cpu_high_pct"],
        )

        self.cluster_low.add(
            now,
            avg <= self.t["cpu_low_pct"],
        )

        out = []

        if self.cluster_high.holds(now):
            out.append(
                Finding(
                    kind="CLUSTER_CPU_HIGH",
                    container=None,
                    detail=(
                        f"average CPU={avg:.1f}% >= "
                        f"{self.t['cpu_high_pct']}% sustained "
                        f"for {self.t['cpu_high_sustain_sec']}s"
                    ),
                )
            )

        if self.cluster_low.holds(now):
            out.append(
                Finding(
                    kind="CLUSTER_CPU_LOW",
                    container=None,
                    detail=(
                        f"average CPU={avg:.1f}% <= "
                        f"{self.t['cpu_low_pct']}% sustained "
                        f"for {self.t['cpu_low_sustain_sec']}s"
                    ),
                )
            )

        self.last_cluster = out
        self.last_cpu_ts = now

        return out

    # --------------------------------------------------------------
    # Main analysis
    # --------------------------------------------------------------

    def analyze(self, snap):
        """Convert one monitoring snapshot into detection findings."""

        now = snap["ts"]
        containers = snap["containers"]
        findings = []

        present = {
            c["name"] for c in containers
        }

        # Remove stale state for deleted containers.
        for table in (
            self.fail_count,
            self.mem_trk,
            self.cpu_trk,
            self.grace_until,
        ):
            for name in list(table):
                if name not in present:
                    table.pop(name, None)

        # Container-level findings.
        for c in containers:
            findings.extend(
                self._check_container(c, now)
            )

        # Cluster-level findings.
        findings.extend(
            self._check_cluster(snap, now)
        )

        return findings

