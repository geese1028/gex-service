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

Before the trend, state two gates:

1. Sign of this week's book. Read `total_gex` on the front expiry (live: that `by_expiry` row). Positive gamma: the call wall can act as a fade, dealers sell rallies into it. Negative gamma: the same strike is not resistance; hedging chases the move and a tag is more likely to run through. Do not recommend scaling out into the wall as a fade when the front book is negative.
2. Name the underlying. The long-call / short-put convention is the working assumption for index products (SPY, QQQ, SPX, and the same family). For a single name (MSFT, NOW, RKLB, SPCX, and other stocks) say the read is weaker: public open interest does not show who holds the inventory.

Compare the latest row with the prior session of the same expiry:

- Heavier: same call-wall strike, and call open interest or `pct_adv` is higher after `oi_asof` moves. Only if the front book is still positive gamma: scale out when price tags the wall on Wednesday through Friday. Done by the weekend.
- Lighter or migrated: call open interest or `pct_adv` falls on that strike, or `call_wall` steps up, especially if `volume_call_wall` moved first. Hold through the old strike and watch the new one.
- Negative: front-book `total_gex` is below zero. Do not fade the call wall. Say a break is the path the hedge amplifies.
- Too small to lean on: `pct_adv` stays well under about 1% of average daily volume. Say the strike is inventory, not a forced hedge, and do not treat a cross as dealer selling.

On the session that expiry dies (the front expiry's calendar date, especially after 14:00 ET), re-read `shares_per_1pct` and `pct_adv` from the live book. The week's open-interest path is the setup. Gamma on that same inventory is much larger in the last hours. If that afternoon hedge is no longer small versus ADV, use it to choose the tag exit or the hold-through. Charm and vanna share counts still stay out of the decision while they are negligible next to ADV. Use them only on that expiry afternoon when their share count is large versus ADV.

## Same-day tape and the index echo

Read these on `GET /api/v1/gex/{symbol}` after the open-interest gates:

- `wall_tape` is classified customer flow at the front-week call wall and one strike either side. `shorter_gamma` means customers are buying there and dealers are shorter gamma: do not fade the open-interest wall today (`wall_read=fade_weaker`). `longer_gamma` means a fade still has today's hedge (`fade_stands`). `quiet` or `unavailable` leaves the open-interest sign in charge. The tape starts on the refresh inside regular hours and drops when the Gateway client is released.
- `path_switch` is the sign of the book that expires today, or this week's book when nothing expires today. Positive gamma is a fade toward the strike where the hedge is largest. Negative gamma is a chase through that strike. A 0DTE path dies at the cash close.
- `session_path` is the one to act on. Source `tape` means today's prints overrode the open-interest sign. Source `oi` means the tape was too small.
- `vol_control` applies to index products only (SPY, QQQ, IWM, DIA, SPX, NDX, RUT, and the same family). `selling` means a completed session jumped the 20-session realized-vol window and target-vol funds are still adjusting (`echo_left` sessions). `full` means a quiet window has rebuilt that exposure, so the next shock has more to unwind. `off` on a single name. This is not a strike and not a measured fund flow.

## Reply

State symbol, whether it is an index product or a single name, the sign of the front book, front expiry, spot, call wall, distance, call-wall open interest, and hedge as percent of ADV. Then the tape (`wall_tape.tape` / `session_path`) and, for an index product, `vol_control.state`. Then one line: heavier, lighter, migrated, negative, too small, or not enough sessions. Then the action: scale out on a tag only when the book is positive, the tape is not `shorter_gamma`, and the wall is heavier; hold through when it is lighter or has migrated; do not fade a negative book or a tape that has made dealers shorter gamma. On the expiry afternoon, say whether the live hedge changed that action. If the service is down or the symbol was never refreshed, say persistence has not started.
