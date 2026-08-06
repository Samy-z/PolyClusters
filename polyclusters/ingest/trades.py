"""Complete trade-history crawling for a set of markets.

The Data API refuses ``offset`` beyond 10,000 on ``/trades``, which alone caps
a market at ~10.5k of its most recent trades. It does, however, honour
undocumented ``start`` / ``end`` epoch-second parameters. This module exploits
that: it walks a market's lifetime in time slices and recursively bisects any
slice that is dense enough to hit the offset ceiling, so the crawl is complete
no matter how heavily traded the market is.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Callable

import pandas as pd

from ..config import ACTIVITY_MAX_OFFSET, TRADES_MAX_LIMIT, TRADES_MAX_OFFSET
from .client import DataApiLimit, PolymarketClient

ProgressFn = Callable[[str], None]
CancelFn = Callable[[], bool]

# Below this width we stop bisecting: a single second holding >10k trades is
# not something the API can express, so we accept the truncation and flag it.
MIN_SLICE_SECONDS = 60


def _trade_uid_series(df: pd.DataFrame) -> pd.Series:
    """Stable per-row identity so re-crawling is idempotent.

    One transaction hash covers both sides of a fill (and several maker fills),
    so the hash alone is not unique - wallet, asset, side, size, price and time
    are all folded in.
    """
    key = (
        df["tx_hash"].astype(str)
        + "|" + df["proxy_wallet"].astype(str)
        + "|" + df["asset"].astype(str)
        + "|" + df["side"].astype(str)
        + "|" + df["size"].round(6).astype(str)
        + "|" + df["price"].round(6).astype(str)
        + "|" + df["ts"].astype(str)
    )
    return key.map(lambda s: hashlib.md5(s.encode()).hexdigest())


def normalise_trades(raw: list[dict[str, Any]]) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw)
    keep = {
        "transactionHash": "tx_hash",
        "conditionId": "condition_id",
        "proxyWallet": "proxy_wallet",
        "asset": "asset",
        "side": "side",
        "outcomeIndex": "outcome_index",
        "outcome": "outcome",
        "size": "size",
        "price": "price",
        "timestamp": "ts",
    }
    for src in keep:
        if src not in df.columns:
            df[src] = None
    out = df[list(keep)].rename(columns=keep)
    out["size"] = pd.to_numeric(out["size"], errors="coerce").fillna(0.0)
    out["price"] = pd.to_numeric(out["price"], errors="coerce").fillna(0.0)
    out["ts"] = pd.to_numeric(out["ts"], errors="coerce").fillna(0).astype("int64")
    out["outcome_index"] = (
        pd.to_numeric(out["outcome_index"], errors="coerce").fillna(0).astype("int32")
    )
    out["usd"] = out["size"] * out["price"]
    out["side"] = out["side"].astype(str).str.upper()
    out["proxy_wallet"] = out["proxy_wallet"].astype(str).str.lower()
    out = out[out["size"] > 0]
    out["trade_uid"] = _trade_uid_series(out)
    return out.drop_duplicates(subset=["trade_uid"])


def profile_rows(raw: list[dict[str, Any]]) -> pd.DataFrame:
    """Extract the display identity Polymarket attaches to each trade."""
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw)
    for col in ("proxyWallet", "name", "pseudonym", "timestamp"):
        if col not in df.columns:
            df[col] = None
    out = pd.DataFrame(
        {
            "proxy_wallet": df["proxyWallet"].astype(str).str.lower(),
            "name": df["name"].fillna("").astype(str),
            "pseudonym": df["pseudonym"].fillna("").astype(str),
            "first_seen": pd.to_numeric(df["timestamp"], errors="coerce").fillna(0).astype("int64"),
        }
    )
    out["last_seen"] = out["first_seen"]
    return out.groupby("proxy_wallet", as_index=False).agg(
        name=("name", "max"),
        pseudonym=("pseudonym", "max"),
        first_seen=("first_seen", "min"),
        last_seen=("last_seen", "max"),
    )


class TradeCrawler:
    """Fetches every trade for a market inside a time window."""

    def __init__(
        self,
        client: PolymarketClient,
        *,
        taker_only: bool = False,
        min_trade_usd: float = 0.0,
        progress: ProgressFn | None = None,
        cancelled: CancelFn | None = None,
        max_page_batch: int = 8,
    ):
        self.client = client
        self.taker_only = taker_only
        self.min_trade_usd = min_trade_usd
        self.max_page_batch = max(1, max_page_batch)
        self.progress = progress or (lambda _m: None)
        self.cancelled = cancelled or (lambda: False)
        self.truncated_slices = 0

    async def _fetch_page(
        self, condition_id: str, start: int, end: int, offset: int
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "market": condition_id,
            "limit": TRADES_MAX_LIMIT,
            "offset": offset,
            "takerOnly": self.taker_only,
            "start": start,
            "end": end,
        }
        if self.min_trade_usd > 0:
            params["filterType"] = "CASH"
            params["filterAmount"] = self.min_trade_usd
        batch = await self.client.data("/trades", **params)
        return batch if isinstance(batch, list) else []

    async def _page_slice(
        self, condition_id: str, start: int, end: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """Page one time slice. Returns (rows, hit_offset_ceiling).

        Pages are fetched in a doubling batch rather than one at a time. Most
        markets fit in a single page, so the ramp starts at one and no requests
        are wasted on them; a dense market reaches full parallelism within a
        few rounds instead of paying one round trip per 500 trades. Safe
        because the window is bounded by start/end, so the result set does not
        shift underneath us.
        """
        rows: list[dict[str, Any]] = []
        offset = 0
        batch_size = 1
        rounds = 0
        while offset <= TRADES_MAX_OFFSET:
            if self.cancelled():
                return rows, False
            offsets = [
                offset + i * TRADES_MAX_LIMIT
                for i in range(batch_size)
                if offset + i * TRADES_MAX_LIMIT <= TRADES_MAX_OFFSET
            ]
            if not offsets:
                break
            results = await asyncio.gather(
                *(self._fetch_page(condition_id, start, end, o) for o in offsets),
                return_exceptions=True,
            )
            exhausted = False
            for res in results:
                if isinstance(res, DataApiLimit):
                    return rows, True
                if isinstance(res, BaseException):
                    raise res
                rows.extend(res)
                if len(res) < TRADES_MAX_LIMIT:
                    exhausted = True
            if exhausted:
                return rows, False
            offset += len(offsets) * TRADES_MAX_LIMIT
            # Hold at one page for the first two rounds. Almost every market
            # fits in one or two pages, and speculating on them costs a wasted
            # request each; with hundreds of markets running at once the
            # parallelism is already there. Only genuinely deep markets ramp.
            rounds += 1
            batch_size = 1 if rounds < 2 else min(max(batch_size, 1) * 2, self.max_page_batch)
        # Exhausted the allowed offset range with a full final page: there is
        # more data in this slice than the API will hand over.
        return rows, True

    async def crawl_market(
        self, condition_id: str, start: int, end: int, depth: int = 0
    ) -> list[dict[str, Any]]:
        """Recursively bisect the window until every slice fits under the cap."""
        if self.cancelled() or start > end:
            return []
        rows, ceiling_hit = await self._page_slice(condition_id, start, end)
        if not ceiling_hit:
            return rows
        span = end - start
        if span < MIN_SLICE_SECONDS:
            self.truncated_slices += 1
            self.progress(
                f"  ! {condition_id[:10]} {span}s slice exceeds the API cap; "
                f"kept {len(rows)} trades"
            )
            return rows
        mid = start + span // 2
        self.progress(f"  ~ splitting dense slice ({span}s) for {condition_id[:10]}")
        # Both halves at once; the client's own limiter decides the real pace.
        left, right = await asyncio.gather(
            self.crawl_market(condition_id, start, mid, depth + 1),
            self.crawl_market(condition_id, mid + 1, end, depth + 1),
        )
        return left + right


async def fetch_user_activity(
    client: PolymarketClient,
    wallet: str,
    start: int,
    end: int,
    *,
    cancelled: CancelFn | None = None,
) -> list[dict[str, Any]]:
    """Every trade one wallet made in a window, across all markets.

    Watching a wallet means following it everywhere, including markets outside
    the analysis universe, which the market-keyed crawler never visits. The
    ``/activity`` endpoint is user-keyed and caps offset at 5,000, so the window
    is halved whenever that ceiling is reached - the same trick the market
    crawler uses against its own 10,000 cap.
    """
    cancelled = cancelled or (lambda: False)
    out: list[dict[str, Any]] = []
    stack: list[tuple[int, int]] = [(start, max(start, end))]

    while stack:
        if cancelled():
            break
        lo, hi = stack.pop()
        offset, ceiling_hit = 0, False
        while offset <= ACTIVITY_MAX_OFFSET:
            try:
                batch = await client.data(
                    "/activity", user=wallet, limit=TRADES_MAX_LIMIT, offset=offset,
                    type="TRADE", start=lo, end=hi, sortDirection="ASC",
                )
            except DataApiLimit:
                ceiling_hit = True
                break
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < TRADES_MAX_LIMIT:
                break
            offset += TRADES_MAX_LIMIT
        else:
            ceiling_hit = True
        if ceiling_hit and hi - lo >= MIN_SLICE_SECONDS:
            mid = lo + (hi - lo) // 2
            stack.extend([(lo, mid), (mid + 1, hi)])
    return out


def market_window(
    market_start: int, market_end: int, req_start: int, req_end: int
) -> tuple[int, int]:
    """Clamp a requested window to a market's own lifetime."""
    lo = max(req_start, market_start or req_start)
    hi = min(req_end, market_end or req_end)
    # Trades can land slightly after the stated end date (late settlement
    # activity), so extend the upper bound by a day rather than trusting it.
    hi = min(req_end, hi + 86_400) if market_end else req_end
    return lo, max(lo, hi)


def now_ts() -> int:
    return int(time.time())
