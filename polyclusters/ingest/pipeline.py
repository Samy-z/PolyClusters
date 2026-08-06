"""Ingestion orchestration: discover markets, then crawl their trades."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import pandas as pd

from ..config import AnalysisFilters, AppSettings
from ..core.db import Database
from .client import PolymarketClient
from .gamma import fetch_all_tags, fetch_events, fetch_markets_by_condition, flatten_events, normalise_market
from .trades import TradeCrawler, market_window, normalise_trades, now_ts, profile_rows

ProgressFn = Callable[[str], None]
StatusFn = Callable[[int, int, str], None]  # done, total, label


def merge_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted(intervals)
    merged: list[tuple[int, int]] = []
    for lo, hi in ordered:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def subtract_covered(
    want: tuple[int, int], covered: Iterable[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Return the parts of ``want`` not already present in ``covered``."""
    lo, hi = want
    gaps: list[tuple[int, int]] = []
    cursor = lo
    for c_lo, c_hi in merge_intervals(covered):
        if c_hi < cursor:
            continue
        if c_lo > hi:
            break
        if c_lo > cursor:
            gaps.append((cursor, min(hi, c_lo - 1)))
        cursor = max(cursor, c_hi + 1)
        if cursor > hi:
            break
    if cursor <= hi:
        gaps.append((cursor, hi))
    return [g for g in gaps if g[0] <= g[1]]


@dataclass
class IngestReport:
    markets_seen: int = 0
    markets_crawled: int = 0
    markets_skipped: int = 0
    trades_added: int = 0
    requests: int = 0
    truncated_slices: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False

    def summary(self) -> str:
        parts = [
            f"{self.markets_crawled} markets crawled",
            f"{self.markets_skipped} already covered",
            f"{self.trades_added:,} trade rows stored",
            f"{self.requests} API requests",
        ]
        if self.truncated_slices:
            parts.append(f"{self.truncated_slices} slice(s) truncated by API cap")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        if self.cancelled:
            parts.append("CANCELLED")
        return " | ".join(parts)


