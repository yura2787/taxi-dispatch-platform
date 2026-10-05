"""Prometheus metrics endpoint of the realtime service."""


async def test_metrics_are_exported_in_prometheus_format(client):
    response = await client.get("/rt/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE osrm_requests_total counter" in response.text
    assert "# TYPE osrm_request_duration_seconds histogram" in response.text
