"""With PROMETHEUS_MULTIPROC_DIR set (the Docker image does), /metrics shows the numbers of every uvicorn process, not just one."""

import subprocess
import sys

CODE = """
import os
from app.telemetry.metrics import JOINS, EVENT_LOOP_LAG, render_all
JOINS.labels(source="invite", result="ok").inc(3)
EVENT_LOOP_LAG.set(0.25)
body, _ = render_all()
print(body.decode())
"""


def run(tmp_path):
    env = {"PROMETHEUS_MULTIPROC_DIR": str(tmp_path), "PATH": __import__("os").environ.get("PATH", ""), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    return subprocess.run([sys.executable, "-c", CODE], capture_output=True, text=True, env=env, cwd=".", timeout=60)


def test_two_processes_add_up_in_one_scrape(tmp_path):
    first, second = run(tmp_path), run(tmp_path)               # two separate processes writing to the same directory
    assert first.returncode == 0, first.stderr[-500:]
    assert 'org_joins_total{result="ok",source="invite"} 3.0' in first.stdout
    assert 'org_joins_total{result="ok",source="invite"} 6.0' in second.stdout          # the second sees both processes' counts
    assert "org_event_loop_lag_seconds 0.25" in second.stdout                            # a gauge reports the max, not a sum
    assert list(tmp_path.iterdir())


def test_without_the_variable_the_page_is_this_process_only():
    from app.telemetry.metrics import render_all
    body, content_type = render_all()
    assert b"org_joins_total" in body and content_type.startswith("text/plain")
