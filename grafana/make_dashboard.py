"""Generates grafana/dashboards/shi-main.json (run: python3 grafana/make_dashboard.py)."""
import json
import pathlib

DS = {"type": "prometheus", "uid": "prometheus"}
CPU_LIMIT = 0.5  # must match deploy.resources.limits.cpus in docker-compose.yml


def targets(exprs):
    return [
        {"refId": chr(65 + i), "expr": e, "legendFormat": l, "datasource": DS}
        for i, (e, l) in enumerate(exprs)
    ]


def timeseries(pid, title, exprs, x, y, unit="short", w=12, h=8):
    return {
        "id": pid, "type": "timeseries", "title": title, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": targets(exprs),
    }


def stat(pid, title, expr, x, y, unit="short", w=4, h=4):
    return {
        "id": pid, "type": "stat", "title": title, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": targets([(expr, "")]),
    }


CONT = 'name=~"shi-app.*"'
NOHEALTH = 'endpoint!="/health"'
HOST_CPU = '100 - avg(rate(node_cpu_seconds_total{mode="idle"}[30s])) * 100'
HOST_MEM = '(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100'

panels = [
    stat(1, "App instances scraped", 'count(up{job="app"}) or vector(0)', 0, 0),
    stat(2, "Healthy", 'count(app_health_status == 1) or vector(0)', 4, 0),
    stat(3, "Unhealthy", 'count(app_health_status == 0) or vector(0)', 8, 0),
    stat(4, "Request rate (excl. /health)",
         f'sum(rate(app_requests_total{{{NOHEALTH}}}[30s])) or vector(0)',
         12, 0, "reqps"),
    stat(5, "Host CPU %", HOST_CPU, 16, 0, "percent"),
    stat(6, "Host memory %", HOST_MEM, 20, 0, "percent"),
    timeseries(7, "Container CPU (% of CPU limit)",
               [(f'sum by (name) (rate(container_cpu_usage_seconds_total{{{CONT}}}[30s])) * 100 / {CPU_LIMIT}',
                 "{{name}}")], 0, 4, "percent"),
    timeseries(8, "Container memory (working set)",
               [(f'container_memory_working_set_bytes{{{CONT}}}', "{{name}}")],
               12, 4, "bytes"),
    timeseries(9, "Container network received",
               [(f'sum by (name) (rate(container_network_receive_bytes_total{{{CONT}}}[30s]))',
                 "{{name}}")], 0, 12, "Bps"),
    timeseries(10, "Request rate per instance (excl. /health)",
               [(f'sum by (instance) (rate(app_requests_total{{{NOHEALTH}}}[30s]))',
                 "{{instance}}")], 12, 12, "reqps"),
    timeseries(11, "Response time p95 (excl. /health)",
               [(f'histogram_quantile(0.95, sum by (le) (rate(app_request_duration_seconds_bucket{{{NOHEALTH}}}[1m])))',
                 "p95")], 0, 20, "s"),
    timeseries(12, "Host CPU and memory %",
               [(HOST_CPU, "CPU"), (HOST_MEM, "Memory")], 12, 20, "percent"),
]

dashboard = {
    "uid": "shi-main",
    "title": "Self-Healing Infrastructure",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "5s",
    "time": {"from": "now-15m", "to": "now"},
    "panels": panels,
}

out = pathlib.Path(__file__).parent / "dashboards" / "shi-main.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(dashboard, indent=2))
print("wrote", out)
