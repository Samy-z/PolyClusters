"""Rate-limited, retrying HTTP client for the public Polymarket APIs.

All three hosts (Gamma / Data / CLOB) are unauthenticated but will throttle a
naive crawler, so every request goes through a token-bucket limiter and an
exponential backoff on 429/5xx.
"""

from __future__ import annotations

import asyncio
import random
import ssl
import time
from typing import Any

import httpx

from ..config import CLOB_API, DATA_API, GAMMA_API


def _ssl_context() -> Any:
    """Trust the OS certificate store, falling back to certifi's bundle.

    Machines behind a TLS-inspecting proxy present a corporate root CA that is
    in the system store but not in certifi, which makes every request fail with
    CERTIFICATE_VERIFY_FAILED. truststore reads the platform store instead.
    """
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001 - fall back to httpx's default behaviour
        return True


class RateLimiter:
    """Simple async token bucket."""

    def __init__(self, rate_per_sec: float, burst: float | None = None):
        self.rate = max(rate_per_sec, 0.1)
        self.capacity = burst if burst is not None else max(self.rate, 1.0)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._last) * self.rate
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self.rate)


class PolymarketClient:
    """Async client over the three public hosts."""

    def __init__(
        self,
        concurrency: int = 8,
        rate_per_sec: float = 12.0,
        timeout: float = 45.0,
        max_retries: int = 4,
    ):
        self.max_retries = max_retries
        self._limiter = RateLimiter(rate_per_sec)
        self._sem = asyncio.Semaphore(concurrency)
        self._client = httpx.AsyncClient(
            verify=_ssl_context(),
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(max_connections=concurrency * 2),
            headers={"User-Agent": "PolyClusters/1.0 (research)", "Accept": "application/json"},
            follow_redirects=True,
        )
        self.request_count = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "PolymarketClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self._limiter.acquire()
            async with self._sem:
                try:
                    r = await self._client.get(url, params=clean)
                    self.request_count += 1
                    if r.status_code == 429 or r.status_code >= 500:
                        raise httpx.HTTPStatusError(
                            f"HTTP {r.status_code}", request=r.request, response=r
                        )
                    r.raise_for_status()
                    return r.json()
                except (httpx.HTTPError, ValueError) as exc:
                    last_err = exc
                    if attempt >= self.max_retries:
                        break
            # backoff happens outside the semaphore so we don't hold a slot
            await asyncio.sleep(min(30.0, 1.5 * (2**attempt)) * (0.7 + 0.6 * random.random()))
        raise RuntimeError(f"GET {url} failed after {self.max_retries + 1} tries: {last_err}")

    # -- Gamma --------------------------------------------------------------
    async def gamma(self, path: str, **params: Any) -> Any:
        return await self._get(f"{GAMMA_API}{path}", params)

    # -- Data ---------------------------------------------------------------
    async def data(self, path: str, **params: Any) -> Any:
        out = await self._get(f"{DATA_API}{path}", params)
        # The Data API signals limit violations with a JSON object rather than
        # an HTTP error code; surface that to the caller as an exception.
        if isinstance(out, dict) and "error" in out:
            raise DataApiLimit(str(out["error"]))
        return out

    # -- CLOB ---------------------------------------------------------------
    async def clob(self, path: str, **params: Any) -> Any:
        return await self._get(f"{CLOB_API}{path}", params)


class DataApiLimit(RuntimeError):
    """Raised when the Data API rejects a request for exceeding its offset cap."""
