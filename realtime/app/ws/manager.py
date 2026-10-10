"""Registry of open WebSocket connections in this process: one per user.

A new connection of the same user supersedes the old one, which then closes itself
with 4409. The registry never closes a socket from the outside: two tasks sending
on one WebSocket at once is unsafe, so it only signals the old connection's own
handler. Stage 5 extends this to several gateway instances via pub/sub; until then
the conn_id check in the Lua scripts already stops a stale connection on another
instance from changing the driver's state.
"""

import asyncio


class Connection:
    """A connection's handle in the registry."""

    def __init__(self, conn_id: str):
        self.conn_id = conn_id
        self._superseded = asyncio.Event()

    @property
    def superseded(self) -> bool:
        return self._superseded.is_set()

    async def wait_superseded(self) -> None:
        """Returns once a newer connection of the same user has been registered."""
        await self._superseded.wait()

    def _supersede(self) -> None:
        self._superseded.set()

    def __repr__(self) -> str:
        return f"Connection({self.conn_id!r})"


class ConnectionRegistry:
    def __init__(self):
        self._connections: dict[int, Connection] = {}

    def register(self, user_id: int, connection: Connection) -> Connection | None:
        """Make `connection` the user's current one; the previous one, if any, is
        told to close and returned.

        For drivers, call this only after the new conn_id is written to Redis: the old
        handler disconnects with its own conn_id on the way out, and that must already
        be a foreign one, or it would take the driver out of the GEO sets.
        """
        previous = self._connections.get(user_id)
        self._connections[user_id] = connection
        if previous is None or previous is connection:
            return None
        previous._supersede()
        return previous

    def unregister(self, user_id: int, connection: Connection) -> None:
        """Forget `connection` if it is still the user's current one.

        An old handler finishing after a takeover must not remove the new connection.
        """
        if self._connections.get(user_id) is connection:
            del self._connections[user_id]

    def get(self, user_id: int) -> Connection | None:
        return self._connections.get(user_id)

    def __len__(self) -> int:
        return len(self._connections)
