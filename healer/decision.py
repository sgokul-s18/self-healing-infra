"""Decision engine: convert detector findings into recovery and scaling actions."""

from collections import defaultdict
from dataclasses import dataclass
from typing import Optional


RECOVERY_PRIORITY = (
    "CONTAINER_DOWN",
    "HEALTH_FAIL",
    "MEM_HIGH",
)

ALERT_REPEAT_SEC = 60


@dataclass
class Action:
    type: str
    container: Optional[str]
    reason: str


class DecisionEngine:

    def __init__(self, cfg, initial_replicas):
        self.s = cfg["scaling"]
        self.r = cfg["recovery"]

        self.desired = initial_replicas

        # Recovery history tracked separately for each container.
        self.restarts = defaultdict(list)
        self.replacements = defaultdict(list)

        # Last recovery action per container.
        self.last_action = {}

        # Independent cooldowns.
        self.last_scale = None
        self.last_reconcile = None

        # Alert rate limiting.
        self.last_alert = {}

        self.alert_repeat_sec = ALERT_REPEAT_SEC

    # --------------------------------------------------
    # Alert handling
    # --------------------------------------------------

    def _alert(self, key, container, reason, now):
        last = self.last_alert.get(key)

        # Allow the first alert immediately.
        if last is not None and now - last < self.alert_repeat_sec:
            return None

        self.last_alert[key] = now
        return Action("ALERT", container, reason)

    # --------------------------------------------------
    # History cleanup
    # --------------------------------------------------

    def _prune(self, name, now):
        window = self.r["window_sec"]

        self.restarts[name] = [
            t for t in self.restarts[name]
            if 0 <= now - t < window
        ]

        self.replacements[name] = [
            t for t in self.replacements[name]
            if 0 <= now - t < window
        ]

    # --------------------------------------------------
    # Recovery decision
    # --------------------------------------------------

    def _recover(self, name, fault, now):
        actions = []

        cooldown = self.r["action_cooldown_sec"]

        last = self.last_action.get(name)

        # Give the previous action time to work.
        if last is not None and now - last < cooldown:
            return actions

        self._prune(name, now)

        reason = f"{fault.kind}: {fault.detail}"

        restart_limit = self.r["restarts_before_replace"]
        replacement_limit = self.r["replacements_before_giving_up"]

        # Circuit breaker: stop repeated replacement attempts.
        if len(self.replacements[name]) >= replacement_limit:
            alert = self._alert(
                ("giveup", name),
                name,
                (
                    f"{reason}; {len(self.replacements[name])} replacements "
                    f"within {self.r['window_sec']}s; "
                    "recovery limit reached, manual intervention required"
                ),
                now,
            )

            if alert:
                actions.append(alert)

            return actions

        # Replace after repeated restart attempts.
        if len(self.restarts[name]) >= restart_limit:
            self.replacements[name].append(now)
            self.restarts[name].clear()

            actions.append(
                Action(
                    "REPLACE",
                    name,
                    (
                        f"{reason}; restart limit reached "
                        f"({restart_limit} within "
                        f"{self.r['window_sec']}s)"
                    ),
                )
            )

        else:
            self.restarts[name].append(now)

            actions.append(
                Action(
                    "RESTART",
                    name,
                    (
                        f"{reason}; restart attempt "
                        f"{len(self.restarts[name])}/{restart_limit}"
                    ),
                )
            )

        self.last_action[name] = now
        return actions

    # --------------------------------------------------
    # Main decision function
    # --------------------------------------------------

    def decide(self, findings, snap, now):

        actions = []

        # ----------------------------------------------
        # 1. Select the most important fault per container
        # ----------------------------------------------

        priority = {
            kind: index
            for index, kind in enumerate(RECOVERY_PRIORITY)
        }

        per_container = {}

        for fault in findings:
            if not fault.container:
                continue

            if fault.kind not in priority:
                continue

            current = per_container.get(fault.container)

            if (
                current is None
                or priority[fault.kind] < priority[current.kind]
            ):
                per_container[fault.container] = fault

        # ----------------------------------------------
        # 2. Recover faulty containers first
        # ----------------------------------------------

        recovering = bool(per_container)

        for name in sorted(per_container):
            fault = per_container[name]

            actions.extend(
                self._recover(name, fault, now)
            )

        # ----------------------------------------------
        # 3. Alert on container CPU findings
        # ----------------------------------------------

        for fault in findings:
            if fault.kind != "CPU_HIGH" or not fault.container:
                continue

            alert = self._alert(
                ("container_cpu", fault.container),
                fault.container,
                (
                    f"Container CPU high: {fault.detail}; "
                    "investigate individual container load"
                ),
                now,
            )

            if alert:
                actions.append(alert)

        # ----------------------------------------------
        # 4. Count running replicas only
        # ----------------------------------------------

        running_count = sum(
            1 for c in snap["containers"]
            if c.get("running") is True
        )

        # ----------------------------------------------
        # 5. Reconcile missing replicas before scaling
        # ----------------------------------------------

        reconcile_cooldown = self.r["action_cooldown_sec"]

        reconcile_ready = (
            self.last_reconcile is None
            or now - self.last_reconcile >= reconcile_cooldown
        )

        if not recovering and running_count < self.desired:
            if reconcile_ready:
                actions.append(
                    Action(
                        "RECONCILE",
                        None,
                        (
                            f"Running replicas={running_count}, "
                            f"desired replicas={self.desired}"
                        ),
                    )
                )

                self.last_reconcile = now

            # Do not scale during a replica reconciliation.
            recovering = True

        # ----------------------------------------------
        # 6. Cluster scaling
        # ----------------------------------------------

        scale_cooldown = self.s["cooldown_sec"]

        scale_ready = (
            self.last_scale is None
            or now - self.last_scale >= scale_cooldown
        )

        if recovering or not scale_ready:
            return actions

        kinds = {fault.kind for fault in findings}

        min_replicas = self.s["min_replicas"]
        max_replicas = self.s["max_replicas"]

        # Scale-up has priority if conflicting findings exist.
        if "CLUSTER_CPU_HIGH" in kinds:

            high = next(
                f for f in findings
                if f.kind == "CLUSTER_CPU_HIGH"
            )

            if self.desired >= max_replicas:
                alert = self._alert(
                    ("max_replicas", None),
                    None,
                    (
                        f"{high.detail}; already at maximum replicas "
                        f"({max_replicas})"
                    ),
                    now,
                )

                if alert:
                    actions.append(alert)

            else:
                old = self.desired
                self.desired += 1
                self.last_scale = now

                actions.append(
                    Action(
                        "SCALE_UP",
                        None,
                        (
                            f"{high.detail}; desired replicas "
                            f"{old} -> {self.desired}"
                        ),
                    )
                )

        elif "CLUSTER_CPU_LOW" in kinds:

            low = next(
                f for f in findings
                if f.kind == "CLUSTER_CPU_LOW"
            )

            if self.desired > min_replicas:
                old = self.desired
                self.desired -= 1
                self.last_scale = now

                actions.append(
                    Action(
                        "SCALE_DOWN",
                        None,
                        (
                            f"{low.detail}; desired replicas "
                            f"{old} -> {self.desired}"
                        ),
                    )
                )

        return actions

