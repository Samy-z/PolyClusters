"""Market / event / tag discovery via the Gamma API.

Markets are discovered through ``/events`` rather than ``/markets`` because the
event payload carries the ``tags`` array, which is what the app exposes as
"sectors". A single fetch therefore populates markets and their sector labels.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd

from ..config import GAMMA_MAX_LIMIT
from .client import PolymarketClient


def _parse_iso(value: Any) -> int | None:
    """Gamma mixes ISO-8601 'Z' and '+00' forms; normalise both to epoch secs."""
    if not value or not isinstance(value, str):
        return None
    txt = value.strip().replace("Z", "+00:00")
    if txt.endswith("+00"):
        txt += ":00"
    if " " in txt and "T" not in txt:
        txt = txt.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _winning_outcome(market: dict[str, Any], resolved: bool) -> int | None:
    """A resolved market's outcomePrices collapse to a one-hot vector."""
    if not resolved:
        return None
    prices = _loads(market.get("outcomePrices"), [])
    try:
        vals = [float(p) for p in prices]
    except (TypeError, ValueError):
        return None
    if not vals:
        return None
    top = max(range(len(vals)), key=lambda i: vals[i])
    # Guard against genuinely unresolved markets that happen to be closed.
    return top if vals[top] > 0.99 else None


def normalise_market(market: dict[str, Any], event: dict[str, Any] | None) -> dict[str, Any]:
    event = event or {}
    closed = bool(market.get("closed"))
    uma = (market.get("umaResolutionStatus") or "").lower()
    resolved = closed and uma in ("resolved", "")
    win = _winning_outcome(market, closed)
    resolved = win is not None
    outcomes = _loads(market.get("outcomes"), [])
    return {
        "condition_id": market.get("conditionId") or "",
        "market_id": str(market.get("id") or ""),
        "question": market.get("question") or market.get("groupItemTitle") or "",
        "slug": market.get("slug") or "",
        "event_id": str(event.get("id") or ""),
        "event_slug": event.get("slug") or market.get("eventSlug") or "",
        "event_title": event.get("title") or "",
        "start_ts": _parse_iso(market.get("startDate")) or _parse_iso(event.get("startDate")) or 0,
        "end_ts": _parse_iso(market.get("endDate")) or _parse_iso(event.get("endDate")) or 0,
        "closed": closed,
        "resolved": resolved,
        "volume": float(market.get("volumeNum") or market.get("volume") or 0.0),
        "liquidity": float(market.get("liquidityNum") or market.get("liquidity") or 0.0),
        "n_outcomes": len(outcomes),
        "outcomes_json": json.dumps(outcomes),
        "outcome_prices_json": json.dumps(_loads(market.get("outcomePrices"), [])),
        "winning_outcome": win,
        "neg_risk": bool(market.get("negRisk")),
        "clob_token_ids_json": json.dumps(_loads(market.get("clobTokenIds"), [])),
        "ingested_at": int(time.time()),
    }


