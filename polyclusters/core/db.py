"""DuckDB storage layer.

DuckDB is used rather than SQLite because every analytical query here is a
wide aggregation over millions of trade rows, which is exactly its workload.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb
import pandas as pd

from ..config import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    condition_id      VARCHAR PRIMARY KEY,
    market_id         VARCHAR,
    question          VARCHAR,
    slug              VARCHAR,
    event_id          VARCHAR,
    event_slug        VARCHAR,
    event_title       VARCHAR,
    start_ts          BIGINT,
    end_ts            BIGINT,
    closed            BOOLEAN,
    resolved          BOOLEAN,
    volume            DOUBLE,
    liquidity         DOUBLE,
    n_outcomes        INTEGER,
    outcomes_json     VARCHAR,
    outcome_prices_json VARCHAR,
    winning_outcome   INTEGER,      -- NULL while unresolved
    neg_risk          BOOLEAN,
    clob_token_ids_json VARCHAR,
    ingested_at       BIGINT
);

CREATE TABLE IF NOT EXISTS market_tags (
    condition_id VARCHAR,
    tag_id       INTEGER,
    tag_label    VARCHAR,
    tag_slug     VARCHAR,
    PRIMARY KEY (condition_id, tag_id)
);

CREATE TABLE IF NOT EXISTS tags (
    tag_id    INTEGER PRIMARY KEY,
    label     VARCHAR,
    slug      VARCHAR,
    n_markets INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trades (
    trade_uid     VARCHAR PRIMARY KEY,
    tx_hash       VARCHAR,
    condition_id  VARCHAR,
    proxy_wallet  VARCHAR,
    asset         VARCHAR,
    side          VARCHAR,          -- BUY / SELL
    outcome_index INTEGER,
    outcome       VARCHAR,
    size          DOUBLE,           -- shares
    price         DOUBLE,           -- 0..1
    usd           DOUBLE,           -- size * price
    ts            BIGINT
);

CREATE TABLE IF NOT EXISTS users (
    proxy_wallet VARCHAR PRIMARY KEY,
    name         VARCHAR,
    pseudonym    VARCHAR,
    first_seen   BIGINT,
    last_seen    BIGINT
);

-- Records which (market, time window) slices have been fully crawled so a
-- re-run can skip them instead of re-paging the whole history.
CREATE TABLE IF NOT EXISTS ingest_log (
    condition_id VARCHAR,
    window_start BIGINT,
    window_end   BIGINT,
    n_trades     BIGINT,
    truncated    BOOLEAN,
    fetched_at   BIGINT,
    PRIMARY KEY (condition_id, window_start, window_end)
);

CREATE TABLE IF NOT EXISTS price_history (
    asset VARCHAR,
    ts    BIGINT,
    price DOUBLE,
    PRIMARY KEY (asset, ts)
);

CREATE TABLE IF NOT EXISTS saved_runs (
    run_id      VARCHAR PRIMARY KEY,
    created_at  BIGINT,
    label       VARCHAR,
    filters_json VARCHAR,
    params_json  VARCHAR,
    summary_json VARCHAR
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_trades_cond ON trades(condition_id)",
    "CREATE INDEX IF NOT EXISTS idx_trades_wallet ON trades(proxy_wallet)",
    "CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts)",
    "CREATE INDEX IF NOT EXISTS idx_mtags_tag ON market_tags(tag_id)",
    "CREATE INDEX IF NOT EXISTS idx_markets_end ON markets(end_ts)",
]

TRADE_COLUMNS = [
    "trade_uid", "tx_hash", "condition_id", "proxy_wallet", "asset", "side",
    "outcome_index", "outcome", "size", "price", "usd", "ts",
]

MARKET_COLUMNS = [
    "condition_id", "market_id", "question", "slug", "event_id", "event_slug",
    "event_title", "start_ts", "end_ts", "closed", "resolved", "volume",
    "liquidity", "n_outcomes", "outcomes_json", "outcome_prices_json",
    "winning_outcome", "neg_risk", "clob_token_ids_json", "ingested_at",
]


class Database:
    """Thin serialised wrapper around a DuckDB connection.

    DuckDB connections are not thread-safe for concurrent use, and this app
    writes from an ingest worker while the UI thread reads. A single lock is
    plenty: every call here is short, and the heavy work is inside DuckDB.
    """

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else db_path()
        self._lock = threading.RLock()
        self.con = duckdb.connect(str(self.path))
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            # DuckDB's ASCII progress bar writes control codes to stderr, which
            # is noise in a GUI app and unreadable in captured logs.
            self.con.execute("SET enable_progress_bar = false")
            for stmt in SCHEMA.strip().split(";"):
                if stmt.strip():
                    self.con.execute(stmt)
            for stmt in INDEXES:
                self.con.execute(stmt)

    # -- basic access -------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] | None = None) -> pd.DataFrame:
        with self._lock:
            return self.con.execute(sql, params or []).fetchdf()

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        with self._lock:
            self.con.execute(sql, params or [])

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        with self._lock:
            row = self.con.execute(sql, params or []).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        with self._lock:
            self.con.close()

    def stage_temp(self, name: str, df: pd.DataFrame) -> None:
        """Materialise a DataFrame as a temp table so queries can join on it.

        Used instead of a multi-thousand-element ``IN (...)`` list, which
        DuckDB parses far more slowly than a join.
        """
        with self._lock:
            self.con.execute(f"DROP TABLE IF EXISTS {name}")
            self.con.register(f"_stage_{name}", df)
            self.con.execute(f"CREATE TEMP TABLE {name} AS SELECT * FROM _stage_{name}")
            self.con.unregister(f"_stage_{name}")

    def drop_temp(self, name: str) -> None:
        with self._lock:
            self.con.execute(f"DROP TABLE IF EXISTS {name}")

    # -- bulk upserts -------------------------------------------------------
    def _upsert_df(self, table: str, df: pd.DataFrame, columns: list[str]) -> int:
        if df.empty:
            return 0
        df = df.reindex(columns=columns)
        with self._lock:
            self.con.register("_stage", df)
            self.con.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"SELECT {', '.join(columns)} FROM _stage ON CONFLICT DO NOTHING"
            )
            self.con.unregister("_stage")
        return len(df)

    def upsert_trades(self, df: pd.DataFrame) -> int:
        return self._upsert_df("trades", df, TRADE_COLUMNS)

    def upsert_markets(self, df: pd.DataFrame) -> int:
        """Markets change (they resolve), so replace rather than ignore."""
        if df.empty:
            return 0
        df = df.reindex(columns=MARKET_COLUMNS)
        with self._lock:
            self.con.register("_stage_m", df)
            self.con.execute(
                "DELETE FROM markets WHERE condition_id IN (SELECT condition_id FROM _stage_m)"
            )
            self.con.execute(
                f"INSERT INTO markets ({', '.join(MARKET_COLUMNS)}) "
                f"SELECT {', '.join(MARKET_COLUMNS)} FROM _stage_m"
            )
            self.con.unregister("_stage_m")
        return len(df)

    def upsert_market_tags(self, df: pd.DataFrame) -> int:
        return self._upsert_df(
            "market_tags", df, ["condition_id", "tag_id", "tag_label", "tag_slug"]
        )

    def upsert_tags(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        df = df.reindex(columns=["tag_id", "label", "slug"])
        with self._lock:
            self.con.register("_stage_t", df)
            self.con.execute(
                "DELETE FROM tags WHERE tag_id IN (SELECT tag_id FROM _stage_t)"
            )
            self.con.execute(
                "INSERT INTO tags (tag_id, label, slug) SELECT tag_id, label, slug FROM _stage_t"
            )
            self.con.unregister("_stage_t")
        return len(df)

    def upsert_users(self, df: pd.DataFrame) -> int:
        """Keep the widest seen name and the widest first/last-seen span."""
        if df.empty:
            return 0
        df = df.reindex(columns=["proxy_wallet", "name", "pseudonym", "first_seen", "last_seen"])
        with self._lock:
            self.con.register("_stage_u", df)
            self.con.execute(
                """
                INSERT INTO users (proxy_wallet, name, pseudonym, first_seen, last_seen)
                SELECT proxy_wallet, any_value(name), any_value(pseudonym),
                       min(first_seen), max(last_seen)
                FROM _stage_u GROUP BY proxy_wallet
                ON CONFLICT (proxy_wallet) DO UPDATE SET
                    name = coalesce(nullif(excluded.name, ''), users.name),
                    pseudonym = coalesce(nullif(excluded.pseudonym, ''), users.pseudonym),
                    first_seen = least(users.first_seen, excluded.first_seen),
                    last_seen = greatest(users.last_seen, excluded.last_seen)
                """
            )
            self.con.unregister("_stage_u")
        return len(df)

    def log_window(
        self, condition_id: str, start: int, end: int, n: int, truncated: bool, now: int
    ) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO ingest_log VALUES (?,?,?,?,?,?) "
                "ON CONFLICT (condition_id, window_start, window_end) DO UPDATE SET "
                "n_trades = excluded.n_trades, truncated = excluded.truncated, "
                "fetched_at = excluded.fetched_at",
                [condition_id, start, end, n, truncated, now],
            )

    def covered_windows(self, condition_id: str) -> set[tuple[int, int]]:
        df = self.query(
            "SELECT window_start, window_end FROM ingest_log "
            "WHERE condition_id = ? AND truncated = FALSE",
            [condition_id],
        )
        return set(zip(df.window_start.tolist(), df.window_end.tolist()))

    # -- convenience --------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        return {
            "markets": self.scalar("SELECT count(*) FROM markets") or 0,
            "resolved_markets": self.scalar(
                "SELECT count(*) FROM markets WHERE resolved"
            ) or 0,
            "trades": self.scalar("SELECT count(*) FROM trades") or 0,
            "users": self.scalar("SELECT count(DISTINCT proxy_wallet) FROM trades") or 0,
            "tags": self.scalar("SELECT count(*) FROM tags") or 0,
            "usd_volume": self.scalar("SELECT coalesce(sum(usd), 0) FROM trades") or 0.0,
            "ts_min": self.scalar("SELECT min(ts) FROM trades"),
            "ts_max": self.scalar("SELECT max(ts) FROM trades"),
        }

    def clear_trades(self) -> None:
        with self._lock:
            self.con.execute("DELETE FROM trades")
            self.con.execute("DELETE FROM ingest_log")

    def vacuum(self) -> None:
        with self._lock:
            self.con.execute("CHECKPOINT")
