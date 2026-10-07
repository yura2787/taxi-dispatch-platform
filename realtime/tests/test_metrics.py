"""Prometheus metrics endpoint of the realtime service."""

import subprocess
import sys


async def test_metrics_are_exported_in_prometheus_format(client):
    response = await client.get("/rt/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE osrm_requests_total counter" in response.text
    assert "# TYPE osrm_request_duration_seconds histogram" in response.text


def test_gateway_does_not_export_dispatcher_metrics():
    # In the test process both modules are imported; the gateway alone must not
    # register drivers_online, or it would export zeros next to the real values.
    code = (
        "import app.main\n"
        "from prometheus_client import REGISTRY, generate_latest\n"
        "print(generate_latest(REGISTRY).decode())\n"
    )

    exported = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout

    assert "ws_connections" in exported
    assert "drivers_online" not in exported
    assert "reaper_" not in exported
