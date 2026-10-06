---
name: gex-wall-read
description: >-
  Read gex-service call walls, put walls, and zero gamma for entries and exits.
  Use when the user asks about a GEX wall, call wall, put wall, zero gamma,
  whether a price level broke, wall migration, 0DTE, charm into the close,
  or vanna on a symbol served by gex-service.
---

# GEX wall read

Dealer convention in this service: long calls, short puts. Call wall is the strike with the largest call GEX. A wall only pressures price when the hedge is large versus average daily volume.

## What history is for

One live snapshot answers where the wall is now, how much gamma sits on it, and the hedge in shares per 1% move (`hedge_flow.shares_per_1pct`, `hedge_flow.pct_adv_per_1pct`).

History is required to say the wall moved, thinned, or was accepted. Open interest is the prior session (`oi_asof`). The OI call wall usually does not change until the next settle. Intraday, use the volume-weighted call wall (`volume_lens.call_wall`) as the new-positioning print. After `oi_asof` rolls forward, compare that day's call wall with the previous EOD row.

## Persistence

Running `gex-service` writes each refresh of a watched symbol to `data/gex.sqlite`:

- `snapshots`: intraday rows, full payload, kept `GEX_SNAPSHOT_RETENTION_DAYS` (14). Read `GET /api/v1/gex/{symbol}/history`.
- `eod_snapshots`: one row per symbol per session, taken from the last snapshot at or before 16:00 ET after `GEX_EOD_ARCHIVE_TIME` (16:05 ET). Not pruned with intraday snapshots. Read `GET /api/v1/gex/{symbol}/eod`.

A one-off script that never starts the scheduler does not persist. Do not treat a single smoke fetch as a history series.

## How to read a level

Pull `GET /api/v1/gex/{symbol}` for the expiry that matters (`by_expiry`, or `zero_dte` when contracts expire today). Then:

1. Name the expiry call wall and the next call-gamma strike. A technical price between those strikes is not itself a gamma strike.
2. Read size, not just the strike label. Same strike with a falling share of that expiry's absolute gamma, or a falling `pct_adv_per_1pct`, is a thinner wall. Ignore charm and vanna share counts when they are negligible next to ADV.
3. For an intraday break, compare `volume_lens.call_wall` with the OI call wall. Volume wall above the level and OI wall still below means new gamma is being built above; the OI wall updates after settle.
4. Pressure still there: price back through the level toward the OI call wall, and that expiry's call wall unchanged. Pressure gone: price holds beyond the level and that expiry's call wall leaves the old strike.

Gamma is small several days before expiry and large near the close of the expiring session, especially if spot is back on the strike. Do not treat an early cross of a distant wall as forced dealer selling.

## Reply shape

State spot, the expiry, its call wall, the next call-gamma strike, hedge shares per 1%, and that as a percent of ADV. Then say whether the level is a gamma strike or a gap between strikes, and which of the two checks above would confirm or reject a break. If no persisted history exists for the symbol, say the migration cannot be confirmed yet.
