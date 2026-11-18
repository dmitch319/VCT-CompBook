"""HTTP client for the self-hosted vlrggapi.

Every response, successful or not, is written to raw_responses (and committed straight
away) before anything parses it, so a parser bug never costs us a refetch.

vlrggapi rate-limits per endpoint tier with a 60 s sliding window: /v2/match/details is
"expensive" (20/min), /v2/events/matches "moderate" (60/min), the rest "cheap" (200/min).
We space our own requests to stay under those, and on HTTP 429 wait for Retry-After.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from psycopg.types.json import Jsonb

from vct.db import Connection

log = logging.getLogger(__name__)

# Minimum seconds between requests to one endpoint, with a little headroom.
MIN_INTERVAL = {
    "/v2/match/details": 60 / 20 + 0.2,
    "/v2/events/matches": 60 / 60 + 0.1,
}
DEFAULT_MIN_INTERVAL = 0.5


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
        interval = MIN_INTERVAL.get(endpoint, DEFAULT_MIN_INTERVAL)
        wait = self._last_call.get(endpoint, 0.0) + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call[endpoint] = time.monotonic()

    def get(self, endpoint: str, **params: str | int) -> ApiResponse:
        """GET an endpoint, store the response, and return it. Raises VlrApiError on a
        non-2xx response (after retrying 429s and 5xx) or a body that isn't vlrggapi's
        {"status": "success", "data": ...} envelope."""
        attempt = 0
        while True:
            attempt += 1
            self._throttle(endpoint)
            try:
                resp = self.http.get(endpoint, params=params)
            except httpx.TransportError as exc:
                if attempt > self.max_retries:
                    raise
                log.warning("%s %s failed (%s); retrying", endpoint, params, exc)
                time.sleep(min(2**attempt, 30))
                continue

            raw_id, body = store_raw(self.conn, endpoint, params, resp.status_code, resp.content)

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
