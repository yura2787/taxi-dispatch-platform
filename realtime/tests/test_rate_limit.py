"""Token bucket of a WebSocket connection: bursts, refill, throttled error replies
and closing after a long run of refused messages."""

import pytest

from app import config
from app.ws.limits import RateVerdict, TokenBucket


def take(bucket: TokenBucket, times: int) -> list[RateVerdict]:
    return [bucket.take() for _ in range(times)]


@pytest.fixture
def bucket(clock) -> TokenBucket:
    return TokenBucket(clock, rate_per_s=2, burst=10, close_after=20)


class TestTokenBucket:
    def test_burst_within_capacity_is_allowed(self, bucket):
        assert take(bucket, 10) == [RateVerdict.ALLOW] * 10

    def test_message_beyond_capacity_is_refused_with_a_warning(self, bucket):
        take(bucket, 10)

        assert bucket.take() is RateVerdict.WARN

    def test_warning_is_sent_at_most_once_per_interval(self, bucket, clock):
        take(bucket, 10)
        assert bucket.take() is RateVerdict.WARN

        # Within the interval: refused silently.
        clock.advance(0.1)
        assert bucket.take() is RateVerdict.DROP
        # A full interval later the token that came back meanwhile is spent first.
        clock.advance(config.WS_RATE_LIMIT_ERROR_INTERVAL_S)
        assert take(bucket, 3) == [RateVerdict.ALLOW, RateVerdict.ALLOW, RateVerdict.WARN]

    def test_tokens_come_back_at_the_rate(self, bucket, clock):
        take(bucket, 10)

        clock.advance(1.5)

        # 1.5 s at 2/s: 3 tokens.
        assert take(bucket, 4) == [RateVerdict.ALLOW] * 3 + [RateVerdict.WARN]

    def test_refill_stops_at_capacity(self, bucket, clock):
        take(bucket, 10)

        clock.advance(60)

        assert take(bucket, 11)[-2:] == [RateVerdict.ALLOW, RateVerdict.WARN]

    def test_steady_phone_traffic_is_never_refused(self, bucket, clock):
        # A point every 2 s and a ping every 20 s, for 10 minutes.
        for second in range(0, 600, 2):
            assert bucket.take() is RateVerdict.ALLOW
            if second % 20 == 0:
                assert bucket.take() is RateVerdict.ALLOW
            clock.advance(2)

    def test_twentieth_refused_message_in_a_row_closes(self, bucket):
        take(bucket, 10)

        verdicts = take(bucket, 20)

        assert RateVerdict.CLOSE not in verdicts[:19]
        assert verdicts[19] is RateVerdict.CLOSE

    def test_allowed_message_restarts_the_run(self, bucket, clock):
        take(bucket, 10)
        take(bucket, 19)
        clock.advance(0.5)  # one token back
        assert bucket.take() is RateVerdict.ALLOW

        verdicts = take(bucket, 19)

        assert RateVerdict.CLOSE not in verdicts

    def test_limits_come_from_config(self, clock, monkeypatch):
        monkeypatch.setattr(config, "WS_MSG_BURST", 1)
        monkeypatch.setattr(config, "WS_RATE_LIMIT_CLOSE_AFTER", 2)
        bucket = TokenBucket(clock)

        assert take(bucket, 3) == [RateVerdict.ALLOW, RateVerdict.WARN, RateVerdict.CLOSE]
