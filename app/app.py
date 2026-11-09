"""Sample Flask application for the self-healing infrastructure project.

Normal endpoints:  /  /health  /metrics
Lab-only failure-injection endpoints (blocked from outside by Nginx later):
  /cpu?seconds=N   burn CPU on one thread
  /mem?mb=N        allocate and hold N MB of memory
  /mem/release     free the held memory
  /fail-health     make /health return 503
  /recover-health  make /health return 200 again
  /crash           stop the application process shortly after replying
"""
import os
import signal
import socket
import threading
import time

from flask import Flask, Response, jsonify, request
from prometheus_client import (CONTENT_TYPE_LATEST, Counter, Gauge, Histogram,
                               generate_latest)

app = Flask(__name__)
HOSTNAME = socket.gethostname()  # inside a container this is the container ID

state = {"healthy": True}
mem_hold = []  # keeps allocated memory alive until released

REQUESTS = Counter("app_requests_total", "Total HTTP requests",
                   ["endpoint", "status"])
LATENCY = Histogram("app_request_duration_seconds", "Request latency in seconds",
                    ["endpoint"])
HEALTH = Gauge("app_health_status", "1 = healthy, 0 = unhealthy")
HEALTH.set(1)


@app.before_request
def start_timer():
    request.start_time = time.perf_counter()


@app.after_request
def record_metrics(response):
    endpoint = request.url_rule.rule if request.url_rule else "unmatched"
    if endpoint != "/metrics" and hasattr(request, "start_time"):
        LATENCY.labels(endpoint).observe(time.perf_counter() - request.start_time)
        REQUESTS.labels(endpoint, response.status_code).inc()
    return response


@app.route("/")
def index():
    return jsonify(message="Application is running", container=HOSTNAME)


@app.route("/health")
def health():
    if state["healthy"]:
        return jsonify(status="healthy", container=HOSTNAME), 200
    return jsonify(status="unhealthy", container=HOSTNAME), 503


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.route("/cpu")
def cpu():
    seconds = min(int(request.args.get("seconds", 10)), 120)
    end = time.time() + seconds
    while time.time() < end:
        pass  # busy loop keeps one core at 100%
    return jsonify(status="cpu burn finished", seconds=seconds)


@app.route("/mem")
def mem():
    mb = min(int(request.args.get("mb", 100)), 512)
    mem_hold.append(b"x" * (mb * 1024 * 1024))  # writing bytes forces real allocation
    return jsonify(status="allocated", mb=mb, total_mb=len(mem_hold) * mb)


@app.route("/mem/release")
def mem_release():
    mem_hold.clear()
    return jsonify(status="released")


@app.route("/fail-health")
def fail_health():
    state["healthy"] = False
    HEALTH.set(0)
    return jsonify(status="health endpoint will now fail")


@app.route("/recover-health")
def recover_health():
    state["healthy"] = True
    HEALTH.set(1)
    return jsonify(status="health endpoint restored")


@app.route("/crash")
def crash():
    # Ask the gunicorn master (our parent process) to shut down so the whole
    # container exits, which is what a real application crash looks like.
    threading.Timer(0.5, lambda: os.kill(os.getppid(), signal.SIGTERM)).start()
    return jsonify(status="crashing"), 202


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
