"""VlrClient throttling and 429 handling, with a fake clock and a stub connection (no
database or network needed)."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from vct.pipeline import client as client_mod
from vct.pipeline.client import VlrApiError, VlrClient, retry_after_seconds, tier_for


@pytest.mark.parametrize(
    ("endpoint", "tier"),
    [
        ("/v2/match/details", "expensive"),
        ("/v2/match/{match_id}/details", "expensive"),
        ("/v2/match/123/logs", "expensive"),
        ("/v2/match", "expensive"),
        ("/v2/stats", "expensive"),
        ("/v2/player/{player_id}", "expensive"),
        ("/v2/player", "expensive"),
        ("/v2/team/{team_id}/roster", "expensive"),
        ("/v2/events", "moderate"),
        ("/v2/events/matches", "moderate"),
        ("/v2/event/{event_id}", "moderate"),
        ("/v2/rankings", "moderate"),
        ("/v2/health", "cheap"),
        ("/v2/matchup", "cheap"),  # prefix match is per path segment
    ],
)
def test_tier_for(endpoint: str, tier: str) -> None:
    assert tier_for(endpoint) == tier


def test_expensive_tier_spacing_stays_under_20_per_minute() -> None:
    assert 60 / client_mod.MIN_INTERVAL["expensive"] < 20


def _response(headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(429, headers=headers or {})


def test_retry_after_seconds() -> None:
    slack = client_mod.RETRY_AFTER_SLACK
    # Header, as vlrggapi sends it
    assert retry_after_seconds(_response({"Retry-After": "43"}), None) == 43 + slack
    # Body fallback: {"detail": {"retry_after": N, ...}}
    body = {"detail": {"error": "Rate limit exceeded", "tier": "expensive", "retry_after": 12}}
    assert retry_after_seconds(_response(), body) == 12 + slack
    # Header wins over body
    assert retry_after_seconds(_response({"Retry-After": "5"}), body) == 5 + slack
    # Unparseable header (HTTP-date) and no body: default
    date = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
    assert retry_after_seconds(_response(date), None, default=60) == 60


class StubConn:
    """Just enough of a psycopg connection for store_raw."""

    def __init__(self) -> None:
        self.stored: list[tuple[Any, ...]] = []

    def execute(self, _sql: str, params: tuple[Any, ...]) -> "StubConn":
        self.stored.append(params)
        return self

    def fetchone(self) -> dict[str, int]:
        return {"id": len(self.stored)}

    def commit(self) -> None:
        pass


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Replace the client module's `time` (only there, not the global module)."""
    fake = FakeClock()
    monkeypatch.setattr("vct.pipeline.client.time", fake)
    return fake


OK = {"status": "success", "data": {"status": 200, "segments": []}}


def make_client(
    handler: Callable[[httpx.Request], httpx.Response], clock: FakeClock, throttle: bool = True
) -> tuple[VlrClient, list[float]]:
    """A client whose transport records the fake time of every request."""
    sent: list[float] = []

    def transport(request: httpx.Request) -> httpx.Response:
        sent.append(clock.now)
        return handler(request)

    c = VlrClient(
        StubConn(),  # type: ignore[arg-type]
        "http://vlrggapi.test",
        throttle=throttle,
        transport=httpx.MockTransport(transport),
    )
    return c, sent


def test_throttle_spaces_requests_per_tier(clock: FakeClock) -> None:
    c, sent = make_client(lambda _r: httpx.Response(200, json=OK), clock)
    # Match details and team rosters share the expensive budget
    c.get("/v2/match/details", match_id=1)
    c.get("/v2/team/{team_id}/roster", team_id=7)
    c.get("/v2/match/details", match_id=2)
    # A moderate request doesn't wait behind the expensive ones
    c.get("/v2/event/{event_id}", event_id=3)

    gap = client_mod.MIN_INTERVAL["expensive"]
    assert sent[1] - sent[0] == pytest.approx(gap)
    assert sent[2] - sent[1] == pytest.approx(gap)
    assert sent[3] == sent[2]

    # Any 60 s window holds at most 20 expensive requests
    for _ in range(40):
        c.get("/v2/match/details", match_id=9)
    expensive = sent[:3] + sent[4:]
    assert max(sum(1 for t in expensive if s <= t < s + 60) for s in expensive) <= 20


def test_429_waits_for_retry_after_then_succeeds(clock: FakeClock) -> None:
    responses = iter(
        [
            httpx.Response(
                429,
                headers={"Retry-After": "43"},
                json={"detail": {"error": "Rate limit exceeded", "retry_after": 43}},
            ),
            httpx.Response(200, json=OK),
        ]
    )
    c, sent = make_client(lambda _r: next(responses), clock, throttle=False)

    resp = c.get("/v2/match/details", match_id=790757)

    assert resp.status_code == 200
    assert clock.sleeps == [43 + client_mod.RETRY_AFTER_SLACK]
    assert sent[1] - sent[0] == pytest.approx(44)
    assert [row[2] for row in c.conn.stored] == [429, 200]  # type: ignore[attr-defined]


def test_429_gives_up_after_max_retries(clock: FakeClock) -> None:
    c, sent = make_client(
        lambda _r: httpx.Response(429, headers={"Retry-After": "1"}, json={}), clock, throttle=False
    )
    with pytest.raises(VlrApiError) as err:
        c.get("/v2/match/details", match_id=1)
    assert err.value.status_code == 429
    assert len(sent) == c.max_retries + 1
