"""OsrmClient against httpx.MockTransport: the lon,lat order, parsing and how every
kind of failure is reported."""

import asyncio
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from sim.geo import Point
from sim.osrm import NoRoute, OsrmClient, OsrmUnavailable

ORIGIN = Point(lat=48.2921, lon=25.9358)
DESTINATION = Point(lat=48.3036, lon=25.9325)

Handler = Callable[[httpx.Request], httpx.Response]

ROUTE_BODY = {
    "code": "Ok",
    "routes": [
        {
            "distance": 1300.0,
            "duration": 200.0,
            "geometry": {
                "type": "LineString",
                "coordinates": [[25.9358, 48.2921], [25.934, 48.298], [25.9325, 48.3036]],
            },
        }
    ],
}
NEAREST_BODY = {"code": "Ok", "waypoints": [{"location": [25.9361, 48.2925]}]}


class Server:
    """A fake OSRM: answers every request with the same response and records it."""

    def __init__(self, body: object = None, *, status: int = 200, text: str | None = None):
        self.requests: list[httpx.Request] = []
        self._body, self._status, self._text = body, status, text

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._text is not None:
            return httpx.Response(self._status, text=self._text)
        return httpx.Response(self._status, json=self._body)


def failing_with(error: Exception) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return handler


@pytest.fixture
async def make_client() -> AsyncIterator[Callable[[Handler], OsrmClient]]:
    """OsrmClient over the given handler; the HTTP clients are closed after the test."""
    clients: list[httpx.AsyncClient] = []

    def make(handler: Handler) -> OsrmClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://osrm")
        clients.append(http)
        return OsrmClient(http)

    yield make
    for http in clients:
        await http.aclose()


class TestNearest:
    async def test_returns_the_snapped_point(self, make_client):
        client = make_client(Server(NEAREST_BODY))

        snapped = await client.nearest(ORIGIN)

        assert snapped == Point(lat=48.2925, lon=25.9361)

    async def test_sends_lon_lat_and_no_snap_radius(self, make_client):
        server = Server(NEAREST_BODY)
        client = make_client(server)

        await client.nearest(ORIGIN)

        request = server.requests[0]
        assert request.url.path == "/nearest/v1/driving/25.9358,48.2921"
        # No radius: a destination in a field must still snap to some road.
        assert dict(request.url.params) == {"number": "1"}

    async def test_no_road_at_all_is_no_route(self, make_client):
        client = make_client(Server({"code": "NoSegment"}, status=400))

        with pytest.raises(NoRoute):
            await client.nearest(ORIGIN)


class TestRoute:
    async def test_geometry_is_converted_to_lat_lon_points(self, make_client):
        client = make_client(Server(ROUTE_BODY))

        route = await client.route(ORIGIN, DESTINATION)

        assert route.geometry == (
            Point(48.2921, 25.9358),
            Point(48.298, 25.934),
            Point(48.3036, 25.9325),
        )

    async def test_asks_for_the_full_geometry_in_lon_lat(self, make_client):
        server = Server(ROUTE_BODY)
        client = make_client(server)

        await client.route(ORIGIN, DESTINATION)

        request = server.requests[0]
        assert request.url.path == "/route/v1/driving/25.9358,48.2921;25.9325,48.3036"
        assert dict(request.url.params) == {"overview": "full", "geometries": "geojson"}

    @pytest.mark.parametrize("code", ["NoSegment", "NoRoute"])
    async def test_no_road_or_no_route_is_no_route(self, make_client, code):
        client = make_client(Server({"code": code, "message": "..."}, status=400))

        with pytest.raises(NoRoute):
            await client.route(ORIGIN, DESTINATION)


async def call_route(client: OsrmClient) -> object:
    return await client.route(ORIGIN, DESTINATION)


async def call_nearest(client: OsrmClient) -> object:
    return await client.nearest(ORIGIN)


class TestUnavailable:
    @pytest.mark.parametrize("call", [call_route, call_nearest], ids=["route", "nearest"])
    @pytest.mark.parametrize(
        "handler",
        [
            failing_with(httpx.ReadTimeout("slow")),
            failing_with(httpx.ConnectError("refused")),
            Server({"code": "Ok"}, status=500),
            Server(text="<html>bad gateway</html>", status=502),
            Server(text="not json"),
            Server({"code": "TooBig"}, status=400),
            Server(["not", "an", "object"]),
        ],
        ids=[
            "timeout",
            "connection_error",
            "http_500",
            "html_502",
            "broken_json",
            "unexpected_code",
            "not_an_object",
        ],
    )
    async def test_failure_is_unavailable(self, make_client, handler, call):
        client = make_client(handler)

        with pytest.raises(OsrmUnavailable):
            await call(client)

    @pytest.mark.parametrize(
        "body",
        [
            {"code": "Ok", "routes": []},
            {"code": "Ok", "routes": [{"geometry": {"coordinates": []}}]},
            {"code": "Ok", "routes": [{"geometry": {"coordinates": [[25.9]]}}]},
        ],
        ids=["no_routes", "empty_geometry", "point_without_lat"],
    )
    async def test_malformed_route_is_unavailable(self, make_client, body):
        client = make_client(Server(body))

        with pytest.raises(OsrmUnavailable):
            await client.route(ORIGIN, DESTINATION)

    async def test_malformed_nearest_is_unavailable(self, make_client):
        client = make_client(Server({"code": "Ok", "waypoints": [{"location": "here"}]}))

        with pytest.raises(OsrmUnavailable):
            await client.nearest(ORIGIN)

    async def test_whole_request_is_capped_by_the_timeout(self, make_client, monkeypatch):
        monkeypatch.setattr("sim.config.OSRM_TIMEOUT_S", 0.01)

        async def slow(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(1)
            return httpx.Response(200, json=ROUTE_BODY)

        client = make_client(slow)

        with pytest.raises(OsrmUnavailable, match="timeout"):
            await client.route(ORIGIN, DESTINATION)
