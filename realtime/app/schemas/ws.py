"""WebSocket messages of the driver endpoint, in both directions.

Every message is an envelope {"type": "...", "data": {...}}. Incoming messages are a
discriminated union on "type", parsed straight from the raw text in one step, so a
broken JSON, an unknown type and invalid data each get their own error code.

Incoming models are strict: no type coercion (ts 1.0 or "123" is not an int), no
NaN or Infinity, no unknown fields. A phone sends these several times a second;
anything that is not exactly right is a bug worth seeing, not something to guess at.
"""

from enum import IntEnum, StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.services.drivers import DriverState, DriverStatus

# Lua numbers are doubles: integers stay exact only below 2**53 (~9e15). 1e14 ms is
# the year 5138, far beyond any real phone clock.
MAX_TS_MS = 10**14


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False, frozen=True)


class EmptyData(_Strict):
    pass


# ---------------------------------------------------------------------------
# Driver -> server
# ---------------------------------------------------------------------------


class AuthData(_Strict):
    token: str = Field(min_length=1)


class AuthMessage(_Strict):
    type: Literal["auth"]
    data: AuthData


class PingMessage(_Strict):
    type: Literal["ping"]
    data: EmptyData


class GoOnlineMessage(_Strict):
    type: Literal["go_online"]
    data: EmptyData


class GoOfflineMessage(_Strict):
    type: Literal["go_offline"]
    data: EmptyData


class LocationData(_Strict):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    # Degrees clockwise from north. Browsers report null while the car stands still.
    heading: float | None = Field(default=None, ge=0, lt=360)
    # m/s; null when the phone does not know it.
    speed: float | None = Field(default=None, ge=0)
    # Phone's wall clock, unix ms.
    ts: int = Field(gt=0, lt=MAX_TS_MS)


class LocationMessage(_Strict):
    type: Literal["location"]
    data: LocationData


IncomingMessage = Annotated[
    AuthMessage | PingMessage | GoOnlineMessage | GoOfflineMessage | LocationMessage,
    Field(discriminator="type"),
]
_INCOMING = TypeAdapter(IncomingMessage)


class ErrorCode(StrEnum):
    INVALID_JSON = "invalid_json"
    INVALID_MESSAGE = "invalid_message"
    UNKNOWN_TYPE = "unknown_type"
    ALREADY_AUTHENTICATED = "already_authenticated"
    NOT_ONLINE = "not_online"
    NOT_ELIGIBLE = "not_eligible"
    BUSY = "busy"
    LOCATION_STALE = "location_stale"
    LOCATION_OUT_OF_ORDER = "location_out_of_order"
    LOCATION_JUMP = "location_jump"
    RATE_LIMITED = "rate_limited"


class CloseCode(IntEnum):
    # The server closes after 45 s without a message. Not 1000: the client must tell
    # it from a deliberate close and reconnect.
    IDLE = 4000
    INVALID_TOKEN = 4401
    FORBIDDEN = 4403
    AUTH_TIMEOUT = 4408
    # A newer connection of the same user took over.
    SUPERSEDED = 4409
    POLICY_VIOLATION = 1008
    MESSAGE_TOO_BIG = 1009
    INTERNAL_ERROR = 1011


class InvalidMessage(Exception):
    def __init__(self, code: ErrorCode, message: str, message_type: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        # The type, if the envelope was readable (e.g. to count invalid locations).
        self.message_type = message_type


def parse_message(raw: str | bytes) -> IncomingMessage:
    """Parse one incoming message or raise InvalidMessage with the error code."""
    try:
        return _INCOMING.validate_json(raw)
    except ValidationError as exc:
        raise _invalid(exc) from None


def _invalid(exc: ValidationError) -> InvalidMessage:
    error = exc.errors(include_input=False, include_url=False)[0]
    kind, loc = error["type"], error["loc"]
    if kind == "json_invalid":
        return InvalidMessage(ErrorCode.INVALID_JSON, "message is not valid JSON")
    if kind == "union_tag_invalid":
        return InvalidMessage(ErrorCode.UNKNOWN_TYPE, "unknown message type")
    if kind == "union_tag_not_found":
        return InvalidMessage(ErrorCode.INVALID_MESSAGE, 'message has no "type"')
    if not loc:
        return InvalidMessage(ErrorCode.INVALID_MESSAGE, "message must be a JSON object")
    # loc starts with the union tag: ("location", "data", "lat").
    message_type, *path = loc
    where = ".".join(str(part) for part in path)
    return InvalidMessage(
        ErrorCode.INVALID_MESSAGE, f"{where}: {error['msg']}", message_type=str(message_type)
    )


# ---------------------------------------------------------------------------
# Server -> driver
# ---------------------------------------------------------------------------


class PositionOut(BaseModel):
    lat: float
    lon: float
    heading: float | None
    speed: float | None


class StateSnapshotData(BaseModel):
    status: DriverStatus
    tariff: str | None
    in_zone: bool
    position: PositionOut | None


class StateSnapshotMessage(BaseModel):
    type: Literal["state_snapshot"] = "state_snapshot"
    data: StateSnapshotData

    @classmethod
    def build(cls, state: DriverState, *, profile_tariff: str | None) -> "StateSnapshotMessage":
        """The full driver state; the tariff comes from the profile until the driver
        has gone online once and the state has its own."""
        position = state.position
        return cls(
            data=StateSnapshotData(
                status=state.status,
                tariff=state.tariff or profile_tariff,
                in_zone=state.in_zone,
                position=None
                if position is None
                else PositionOut(
                    lat=position.lat,
                    lon=position.lon,
                    heading=position.heading,
                    speed=position.speed,
                ),
            )
        )


class PongMessage(BaseModel):
    type: Literal["pong"] = "pong"
    data: EmptyData = EmptyData()


class ErrorData(BaseModel):
    code: ErrorCode
    message: str


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    data: ErrorData

    @classmethod
    def build(cls, code: ErrorCode, message: str) -> "ErrorMessage":
        return cls(data=ErrorData(code=code, message=message))
