import pytest
from django.contrib.gis.geos import Point
from django.test import Client


@pytest.mark.django_db
def test_health_returns_ok():
    response = Client().get("/api/health/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "up"}


def test_geodjango_is_available():
    point = Point(25.94, 48.29, srid=4326)

    assert point.x == 25.94
    assert point.y == 48.29
