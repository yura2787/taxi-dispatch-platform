"""Health check of the core service and the GeoDjango system libraries it relies on."""

import pytest
from django.contrib.gis.geos import Point
from django.test import Client


@pytest.mark.django_db
def test_health_reports_db_up():
    response = Client().get("/api/health/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "up"}


def test_geodjango_builds_a_point_with_installed_gdal_and_geos():
    point = Point(25.94, 48.29, srid=4326)

    assert point.x == 25.94
    assert point.y == 48.29
