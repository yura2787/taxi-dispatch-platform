import asyncio

import httpx
import pytest
from prometheus_client import REGISTRY

from app import config
from app.services.osrm import CircuitBreaker, NoRoute, OsrmClient, OsrmUnavailable, Point

CENTRE = Point(lat=48.2921, lon=25.9358)
STATION = Point(lat=48.2668, lon=25.9306)


class FakeOsrm:
    """httpx transport that records requests and answers with a canned response."""

    def __init__(self, response=None):
        self.response = response or httpx.Response(200, json={"code": "Ok"})
        self.requests: list[httpx.Request] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def client(self) -> OsrmClient:
        transport = httpx.MockTransport(self.handler)
        return OsrmClient(httpx.AsyncClient(transport=transport, base_url="http://osrm"))


def ok(body: dict) -> httpx.Response:
    return httpx.Response(200, json={"code": "Ok", **body})


ROUTE_BODY = {
    "routes": [
        {
            "distance": 3456.7,
            "duration": 400.0,
            "geometry": {"type": "LineString", "coordinates": [[25.9357, 48.2922], [25.93, 48.27]]},
        }
    ],
    "waypoints": [{"location": [25.9357, 48.2922]}, {"location": [25.9305, 48.2667]}],
}


# --- request URL ---


async def test_route_sends_lon_lat_radiuses_and_no_overview():
    osrm = FakeOsrm(ok(ROUTE_BODY))

    await osrm.client().route(CENTRE, STATION)

    request = osrm.requests[0]
    assert request.url.path == "/route/v1/driving/25.9358,48.2921;25.9306,48.2668"
    assert request.url.params["radiuses"].split(";") == [str(config.OSRM_SNAP_RADIUS_M)] * 2
    assert request.url.params["overview"] == "false"
    assert "geometries" not in request.url.params


async def test_route_asks_for_geojson_geometry_only_when_requested():
    osrm = FakeOsrm(ok(ROUTE_BODY))

    await osrm.client().route(CENTRE, STATION, with_geometry=True)

    params = osrm.requests[0].url.params
    assert params["overview"] == "full"
    assert params["geometries"] == "geojson"


async def test_table_sends_source_and_destination_indexes_with_duration_annotation():
    osrm = FakeOsrm(
        ok(
            {
                "durations": [[10.0], [20.0]],
                "sources": [{"location": [25.0, 48.0]}, {"location": [25.1, 48.1]}],
                "destinations": [{"location": [25.2, 48.2]}],
            }
        )
    )

    await osrm.client().table([CENTRE, STATION], [CENTRE])

    request = osrm.requests[0]
    assert request.url.path == "/table/v1/driving/25.9358,48.2921;25.9306,48.2668;25.9358,48.2921"
    assert request.url.params["sources"] == "0;1"
    assert request.url.params["destinations"] == "2"
    assert request.url.params["annotations"] == "duration"
    assert request.url.params["radiuses"].split(";") == [str(config.OSRM_SNAP_RADIUS_M)] * 3


# --- response parsing ---


async def test_route_parses_distance_duration_with_factor_and_snapped_points():
    route = await FakeOsrm(ok(ROUTE_BODY)).client().route(CENTRE, STATION)

    assert route.distance_m == 3457
    assert route.duration_s == round(400 * config.OSRM_DURATION_FACTOR)
    assert route.snapped_origin == Point(lat=48.2922, lon=25.9357)
    assert route.snapped_destination == Point(lat=48.2667, lon=25.9305)
    assert route.geometry is None


async def test_route_converts_geometry_to_lat_lon():
    route = await FakeOsrm(ok(ROUTE_BODY)).client().route(CENTRE, STATION, with_geometry=True)

    assert route.geometry == [Point(lat=48.2922, lon=25.9357), Point(lat=48.27, lon=25.93)]


async def test_table_keeps_none_for_unreachable_pairs():
    osrm = FakeOsrm(
        ok(
            {
                "durations": [[100.0, None], [0.0, 50.0]],
                "sources": [{"location": [25.0, 48.0]}, {"location": [25.1, 48.1]}],
                "destinations": [{"location": [25.2, 48.2]}, {"location": [25.3, 48.3]}],
            }
        )
    )

    table = await osrm.client().table([CENTRE, STATION], [CENTRE, STATION])

    assert table.durations == [[130, None], [0, 65]]
    assert table.snapped_sources == [Point(lat=48.0, lon=25.0), Point(lat=48.1, lon=25.1)]
    assert table.snapped_destinations == [Point(lat=48.2, lon=25.2), Point(lat=48.3, lon=25.3)]


