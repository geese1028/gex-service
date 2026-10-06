---
name: gex-trader
description: >-
  Add or remove gex-service symbols and judge this week's call-wall trend for a short-term exit or hold.
  Use when the user invokes GEX-trader, says 增加标的, 去掉标的, 看墙, call wall, 压力增加, 墙在减弱,
  or asks whether to sell into the wall or hold through it for SPY, RKLB, MSFT, or any other name.
---

# GEX-trader

Short-term read of one expiry: this week's call wall. The question is whether that wall is getting heavier or lighter, and whether price is tagging it.

Dealer convention in this service: long calls, short puts. Positive gamma means dealers sell rallies and buy dips. That is a dampener, not a direction forecast. Barbon and Buraschi (2021) find the effect shows up when the hedge is large relative to the stock's liquidity. A wall several days before expiry, with a tiny hedge versus average daily volume, does not force selling just because price has crossed the strike.

## Watch list

The service must be running. Pinned names are stored in `data/gex.sqlite` and restored on startup. Persistence starts at the next refresh, not at insert time.

- Add: `PUT /api/v1/watch/{SYMBOL}` with the service defaults unless the user names `max_dte`, `strike_range_pct`, or `max_contracts`.
- Remove: `DELETE /api/v1/watch/{SYMBOL}`.
- Confirm with `GET /api/v1/watch`.

Tell the user the first comparable print is the second session of the same expiry. A brand-new name has a location and a thickness, not a trend.

## Data to read

`GET /api/v1/gex/{symbol}/walls` is the series. Each `days[]` row is one session of one expiry (expiries with DTE ≤ 7, else the nearest). `front_expiry` is this week's book: the latest expiry on or before this Friday, not today's 0DTE when both exist.

`touches[]` are intraday prints taken while spot is within 2% of that call wall, at most one every 15 minutes.

`GET /api/v1/gex/{symbol}` is the live book when a trend row is not enough. Use `by_expiry` for the front expiry, `volume_lens` only as the whole-chain check, and `hedge_flow.pct_adv_per_1pct` for size.

Open interest is the prior settle (`oi_asof`). Call-wall open interest changes when `oi_asof` advances. Same-day changes in call GEX are spot and IV moving gamma, not new inventory. The volume call wall is where today's trades are landing; it leads the next settle.

## Opinion

Use only rows for `front_expiry`. Need two sessions before calling a trend. If there is one row, say the wall's location and size and that the slope is not in the database yet.

Compare the latest row with the prior session of the same expiry:

- Heavier: same call-wall strike, and call open interest or `pct_adv` is higher after `oi_asof` moves. Scale out when price tags the wall (a touch, or spot back at the strike) on Wednesday through Friday. Done by the weekend.
- Lighter or migrated: call open interest or `pct_adv` falls on that strike, or `call_wall` steps up, especially if `volume_call_wall` moved first. Hold through the old strike and watch the new one.
- Too small to lean on: `pct_adv` stays well under about 1% of average daily volume. Say the strike is inventory, not a forced hedge, and do not treat a cross as dealer selling.

Do not use charm or vanna share counts when they are negligible next to average daily volume.

## Reply

State symbol, front expiry, spot, call wall, distance, call-wall open interest, and hedge as percent of ADV. Then one line: heavier, lighter, migrated, or not enough sessions. Then the action that follows from the user's plan (scale out on a tag, or hold through). If the service is down or the symbol was never refreshed, say persistence has not started.