def flatten_events(events: Iterable[dict[str, Any]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split an ``/events`` payload into (markets, market_tags) frames."""
    market_rows: list[dict[str, Any]] = []
    tag_rows: list[dict[str, Any]] = []
    for event in events:
        tags = event.get("tags") or []
        for market in event.get("markets") or []:
            row = normalise_market(market, event)
            if not row["condition_id"]:
                continue
            market_rows.append(row)
            for tag in tags:
                try:
                    tid = int(tag.get("id"))
                except (TypeError, ValueError):
                    continue
                tag_rows.append(
                    {
                        "condition_id": row["condition_id"],
                        "tag_id": tid,
                        "tag_label": tag.get("label") or "",
                        "tag_slug": tag.get("slug") or "",
                    }
                )
    markets = pd.DataFrame(market_rows)
    if not markets.empty:
        markets = markets.drop_duplicates(subset=["condition_id"], keep="last")
    tags_df = pd.DataFrame(tag_rows)
    if not tags_df.empty:
        tags_df = tags_df.drop_duplicates(subset=["condition_id", "tag_id"])
    return markets, tags_df


# Sectors worth pinning to the top of the picker. Gamma has ~6,000 tags, most of
# them single-event noise, so the useful ones need surfacing explicitly. Verified
# to resolve against /tags/slug/{slug}; any that stop resolving are skipped.
CURATED_SECTOR_SLUGS: list[str] = [
    "politics", "geopolitics", "world", "elections", "economy", "fed",
    "inflation", "business", "earnings", "crypto", "tech", "ai", "science",
    "war", "middle-east", "israel", "china", "defense", "trump",
    "breaking-news", "sports",
]


async def fetch_curated_tags(client: PolymarketClient) -> pd.DataFrame:
    """Resolve the pinned sector slugs to ids, skipping any that 404."""

    async def one(slug: str) -> dict[str, Any] | None:
        try:
            tag = await client.gamma(f"/tags/slug/{slug}")
        except Exception:  # noqa: BLE001 - a retired slug must not break startup
            return None
        try:
            return {
                "tag_id": int(tag["id"]),
                "label": tag.get("label") or slug,
                "slug": tag.get("slug") or slug,
            }
        except (KeyError, TypeError, ValueError):
            return None

    results = await asyncio.gather(*(one(s) for s in CURATED_SECTOR_SLUGS))
    return pd.DataFrame([r for r in results if r])


async def fetch_all_tags(client: PolymarketClient, max_pages: int = 60) -> pd.DataFrame:
    """Page the full tag catalogue (the app's "sector" vocabulary).

    A page that fails after its retries ends the walk and returns what was
    collected so far. Sixty sequential pages is enough requests that an
    occasional throttle is normal, and losing the whole catalogue - leaving the
    picker with only the pinned sectors - is far worse than returning a
    slightly short one.
    """
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for page in range(max_pages):
        try:
            batch = await client.gamma(
                "/tags", limit=GAMMA_MAX_LIMIT, offset=page * GAMMA_MAX_LIMIT
            )
        except Exception:  # noqa: BLE001 - keep the pages already gathered
            break
        if not isinstance(batch, list) or not batch:
            break
        for tag in batch:
            try:
                tid = int(tag.get("id"))
            except (TypeError, ValueError):
                continue
            if tid in seen:
                continue
            seen.add(tid)
            rows.append({"tag_id": tid, "label": tag.get("label") or "", "slug": tag.get("slug") or ""})
        if len(batch) < GAMMA_MAX_LIMIT:
            break
    return pd.DataFrame(rows)


async def fetch_events(
    client: PolymarketClient,
    *,
    tag_id: int | None = None,
    start_ts: int | None = None,
    end_ts: int | None = None,
    closed: bool | None = None,
    min_volume: float = 0.0,
    max_pages: int = 400,
    related_tags: bool = True,
    progress: Any = None,
) -> list[dict[str, Any]]:
    """Page every event overlapping the requested window.

    A market is "in the window" when its lifetime overlaps it, i.e. it started
    on or before the window end and ended on or after the window start.

    Uses ``/events/keyset`` with ``after_cursor`` rather than ``/events`` with
    ``offset``: the offset-based endpoint returns HTTP 422
    ("offset too large, use /events/keyset for deeper pagination") beyond about
    2,000 rows, which any unfiltered sweep reaches almost immediately.
    """
    params: dict[str, Any] = {
        "limit": GAMMA_MAX_LIMIT,
        "order": "volume",
        "ascending": False,
    }
    if tag_id is not None:
        params["tag_id"] = tag_id
        params["related_tags"] = related_tags
    if closed is not None:
        params["closed"] = closed
    if min_volume > 0:
        # NB: /events spells this "volume_min"; only /markets uses
        # "volume_num_min". The wrong name is silently ignored, not rejected.
        params["volume_min"] = min_volume
    if end_ts:
        params["start_date_max"] = datetime.fromtimestamp(end_ts, timezone.utc).isoformat()
    if start_ts:
        params["end_date_min"] = datetime.fromtimestamp(start_ts, timezone.utc).isoformat()

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor: str | None = None
    for page in range(max_pages):
        if cursor:
            params["after_cursor"] = cursor
        payload = await client.gamma("/events/keyset", **params)
        if isinstance(payload, dict):
            batch = payload.get("events") or []
            cursor = payload.get("next_cursor") or None
        else:  # defensive: tolerate a plain-list response
            batch = payload or []
            cursor = None
        if not batch:
            break
        fresh = [e for e in batch if str(e.get("id")) not in seen]
        seen.update(str(e.get("id")) for e in batch)
        out.extend(fresh)
        if progress:
            progress(f"  events page {page + 1}: +{len(fresh)} (total {len(out)})")
        # A cursor that stops advancing would otherwise loop forever.
        if not cursor or not fresh:
            break
    return out


async def fetch_markets_by_condition(
    client: PolymarketClient, condition_ids: list[str]
) -> list[dict[str, Any]]:
    """Look markets up directly, for the 'analyse this exact bet' path."""
    out: list[dict[str, Any]] = []
    for i in range(0, len(condition_ids), 20):
        chunk = condition_ids[i : i + 20]
        batch = await client.gamma("/markets", condition_ids=chunk, limit=GAMMA_MAX_LIMIT)
        if isinstance(batch, list):
            out.extend(batch)
    return out


async def search_events(client: PolymarketClient, term: str, limit: int = 100) -> list[dict[str, Any]]:
    """Best-effort text search used by the market picker."""
    try:
        res = await client.gamma("/public-search", q=term, limit_per_type=limit)
        if isinstance(res, dict) and res.get("events"):
            return res["events"]
    except Exception:  # noqa: BLE001 - search is a convenience, never fatal
        pass
    for closed in (False, True):
        batch = await client.gamma(
            "/events", slug=term, limit=limit, closed=closed
        )
        if isinstance(batch, list) and batch:
            return batch
    return []
