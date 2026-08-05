# PolyClusters

Desktop app for finding **clusters of high-stake Polymarket wallets that repeatedly
take the same side of the same bets** — the signature of shared information,
coordinated books, or a single operator running many wallets.

Everything runs locally against Polymarket's public, unauthenticated APIs.
Python + Qt, so it runs on Windows, macOS and Linux from the same source.

## Getting started

Double-click **`run.bat`** on Windows, or run **`./run.sh`** on macOS and Linux.

The first launch builds a virtual environment, installs the dependencies, and —
on Windows — puts a **PolyClusters shortcut on your Desktop, in the Start Menu
and in the project folder**, all carrying the app icon. After that, launch from
the shortcut. Nothing else to set up.

Requires Python 3.11 or newer on PATH.

<details>
<summary>Manual setup, or moving the project afterwards</summary>

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m polyclusters
```

Shortcuts embed absolute paths, so they are generated per machine rather than
shipped, and are git-ignored. If you move the project, recreate them:

```bash
.venv/Scripts/python scripts/make_shortcut.py
```

`--project-only` or `--desktop-only` narrows where they go. Each one targets the
venv's `pythonw.exe`, so launching opens no console window.

</details>

---

## How it works

### 1 · Fetch

Markets are discovered through the Gamma API, which carries the `tags` array the
app exposes as **sectors**. Trades come from the Data API (`/trades`).

Discovery uses `/events/keyset` with `after_cursor`, not `/events` with `offset`:
the offset endpoint returns **HTTP 422** (`offset too large, use /events/keyset
for deeper pagination`) beyond roughly 2,000 rows, which any broad sweep reaches
immediately. Note also that `/events` spells its volume filter `volume_min` while
`/markets` uses `volume_num_min` — pass the wrong one and it is silently ignored
rather than rejected.

The Data API caps `offset` at 10,000, which alone would truncate any busy market to
its most recent ~10k trades. It does honour undocumented `start` / `end` epoch
parameters, so the crawler walks each market's lifetime in time slices and
recursively bisects any slice dense enough to hit the ceiling. Coverage is
recorded per `(market, window)`, so re-running only fetches the gaps.

Data lands in DuckDB (`%LOCALAPPDATA%\PolyClusters\polyclusters.duckdb` on Windows).

### 2 · Analyse

Trades collapse into net positions per `(wallet, market, outcome)`. That triple is
a **bet key** — two wallets "agree" only when they hold the same market *and* the
same side, so opposing traders never register as concordant.

Wallets are then filtered by stake and selectivity, and every surviving pair is
scored on cosine similarity over an **IDF-weighted** position vector. Rarity
weighting is the core idea: thousands of wallets bought Trump-2024-Yes, so that
shared position is worthless as evidence, while a shared position in an obscure
low-volume market is strong. Pairs that repeatedly enter *within minutes of each
other* get a synchronicity boost. Communities come from Louvain over the
resulting graph.

### The noise problem, and what handles it

Naively, the biggest and "best-performing" cluster is always the same artefact:
hundreds of wallets buying the 98¢ favourite, winning ~100% of the time, for ~0.5%
return. They agree because arithmetic makes them agree. Four things suppress it:

| Control | Effect |
| --- | --- |
| **Entry price band** (default 0.03–0.92) | drops near-certain fills entirely |
| **Max bet popularity** (default 0.20) | bets held by most of the pool are treated as stopwords |
| **Max bets per wallet** | excludes market makers and spray bots |
| **Longshot metrics** | win rate and ROI restricted to sub-50¢ entries |

Read a high win rate *only* alongside ROI, edge/share and longshot win rate.

---

## Metrics

**Cluster** — members, distinct bets, total bets made, shared bets (2+ members),
unanimous bets (every member), unanimity rate, concordance, win rate (count,
dollar-weighted, shared-only, unanimous-only, longshot-only), ROI in the same
cuts, P&L, edge per share, entry vs market VWAP, earliness, entry spread, sync
rate, price spread, bet rarity, average pairwise similarity, graph density, stake
concentration.

**Member** — stake and share of cluster, bets, shared bets, win rate, ROI, P&L,
**first-mover count and rate**, average entry rank, lead time vs the cluster
median, **entry price vs the cluster's average and vs the market VWAP**,
earliness, similarity to the rest of the cluster, biggest-bettor and lead-mover
flags.

**Bet** — members in it, coverage, unanimity, stake, average/best/worst entry,
price spread, **first entrant and their price**, **biggest bettor**, entry spread
in hours, resolution, ROI, edge per share, rarity, market volume.

### Suspicion score

Every component is robust-z-scored (median/MAD) so a dollar metric cannot swamp a
fraction, combined with the weights in the left panel, then damped by sample size
so a two-wallet pair with two lucky bets cannot top the table. **Moving the weight
sliders re-ranks instantly without re-running the analysis.**

---

## Filters

- **Time window** — presets from 24 hours to a year, or a custom UTC range.
- **Sectors** — searchable multi-select over the full ~6,000-tag catalogue. The
  catalogue is downloaded automatically on first launch, so sectors are
  selectable *before* the first fetch; the headline sectors (Politics,
  Geopolitics, World, Elections, Economy, Fed, Crypto, …) are pinned above the
  rest. **Typing in the search box does not scope the run — you must tick.**
  Press Enter or **Select matching** to tick everything the search finds. With
  nothing ticked the app asks for confirmation before sweeping every sector.
  Selections are remembered between sessions.
- **Specific markets** — search by name, or pick from the local database, in two modes:
  - **Seed** *(use this for a single bet)* — take everyone who traded the selected
    market, then score their similarity across the whole universe.
  - **Restrict** — analyse only the selected markets. Note that restricting to one
    market cannot cluster anything, since two wallets could share at most one bet.
- **Wallet** — minimum stake (the "rich" gate), min/max bets, minimum position
  size, entry-price band.
- **Clustering** — min shared bets, similarity threshold, resolution, minimum
  cluster size, detection method, bet-popularity cap, sync window, core fraction,
  IDF on/off, size weighting.

## Views

- **Clusters** — ranked table with heat shading, then per-cluster: members, cluster
  bets, raw positions, **entry timeline** (one row per shared bet, one dot per
  member, gold ring = first in, size = stake, colour = entry price), and a
  **network** view (node size = stake, gold = lead mover, green/red = P&L).
- **Compare** — any subset of clusters as metrics-by-cluster, heat-shaded, with a
  bar chart for the selected metric.
- **Data & log** — database contents, ingest coverage per market, run log.

Every table has free-text filtering, a right-click column picker, CSV export, and
context-menu links to the wallet or market on Polymarket.

---

## Practical notes

- **Start narrow.** One sector over 30 days to gauge crawl time before widening.
  Trade volume, not market count, drives runtime.
- **Set `Min market volume` before a big fetch** — it is the cheapest way to cut
  the crawl.
- An unfiltered 30-day window matches roughly **28,000 markets**, which would
  crawl for hours. `Max markets per fetch` (default 1,500) keeps only the
  highest-volume slice; markets you picked explicitly are always fetched.
- Cluster granularity follows the size of the universe. A narrow universe has few
  distinct bets, so everyone overlaps and clusters come out large. Pick a sector,
  or raise the market cap, to get tighter groups.
- Re-fetching the same window is nearly free; only uncovered gaps are requested.
- Win rate and ROI need resolutions. For measured performance tick **Resolved
  markets only**; leave it off when hunting open positions to copy.
- Corporate TLS proxies are handled via `truststore`, which reads the OS
  certificate store instead of certifi's bundle.

## Development

```bash
.venv/Scripts/python scripts/smoke_test.py    # backend: ingest + cluster, real API
.venv/Scripts/python scripts/smoke_test.py --fresh   # force a re-crawl
.venv/Scripts/python scripts/ui_smoke.py      # builds every panel, writes screenshots/
.venv/Scripts/python scripts/sector_smoke.py  # first-run path on a wiped database
```

The tests run against the live API rather than mocks. Every hard problem here
came from the API behaving unlike its documentation, and a mock would have
faithfully reproduced the documentation.

Layout: `core/` storage · `ingest/` API clients and crawler · `analysis/`
positions, similarity, clustering, metrics · `ui/` Qt panels and widgets.

## License

MIT — see [LICENSE](LICENSE).

---

*Clusters are statistical associations, not proof of wrongdoing. Co-betting can
equally reflect a shared newsletter, a public thesis, one person's several
wallets, or coincidence. Treat output as leads to investigate.*