class Ingestor:
    """Runs a full discover-then-crawl pass against the public API."""

    def __init__(
        self,
        db: Database,
        settings: AppSettings,
        *,
        progress: ProgressFn | None = None,
        status: StatusFn | None = None,
        cancelled: Callable[[], bool] | None = None,
    ):
        self.db = db
        self.settings = settings
        self.progress = progress or (lambda _m: None)
        self.status = status or (lambda _d, _t, _l: None)
        self.cancelled = cancelled or (lambda: False)

    # -- discovery ----------------------------------------------------------
    async def refresh_tags(self, client: PolymarketClient) -> int:
        self.progress("Fetching sector (tag) catalogue...")
        tags = await fetch_all_tags(client)
        n = self.db.upsert_tags(tags)
        self.db.execute(
            """
            UPDATE tags SET n_markets = COALESCE(c.n, 0) FROM (
                SELECT tag_id, count(*) AS n FROM market_tags GROUP BY tag_id
            ) c WHERE tags.tag_id = c.tag_id
            """
        )
        self.progress(f"Stored {n} tags.")
        return n

    async def discover_markets(
        self, client: PolymarketClient, filters: AnalysisFilters
    ) -> pd.DataFrame:
        """Resolve the filter set into a concrete list of markets."""
        all_markets: list[pd.DataFrame] = []
        all_tags: list[pd.DataFrame] = []

        explicit = filters.discovery_condition_ids()
        if explicit:
            self.progress(f"Looking up {len(explicit)} explicit market(s)...")
            raw = await fetch_markets_by_condition(client, explicit)
            rows = [normalise_market(m, (m.get("events") or [{}])[0]) for m in raw]
            tag_rows = []
            for m, row in zip(raw, rows):
                for ev in m.get("events") or []:
                    for tag in ev.get("tags") or []:
                        try:
                            tag_rows.append(
                                {
                                    "condition_id": row["condition_id"],
                                    "tag_id": int(tag["id"]),
                                    "tag_label": tag.get("label") or "",
                                    "tag_slug": tag.get("slug") or "",
                                }
                            )
                        except (KeyError, TypeError, ValueError):
                            continue
            all_markets.append(pd.DataFrame(rows))
            all_tags.append(pd.DataFrame(tag_rows))

        tag_targets: list[int | None] = list(filters.tag_ids) or ([] if explicit else [None])
        for tag_id in tag_targets:
            if self.cancelled():
                break
            label = f"tag {tag_id}" if tag_id else "all sectors"
            self.progress(f"Discovering events for {label}...")
            for closed in (True, False):
                if self.cancelled():
                    break
                events = await fetch_events(
                    client,
                    tag_id=tag_id,
                    start_ts=filters.start_ts or None,
                    end_ts=filters.end_ts or None,
                    closed=closed,
                    min_volume=filters.min_market_volume,
                    progress=self.progress,
                )
                m_df, t_df = flatten_events(events)
                all_markets.append(m_df)
                all_tags.append(t_df)

        markets = pd.concat([d for d in all_markets if not d.empty], ignore_index=True) \
            if any(not d.empty for d in all_markets) else pd.DataFrame()
        tags = pd.concat([d for d in all_tags if not d.empty], ignore_index=True) \
            if any(not d.empty for d in all_tags) else pd.DataFrame()

        if markets.empty:
            self.progress("No markets matched the discovery filters.")
            return markets

        markets = markets.drop_duplicates(subset=["condition_id"], keep="last")
        # Explicitly requested markets bypass the discovery filters - the user
        # named them, so a low volume or open status must not drop them.
        pinned = markets.condition_id.isin(explicit)
        keep = pinned.copy()
        eligible = ~pinned
        if filters.min_market_volume:
            eligible &= markets["volume"] >= filters.min_market_volume
        if filters.resolved_only:
            eligible &= markets["resolved"]
        if filters.exclude_tag_ids and not tags.empty:
            banned = set(tags[tags.tag_id.isin(filters.exclude_tag_ids)].condition_id)
            eligible &= ~markets.condition_id.isin(banned)
        markets = markets[keep | eligible]

        cap = max(int(self.settings.max_markets_per_fetch), 1)
        if len(markets) > cap:
            pinned_rows = markets[markets.condition_id.isin(explicit)]
            rest = markets[~markets.condition_id.isin(explicit)].nlargest(
                max(cap - len(pinned_rows), 0), "volume"
            )
            self.progress(
                f"  {len(markets):,} markets matched; crawling the {cap:,} "
                f"largest by volume (raise 'Max markets per fetch' to widen). "
                f"Volume floor for this run: ${rest.volume.min():,.0f}"
                if not rest.empty else
                f"  {len(markets):,} markets matched; capped at {cap:,}."
            )
            markets = pd.concat([pinned_rows, rest], ignore_index=True)

        self.db.upsert_markets(markets)
        if not tags.empty:
            tags = tags.drop_duplicates(subset=["condition_id", "tag_id"])
            self.db.upsert_market_tags(tags)
            catalogue = (
                tags[["tag_id", "tag_label", "tag_slug"]]
                .drop_duplicates(subset=["tag_id"])
                .rename(columns={"tag_label": "label", "tag_slug": "slug"})
            )
            self.db.upsert_tags(catalogue)
        self.progress(f"{len(markets)} market(s) matched.")
        return markets

    # -- crawling -----------------------------------------------------------
    async def crawl_trades(
        self,
        client: PolymarketClient,
        markets: pd.DataFrame,
        filters: AnalysisFilters,
        report: IngestReport,
    ) -> None:
        total = len(markets)
        crawler = TradeCrawler(
            client,
            taker_only=False,
            min_trade_usd=self.settings.min_trade_usd,
            progress=self.progress,
            cancelled=self.cancelled,
        )
        # Markets run wider than the connection limit on purpose. The client's
        # own semaphore and token bucket set the real pace; this only has to be
        # generous enough to keep them saturated while some tasks are parsing
        # or writing rather than waiting on the network.
        sem = asyncio.Semaphore(max(4, self.settings.max_concurrency * 2))
        done = 0
        lock = asyncio.Lock()

        # One query for every market's coverage, rather than one per market.
        coverage = self.db.covered_windows_bulk(markets.condition_id.tolist())

        async def one(row: Any) -> None:
            nonlocal done
            cid = row.condition_id
            lo, hi = market_window(
                int(row.start_ts or 0), int(row.end_ts or 0),
                filters.start_ts or int(row.start_ts or 0),
                filters.end_ts or now_ts(),
            )
            gaps = subtract_covered((lo, hi), coverage.get(cid, []))
            if not gaps:
                async with lock:
                    done += 1
                    report.markets_skipped += 1
                    self.status(done, total, f"skipped {row.question[:50]}")
                return
            async with sem:
                if self.cancelled():
                    return
                try:
                    raw_all: list[dict[str, Any]] = []
                    windows: list[tuple[str, int, int, int]] = []
                    for g_lo, g_hi in gaps:
                        raw = await crawler.crawl_market(cid, g_lo, g_hi)
                        raw_all.extend(raw)
                        windows.append((cid, g_lo, g_hi, len(raw)))
                    # Parsing and writing are pure CPU plus a database lock;
                    # doing them inline stalls every other in-flight request.
                    n = await asyncio.to_thread(
                        self.db.store_crawl_batch,
                        normalise_trades(raw_all) if raw_all else pd.DataFrame(),
                        profile_rows(raw_all) if raw_all else pd.DataFrame(),
                        windows,
                        now_ts(),
                    )
                    async with lock:
                        report.trades_added += n
                except Exception as exc:  # noqa: BLE001 - one bad market must not kill the run
                    async with lock:
                        report.errors.append(f"{cid[:12]}: {exc}")
                    self.progress(f"  ! {cid[:12]} failed: {exc}")
            async with lock:
                done += 1
                report.markets_crawled += 1
                self.status(done, total, row.question[:60])

        await asyncio.gather(*(one(r) for r in markets.itertuples()))
        report.truncated_slices = crawler.truncated_slices

    # -- entry point --------------------------------------------------------
    async def run(self, filters: AnalysisFilters, refresh_tags: bool = False) -> IngestReport:
        report = IngestReport()
        async with PolymarketClient(
            concurrency=self.settings.max_concurrency,
            rate_per_sec=self.settings.requests_per_second,
            timeout=self.settings.request_timeout,
            max_retries=self.settings.max_retries,
        ) as client:
            if refresh_tags:
                await self.refresh_tags(client)
            markets = await self.discover_markets(client, filters)
            report.markets_seen = len(markets)
            if not markets.empty and not self.cancelled():
                self.progress(f"Crawling trades for {len(markets)} market(s)...")
                await self.crawl_trades(client, markets, filters, report)
            report.requests = client.request_count
        report.cancelled = self.cancelled()
        self.db.vacuum()
        self.progress("Ingest finished: " + report.summary())
        return report


def run_ingest(
    db: Database,
    settings: AppSettings,
    filters: AnalysisFilters,
    *,
    refresh_tags: bool = False,
    progress: ProgressFn | None = None,
    status: StatusFn | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> IngestReport:
    """Blocking wrapper so a Qt worker thread can drive the async pipeline."""
    ing = Ingestor(db, settings, progress=progress, status=status, cancelled=cancelled)
    return asyncio.run(ing.run(filters, refresh_tags=refresh_tags))
