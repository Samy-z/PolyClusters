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
            everything = await fetch_all_tags(client)
            if not everything.empty:
                self.db.upsert_tags(everything)
            total = len(everything) if not everything.empty else len(curated)
        self.message.emit(f"  {total} sectors available in the picker.")
        return total


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
