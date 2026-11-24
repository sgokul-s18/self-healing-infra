"""Decision engine: findings -> actions, with cooldowns, loop protection, min/max limits."""
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

RECOVERY_PRIORITY = ["CONTAINER_DOWN", "HEALTH_FAIL", "MEM_HIGH"]
ALERT_REPEAT_SEC = 60


@dataclass
class Action:
    type: str                  # RESTART | REPLACE | SCALE_UP | SCALE_DOWN | RECONCILE | ALERT
    container: Optional[str]
    reason: str


class DecisionEngine:
    def __init__(self, cfg, initial_replicas):
        self.s = cfg["scaling"]
        self.r = cfg["recovery"]
        self.desired = initial_replicas
        self.restarts = defaultdict(list)   # name -> restart timestamps (inside the window)
        self.replaces = defaultdict(list)   # name -> replacement timestamps
        self.last_action = {}               # name -> epoch of last recovery action
        self.last_scale = 0.0
        self.last_alert = {}

    def _alert(self, key, container, reason, now):
        if now - self.last_alert.get(key, 0) < ALERT_REPEAT_SEC:
            return None
        self.last_alert[key] = now
        return Action("ALERT", container, reason)

    def _prune(self, name, now):
        w = self.r["window_sec"]
        self.restarts[name] = [t for t in self.restarts[name] if now - t < w]
        self.replaces[name] = [t for t in self.replaces[name] if now - t < w]

    def decide(self, faults, snap, now):
        actions = []

        # 1. Per-container recovery (one action per container, most severe fault first)
        per = {}
        for f in faults:
            if f.kind in RECOVERY_PRIORITY and f.container:
                cur = per.get(f.container)
                if cur is None or RECOVERY_PRIORITY.index(f.kind) < RECOVERY_PRIORITY.index(cur.kind):
                    per[f.container] = f

        for name, f in per.items():
            if now - self.last_action.get(name, 0) < self.r["action_cooldown_sec"]:
                continue
            self._prune(name, now)
            why = f"{f.kind}: {f.detail}"
            if len(self.replaces[name]) >= self.r["replacements_before_giving_up"]:
                a = self._alert(("giveup", name), name,
                                f"{why}; {len(self.replaces[name])} replacements already tried "
                                f"in {self.r['window_sec']}s - giving up, manual action needed", now)
                if a:
                    actions.append(a)
                continue
            if len(self.restarts[name]) >= self.r["restarts_before_replace"]:
                self.replaces[name].append(now)
                self.restarts[name].clear()
                actions.append(Action("REPLACE", name,
                                      f"{why}; crash loop ({self.r['restarts_before_replace']} "
                                      f"restarts in {self.r['window_sec']}s)"))
            else:
                self.restarts[name].append(now)
                actions.append(Action("RESTART", name,
                                      f"{why}; restart {len(self.restarts[name])}/"
                                      f"{self.r['restarts_before_replace']}"))
            self.last_action[name] = now

        # 2. Scaling, only when nothing is being recovered this cycle
        if per:
            return actions
        scale_ok = now - self.last_scale >= self.s["cooldown_sec"]
        kinds = {f.kind for f in faults}
        count = len(snap["containers"])
        lo, hi = self.s["min_replicas"], self.s["max_replicas"]

        if count < self.desired:
            if scale_ok:
                self.last_scale = now
                actions.append(Action("RECONCILE", None,
                                      f"replicas={count} < desired={self.desired}"))
        elif "CPU_HIGH" in kinds:
            hot = next(f for f in faults if f.kind == "CPU_HIGH")
            if self.desired >= hi:
                a = self._alert(("max", None), None,
                                f"{hot.container} {hot.detail}; already at max_replicas={hi}", now)
                if a:
                    actions.append(a)
            elif scale_ok:
                self.desired += 1
                self.last_scale = now
                actions.append(Action("SCALE_UP", None,
                                      f"{hot.container} {hot.detail}; desired -> {self.desired}"))
        elif "CPU_LOW" in kinds and self.desired > lo and scale_ok:
            self.desired -= 1
            self.last_scale = now
            low = next(f for f in faults if f.kind == "CPU_LOW")
            actions.append(Action("SCALE_DOWN", None, f"{low.detail}; desired -> {self.desired}"))
        return actions
