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

The crawl runs markets concurrently, fetches pages in a doubling batch, moves
parsing and database writes to a worker thread, and negotiates **HTTP/2** so
requests multiplex down one connection. On a fixed 120-market slice that is
**45.2s → 19.5s**, with byte-identical output.

Two measurement notes, since both are easy to get wrong. Benchmarking HTTP/2 by
repeating a single URL suggested 22.6 → 57.9 req/s; against *distinct* URLs, as
a real crawl does, the honest figure is nearer 29 — the server caches, so
hammering one endpoint measures the cache. And the page ramp deliberately holds
at one for two rounds: nearly every market fits in a page or two, and
speculating on those costs a wasted request each while hundreds of markets are
already running in parallel.

Beyond roughly 25–30 req/s the API stops rewarding concurrency and starts adding
latency, so the remaining lever is **fewer requests** — `Min market volume` and
`Min trade USD` cut the crawl proportionally.

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

**Member** — stake and share of cluster, bets, shared bets, **shared %** (how
much of the wallet's own activity is spent alongside the cluster — high means
effectively dedicated to it, low means it merely overlaps), win rate, ROI, P&L,
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

**Saved setups** store every control on the panel under a name — sector ticks and
picked markets included, not just the numeric knobs — and reload on later runs.
They live in the local database, so they are personal to your machine and never
travel with the repo.

The mouse wheel deliberately does nothing to these fields. The panel is one tall
scroll area, and a wheel roll aimed at scrolling would otherwise retune whichever
control sat under the cursor, silently changing a filter you never touched.

## Views

- **Clusters** — ranked table with heat shading, then per-cluster: members, cluster
  bets, raw positions, **entry timeline** (one row per shared bet, one dot per
  member, gold ring = first in, size = stake, colour = entry price), and a
  **network** view (node size = stake, gold = lead mover, green/red = P&L).
- **Compare** — any subset of clusters as metrics-by-cluster, heat-shaded, with a
  bar chart for the selected metric, a **Rank by** metric deciding which clusters
  make the top N, and a spin box for N.
- **Watchlist** — see below.
- **Data & log** — database contents, ingest coverage per market, run log, and a
  row-limit selector up to unlimited.

Every table has free-text filtering, a right-click column picker, CSV export, and
context-menu links to the wallet or market on Polymarket.

---

## Watchlist

Click the **★** in the first column of the Clusters, Members, Cluster bets or Raw
positions table to keep something. The Watchlist tab tracks it from then on.

### Why nothing is keyed to a cluster id

A run is a snapshot of one window under one set of filters; change either and the
cluster numbering is meaningless. Wallets and `(market, outcome)` pairs are
permanent, so those are stored directly. A watched **cluster is stored as its
member set** and re-identified on later runs by Jaccard overlap — which also
yields the drift: who joined, who left, and how much of the group survived. In
testing, a cluster starred under one parameter set was found again after a re-run
that produced 18 clusters instead of 12, at 80% overlap with two members gone.

### Checking for updates

**Check for updates** follows every watched wallet across the whole of Polymarket
via the user-keyed activity endpoint — including markets your analysis universe
never covered — pulls metadata for anything new, refreshes the resolution status
of watched markets, then diffs against the previous snapshot and records:

| Event | Meaning |
| --- | --- |
| `new_position` | the wallet entered a bet it did not hold before |
| `added_to` | it materially increased an existing stake |
| `exited` | it closed a position it used to hold |
| `resolved` | a watched market settled, won or lost |
| `baseline` | first observation; there was nothing to compare against yet |

The first refresh of an item can only record a baseline, so expect no change
events until the second. Unseen events show as a count on the tab.

### What the trader table is for

These signals are computed from local data and do not depend on any run, so they
hold still while you change filters: stake, bets, markets, win rate, longshot win
rate, edge per share, median market volume (do they live in thin markets?), lead
time, and **Shadow %** — the largest share of this wallet's book that any single
other wallet also holds. A wallet mirroring most of another's positions is not a
coincidence, and unlike cluster membership it survives a change of parameters.

Watched clusters additionally show **cohesion**: the share of the group's bets
held by at least half its members.

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
.venv/Scripts/python scripts/smoke_test.py      # backend: ingest + cluster, real API
.venv/Scripts/python scripts/smoke_test.py --fresh     # force a re-crawl
.venv/Scripts/python scripts/ui_smoke.py        # builds every panel, writes screenshots/
.venv/Scripts/python scripts/sector_smoke.py    # first-run path on a wiped database
.venv/Scripts/python scripts/watchlist_smoke.py # star, re-identify, diff, persist
.venv/Scripts/python scripts/edge_case_smoke.py # degenerate inputs (see below)
```

`edge_case_smoke.py` covers the shapes that crash rather than the shapes that are
interesting: a universe of only **unresolved** markets, so every win-rate column
is entirely missing; a run that finds a **single cluster**, so there is no spread
to z-score against; and both together. Missing values arrive as pandas NA, whose
truth value *raises* instead of being falsey, so `not np.isfinite(x)` and
`x or 0` are both traps rather than the defaults they look like.

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