async def test_nearest_returns_snapped_point():
    osrm = FakeOsrm(ok({"waypoints": [{"location": [25.9357, 48.2922]}]}))

    point = await osrm.client().nearest(CENTRE)

    assert point == Point(lat=48.2922, lon=25.9357)
    assert osrm.requests[0].url.path == "/nearest/v1/driving/25.9358,48.2921"


async def test_table_rejects_too_many_points_without_a_request():
    osrm = FakeOsrm()
    sources = [CENTRE] * config.OSRM_TABLE_MAX_POINTS

    with pytest.raises(ValueError):
        await osrm.client().table(sources, [STATION])

    assert osrm.requests == []


# --- errors ---


@pytest.mark.parametrize(
    ("code", "reason"), [("NoSegment", "no_road_nearby"), ("NoRoute", "no_route")]
)
async def test_osrm_error_codes_raise_no_route(code, reason):
    osrm = FakeOsrm(httpx.Response(400, json={"code": code, "message": "..."}))

    with pytest.raises(NoRoute) as exc_info:
        await osrm.client().route(CENTRE, STATION)

    assert exc_info.value.reason == reason


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502, text="Bad Gateway"),
        httpx.Response(200, text="not json"),
        httpx.Response(400, json={"code": "InvalidQuery"}),
        httpx.Response(200, json={"code": "Ok", "routes": []}),
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectError("connection refused"),
    ],
    ids=[
        "5xx",
        "invalid-json",
        "unexpected-code",
        "ok-without-route",
        "httpx-timeout",
        "connect-error",
    ],
)
async def test_failures_raise_osrm_unavailable(response):
    with pytest.raises(OsrmUnavailable):
        await FakeOsrm(response).client().route(CENTRE, STATION)


async def test_whole_request_is_capped_by_timeout(monkeypatch):
    monkeypatch.setattr(config, "OSRM_TIMEOUT_S", 0.01)

    async def slow(request):
        await asyncio.sleep(1)
        return ok(ROUTE_BODY)

    transport = httpx.MockTransport(slow)
    client = OsrmClient(httpx.AsyncClient(transport=transport, base_url="http://osrm"))

    with pytest.raises(OsrmUnavailable):
        await client.route(CENTRE, STATION)


# --- retry ---


class Flaky(FakeOsrm):
    """Fails the first `failures` requests with `error`, then answers normally."""

    def __init__(self, error: Exception, failures: int):
        super().__init__(ok(ROUTE_BODY))
        self.error = error
        self.failures = failures

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.requests) <= self.failures:
            raise self.error
        return self.response


async def test_connect_error_is_retried_once():
    osrm = Flaky(httpx.ConnectError("connection refused"), failures=1)

    route = await osrm.client().route(CENTRE, STATION)

    assert route.distance_m == 3457
    assert len(osrm.requests) == 2


async def test_connect_error_gives_up_after_one_retry():
    osrm = Flaky(httpx.ConnectError("connection refused"), failures=5)

    with pytest.raises(OsrmUnavailable):
        await osrm.client().route(CENTRE, STATION)

    assert len(osrm.requests) == 2


@pytest.mark.parametrize(
    "response",
    [httpx.ReadTimeout("read timed out"), httpx.Response(503, text="busy")],
    ids=["timeout", "5xx"],
)
async def test_timeout_and_server_errors_are_not_retried(response):
    osrm = FakeOsrm(response)

    with pytest.raises(OsrmUnavailable):
        await osrm.client().route(CENTRE, STATION)

    assert len(osrm.requests) == 1


# --- circuit breaker ---


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


def client_with_breaker(osrm: FakeOsrm, clock: FakeClock) -> OsrmClient:
    transport = httpx.MockTransport(osrm.handler)
    http = httpx.AsyncClient(transport=transport, base_url="http://osrm")
    return OsrmClient(http, CircuitBreaker(failures=5, reset_s=30, clock=clock))


async def fail_times(client: OsrmClient, times: int) -> None:
    for _ in range(times):
        with pytest.raises(OsrmUnavailable):
            await client.route(CENTRE, STATION)


async def test_breaker_opens_after_consecutive_failures(clock):
    osrm = FakeOsrm(httpx.Response(502))
    client = client_with_breaker(osrm, clock)

    await fail_times(client, 5)
    await fail_times(client, 3)

    # The 3 calls after opening never reached OSRM.
    assert len(osrm.requests) == 5


