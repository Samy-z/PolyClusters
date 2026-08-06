"""Application-wide configuration and filesystem locations."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

APP_NAME = "PolyClusters"
# Incremented when a stored setting must be re-derived from the new default.
SETTINGS_VERSION = 2
LEGACY_APP_NAME = "PolyCluster"  # pre-rename; existing data is migrated across

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
LOGO_DISPLAY = ASSETS_DIR / "PolyClusters_logo_display.png"
LOGO_DISPLAY_DARK = ASSETS_DIR / "PolyClusters_logo_display_dark.png"
LOGO_ICON = ASSETS_DIR / "PolyClusters_logo_icon.png"
APP_ICO = ASSETS_DIR / "PolyClusters.ico"

# Distinguishes the app from generic "python.exe" so Windows groups its taskbar
# button under our own icon rather than the interpreter's.
WINDOWS_APP_ID = "PolyClusters.Desktop.1"

# --- API hosts -------------------------------------------------------------
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Hard server-side limits discovered by probing the public API.
TRADES_MAX_LIMIT = 500
TRADES_MAX_OFFSET = 10_000  # /trades refuses offsets beyond this
ACTIVITY_MAX_OFFSET = 5_000  # /activity refuses offsets beyond this
# Gamma silently clamps any requested page size down to 100.
GAMMA_MAX_LIMIT = 100

POLYMARKET_EPOCH = 1_600_000_000  # ~Sep 2020, safely before any market existed


def _base_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    if os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def data_dir() -> Path:
    """Per-user writable directory for the database and cached artifacts."""
    d = _base_dir() / APP_NAME
    if not d.exists():
        _migrate_legacy_data(d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _migrate_legacy_data(target: Path) -> None:
    """Carry a pre-rename store over instead of silently starting empty.

    The old name put the database under PolyCluster/polycluster.duckdb; without
    this an existing multi-gigabyte crawl would be orphaned and re-fetched.
    """
    legacy = _base_dir() / LEGACY_APP_NAME
    if not legacy.is_dir():
        return
    try:
        legacy.rename(target)
    except OSError:
        return  # in use or cross-device: fall through to a fresh directory
    for old in target.glob("polycluster.duckdb*"):
        renamed = old.with_name(old.name.replace("polycluster.", "polyclusters.", 1))
        try:
            old.rename(renamed)
        except OSError:
            pass


def db_path() -> Path:
    return data_dir() / "polyclusters.duckdb"


def settings_path() -> Path:
    return data_dir() / "settings.json"


@dataclass
class AppSettings:
    """User-tunable knobs persisted between runs."""

    # Networking. Measured against the live API with *distinct* URLs (hammering
    # one URL reads far faster because the server caches it, which flatters the
    # numbers): throughput climbs to roughly 25-30 req/s and then flattens into
    # noise, so these sit at the knee rather than chasing a higher figure that
    # only invites throttling.
    max_concurrency: int = 12
    requests_per_second: float = 30.0
    request_timeout: float = 45.0
    max_retries: int = 4

    # Ingestion defaults
    min_market_volume: float = 25_000.0
    min_trade_usd: float = 0.0
    window_hours: int = 24  # time-slice width used to beat the offset cap
    # An unfiltered 30-day sweep discovers ~28k markets; crawling all of them
    # would run for hours. Keep the highest-volume slice unless told otherwise.
    max_markets_per_fetch: int = 1_500

    # Analysis defaults
    min_user_usd: float = 5_000.0
    min_user_bets: int = 2
    max_user_bets: int = 400
    min_shared_bets: int = 2
    similarity_threshold: float = 0.35
    louvain_resolution: float = 1.4
    min_cluster_size: int = 2
    max_users: int = 25_000
    timing_window_hours: float = 6.0
    unanimity_core_pct: float = 0.75
    # Entry-price band. Excluding near-certain fills is what stops the whole
    # population collapsing into one "everybody bought the favourite" cluster.
    min_entry_price: float = 0.03
    max_entry_price: float = 0.92
    max_bet_user_frac: float = 0.20
    min_position_usd: float = 0.0

    # Suspicion score weights
    weight_winrate: float = 1.0
    weight_roi: float = 1.0
    weight_unanimity: float = 1.0
    weight_sync: float = 1.0
    weight_earliness: float = 1.0
    weight_rarity: float = 1.0
    weight_wealth: float = 0.5

    # Panel collapsed to its strip; restored on the next launch.
    controls_collapsed: bool = False

    # Remembered sector picks, so a scoped run stays scoped between sessions.
    selected_tag_ids: list[int] = field(default_factory=list)

    # Bumped when a stored default becomes wrong rather than merely different.
    settings_version: int = SETTINGS_VERSION

    def save(self) -> None:
        settings_path().write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> "AppSettings":
        p = settings_path()
        if not p.exists():
            return cls()
        try:
            raw: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()

        # A saved file pins every value it holds, so tuning a default does
        # nothing for anyone who has already run the app once. Networking is
        # the case that matters: leaving the old rate in place would keep the
        # crawl at a third of its speed with no way for the user to know why.
        if int(raw.get("settings_version", 1)) < SETTINGS_VERSION:
            for stale in ("max_concurrency", "requests_per_second"):
                raw.pop(stale, None)
            raw["settings_version"] = SETTINGS_VERSION

        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class AnalysisFilters:
    """Everything that defines the slice of the world a run looks at."""

    start_ts: int = 0
    end_ts: int = 0
    tag_ids: list[int] = field(default_factory=list)
    condition_ids: list[str] = field(default_factory=list)
    event_slugs: list[str] = field(default_factory=list)
    min_market_volume: float = 0.0
    resolved_only: bool = False
    exclude_tag_ids: list[int] = field(default_factory=list)
    # Wallets that traded these markets anchor the run, but their similarity is
    # still scored across the whole universe. Restricting the universe to a
    # single market instead would make clustering impossible - two wallets
    # could share at most one bet.
    seed_condition_ids: list[str] = field(default_factory=list)

    def describe(self) -> str:
        bits = []
        if self.seed_condition_ids:
            bits.append(f"seeded on {len(self.seed_condition_ids)} market(s)")
        if self.condition_ids:
            bits.append(f"{len(self.condition_ids)} market(s)")
        if self.tag_ids:
            bits.append(f"{len(self.tag_ids)} sector(s)")
        if self.min_market_volume:
            bits.append(f"vol>=${self.min_market_volume:,.0f}")
        if self.resolved_only:
            bits.append("resolved only")
        return ", ".join(bits) or "everything ingested"

    def discovery_condition_ids(self) -> list[str]:
        """Markets the ingester must fetch regardless of which mode is active."""
        return list(dict.fromkeys(self.condition_ids + self.seed_condition_ids))
