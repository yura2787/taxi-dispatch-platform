"""Rate limit of one WebSocket connection: a token bucket over all its messages."""

from enum import StrEnum

from app import config
from app.clock import Clock


class RateVerdict(StrEnum):
    ALLOW = "allow"
    # Refused; the client already got a rate_limited error within the last interval.
    DROP = "drop"
    # Refused; tell the client with a rate_limited error.
    WARN = "warn"
    # Refused too many times in a row: close the connection with 1008.
    CLOSE = "close"


class TokenBucket:
    """Each message takes a token; tokens come back at a steady rate up to a cap.

    The cap lets a client send a short burst (e.g. right after connecting); the rate
    is what it can sustain. Time is the monotonic clock: a wall clock moved back by
    NTP would otherwise freeze the refill.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        rate_per_s: float | None = None,
        burst: int | None = None,
        close_after: int | None = None,
    ):
        self._clock = clock
        self._rate = config.WS_MSG_RATE_PER_S if rate_per_s is None else rate_per_s
        self._burst = config.WS_MSG_BURST if burst is None else burst
        self._close_after = config.WS_RATE_LIMIT_CLOSE_AFTER if close_after is None else close_after
        self._tokens = float(self._burst)
        self._refilled_at = clock.monotonic()
        self._refused_in_row = 0
        self._warned_at: float | None = None

    def take(self) -> RateVerdict:
        now = self._clock.monotonic()
        self._tokens = min(self._burst, self._tokens + (now - self._refilled_at) * self._rate)
        self._refilled_at = now

        if self._tokens >= 1:
            self._tokens -= 1
            self._refused_in_row = 0
            return RateVerdict.ALLOW

        self._refused_in_row += 1
        if self._refused_in_row >= self._close_after:
            return RateVerdict.CLOSE
        if (
            self._warned_at is None
            or now - self._warned_at >= config.WS_RATE_LIMIT_ERROR_INTERVAL_S
        ):
            self._warned_at = now
            return RateVerdict.WARN
        return RateVerdict.DROP