async def test_breaker_stays_closed_below_threshold(clock):
    osrm = FakeOsrm(httpx.Response(502))
    client = client_with_breaker(osrm, clock)

    await fail_times(client, 4)
    osrm.response = ok(ROUTE_BODY)
    await client.route(CENTRE, STATION)
    osrm.response = httpx.Response(502)
    await fail_times(client, 4)

    # A success resets the count, so 4 + 4 failures never open it.
    assert len(osrm.requests) == 9


async def test_successful_probe_after_pause_closes_breaker(clock):
    osrm = FakeOsrm(httpx.Response(502))
    client = client_with_breaker(osrm, clock)
    await fail_times(client, 5)

    clock.now += 30
    osrm.response = ok(ROUTE_BODY)
    await client.route(CENTRE, STATION)
    await client.route(CENTRE, STATION)

    assert len(osrm.requests) == 7


async def test_failed_probe_opens_breaker_again(clock):
    osrm = FakeOsrm(httpx.Response(502))
    client = client_with_breaker(osrm, clock)
    await fail_times(client, 5)

    clock.now += 30
    await fail_times(client, 1)  # the probe
    await fail_times(client, 2)  # open again: no requests
    assert len(osrm.requests) == 6

    clock.now += 30
    osrm.response = ok(ROUTE_BODY)
    await client.route(CENTRE, STATION)
    assert len(osrm.requests) == 7


async def test_only_one_probe_goes_through_concurrently(clock):
    release = asyncio.Event()
    requests = []

    async def slow_ok(request):
        requests.append(request)
        await release.wait()
        return ok(ROUTE_BODY)

    breaker = CircuitBreaker(failures=1, reset_s=30, clock=clock)
    breaker.record_failure()
    clock.now += 30
    http = httpx.AsyncClient(transport=httpx.MockTransport(slow_ok), base_url="http://osrm")
    client = OsrmClient(http, breaker)

    probe = asyncio.create_task(client.route(CENTRE, STATION))
    await asyncio.sleep(0)
    with pytest.raises(OsrmUnavailable):
        await client.route(CENTRE, STATION)
    release.set()
    await probe

    assert len(requests) == 1


async def test_no_route_does_not_open_breaker(clock):
    osrm = FakeOsrm(httpx.Response(400, json={"code": "NoSegment"}))
    client = client_with_breaker(osrm, clock)

    for _ in range(10):
        with pytest.raises(NoRoute):
            await client.route(CENTRE, STATION)

    assert len(osrm.requests) == 10


# --- metrics ---


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.mark.parametrize(
    ("response", "error", "result"),
    [
        (ok(ROUTE_BODY), None, "ok"),
        (httpx.Response(400, json={"code": "NoRoute"}), NoRoute, "no_route"),
        (httpx.Response(502), OsrmUnavailable, "unavailable"),
    ],
)
async def test_requests_are_counted_by_result(response, error, result):
    before = sample("osrm_requests_total", endpoint="route", result=result)
    duration_before = sample("osrm_request_duration_seconds_count", endpoint="route")

    if error:
        with pytest.raises(error):
            await FakeOsrm(response).client().route(CENTRE, STATION)
    else:
        await FakeOsrm(response).client().route(CENTRE, STATION)

    assert sample("osrm_requests_total", endpoint="route", result=result) == before + 1
    assert sample("osrm_request_duration_seconds_count", endpoint="route") == duration_before + 1


async def test_refused_calls_are_counted_as_circuit_open(clock):
    client = client_with_breaker(FakeOsrm(httpx.Response(502)), clock)
    await fail_times(client, 5)
    before = sample("osrm_requests_total", endpoint="route", result="circuit_open")
    duration_before = sample("osrm_request_duration_seconds_count", endpoint="route")

    await fail_times(client, 1)

    assert sample("osrm_requests_total", endpoint="route", result="circuit_open") == before + 1
    # No request was sent, so no duration either.
    assert sample("osrm_request_duration_seconds_count", endpoint="route") == duration_before


async def test_metrics_label_the_endpoint():
    osrm = FakeOsrm(ok({"waypoints": [{"location": [25.9357, 48.2922]}]}))
    before = sample("osrm_requests_total", endpoint="nearest", result="ok")

    await osrm.client().nearest(CENTRE)

    assert sample("osrm_requests_total", endpoint="nearest", result="ok") == before + 1
