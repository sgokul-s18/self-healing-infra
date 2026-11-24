"""Monitor: Docker API + HTTP /health probe + Prometheus -> one snapshot per cycle."""
import time

import docker
import requests


class Monitor:
    def __init__(self, cfg):
        self.project = cfg["project"]
        self.service = cfg["service"]
        self.network = cfg["network"]
        self.port = cfg["app_port"]
        self.health_path = cfg["health"]["path"]
        self.health_timeout = cfg["health"]["timeout_sec"]
        self.prom_url = cfg["prometheus_url"].rstrip("/")
        self.last_prom_error = ""
        self.client = docker.from_env()
        info = self.client.info()
        self.host_cpus = info["NCPU"]
        self.host_mem = info["MemTotal"]

    # ---- Docker API -------------------------------------------------------
    def list_containers(self):
        """All replicas of the service (running or not), ordered by replica number."""
        found = self.client.containers.list(all=True, filters={"label": [
            f"com.docker.compose.project={self.project}",
            f"com.docker.compose.service={self.service}"]})
        out = []
        for c in found:
            try:
                c.reload()  # the list API returns partial data; inspect gives limits and IPs
            except docker.errors.NotFound:
                continue
            out.append(c)
        out.sort(key=lambda c: int(c.labels.get("com.docker.compose.container-number", 0)))
        return out

    def running_names(self):
        return {c.name for c in self.list_containers() if c.status == "running"}

    def _ip(self, c):
        nets = c.attrs.get("NetworkSettings", {}).get("Networks", {})
        return nets.get(self.network, {}).get("IPAddress") or None

    # ---- Application health ----------------------------------------------
    def _probe(self, ip):
        if not ip:
            return False
        try:
            r = requests.get(f"http://{ip}:{self.port}{self.health_path}",
                             timeout=self.health_timeout)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def is_healthy(self, name):
        try:
            c = self.client.containers.get(name)
            c.reload()
        except docker.errors.NotFound:
            return False
        return c.status == "running" and self._probe(self._ip(c))

    # ---- Prometheus --------------------------------------------------------
    def _prom(self, query):
        """Returns {container_name: value}, or None if Prometheus is unreachable."""
        try:
            r = requests.get(f"{self.prom_url}/api/v1/query",
                             params={"query": query}, timeout=3)
            r.raise_for_status()
            return {s["metric"].get("name"): float(s["value"][1])
                    for s in r.json()["data"]["result"]}
        except (requests.RequestException, KeyError, ValueError) as exc:
            self.last_prom_error = f"{type(exc).__name__}: {exc}"[:120]
            return None

    # ---- Snapshot ------------------------------------------------------------
    def snapshot(self):
        ts = time.time()
        conts = self.list_containers()
        sel = f'name=~"{self.project}-{self.service}-.*"'
        cpu = self._prom(f"sum by (name) (rate(container_cpu_usage_seconds_total{{{sel}}}[30s]))")
        mem = self._prom(f"max by (name) (container_memory_working_set_bytes{{{sel}}})")
        out = []
        for c in conts:
            host = c.attrs.get("HostConfig", {})
            cpu_limit = (host.get("NanoCpus") or 0) / 1e9 or self.host_cpus
            mem_limit = host.get("Memory") or self.host_mem
            running = c.status == "running"
            cores = None if cpu is None else cpu.get(c.name)
            used = None if mem is None else mem.get(c.name)
            out.append({
                "name": c.name,
                "running": running,
                "status": c.status,
                "health_ok": running and self._probe(self._ip(c)),
                "cpu_pct": None if cores is None else cores / cpu_limit * 100,
                "mem_pct": None if used is None else used / mem_limit * 100,
            })
        return {"ts": ts, "prom_ok": cpu is not None and mem is not None, "containers": out}
