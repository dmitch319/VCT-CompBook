"""HTTP client for the self-hosted vlrggapi.

Every response, successful or not, is written to raw_responses (and committed straight
away) before anything parses it, so a parser bug never costs us a refetch.

vlrggapi rate-limits per tier with a 60 s sliding window, and endpoints in one tier share
its budget: "expensive" (20/min) covers /v2/match/details and /v2/team/...; "moderate"
(60/min) covers the event endpoints. We space requests per tier to stay under those, and
on HTTP 429 wait for Retry-After.

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

# vlrggapi's tier per endpoint (api/utils/rate_limiter.py) and our spacing per tier,
# with a little headroom.
TIER = {
    "/v2/match/details": "expensive",
    "/v2/team/{team_id}/roster": "expensive",
    "/v2/events/matches": "moderate",
    "/v2/events": "moderate",
    "/v2/event/{event_id}": "moderate",
}
MIN_INTERVAL = {"expensive": 60 / 20 + 0.2, "moderate": 60 / 60 + 0.1, "cheap": 0.5}


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
        tier = TIER.get(endpoint, "cheap")
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
                delay = _retry_after(resp, default=60.0)
                log.warning("%s rate-limited; waiting %.0fs", endpoint, delay)
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


def _retry_after(resp: httpx.Response, default: float) -> float:
    try:
        return max(1.0, float(resp.headers.get("Retry-After", default)))
    except ValueError:
        return default
