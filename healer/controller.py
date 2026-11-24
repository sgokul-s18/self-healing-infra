"""Controller: executes actions through the docker CLI and verifies recovery."""
import subprocess
import time


class Controller:
    def __init__(self, cfg, mon):
        self.cfg = cfg
        self.mon = mon

    def _run(self, *args):
        # cwd = project dir so docker compose reads the same .env as manual runs
        return subprocess.run(list(args), cwd=self.cfg["project_dir"], check=True,
                              capture_output=True, text=True, timeout=120)

    def _scale(self, n):
        self._run("docker", "compose", "-p", self.cfg["project"], "-f", self.cfg["compose_file"],
                  "up", "-d", "--no-recreate", "--no-build", "--no-deps",
                  "--scale", f"{self.cfg['service']}={n}", self.cfg["service"])

    def execute(self, a, desired):
        """Runs the action. Returns the names of containers to verify ([] if none)."""
        if a.type == "RESTART":
            self._run("docker", "restart", "-t", "5", a.container)  # also starts an exited one
            return [a.container]

        if a.type == "REPLACE":
            self._run("docker", "rm", "-f", a.container)
            before = self.mon.running_names()
            self._scale(desired)
            return sorted(self.mon.running_names() - before) or [a.container]

        if a.type in ("SCALE_UP", "RECONCILE"):
            before = self.mon.running_names()
            self._scale(desired)
            return sorted(self.mon.running_names() - before)

        if a.type == "SCALE_DOWN":
            self._scale(desired)   # compose removes the highest-numbered replicas
            return []

        raise ValueError(f"unknown action type {a.type}")

    def verify(self, targets, timeout):
        """Waits until every target is running and answers /health with 200."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if all(self.mon.is_healthy(n) for n in targets):
                return True, time.time() - t0
            time.sleep(1)
        return False, time.time() - t0
