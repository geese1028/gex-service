# gex-service

IBKR-backed gamma exposure (GEX) backend. Pulls an option chain through IB
Gateway for any symbol on demand, computes the GEX profile, **zero gamma**,
**call wall** and **put wall**, and serves them over REST and WebSocket.

Python 3.11+, [NautilusTrader](https://nautilustrader.io/docs/latest/developer_guide/) IB adapter, FastAPI, numpy/scipy, SQLite. Single asyncio process.

## How it works

```
Frontend --REST/WS--> FastAPI --> Scheduler (watch list, cache, 90 s refresh)
                                     |
                                 ChainFetcher
                                     |
                    Nautilus HistoricInteractiveBrokersClient
                    + InteractiveBrokersInstrumentProvider
                                     |
                                 IB Gateway (127.0.0.1:4002)
                                     |
                                 compute_gex --> in-memory result + SQLite snapshots
```

The service does **not** open its own TWS socket. It uses the NautilusTrader
IB adapter as documented in the [developer guide](https://nautilustrader.io/docs/latest/developer_guide/)
and [Interactive Brokers integration](https://nautilustrader.io/docs/latest/integrations/interactive_brokers/):
`HistoricInteractiveBrokersClient`, `InteractiveBrokersInstrumentProvider`,
`IBContract(build_options_chain=True)`, `subscribe_market_data`, and
`request_bars`. During RTH this process is the only IBKR data consumer on the
Gateway; defaults follow the adapter (`client_id=1`, paper port `4002`) and
IB's own simultaneous-line ceiling (100), not a reserved band for other apps.

Per symbol fetch (`src/gex_service/chain.py`):

1. Resolve the underlying with Nautilus `request_instruments` (`STK` on SMART, else `IND`; SPX, NDX, VIX, RUT, XSP, DJX, OEX are pre-mapped).
2. Read spot (`last`, falling back to `close` / mid) via Nautilus market-data subscribe.
3. Load the chain the adapter way: `IBContract(..., build_options_chain=True, min_expiry_days=0, max_expiry_days=max_dte)` (or `lastTradeDateOrContractMonth` when `expiries=` is set). Trading classes are merged, so SPX includes both `SPX` monthlies and `SPXW` weeklies/0DTE.
4. Keep strikes within `spot * (1 ± strike_range_pct)`; cap at `max_contracts` by dropping the strikes furthest from spot symmetrically.
5. Subscribe in batches of 100 (`GEX_MAX_MD_LINES`) with generic ticks `100,101,106` (volume, open interest, model greeks), wait up to 3 s per batch, cancel, repeat. Historical ADV / IV / HV go through Nautilus `request_bars`, spaced by IB pacing (`GEX_HISTORICAL_REQUEST_DELAY_S=1`).

GEX math (`src/gex_service/gex.py`):

- Per contract: `gex = gamma * OI * multiplier * spot^2 * 0.01` (dollars per 1 % move), calls positive, puts negative (dealer long-call / short-put convention).
- `gamma` is IBKR's model gamma when present; otherwise Black-Scholes from the IV, borrowing the same-strike opposite right's IV if a contract has none (common for deep ITM).
- **Call wall** = strike with the largest call GEX; **put wall** = strike with the most negative put GEX. OI-based variants (`call_wall_oi`, `put_wall_oi`) are returned too.
- **Zero gamma** = re-price every contract's gamma with Black-Scholes across a grid of hypothetical spot levels (±10 %, 161 points), sum net GEX per level, take the negative→positive crossing nearest the current spot. The whole curve is returned as `gamma_curve`. Falls back to the strike profile crossing if no IV is available.
- Also: `by_expiry` sub-profiles (use `expiries=YYYYMMDD` for a pure 0DTE view), `max_pain`, total OI and put/call OI ratio.

### Beyond the walls: companion analytics

The response also carries the analytics that the academic literature and the
practitioner dashboards (SpotGamma, SqueezeMetrics-style models) layer on top
of the basic GEX profile:

| Block | Fields | What it is |
| --- | --- | --- |
| `hedge_flow` | `shares_per_1pct`, `pct_adv_per_1pct`, `adv_shares`, `regime`, `distance_to_zero_gamma_pct` | Dealer delta-hedging flow for a 1 % move, in shares and as % of the 21-day average daily volume. `pct_adv_per_1pct` is the **gamma imbalance Γ^IB of Barbon & Buraschi (2021)**: the fraction of a day's volume that hedgers must trade per 1 % move. Negative-gamma regimes are associated with intraday momentum, higher realised volatility and flash-crash risk; positive-gamma regimes with mean reversion. The effect scales with illiquidity, which is why the normalisation by ADV matters more than the raw dollar number. |
| `exposures` | `dex`, `dex_shares`, `vex`, `cex`, `vanna_flip`, `net_gex_up_1pct`, `net_gex_down_1pct` | Dealer **delta** (dollar and shares; dealers hedge by shorting `dex_shares`), **vanna** exposure (dollar delta change per +1 IV point; the vol-up/spot-down and vol-crush rally engine), **charm** exposure (dollar delta decay per calendar day; end-of-day and OPEX drift), the spot level where net vanna flips sign, and the net GEX one percent above/below spot from the Black-Scholes curve. |
| `volume_lens` | `total_gex`, `call_wall`, `put_wall`, `zero_gamma`, volumes | The same GEX machinery weighted by **today's volume** instead of settled OI. Settled OI is a prior-night snapshot; volume shows where positioning is being built today. The flip from this lens typically sits closer to spot. |
| `implied_move` | `straddle_price`, `straddle_move_pct`, `atm_iv`, `iv_move_to_expiry_pct`, `iv_daily_move_pct` | Expected move from the nearest expiry's ATM straddle and from ATM IV. Lets the frontend draw the expected range against the walls. |
| `concentration` | `absolute_gamma_strike`, `top_strikes`, `gex_hhi`, `zero_dte_share` | The **absolute gamma strike** (largest total gamma regardless of sign, a classic magnet/pin candidate), the top five strikes, a Herfindahl index of how concentrated gamma is, and the share of gamma sitting in 0DTE contracts. |
| `zero_dte` | walls, `zero_gamma`, `regime`, `in_walls`, hedge shares to each wall | The book that expires today, separate from the full chain. Positive gamma inside the walls is a fade; negative gamma is a chase. The level dies at 16:00 ET. |
| `charm_clock` | `shares_to_close`, `direction`, `pin_strike` | Shares dealers buy (positive) or sell into 16:00 ET if spot stays here, from repricing delta at the close. `pin_strike` is where that hedge concentrates. |
| `vanna_play` | `shares_if_iv_down_1pt`, `shares_if_iv_up_1pt`, `vanna_flip` | Dealer share hedge for a one-point IV move, and the spot where that sign flips. A crush that makes dealers buy is a tailwind for longs. |
| `roll_off` | `drop_nearest`, `drop_week` | Profile after the nearest expiry (and everything ≤ 7 DTE) drops off. Same numbers as `?exclude_expiries=`. |
| `iv_context` | `iv30`, `iv30_rank_1y`, `iv30_percentile_1y`, `hv30`, `iv_hv_spread` | IBKR 30-day implied and historical vol (1 year of daily bars). |
| `gamma_curve[].net_vex`, `profile[].net_dex/net_vex/net_cex/net_gex_volume`, `by_expiry[].vex/cex/abs_gex_share` | | Per-level and per-strike breakdowns of the above for heatmaps. |

Formulas (dealer long calls / short puts, sign `s = +1` calls, `-1` puts):

```
GEX = Σ s · Γ · OI · mult · S² · 0.01            ($ per 1 % move)
DEX = Σ s · Δ · OI · mult · S                    ($ delta)
VEX = Σ s · vanna · OI · mult · S · 0.01         ($ delta per +1 vol point)
CEX = Σ s · charm / 365 · OI · mult · S          ($ delta per calendar day)
Γ^IB = (GEX / S) / ADV_21d · 100                 (% of daily volume per 1 % move)
```

Greeks come from IBKR's model when present, otherwise Black-Scholes with the
contract's IV (continuous dividend yield `q`, rate `r` from settings).

References: Barbon & Buraschi, *Gamma Fragility* (2021); Ni, Pearson,
Poteshman & White, *Does option trading have a pervasive impact on underlying
stock prices?* (JFE 2021); Baltussen, Da, Lammers & Martens, *Hedging demand
and market intraday momentum* (JFE 2021); SpotGamma's documentation of the
Call/Put Wall, Zero Gamma, Absolute Gamma Strike and Volatility Trigger
(the latter is proprietary and not reproduced here; the closest open
analogue is the zero gamma from the Black-Scholes curve).

Everything is still an inference: public OI does not say who holds which
side. The dealer-short-puts / long-calls convention is robust for index
products and liquid names, weaker for single stocks around events.

### Data caveats

- **Open interest from IBKR is the prior session's settlement value**, updated once a day. The response carries `oi_asof`. Intraday changes in GEX therefore come from spot / IV / gamma, not from OI.
- Market data lines are an IB account limit. This service uses up to `GEX_MAX_MD_LINES` (100) simultaneous `reqMktData` subscriptions, then cancels and takes the next batch. Watched symbols refresh serially so one fetch finishes before the next starts.
- The account needs OPRA (US options) market data. Outside RTH the service switches to frozen data (`GEX_AUTO_FROZEN`).
- IB publishes no model greeks for some deep ITM / illiquid contracts and no OI tick for contracts with zero OI; those rows are counted in `meta.dropped_contracts`. Both contribute ~0 GEX anyway.

Measured against the production Gateway on 2026-10-05 (RTH): first SPY fetch with 992 contracts took ~97 s (36 s contract details + 59 s market data); subsequent refreshes ~20 s; SPX 0DTE with 182 contracts ~13 s of market data.

## API

All `/api/v1/*` routes accept an optional `X-API-Key` header (or `?api_key=`) when `GEX_API_KEY` is set.

| Route | Purpose |
| --- | --- |
| `GET /health` | Gateway connection, market data type, lines in use, watched symbols, RSS |
| `GET /api/v1/gex/{symbol}` | Full GEX payload. Query: `max_dte`, `strike_range_pct`, `max_contracts`, `expiries=YYYYMMDD,...`, `exclude_expiries=YYYYMMDD,...` (OPEX roll-off, from cache), `refresh=true`, `wait=false` |
| `GET /api/v1/gex/{symbol}/chain` | Per-contract rows (quotes, IV, gamma, OI, per-contract GEX) |
| `GET /api/v1/gex/{symbol}/history?from=&to=&limit=` | Intraday snapshot summaries from SQLite |
| `GET /api/v1/gex/{symbol}/scenarios` | Spot × IV × time hedging-pressure grid (`spot_pct`, `spot_steps`, `iv_points`, `iv_steps`, `days=0,1`) |
| `GET /api/v1/gex/{symbol}/surface` | IV smile / 25Δ RR / butterfly / term structure + 1y IV rank / HV |
| `GET /api/v1/gex/{symbol}/drift?date=` | Intraday movement of walls, zero gamma, Γ^IB and regime |
| `GET /api/v1/gex/{symbol}/eod` | Archived end-of-day rows |
| `GET /api/v1/gex/{symbol}/realized?days=` | 5-minute realized vol, lag-1 autocorr, last-30 vs rest-of-day |
| `GET /api/v1/gex/{symbol}/validation?days=` | Next-day realized vol / autocorr vs yesterday's Γ^IB (Barbon–Buraschi style) |
| `GET /api/v1/gex/{symbol}/backtest?days=` | Call/put wall hold rates, OI-wall comparison, zero-gamma side persistence |
| `GET /api/v1/gex/{symbol}/oi-estimate` | Intraday OI = settlement OI + `opening_ratio` × today's volume, plus next-day reconcile |
| `GET /api/v1/gex/{symbol}/features?date=` | One flat feature row (live or archived EOD) for a research catalog |
| `GET /api/v1/features/export` | CSV/JSON of archived EOD features (`symbols=`, `days=`, `format=`) |
| `GET /api/v1/scan` | Cross-section of watched (or `symbols=`) names, sortable by Γ^IB / 0DTE share / HHI / … |
| `GET /api/v1/complex` / `GET /api/v1/complex/{SPX\|NDX\|RUT\|VIX}` | Index-complex GEX (members mapped to the anchor by spot ratio) |
| `PUT/GET/DELETE /api/v1/flow/{symbol}` | Lee-Ready flow GEX on near-ATM nearest-expiry contracts (budgeted MD lines) |
| `GET /api/v1/alerts` / `PUT /api/v1/alerts/{symbol}/config` | Edge-triggered alerts; optional `GEX_ALERT_WEBHOOK_URL` |
| `POST /api/v1/eod/archive` | Force today's EOD archive (normally automatic after 16:05 ET) |
| `GET /api/v1/watch` / `PUT /api/v1/watch/{symbol}` / `DELETE /api/v1/watch/{symbol}` | Pin symbols for continuous refresh (same query params as the GEX route) |
| `WS /ws/gex/{symbol}` | Sends the cached payload on connect, then every refresh |
| `WS /ws/alerts` | Streams alert events |

`GET /api/v1/gex/{symbol}` blocks until the first fetch finishes (up to `GEX_FETCH_TIMEOUT_S`, then 504). Pass `wait=false` to get `202 {"status":"pending"}` immediately and poll. Any symbol requested is auto-watched and refreshed every `GEX_REFRESH_INTERVAL_S` until nobody has asked for it for `GEX_IDLE_TTL_S`; pinned symbols (via `PUT /watch`) and symbols with open WebSockets are never evicted.

Response shape (abridged):

```json
{
  "symbol": "SPY", "sec_type": "STK", "spot": 770.68, "ts": "...", "oi_asof": "2026-10-02",
  "params": {"max_dte": 10, "strike_range_pct": 0.04, "max_contracts": 1500, "expiries": [], "risk_free_rate": 0.045, "dividend_yield": 0.0},
  "summary": {
    "total_gex": 4.24e8, "call_gex": 6.98e9, "put_gex": -6.55e9,
    "zero_gamma": 770.29, "zero_gamma_method": "bs_grid",
    "call_wall": 772.0, "put_wall": 767.0, "call_wall_oi": 785.0, "put_wall_oi": 767.0,
    "max_pain": 769.0, "total_call_oi": 447464, "total_put_oi": 383224, "put_call_oi_ratio": 0.856,
    "curve_gex_at_spot": 5.62e8
  },
  "exposures": {"dex": ..., "dex_shares": ..., "vex": ..., "cex": ..., "vanna_flip": ..., "net_gex_up_1pct": ..., "net_gex_down_1pct": ...},
  "hedge_flow": {"shares_per_1pct": ..., "pct_adv_per_1pct": 0.75, "adv_shares": ..., "regime": "positive_gamma", "distance_to_zero_gamma_pct": ..., "direction_note": "..."},
  "volume_lens": {"total_gex": ..., "call_wall": ..., "put_wall": ..., "zero_gamma": ..., "total_call_volume": ..., "total_put_volume": ..., "put_call_volume_ratio": ...},
  "implied_move": {"expiry": "20261009", "dte": 4.2, "atm_strike": 73.0, "straddle_price": 4.59, "straddle_move_pct": 6.29, "atm_iv": 0.73, "iv_move_to_expiry_pct": 7.86, "iv_daily_move_pct": 4.59},
  "concentration": {"absolute_gamma_strike": 80.0, "top_strikes": [80.0, 70.0, 75.0], "gex_hhi": 0.12, "zero_dte_share": 0.0},
  "profile": [{"strike": 740.0, "call_gex": ..., "put_gex": ..., "net_gex": ..., "call_oi": ..., "put_oi": ..., "call_volume": ..., "put_volume": ..., "net_gex_volume": ..., "net_dex": ..., "net_vex": ..., "net_cex": ...}],
  "gamma_curve": [{"level": 693.6, "net_gex": ..., "net_vex": ...}],
  "by_expiry": [{"expiry": "20261005", "dte": 0.26, "total_gex": ..., "call_gex": ..., "put_gex": ..., "call_wall": 772.0, "put_wall": 769.0, "contracts": 98}],
  "meta": {"contracts_total": 992, "contracts_used": 543, "dropped_contracts": 449, "fetch_duration_s": 96.9, "data_age_s": 1.2, "market_data_type": 1, "spot_source": "last", "stale": false, "warnings": []}
}
```

`zero_gamma_method` is one of `bs_grid`, `bs_grid_no_flip` (no crossing within ±10 %, `zero_gamma` is null), `strike_profile`, `none`. The payload also includes `roll_off` (post-expiry book) and `iv_context` (30-day IV rank).

Offline catalog dump without the service running:

```sh
uv run gex-export --db data/gex.sqlite --symbols SPY,QQQ --days 250 > features.csv
```

Feed that CSV into a research pipeline (or a Nautilus/catalog writer) as daily features: Γ^IB, walls, zero-gamma distance, 0DTE share, IV rank, roll-off, etc.

What this backend does **not** do (and why):

- **SpotGamma Volatility Trigger**: proprietary, no public definition. The open analogue is the Black-Scholes `zero_gamma` plus `hedge_flow.distance_to_zero_gamma_pct`.
- **True dealer inventory**: public OI has no participant type. Flow GEX is Lee-Ready on a handful of near-ATM contracts, not a book reconstruction.
- **CME futures options (ES/NQ)**: different IBKR `secType` / exchange path; index complexes use the cash/ETF overlay (SPX+SPY+XSP, NDX+QQQ, RUT+IWM, VIX).
- **LETF rebalance flow**: needs external AUM, not in IBKR.

## Local development

If IB Gateway runs on another host and only trusts `127.0.0.1` (the default
`TrustedIPs` setting), develop against it through an SSH tunnel:

```sh
uv sync --all-extras
cp .env.example .env            # set GEX_IB_PORT=14002 for the tunnel
ssh -N -L 14002:127.0.0.1:4002 user@gateway-host &
uv run gex-service              # http://127.0.0.1:8090/docs
curl 'http://127.0.0.1:8090/api/v1/gex/SPY?max_dte=10&strike_range_pct=0.04'
uv run pytest
```

Use a `GEX_IB_CLIENT_ID` that is unique for this process (Nautilus default is 1).

## Deploying next to IB Gateway (macOS, launchd)

The service is designed to run on the same machine as IB Gateway under
launchd, as the login user that owns the Gateway. It is light enough for a
small box (idles around 75-135 MB RSS). No Docker required.

1. Tag a release (`git tag v0.1.0 && git push --tags`).
2. On the Gateway host:

   ```sh
   git clone https://github.com/<you>/gex-service.git ~/gex-service   # first time
   cd ~/gex-service && git fetch --tags && git checkout v0.1.0
   deploy/bootstrap_venv.sh        # uses uv if present, else any Python >= 3.11 (PYTHON=/path/to/python3 to pin)
   cp .env.example .env            # GEX_IB_PORT=4002 (paper) or 4001 (live), GEX_API_HOST=127.0.0.1
   deploy/gex_ctl.sh install       # renders ~/Library/LaunchAgents/com.gex.service.plist and starts it
   deploy/gex_ctl.sh status
   ```

3. Upgrades: `git fetch --tags && git checkout vX.Y.Z && deploy/bootstrap_venv.sh && deploy/gex_ctl.sh restart`.
4. Logs: `deploy/gex_ctl.sh logs` (files under `deploy/runtime/`).

The API binds to `127.0.0.1:8090` by default. From the frontend machine either
tunnel in (`ssh -N -L 8090:127.0.0.1:8090 user@gateway-host`) or set
`GEX_API_HOST=0.0.0.0` together with `GEX_API_KEY` and a reverse proxy.

Keep an eye on `rss_mb` in `/health`.

## Copyright

gex-service is Copyright (c) 2026 geese1028 and is released under the MIT License (`LICENSE`).

The IBKR session is the [NautilusTrader](https://nautilustrader.io/) Interactive Brokers adapter (`nautilus_trader`), Copyright (c) 2015–2026 Nautech Systems Pty Ltd, licensed under the [GNU Lesser General Public License v3.0](https://github.com/nautechsystems/nautilus_trader/blob/master/LICENSE). This repository depends on that published package. It does not vendor NautilusTrader source and does not relicense it. See `NOTICE`.

Interactive Brokers, IBKR, Trader Workstation, and IB Gateway are trademarks of Interactive Brokers LLC. This project is not affiliated with Interactive Brokers or Nautech Systems.

## Layout

```
src/gex_service/
  config.py      Settings (env prefix GEX_)
  ib_client.py   Nautilus HistoricInteractiveBrokersClient session, reconnect, IB line/hist pacing
  chain.py       underlying/chain discovery, batched market data, ADV, IV/HV history
  greeks.py      vectorised Black-Scholes gamma / delta / vanna / charm
  gex.py         GEX aggregation, walls, zero-gamma grid, roll-off, companion exposures
  scenarios.py   spot × IV × time hedging-pressure surface
  surface.py     IV smile, 25Δ risk reversal, butterfly, IV rank
  analytics.py   drift, realized, validation, backtest, scan, complex, OI estimate, features
  flow.py        Lee-Ready RTVolume flow GEX (opt-in, line-budgeted)
  alerts.py      edge-triggered rules + webhook
  models.py      pydantic response models
  store.py       SQLite snapshots, EOD archive, alerts, OI reconcile
  scheduler.py   watch list, cache, serial refresh, EOD archive
  api.py / api_ext.py   FastAPI routes
  cli.py         gex-export (offline feature CSV)
  main.py        entry point
deploy/
  com.gex.service.plist.template, gex_ctl.sh, bootstrap_venv.sh
tests/
```
