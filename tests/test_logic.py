"""Offline test of detector + decision logic with simulated time (touches no containers)."""
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "healer"))
from decision import DecisionEngine  # noqa: E402
from detector import Detector        # noqa: E402

cfg = yaml.safe_load(open(ROOT / "healer" / "config.yaml"))


def c(name, running=True, health=True, cpu=5.0, mem=40.0):
    return {"name": name, "running": running, "status": "running" if running else "exited",
            "health_ok": health, "cpu_pct": cpu, "mem_pct": mem}


def snap(ts, *cs):
    return {"ts": ts, "prom_ok": True, "containers": list(cs)}


def step(det, dec, s):
    return [a.type for a in dec.decide(det.analyze(s), s, s["ts"])]


# 1. Container down -> restart; repeated -> replace
det, dec, t = Detector(cfg), DecisionEngine(cfg, 2), 1000.0
seq = []
for i in range(4):
    seq += step(det, dec, snap(t + 30 * i, c("shi-app-1", running=False), c("shi-app-2")))
assert seq == ["RESTART", "RESTART", "RESTART", "REPLACE"], seq
print("1 crash loop        :", seq)

# 2. Health failures need 3 consecutive probes
det, dec = Detector(cfg), DecisionEngine(cfg, 2)
seq = [step(det, dec, snap(t + 5 * i, c("shi-app-1", health=False), c("shi-app-2")))
       for i in range(3)]
assert seq == [[], [], ["RESTART"]], seq
print("2 health failure    :", seq)

# 3. Sustained CPU -> scale up (and not before 30 s)
det, dec, seq = Detector(cfg), DecisionEngine(cfg, 2), []
for k in range(0, 40, 5):
    seq += step(det, dec, snap(t + k, c("shi-app-1", cpu=95), c("shi-app-2")))
assert seq == ["SCALE_UP"] and dec.desired == 3, (seq, dec.desired)
print("3 cpu scale up      :", seq, "desired =", dec.desired)

# 4. Idle -> scale down, never below min_replicas
det, dec, seq = Detector(cfg), DecisionEngine(cfg, 3), []
for k in range(0, 70, 5):
    seq += step(det, dec, snap(t + k, c("a"), c("b"), c("c")))
assert seq == ["SCALE_DOWN"] and dec.desired == 2, (seq, dec.desired)
det, dec, seq = Detector(cfg), DecisionEngine(cfg, 2), []
for k in range(0, 130, 5):
    seq += step(det, dec, snap(t + k, c("a"), c("b")))
assert seq == [], seq
print("4 scale down / min  : ok")

# 5. Missing replica -> reconcile
det, dec = Detector(cfg), DecisionEngine(cfg, 2)
assert step(det, dec, snap(t + 200, c("shi-app-2"))) == ["RECONCILE"]
print("5 missing replica   : RECONCILE")
print("all logic tests passed")
