"""The public /rt/ws/driver protocol, as the client sees it (realtime ТЗ 07, section 5).

Builds the client's messages, reads the server's ones and sorts close codes and error
codes into what a bot must do about them. Every message is {"type": ..., "data": ...}.
"""

import enum
import json
import logging
from dataclasses import dataclass

from sim.geo import Point


def token_for(driver_id: int) -> str:
    """The token a bot logs in with. Stage 2 replaces dev tokens with a login."""
    return f"dev:driver:{driver_id}"


# ---------------------------------------------------------------------------
# Client -> server
# ---------------------------------------------------------------------------


def _message(message_type: str, data: dict | None = None) -> str:
    return json.dumps({"type": message_type, "data": data or {}}, separators=(",", ":"))


def auth(token: str) -> str:
    return _message("auth", {"token": token})


def ping() -> str:
    return _message("ping")


def go_online() -> str:
    return _message("go_online")


def go_offline() -> str:
    return _message("go_offline")


def location(point: Point, *, heading: float | None, speed: float | None, ts: int) -> str:
    """6 decimals of a degree are ~10 cm: more is noise in every message."""
    return _message(
        "location",
        {
            "lat": round(point.lat, 6),
            "lon": round(point.lon, 6),
            "heading": heading,
            "speed": None if speed is None else round(speed, 2),
            "ts": ts,
        },
    )


# ---------------------------------------------------------------------------
# Server -> client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StateSnapshot:
    status: str
    position: Point | None


@dataclass(frozen=True)
class Pong:
    pass


@dataclass(frozen=True)
class ServerError:
    code: str


@dataclass(frozen=True)
class UnknownMessage:
    """A type this client does not know (a newer server) or a broken message."""

    type: str | None


ServerMessage = StateSnapshot | Pong | ServerError | UnknownMessage


def parse_server_message(raw: str) -> ServerMessage:
    message_type = None
    try:
        message = json.loads(raw)
        message_type = message["type"]
        data = message["data"]
        if message_type == "state_snapshot":
            position = data["position"]
            point = None if position is None else Point(position["lat"], position["lon"])
            return StateSnapshot(status=data["status"], position=point)
        if message_type == "pong":
            return Pong()
        if message_type == "error":
            return ServerError(code=data["code"])
    except (ValueError, KeyError, TypeError):
        # A known type with broken data keeps its type: the log says what drifted.
        return UnknownMessage(type=message_type if isinstance(message_type, str) else None)
    return UnknownMessage(type=str(message_type))


# ---------------------------------------------------------------------------
# What a bot does about a close or an error (ТЗ 08, section 5.4)
# ---------------------------------------------------------------------------

# A close without a close frame (a dropped TCP connection) is reported as this code.
ABNORMAL_CLOSURE = 1006


class CloseVerdict(enum.Enum):
    # The server will refuse again: no point reconnecting.
    FATAL = "fatal"
    # The bot broke the protocol: a bug of the simulator, stop and show it.
    BUG = "bug"
    # Transient (idle, restart, network): reconnect with backoff.
    RECONNECT = "reconnect"


@dataclass(frozen=True)
class CloseRule:
    """What a bot does about a close code and how loudly it says so."""

    verdict: CloseVerdict
    log_level: int
    # Appended to the log line: how to fix it, or whose fault it is.
    hint: str = ""


_CLOSE_RULES = {
    4401: CloseRule(CloseVerdict.FATAL, logging.ERROR),  # invalid token
    # No driver profile, or not eligible.
    4403: CloseRule(CloseVerdict.FATAL, logging.ERROR, "run make seed-drivers"),
    # Another connection of this driver took over; reconnecting would push it out in
    # turn, and two runs (or a bot and a browser tab) would fight forever.
    4409: CloseRule(CloseVerdict.FATAL, logging.WARNING),
    # Rate limit broken systematically, message too big.
    1008: CloseRule(CloseVerdict.BUG, logging.ERROR, "simulator bug"),
    1009: CloseRule(CloseVerdict.BUG, logging.ERROR, "simulator bug"),
}

_RECONNECT = CloseRule(CloseVerdict.RECONNECT, logging.WARNING)


def close_rule(code: int | None) -> CloseRule:
    """4000, 4408, 1011, 1001, 1006 and anything unknown mean: reconnect."""
    return _CLOSE_RULES.get(code, _RECONNECT)


class ErrorVerdict(enum.Enum):
    # Expected after the reaper took the bot offline: the snapshot that follows makes
    # the bot go online again.
    NOT_ONLINE = "not_online"
    # The server refused a point: the bot's motion is wrong somewhere.
    REJECTED_LOCATION = "rejected_location"
    # The bot sent something the server did not accept: a bug of the simulator.
    BUG = "bug"
    # The bot may not (or no longer) be online: stop it.
    FATAL = "fatal"


_ERROR_VERDICTS = {
    "not_online": ErrorVerdict.NOT_ONLINE,
    "location_jump": ErrorVerdict.REJECTED_LOCATION,
    "location_stale": ErrorVerdict.REJECTED_LOCATION,
    "location_out_of_order": ErrorVerdict.REJECTED_LOCATION,
    "not_eligible": ErrorVerdict.FATAL,
    "busy": ErrorVerdict.FATAL,
}


def classify_error(code: str) -> ErrorVerdict:
    """rate_limited, invalid_*, unknown_type, already_authenticated and codes this
    client does not know are bugs of the simulator."""
    return _ERROR_VERDICTS.get(code, ErrorVerdict.BUG)
