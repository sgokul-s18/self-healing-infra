"""Controller: executes Docker recovery actions and verifies the result."""

import subprocess
import time

import docker


class Controller:

    def __init__(self, cfg, monitor):
        self.cfg = cfg
        self.mon = monitor
        self.client = monitor.client

        # Set by execute() for service-level operations.
        self.expected_replicas = None
        self.verify_service = False

    # --------------------------------------------------
    # Docker Compose execution
    # --------------------------------------------------

    def _compose_scale(self, replicas):
        service = self.cfg["service"]

        cmd = [
            "docker", "compose",
            "-p", self.cfg["project"],
            "-f", self.cfg["compose_file"],
            "up", "-d",
            "--no-recreate",
            "--no-build",
            "--no-deps",
            "--scale", f"{service}={replicas}",
            service,
        ]

        return subprocess.run(
            cmd,
            cwd=self.cfg["project_dir"],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )

    # --------------------------------------------------
    # Container information
    # --------------------------------------------------

    def _service_containers(self):
        """Return current containers belonging to the configured service."""
        containers = []

        for container in self.mon.list_containers():
            try:
                container.reload()
                containers.append(container)
            except docker.errors.NotFound:
                continue

        return containers

    def _running_containers(self):
        return [
            container
            for container in self._service_containers()
            if container.status == "running"
        ]

    def _running_count(self):
        return len(self._running_containers())

    # --------------------------------------------------
    # Execute recovery action
    # --------------------------------------------------

    def execute(self, action, desired):
        """
        Execute an action.

        Returns container names for individual recovery.
        Service-level operations are verified dynamically.
        """

        kind = action.type
        name = action.container

        # Reset verification mode for each new action.
        self.expected_replicas = None
        self.verify_service = False

        # ----------------------------------------------
        # Restart an existing or stopped container
        # ----------------------------------------------

        if kind == "RESTART":
            container = self.client.containers.get(name)
            container.restart(timeout=5)
            return [name]

        # ----------------------------------------------
        # Replace a faulty container
        # ----------------------------------------------

        if kind == "REPLACE":
            container = self.client.containers.get(name)
            container.remove(force=True)

            self._compose_scale(desired)

            self.expected_replicas = desired
            self.verify_service = True

            return []

        # ----------------------------------------------
        # Reconcile or scale service replicas
        # ----------------------------------------------

        if kind in ("RECONCILE", "SCALE_UP", "SCALE_DOWN"):
            self._compose_scale(desired)

            self.expected_replicas = desired
            self.verify_service = True

            return []

        # ----------------------------------------------
        # Reject unsupported actions
        # ----------------------------------------------

        raise ValueError(f"Unsupported action type: {kind}")

    # --------------------------------------------------
    # Verify individual container health
    # --------------------------------------------------

    def _verify_container(self, name):
        try:
            return self.mon.is_healthy(name)[0]

        except docker.errors.NotFound:
            return False

    # --------------------------------------------------
    # Verify entire service
    # --------------------------------------------------

    def _verify_service_health(self):
        containers = self._running_containers()

        if len(containers) != self.expected_replicas:
            return False

        for container in containers:
            if not self.mon.is_healthy(container.name)[0]:
                return False

        return True

    # --------------------------------------------------
    # Wait for recovery
    # --------------------------------------------------

    def verify(self, targets, timeout):
        """
        Wait until recovery is confirmed.

        Service-level actions:
            Verify exact running replica count and health.

        Individual actions:
            Verify the specified container is healthy.
        """

        start = time.monotonic()
        deadline = start + timeout

        while time.monotonic() < deadline:

            if self.verify_service:
                if self._verify_service_health():
                    return True, time.monotonic() - start

            else:
                if targets and all(
                    self._verify_container(name)
                    for name in targets
                ):
                    return True, time.monotonic() - start

            time.sleep(1)

        return False, time.monotonic() - start

