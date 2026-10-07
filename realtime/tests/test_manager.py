"""Registry of WebSocket connections: one connection per user, takeover signalling
and cleanup that never removes a newer connection."""

import asyncio

import pytest

from app.ws.manager import Connection, ConnectionRegistry

USER = 1


@pytest.fixture
def registry() -> ConnectionRegistry:
    return ConnectionRegistry()


class TestRegister:
    def test_first_connection_has_no_predecessor(self, registry):
        connection = Connection("conn-1")

        assert registry.register(USER, connection) is None
        assert registry.get(USER) is connection
        assert not connection.superseded

    def test_new_connection_supersedes_the_old_one(self, registry):
        old, new = Connection("conn-1"), Connection("conn-2")
        registry.register(USER, old)

        previous = registry.register(USER, new)

        assert previous is old
        assert old.superseded
        assert not new.superseded
        assert registry.get(USER) is new

    def test_registering_the_same_connection_again_does_not_supersede_it(self, registry):
        connection = Connection("conn-1")
        registry.register(USER, connection)

        assert registry.register(USER, connection) is None
        assert not connection.superseded

    def test_connections_of_different_users_are_independent(self, registry):
        first, second = Connection("conn-1"), Connection("conn-2")

        registry.register(1, first)
        registry.register(2, second)

        assert not first.superseded
        assert len(registry) == 2

    async def test_waiting_old_handler_wakes_up_on_takeover(self, registry):
        old = Connection("conn-1")
        registry.register(USER, old)
        waiter = asyncio.create_task(old.wait_superseded())
        await asyncio.sleep(0)  # let the waiter start waiting
        assert not waiter.done()

        registry.register(USER, Connection("conn-2"))

        await asyncio.wait_for(waiter, timeout=1)


class TestUnregister:
    def test_current_connection_is_removed(self, registry):
        connection = Connection("conn-1")
        registry.register(USER, connection)

        registry.unregister(USER, connection)

        assert registry.get(USER) is None
        assert len(registry) == 0

    def test_old_connection_after_takeover_does_not_remove_the_new_one(self, registry):
        old, new = Connection("conn-1"), Connection("conn-2")
        registry.register(USER, old)
        registry.register(USER, new)

        registry.unregister(USER, old)

        assert registry.get(USER) is new

    def test_unknown_user_is_ignored(self, registry):
        registry.unregister(USER, Connection("conn-1"))

        assert len(registry) == 0


def test_connection_repr_shows_its_id():
    assert repr(Connection("conn-1")) == "Connection('conn-1')"
