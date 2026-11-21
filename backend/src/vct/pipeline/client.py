"""HTTP client for the self-hosted vlrggapi.

Every response, successful or not, is written to raw_responses (and committed straight
away) before anything parses it, so a parser bug never costs us a refetch.

Rate limits. vlrggapi limits each client IP per tier with a 60 s sliding window, and
every endpoint in a tier shares one budget (cached responses count too):
  expensive  20/min   /v2/match/..., /v2/stats, /v2/player..., /v2/team...
  moderate   60/min   /v2/events, /v2/events/matches, /v2/event/..., rankings, news, search
  cheap     200/min   /v2/health and anything else
We space requests per tier so we stay under those (one expensive request every 3.2 s is
at most 19 a minute). If vlrggapi still answers 429 (another client sharing the
container, or a restart mid-window) we wait for its Retry-After, which it sends in whole
seconds, plus a second of slack, and retry.

Match details. vlrggapi serves the same handler at /v2/match/details?match_id=N and
/v2/match/N/details: checked against the container built from the pinned commit, both
return identical bodies and draw on the same expensive budget. We use the query form,
which is what raw_responses rows are stored under.

Endpoints with path parameters are written as templates, e.g.
client.get("/v2/event/{event_id}", event_id=2001). raw_responses stores the template as
`endpoint` and every parameter in `params`, so reparse can find all responses of a kind.
"""

import json
import logging
import string
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from psycopg.types.json import Jsonb

from vct.db import Connection

log = logging.getLogger(__name__)

# vlrggapi's tier table (api/utils/rate_limiter.py at the pinned commit), most specific
# prefix first. "/v2/match" alone (the ?q= listings) is treated as expensive to be safe.
TIER_PREFIXES: list[tuple[str, str]] = [
    ("/v2/match", "expensive"),  # /v2/match/details, /v2/match/{id}/details, /logs, ...
    ("/v2/stats", "expensive"),
    ("/v2/player", "expensive"),
    ("/v2/team", "expensive"),
    ("/v2/events", "moderate"),  # also /v2/events/matches
    ("/v2/event/", "moderate"),
    ("/v2/rankings", "moderate"),
    ("/v2/news", "moderate"),
    ("/v2/search", "moderate"),
]
TIER_LIMIT_PER_MINUTE = {"expensive": 20, "moderate": 60, "cheap": 200}
SPACING_SLACK = 0.2  # seconds added to each interval so we never sit exactly at the limit
RETRY_AFTER_SLACK = 1.0
MIN_INTERVAL = {tier: 60 / limit + SPACING_SLACK for tier, limit in TIER_LIMIT_PER_MINUTE.items()}


def tier_for(endpoint: str) -> str:
    """vlrggapi's rate-limit tier for an endpoint (template or concrete path)."""
    for prefix, tier in TIER_PREFIXES:
        if endpoint == prefix or endpoint.startswith(prefix.rstrip("/") + "/"):
            return tier
    return "cheap"


def path_fields(endpoint: str) -> set[str]:
    """Names of the {placeholders} in an endpoint template."""
    return {name for _, name, _, _ in string.Formatter().parse(endpoint) if name}


class VlrApiError(RuntimeError):
    def __init__(self, endpoint: str, status_code: int, raw_id: int, message: str) -> None:
        super().__init__(f"{endpoint} -> HTTP {status_code}: {message} (raw_responses.id={raw_id})")
        self.endpoint = endpoint
        self.status_code = status_code
        self.raw_id = raw_id


@dataclass
class ApiResponse:
    raw_id: int
    status_code: int
    body: Any  # decoded JSON, or None if the body wasn't JSON


def store_raw(
    conn: Connection,
    endpoint: str,
    params: dict[str, Any],
    status_code: int,
    content: bytes,
) -> tuple[int, Any]:
    """Insert one response into raw_responses and commit. Returns (id, decoded body)."""
    body: Any = None
    body_raw: bytes | None = None
    try:
        body = json.loads(content) if content else None
    except ValueError:
        body_raw = content
    row = conn.execute(
        """
        INSERT INTO raw_responses (endpoint, params, status_code, body, body_raw)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (endpoint, Jsonb(params), status_code, None if body is None else Jsonb(body), body_raw),
    ).fetchone()
    conn.commit()
    assert row is not None
    return int(row["id"]), body


@dataclass
class VlrClient:
    conn: Connection
    base_url: str
    timeout: float = 90.0  # match details fans out to several vlr.gg pages
    max_retries: int = 4
    throttle: bool = True
    transport: httpx.BaseTransport | None = None  # tests pass an httpx.MockTransport
    http: httpx.Client = field(init=False)
    _last_call: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.http = httpx.Client(
            base_url=self.base_url, timeout=self.timeout, transport=self.transport
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "VlrClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self, endpoint: str) -> None:
        if not self.throttle:
            return
        tier = tier_for(endpoint)
        wait = self._last_call.get(tier, 0.0) + MIN_INTERVAL[tier] - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call[tier] = time.monotonic()

    def get(self, endpoint: str, **params: str | int) -> ApiResponse:
        """GET an endpoint, store the response, and return it. Raises VlrApiError on a
        non-2xx response (after retrying 429s and 5xx) or a body that isn't vlrggapi's
        {"status": "success", "data": ...} envelope."""
        in_path = path_fields(endpoint)
        url = endpoint.format(**{k: params[k] for k in in_path})
        query = {k: v for k, v in params.items() if k not in in_path}
        stored = {k: str(v) for k, v in params.items()}  # stored form: all strings
        attempt = 0
        while True:
            attempt += 1
            self._throttle(endpoint)
            try:
                resp = self.http.get(url, params=query)
            except httpx.TransportError as exc:
                if attempt > self.max_retries:
                    raise
                log.warning("%s %s failed (%s); retrying", endpoint, stored, exc)
                time.sleep(min(2**attempt, 30))
                continue

            raw_id, body = store_raw(self.conn, endpoint, stored, resp.status_code, resp.content)

            if resp.status_code == 429 and attempt <= self.max_retries:
                delay = retry_after_seconds(resp, body)
                log.warning(
                    "%s rate-limited (%s tier); waiting %.0fs", endpoint, tier_for(endpoint), delay
                )
                time.sleep(delay)
                continue
            if resp.status_code >= 500 and attempt <= self.max_retries:
                log.warning("%s -> HTTP %d; retrying", endpoint, resp.status_code)
                time.sleep(min(2**attempt, 30))
                continue
            if not resp.is_success:
                raise VlrApiError(endpoint, resp.status_code, raw_id, resp.text[:200])
            if not isinstance(body, dict) or body.get("status") != "success":
                raise VlrApiError(endpoint, resp.status_code, raw_id, "unexpected body")
            return ApiResponse(raw_id=raw_id, status_code=resp.status_code, body=body)

    def ping(self) -> bool:
        try:
            resp = self.http.get("/v2/health", timeout=5)
        except httpx.HTTPError:
            return False
        return resp.is_success


def retry_after_seconds(resp: httpx.Response, body: Any, default: float = 60.0) -> float:
    """How long to wait after a 429: the Retry-After header (seconds), else the
    `detail.retry_after` vlrggapi puts in the body, else `default`; plus slack."""
    candidates: list[Any] = [resp.headers.get("Retry-After")]
    if isinstance(body, dict) and isinstance(body.get("detail"), dict):
        candidates.append(body["detail"].get("retry_after"))
    for value in candidates:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue  # missing, or an HTTP-date, which vlrggapi doesn't send
        if seconds >= 0:
            return seconds + RETRY_AFTER_SLACK
    return default
