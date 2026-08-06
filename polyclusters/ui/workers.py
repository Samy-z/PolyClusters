"""Background workers so the UI never blocks on network or analysis work."""

from __future__ import annotations

import asyncio
import traceback
from typing import Any

import pandas as pd
from PySide6.QtCore import QObject, QThread, Signal

from ..analysis.cluster import ClusterParams
from ..analysis.engine import AnalysisResult, run_analysis
from ..config import AnalysisFilters, AppSettings
from ..core.db import Database
from ..ingest.client import PolymarketClient
from ..ingest.gamma import search_events
from ..ingest.pipeline import IngestReport, run_ingest


class _Cancellable(QThread):
    """Common cancel flag + error plumbing."""

    message = Signal(str)
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True
        self.message.emit("Cancellation requested; finishing in-flight work...")

    def is_cancelled(self) -> bool:
        return self._cancel


class IngestWorker(_Cancellable):
    """Runs the discover + crawl pipeline off the UI thread."""

    progress = Signal(int, int, str)  # done, total, label
    finished_ok = Signal(object)      # IngestReport

    def __init__(
        self,
        db: Database,
        settings: AppSettings,
        filters: AnalysisFilters,
        refresh_tags: bool = False,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.db = db
        self.settings = settings
        self.filters = filters
        self.refresh_tags = refresh_tags

    def run(self) -> None:  # noqa: D102
        try:
            report: IngestReport = run_ingest(
                self.db,
                self.settings,
                self.filters,
                refresh_tags=self.refresh_tags,
                progress=self.message.emit,
                status=lambda d, t, label: self.progress.emit(d, t, label),
                cancelled=self.is_cancelled,
            )
            self.finished_ok.emit(report)
        except Exception:  # noqa: BLE001 - surface the traceback in the log pane
            self.failed.emit(traceback.format_exc())


class AnalysisWorker(_Cancellable):
    """Runs clustering + metrics off the UI thread."""

    finished_ok = Signal(object)  # AnalysisResult

    def __init__(
        self,
        db: Database,
        settings: AppSettings,
        filters: AnalysisFilters,
        params: ClusterParams,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.db = db
        self.settings = settings
        self.filters = filters
        self.params = params

    def run(self) -> None:  # noqa: D102
        try:
            result: AnalysisResult = run_analysis(
                self.db, self.filters, self.settings, self.params,
                progress=self.message.emit,
            )
            self.finished_ok.emit(result)
        except Exception:  # noqa: BLE001
            self.failed.emit(traceback.format_exc())


class TagBootstrapWorker(_Cancellable):
    """Populate the sector catalogue so sectors are selectable before any fetch.

    Without this the picker is empty on a fresh install - it reads from the
    local database, which only gets tags after an ingest, so the first fetch
    could never be scoped to a sector.
    """

    finished_ok = Signal(int)  # number of tags stored

    def __init__(self, db: Database, settings: AppSettings, parent: QObject | None = None):
        super().__init__(parent)
        self.db = db
        self.settings = settings

    def run(self) -> None:  # noqa: D102
        try:
            self.finished_ok.emit(asyncio.run(self._load()))
        except Exception:  # noqa: BLE001
            self.failed.emit(traceback.format_exc())

    async def _load(self) -> int:
        from ..ingest.gamma import fetch_all_tags, fetch_curated_tags

        async with PolymarketClient(
            concurrency=self.settings.max_concurrency,
            rate_per_sec=self.settings.requests_per_second,
            timeout=self.settings.request_timeout,
        ) as client:
            self.message.emit("Loading sector catalogue...")
            curated = await fetch_curated_tags(client)
            if not curated.empty:
                self.db.upsert_tags(curated)
                self.message.emit(f"  {len(curated)} headline sectors ready.")
            if self.is_cancelled():
                return len(curated)
            try:
                everything = await fetch_all_tags(client)
            except Exception as exc:  # noqa: BLE001
                # The pinned sectors are already stored, so the picker is
                # usable; a partial catalogue beats an empty one.
                self.message.emit(f"  full catalogue unavailable ({exc}); "
                                  "headline sectors are still selectable.")
                everything = pd.DataFrame()
            if not everything.empty:
                self.db.upsert_tags(everything)
            total = int(self.db.scalar("SELECT count(*) FROM tags") or len(curated))
        self.message.emit(f"  {total} sectors available in the picker.")
        return total


class WatchRefreshWorker(_Cancellable):
    """Pull fresh activity for watched items and turn the changes into events."""

    finished_ok = Signal(int)  # number of events recorded

    def __init__(
        self,
        db: Database,
        settings: AppSettings,
        store: Any,
        lookback_days: int = 30,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.db = db
        self.settings = settings
        self.store = store
        self.lookback_days = lookback_days

    def run(self) -> None:  # noqa: D102
        try:
            self.finished_ok.emit(asyncio.run(self._refresh()))
        except Exception:  # noqa: BLE001
            self.failed.emit(traceback.format_exc())

    async def _refresh(self) -> int:
        import time

        from ..core.watchlist import diff_wallet, observe_bet, observe_wallet
        from ..ingest.gamma import fetch_markets_by_condition, normalise_market
        from ..ingest.trades import fetch_user_activity, normalise_trades, profile_rows

        items = self.store.items()
        if not items:
            self.message.emit("Watchlist is empty; nothing to refresh.")
            return 0

        wallets = {i.ref["wallet"] for i in items if i.kind == "member"}
        wallets |= {i.ref["wallet"] for i in items if i.kind == "position"}
        for item in items:
            if item.kind == "cluster":
                wallets |= set(item.ref.get("wallets") or [])
        bet_keys = {i.ref["bet_key"] for i in items if i.kind in ("bet", "position")}

        now = int(time.time())
        events = 0

        async with PolymarketClient(
            concurrency=self.settings.max_concurrency,
            rate_per_sec=self.settings.requests_per_second,
            timeout=self.settings.request_timeout,
            max_retries=self.settings.max_retries,
        ) as client:
            # --- 1. follow each watched wallet everywhere it traded ---------
            self.message.emit(f"Following {len(wallets)} watched wallet(s)...")
            seen_conditions: set[str] = set()
            for n, wallet in enumerate(sorted(wallets), start=1):
                if self.is_cancelled():
                    break
                start = now - self.lookback_days * 86_400
                try:
                    raw = await fetch_user_activity(
                        client, wallet, start, now, cancelled=self.is_cancelled
                    )
                except Exception as exc:  # noqa: BLE001
                    self.message.emit(f"  ! {wallet[:10]}: {exc}")
                    continue
                if raw:
                    trades = normalise_trades(raw)
                    self.db.upsert_trades(trades)
                    self.db.upsert_users(profile_rows(raw))
                    seen_conditions |= set(trades.condition_id.dropna())
                self.message.emit(f"  [{n}/{len(wallets)}] {wallet[:10]} "
                                  f"+{len(raw)} activity rows")

            # --- 2. fill in metadata for any market we have not seen --------
            wanted = {k.rpartition(":")[0] for k in bet_keys} | seen_conditions
            known = self.db.query("SELECT condition_id FROM markets")
            missing = sorted(wanted - set(known.condition_id if not known.empty else []))
            if missing and not self.is_cancelled():
                self.message.emit(f"Fetching metadata for {len(missing)} new market(s)...")
                raw_markets = await fetch_markets_by_condition(client, missing)
                rows = [normalise_market(m, (m.get("events") or [{}])[0]) for m in raw_markets]
                if rows:
                    self.db.upsert_markets(pd.DataFrame(rows))

            # --- 3. refresh resolution status of watched markets ------------
            watched_conditions = sorted({k.rpartition(":")[0] for k in bet_keys})
            if watched_conditions and not self.is_cancelled():
                raw_markets = await fetch_markets_by_condition(client, watched_conditions)
                rows = [normalise_market(m, (m.get("events") or [{}])[0]) for m in raw_markets]
                if rows:
                    self.db.upsert_markets(pd.DataFrame(rows))

        # --- 4. diff every watched item against its stored snapshot ---------
        self.message.emit("Comparing against the last snapshot...")
        for item in items:
            if item.kind in ("member", "position"):
                wallet = item.ref["wallet"]
                after = observe_wallet(self.db, wallet)
                for kind, severity, summary, detail in diff_wallet(item.snapshot, after):
                    if item.kind == "position" and detail.get("bet_key") != item.ref.get("bet_key"):
                        continue  # a position watch only cares about its own bet
                    self.store.record_event(item.item_id, kind, severity, summary, detail)
                    events += 1
                self.store.save_snapshot(item.item_id, after)
            elif item.kind == "bet":
                after = observe_bet(self.db, item.ref["bet_key"])
                before = item.snapshot or {}
                if after.get("resolved") and not before.get("resolved"):
                    verdict = "WON" if after.get("won") else "LOST"
                    self.store.record_event(
                        item.item_id, "resolved",
                        "alert" if after.get("won") else "info",
                        f"“{after.get('question', '')[:60]}” resolved — {verdict}",
                        after,
                    )
                    events += 1
                elif before.get("traders") and after.get("traders", 0) > before["traders"]:
                    self.store.record_event(
                        item.item_id, "new_position", "info",
                        f"{after['traders'] - before['traders']} new trader(s) entered "
                        f"“{after.get('question', '')[:50]}”", after,
                    )
                    events += 1
                self.store.save_snapshot(item.item_id, after)

        self.message.emit(f"Watchlist refresh done — {events} new event(s).")
        return events


class MarketSearchWorker(_Cancellable):
    """Looks up markets by free text for the 'analyse this exact bet' picker."""

    results = Signal(object)  # DataFrame of candidate markets

    def __init__(self, settings: AppSettings, term: str, parent: QObject | None = None):
        super().__init__(parent)
        self.settings = settings
        self.term = term

    def run(self) -> None:  # noqa: D102
        try:
            self.results.emit(asyncio.run(self._search()))
        except Exception:  # noqa: BLE001
            self.failed.emit(traceback.format_exc())

    async def _search(self) -> pd.DataFrame:
        from ..ingest.gamma import flatten_events

        async with PolymarketClient(
            concurrency=4,
            rate_per_sec=self.settings.requests_per_second,
            timeout=self.settings.request_timeout,
        ) as client:
            events = await search_events(client, self.term)
            markets, _tags = flatten_events(events)
        if markets.empty:
            return markets
        markets["sectors"] = ""
        return markets.sort_values("volume", ascending=False).head(300)
