"""
Project-wide test fixtures.

Only universal fixtures live here: API clients, users and Redis.
Domain fixtures (orders, drivers, tariffs, ...) belong in each app's
``tests/conftest.py``.

An app-local fixture with the same name shadows the one defined here, so a test
that needs a specific user or a richer object just overrides it locally.

Objects a test builds several of, or at a specific moment, come from
``make_<model>(...)`` functions in ``factories.py`` rather than from fixtures.
"""
