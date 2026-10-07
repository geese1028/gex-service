"""Gamma exposure calculation and companion dealer-positioning analytics.

Conventions (the usual "naive dealer" model):

* dealers are long calls and short puts, so call greeks count positive and
  put greeks negative
* per contract GEX is quoted in dollars per 1% move of the underlying:
  ``gamma * OI * multiplier * spot^2 * 0.01``

Beyond the GEX profile and walls this module produces:

* zero gamma via Black-Scholes re-pricing over a spot grid (and the curve)
* DEX / VEX / CEX (delta, vanna, charm exposures) and the vanna flip
* hedge-flow normalisation (shares per 1% move, as % of ADV): the gamma
  imbalance measure of Barbon & Buraschi (2021), "Gamma Fragility"
* a volume-weighted lens (today's flow instead of settled OI)
* ATM-straddle and IV implied moves, absolute gamma strike, concentration
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np

from .chain import NY, ChainRow, ChainSnapshot
from .greeks import bs_charm, bs_delta, bs_gamma, bs_vanna, years_from_days
from .tape import session_path as oi_session_path
from .models import (
    ChainResponse,
    ChainRowOut,
    Concentration,
    CurvePoint,
    ExpiryStats,
    Exposures,
    GexResponse,
    HedgeFlow,
    CharmClock,
    ImpliedMove,
    IVContext,
    Meta,
    ParamsOut,
    PathSwitch,
    RollOffScenario,
    StrikeLevel,
    Summary,
    VannaPlay,
    VolumeLens,
    ZeroDteBook,
)

log = logging.getLogger(__name__)

GRID_LO, GRID_HI, GRID_N = 0.90, 1.10, 161


def _sign(right: str) -> float:
    return 1.0 if right == "C" else -1.0


def contract_gex(spot: float, gamma: float, oi: float, multiplier: float, right: str) -> float:
    return _sign(right) * gamma * oi * multiplier * spot * spot * 0.01


def _usable(rows: list[ChainRow]) -> list[ChainRow]:
    return [r for r in rows if r.has_gex_inputs and r.dte > 0]


# ------------------------------------------------------------------ per-row greeks


class _Inputs:
    """Vectorised per-contract inputs for a list of usable rows."""

    def __init__(self, rows: list[ChainRow], spot: float, r: float, q: float) -> None:
        self.rows = rows
        n = len(rows)
        self.k = np.array([row.strike for row in rows], dtype=float)
        self.t = years_from_days(np.array([row.dte for row in rows], dtype=float)) if n else np.array([])
        self.iv = np.array([row.iv if row.iv is not None else np.nan for row in rows], dtype=float)
        self.oi = np.array([row.oi or 0.0 for row in rows], dtype=float)
        self.vol = np.array([row.volume or 0.0 for row in rows], dtype=float)
        self.mult = np.array([row.multiplier for row in rows], dtype=float)
        self.sign = np.array([_sign(row.right) for row in rows], dtype=float)
        self.is_call = self.sign > 0
        self.has_iv = np.isfinite(self.iv) & (self.iv > 0)
        iv_safe = np.where(self.has_iv, self.iv, 0.2)

        reported_gamma = np.array([row.gamma if row.gamma is not None else np.nan for row in rows], dtype=float)
        bs_g = bs_gamma(spot, self.k, self.t, iv_safe, r, q) if n else np.array([])
        self.gamma = np.where(np.isnan(reported_gamma), np.where(self.has_iv, bs_g, 0.0), reported_gamma)

        reported_delta = np.array([row.delta if row.delta is not None else np.nan for row in rows], dtype=float)
        bs_d = bs_delta(spot, self.k, self.t, iv_safe, self.is_call, r, q) if n else np.array([])
        self.delta = np.where(np.isnan(reported_delta), np.where(self.has_iv, bs_d, 0.0), reported_delta)

        self.vanna = np.where(self.has_iv, bs_vanna(spot, self.k, self.t, iv_safe, r, q), 0.0) if n else np.array([])
        self.charm = np.where(self.has_iv, bs_charm(spot, self.k, self.t, iv_safe, self.is_call, r, q), 0.0) if n else np.array([])

        s2 = spot * spot * 0.01
        self.gex = self.sign * self.gamma * self.oi * self.mult * s2
        self.gex_vol = self.sign * self.gamma * self.vol * self.mult * s2
        self.dex = self.sign * self.delta * self.oi * self.mult * spot
        self.vex = self.sign * self.vanna * self.oi * self.mult * spot * 0.01  # per +1 vol point
        self.cex = self.sign * self.charm / 365.0 * self.oi * self.mult * spot  # per calendar day


# ------------------------------------------------------------------ grid scans


def _find_zero_crossing(levels: np.ndarray, values: np.ndarray, spot: float) -> float | None:
    """Return the crossing (negative below, positive above) closest to spot, or None."""
    sign = np.sign(values)
    sign[sign == 0] = 1
    flips = np.where(np.diff(sign) != 0)[0]
    if flips.size == 0:
        return None
    crossings: list[float] = []
    for i in flips:
        x0, x1 = levels[i], levels[i + 1]
        y0, y1 = values[i], values[i + 1]
        if y1 == y0:
            crossings.append(float(x0))
        else:
            crossings.append(float(x0 - y0 * (x1 - x0) / (y1 - y0)))
    upward = [c for c, i in zip(crossings, flips) if values[i] < 0 < values[i + 1]]
    pool = upward or crossings
    return min(pool, key=lambda c: abs(c - spot))


def _grid_curves(inp: _Inputs, spot: float, r: float, q: float, weights: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Net GEX and net VEX across hypothetical spot levels using ``weights`` (OI or volume)."""
    if not inp.has_iv.any():
        return None
    levels = spot * np.linspace(GRID_LO, GRID_HI, GRID_N)
    sel = inp.has_iv
    k, t, iv = inp.k[sel][None, :], inp.t[sel][None, :], inp.iv[sel][None, :]
    lv = levels[:, None]
    gamma_grid = np.zeros((levels.size, inp.k.size))
    gamma_grid[:, sel] = bs_gamma(lv, k, t, iv, r, q)
    gamma_grid[:, ~sel] = inp.gamma[~sel][None, :]  # reported gamma held constant
    vanna_grid = np.zeros_like(gamma_grid)
    vanna_grid[:, sel] = bs_vanna(lv, k, t, iv, r, q)
    w = (inp.sign * weights * inp.mult)[None, :]
    net_gex = (gamma_grid * w).sum(axis=1) * levels**2 * 0.01
    net_vex = (vanna_grid * w).sum(axis=1) * levels * 0.01
    return levels, net_gex, net_vex


def zero_gamma_from_profile(profile: list[StrikeLevel], spot: float) -> float | None:
    if len(profile) < 2:
        return None
    strikes = np.array([p.strike for p in profile])
    net = np.array([p.net_gex for p in profile])
    return _find_zero_crossing(strikes, net, spot)


# ------------------------------------------------------------------ aggregates


def max_pain(profile: list[StrikeLevel], multiplier: float = 100.0) -> float | None:
    if not profile:
        return None
    strikes = np.array([p.strike for p in profile])
    call_oi = np.array([p.call_oi for p in profile])
    put_oi = np.array([p.put_oi for p in profile])
    diff = strikes[:, None] - strikes[None, :]  # settle (rows) - strike (cols)
    call_pay = np.clip(diff, 0, None) @ call_oi
    put_pay = np.clip(-diff, 0, None) @ put_oi
    total = (call_pay + put_pay) * multiplier
    return float(strikes[int(np.argmin(total))])


def _walls(profile: list[StrikeLevel]) -> tuple[float | None, float | None, float | None, float | None]:
    if not profile:
        return None, None, None, None
    call_wall = max(profile, key=lambda p: p.call_gex)
    put_wall = min(profile, key=lambda p: p.put_gex)
    call_wall_oi = max(profile, key=lambda p: p.call_oi)
    put_wall_oi = max(profile, key=lambda p: p.put_oi)
    return (
        call_wall.strike if call_wall.call_gex > 0 else None,
        put_wall.strike if put_wall.put_gex < 0 else None,
        call_wall_oi.strike if call_wall_oi.call_oi > 0 else None,
        put_wall_oi.strike if put_wall_oi.put_oi > 0 else None,
    )


def build_profile(inp: _Inputs, idx: np.ndarray | None = None) -> list[StrikeLevel]:
    idx = np.arange(len(inp.rows)) if idx is None else idx
    acc: dict[float, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for i in idx:
        row = inp.rows[i]
        slot = acc[row.strike]
        if row.right == "C":
            slot["cg"] += float(inp.gex[i])
            slot["co"] += float(inp.oi[i])
            slot["cv"] += float(inp.vol[i])
        else:
            slot["pg"] += float(inp.gex[i])
            slot["po"] += float(inp.oi[i])
            slot["pv"] += float(inp.vol[i])
        slot["gv"] += float(inp.gex_vol[i])
        slot["dex"] += float(inp.dex[i])
        slot["vex"] += float(inp.vex[i])
        slot["cex"] += float(inp.cex[i])
    return [
        StrikeLevel(
            strike=k,
            call_gex=v["cg"],
            put_gex=v["pg"],
            net_gex=v["cg"] + v["pg"],
            call_oi=v["co"],
            put_oi=v["po"],
            call_volume=v["cv"],
            put_volume=v["pv"],
            net_gex_volume=v["gv"],
            net_dex=v["dex"],
            net_vex=v["vex"],
            net_cex=v["cex"],
        )
        for k, v in sorted(acc.items())
    ]


def _volume_lens(inp: _Inputs, spot: float, r: float, q: float) -> VolumeLens | None:
    if not (inp.vol > 0).any():
        return None
    by_strike: dict[float, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for i, row in enumerate(inp.rows):
        slot = by_strike[row.strike]
        slot[0 if row.right == "C" else 1] += float(inp.gex_vol[i])
    call_wall = max(by_strike.items(), key=lambda kv: kv[1][0])
    put_wall = min(by_strike.items(), key=lambda kv: kv[1][1])
    curves = _grid_curves(inp, spot, r, q, inp.vol)
    zero = _find_zero_crossing(curves[0], curves[1], spot) if curves else None
    call_vol = float(inp.vol[inp.is_call].sum())
    put_vol = float(inp.vol[~inp.is_call].sum())
    return VolumeLens(
        total_gex=float(inp.gex_vol.sum()),
        call_wall=call_wall[0] if call_wall[1][0] > 0 else None,
        put_wall=put_wall[0] if put_wall[1][1] < 0 else None,
        zero_gamma=zero,
        total_call_volume=call_vol,
        total_put_volume=put_vol,
        put_call_volume_ratio=(put_vol / call_vol) if call_vol > 0 else None,
    )


def _mid(row: ChainRow) -> float | None:
    if row.bid is not None and row.ask is not None and row.ask >= row.bid > 0:
        return (row.bid + row.ask) / 2
    return row.last if row.last and row.last > 0 else None


def _implied_move(all_rows: list[ChainRow], spot: float) -> ImpliedMove | None:
    """ATM straddle of the nearest live expiry plus IV-based moves."""
    live = [r for r in all_rows if r.dte > 0.02]  # skip the last ~30 minutes of a 0DTE
    if not live:
        return None
    expiry = min(live, key=lambda r: r.dte).expiry
    rows = [r for r in live if r.expiry == expiry]
    strikes = sorted({r.strike for r in rows})
    atm = min(strikes, key=lambda k: abs(k - spot))
    call = next((r for r in rows if r.strike == atm and r.right == "C"), None)
    put = next((r for r in rows if r.strike == atm and r.right == "P"), None)
    dte = rows[0].dte
    straddle = None
    if call and put:
        cm, pm = _mid(call), _mid(put)
        if cm is not None and pm is not None:
            straddle = cm + pm
    ivs = [r.iv for r in (call, put) if r is not None and r.iv is not None and r.iv > 0]
    atm_iv = float(np.mean(ivs)) if ivs else None
    t_years = float(years_from_days(dte))
    return ImpliedMove(
        expiry=expiry,
        dte=round(dte, 3),
        atm_strike=atm,
        straddle_price=straddle,
        straddle_move_pct=(straddle / spot * 100) if straddle else None,
        atm_iv=atm_iv,
        iv_move_to_expiry_pct=(atm_iv * math.sqrt(t_years) * 100) if atm_iv else None,
        iv_daily_move_pct=(atm_iv / math.sqrt(252) * 100) if atm_iv else None,
    )


def _concentration(profile: list[StrikeLevel], inp: _Inputs) -> Concentration:
    if not profile:
        return Concentration(absolute_gamma_strike=None, top_strikes=[], gex_hhi=None, zero_dte_share=None)
    abs_gamma = {p.strike: abs(p.call_gex) + abs(p.put_gex) for p in profile}
    total = sum(abs_gamma.values())
    ranked = sorted(abs_gamma.items(), key=lambda kv: kv[1], reverse=True)
    hhi = sum((v / total) ** 2 for v in abs_gamma.values()) if total > 0 else None
    abs_rows = np.abs(inp.gex)
    abs_total = float(abs_rows.sum())
    zero_dte = np.array([row.dte < 1.0 for row in inp.rows], dtype=bool)
    zero_dte_share = float(abs_rows[zero_dte].sum() / abs_total) if abs_total > 0 else None
    return Concentration(
        absolute_gamma_strike=ranked[0][0] if total > 0 else None,
        top_strikes=[k for k, v in ranked[:5] if v > 0],
        gex_hhi=hhi,
        zero_dte_share=zero_dte_share,
    )


def _regime(total_gex: float) -> str:
    if abs(total_gex) < 1e-9:
        return "flat"
    return "positive_gamma" if total_gex > 0 else "negative_gamma"


def _subset_levels(inp: _Inputs, keep: np.ndarray, spot: float, r: float, q: float) -> tuple[float, float | None, float | None, float | None]:
    """Total GEX, zero gamma and walls for the contracts selected by ``keep``."""
    if not keep.any():
        return 0.0, None, None, None
    idx = np.where(keep)[0]
    profile = build_profile(inp, idx)
    cw, pw, _, _ = _walls(profile)
    total = float(inp.gex[idx].sum())
    sub = _Inputs([inp.rows[i] for i in idx], spot, r, q)
    curves = _grid_curves(sub, spot, r, q, sub.oi)
    zero = _find_zero_crossing(curves[0], curves[1], spot) if curves else zero_gamma_from_profile(profile, spot)
    return total, zero, cw, pw


def roll_off_scenarios(inp: _Inputs, spot: float, r: float, q: float) -> list[RollOffScenario]:
    """What the book looks like after the nearest expiry, and after everything within a week, drops off."""
    if not inp.rows:
        return []
    expiries = sorted({row.expiry for row in inp.rows})
    dte_by_exp = {e: next(row.dte for row in inp.rows if row.expiry == e) for e in expiries}
    abs_total = float(np.abs(inp.gex).sum())
    scenarios: list[RollOffScenario] = []
    candidates = [("drop_nearest", [expiries[0]]), ("drop_week", [e for e in expiries if dte_by_exp[e] <= 7.0])]
    seen: set[tuple[str, ...]] = set()
    for name, excluded in candidates:
        if not excluded or tuple(excluded) in seen or len(excluded) == len(expiries):
            continue
        seen.add(tuple(excluded))
        keep = np.array([row.expiry not in excluded for row in inp.rows])
        removed = float(np.abs(inp.gex[~keep]).sum())
        total, zero, cw, pw = _subset_levels(inp, keep, spot, r, q)
        scenarios.append(
            RollOffScenario(
                name=name,
                excluded_expiries=excluded,
                abs_gex_removed_share=(removed / abs_total) if abs_total > 0 else 0.0,
                total_gex=total,
                zero_gamma=zero,
                call_wall=cw,
                put_wall=pw,
                regime=_regime(total),
            )
        )
    return scenarios


def _hedge_flow(total_gex: float, spot: float, adv: float | None, zero_gamma: float | None) -> HedgeFlow:
    shares = total_gex / spot if spot > 0 else 0.0
    pct_adv = (abs(shares) / adv * 100) if adv and adv > 0 else None
    regime = _regime(total_gex)
    if regime == "positive_gamma":
        note = "dealers sell into rallies and buy dips: hedging dampens moves (mean reversion)"
    elif regime == "negative_gamma":
        note = "dealers buy rallies and sell dips: hedging amplifies moves (intraday momentum)"
    else:
        note = "no meaningful dealer gamma"
    dist = ((zero_gamma - spot) / spot * 100) if zero_gamma is not None and spot > 0 else None
    return HedgeFlow(
        shares_per_1pct=abs(shares),
        pct_adv_per_1pct=pct_adv,
        adv_shares=adv,
        regime=regime,
        distance_to_zero_gamma_pct=dist,
        direction_note=note,
    )


def _iv_context(snapshot: ChainSnapshot) -> IVContext | None:
    from .surface import iv_stats  # local import: surface depends on chain, not on gex

    stats = iv_stats(snapshot.iv30_history, snapshot.hv30_history)
    if stats is None:
        return None
    return IVContext(
        iv30=stats.iv30,
        iv30_rank_1y=stats.iv30_rank_1y,
        iv30_percentile_1y=stats.iv30_percentile_1y,
        hv30=stats.hv30,
        iv_hv_spread=stats.iv_hv_spread,
    )


def front_week_expiry(by_expiry: list[ExpiryStats], day) -> ExpiryStats | None:
    """Latest expiry that still falls on or before this week's Friday.

    Daily names keep a same-day book and a Friday book. The week-long wall is the Friday one.
    """
    if not by_expiry:
        return None
    friday = day + timedelta(days=(4 - day.weekday()) % 7)

    def exp_date(stats: ExpiryStats):
        return datetime.strptime(stats.expiry, "%Y%m%d").date()

    this_week = [stats for stats in by_expiry if day <= exp_date(stats) <= friday]
    if this_week:
        return max(this_week, key=lambda stats: stats.expiry)
    live = [stats for stats in by_expiry if stats.dte > 0]
    return min(live, key=lambda stats: stats.dte) if live else None


def _path_name(regime: str) -> str:
    if regime == "positive_gamma":
        return "fade"
    if regime == "negative_gamma":
        return "chase"
    return "flat"


def _path_switch(zero_dte: ZeroDteBook, by_expiry: list[ExpiryStats], day) -> PathSwitch:
    """0DTE when that book exists, otherwise this week's expiry. The strike is the hedge, not a ceiling."""
    if zero_dte.expiry is not None and (zero_dte.call_wall is not None or abs(zero_dte.total_gex) > 0):
        path = _path_name(zero_dte.regime)
        if path == "fade":
            note = (
                "Today's expiring gamma is positive. The path is a fade toward the strike where the hedge is largest. "
                "It dies at the cash close."
            )
        elif path == "chase":
            note = (
                "Today's expiring gamma is negative. The path is a chase through the strike where the hedge is largest. "
                "It dies at the cash close."
            )
        else:
            note = "Today's expiring book has no meaningful gamma."
        return PathSwitch(
            book="zero_dte",
            regime=zero_dte.regime,
            path=path,
            expiry=zero_dte.expiry,
            strike=zero_dte.call_wall if zero_dte.call_wall is not None else zero_dte.zero_gamma,
            shares_per_1pct=zero_dte.shares_per_1pct,
            note=note,
        )
    front = front_week_expiry(by_expiry, day)
    if front is None:
        return PathSwitch(
            book="front_week", regime="flat", path="flat", expiry=None, strike=None, shares_per_1pct=0.0,
            note="No front-week book.",
        )
    regime = _regime(front.total_gex)
    path = _path_name(regime)
    if path == "fade":
        note = (
            "This week's gamma is positive, so the path is a fade into the call wall. "
            "The hedge still has to be large versus average daily volume before that fade is forced."
        )
    elif path == "chase":
        note = "This week's gamma is negative. The call wall is where the hedge is largest, and the path is a chase through it."
    else:
        note = "This week's book has no meaningful gamma."
    return PathSwitch(
        book="front_week",
        regime=regime,
        path=path,
        expiry=front.expiry,
        strike=front.call_wall,
        shares_per_1pct=front.shares_per_1pct,
        note=note,
    )


def _hours_until_close(now: datetime) -> float:
    """Hours from ``now`` until 16:00 ET. Zero once the cash close has passed."""
    local = now.astimezone(NY)
    close = local.replace(hour=16, minute=0, second=0, microsecond=0)
    return max(0.0, (close - local).total_seconds() / 3600.0)


def _versus(spot: float, level: float | None) -> str:
    if level is None or spot <= 0:
        return "unknown"
    if abs(spot - level) / spot < 0.0005:
        return "at"
    return "above" if spot > level else "below"


def _hedge_shares_to(total_gex: float, spot: float, level: float | None) -> float | None:
    """Linear dealer hedge, in shares, to travel from spot to ``level``.

    Positive means dealers buy the underlying along the way. GEX is the dollar
    delta change per 1% move, so the share change per 1% is GEX/spot and the
    dealer hedge is the opposite of that change.
    """
    if level is None or spot <= 0:
        return None
    dealer_share_change = (total_gex / spot) * ((level - spot) / spot)
    return -dealer_share_change


def _zero_dte_book(inp: _Inputs, spot: float, r: float, q: float, hours_left: float) -> ZeroDteBook:
    mask = np.array([row.dte < 1.0 for row in inp.rows], dtype=bool) if inp.rows else np.array([], dtype=bool)
    if mask.size == 0 or not mask.any():
        return ZeroDteBook(
            expiry=None, dte=None, hours_left=round(hours_left, 2), total_gex=0.0, abs_gex_share=0.0,
            regime="flat", call_wall=None, put_wall=None, zero_gamma=None, spot_vs_zero_gamma="unknown",
            in_walls=None, shares_per_1pct=0.0, hedge_shares_to_call_wall=None, hedge_shares_to_put_wall=None,
            entry_note="No contracts expiring today. Use the full-chain walls.",
        )
    total, zero, call_wall, put_wall = _subset_levels(inp, mask, spot, r, q)
    abs_total = float(np.abs(inp.gex).sum())
    share = float(np.abs(inp.gex[mask]).sum()) / abs_total if abs_total > 0 else 0.0
    expiry = min((inp.rows[i].expiry for i in np.where(mask)[0]), default=None)
    dte = float(min(inp.rows[i].dte for i in np.where(mask)[0]))
    regime = _regime(total)
    in_walls = None
    if call_wall is not None and put_wall is not None:
        lo, hi = min(put_wall, call_wall), max(put_wall, call_wall)
        in_walls = lo <= spot <= hi
    shares = abs(total / spot) if spot > 0 else 0.0
    side = _versus(spot, zero)
    if regime == "positive_gamma" and in_walls:
        note = (
            f"Spot is inside today's walls and gamma is positive. "
            f"Fades back toward zero gamma {zero:g} have the hedge with them."
            if zero is not None else
            "Spot is inside today's walls and gamma is positive. Fades back into the range have the hedge with them."
        )
    elif regime == "positive_gamma" and side == "above":
        note = "Spot is above today's zero gamma while gamma is positive. Rallies are sold back toward it."
    elif regime == "positive_gamma" and side == "below":
        note = "Spot is below today's zero gamma while gamma is positive. Dips are bought back toward it."
    elif regime == "negative_gamma":
        note = "Today's book is negative gamma. Moves away from zero gamma are chased, not faded."
    elif regime == "positive_gamma":
        note = "Today's gamma is positive. Fades back inside the call and put walls have the hedge with them."
    else:
        note = "Today's expiring book has no meaningful gamma."
    return ZeroDteBook(
        expiry=expiry,
        dte=round(dte, 3),
        hours_left=round(hours_left, 2),
        total_gex=total,
        abs_gex_share=share,
        regime=regime,
        call_wall=call_wall,
        put_wall=put_wall,
        zero_gamma=zero,
        spot_vs_zero_gamma=side,
        in_walls=in_walls,
        shares_per_1pct=shares,
        hedge_shares_to_call_wall=_hedge_shares_to(total, spot, call_wall),
        hedge_shares_to_put_wall=_hedge_shares_to(total, spot, put_wall),
        entry_note=note,
    )


def _terminal_delta(spot: float, strike: float, is_call: bool) -> float:
    if is_call:
        return 1.0 if spot > strike else (0.5 if spot == strike else 0.0)
    return -1.0 if spot < strike else (-0.5 if spot == strike else 0.0)


def _charm_clock(inp: _Inputs, spot: float, r: float, q: float, hours_left: float) -> CharmClock:
    """Reprice each contract's delta at the cash close and sum the dealer hedge."""
    day_frac = hours_left / 24.0
    by_strike: dict[float, float] = defaultdict(float)
    hedge = 0.0
    expiring_only = False
    if hours_left > 0 and inp.rows:
        changes: list[tuple[int, float]] = []
        for i, row in enumerate(inp.rows):
            if not inp.has_iv[i]:
                continue
            delta_now = float(bs_delta(spot, row.strike, years_from_days(row.dte), float(inp.iv[i]), row.right == "C", r, q))
            remaining = row.dte - day_frac
            if remaining <= 1e-8:
                delta_later = _terminal_delta(spot, row.strike, row.right == "C")
            else:
                delta_later = float(bs_delta(spot, row.strike, years_from_days(remaining), float(inp.iv[i]), row.right == "C", r, q))
            share_change = inp.sign[i] * (delta_later - delta_now) * inp.oi[i] * inp.mult[i]
            changes.append((i, -float(share_change)))
        expiring = [pair for pair in changes if inp.rows[pair[0]].dte - day_frac <= 1e-6]
        used = expiring if expiring else changes
        expiring_only = bool(expiring)
        for i, dealer_hedge in used:
            hedge += dealer_hedge
            by_strike[inp.rows[i].strike] += dealer_hedge
    pin = max(by_strike.items(), key=lambda kv: abs(kv[1]))[0] if by_strike else None
    if hours_left <= 0 or abs(hedge) < 1.0:
        direction = "flat"
    elif hedge > 0:
        direction = "buy"
    else:
        direction = "sell"
    side = _versus(spot, pin)
    if direction == "flat":
        note = "Cash close has passed, or time decay does not move the hedge." if hours_left <= 0 else "Time decay into the close does not move the dealer hedge."
    else:
        book = "today's expiring contracts" if expiring_only else "the nearest contracts"
        verb = "buy" if direction == "buy" else "sell"
        where = f" toward {pin:g}" if pin is not None else ""
        note = f"If spot stays here, dealers {verb} about {abs(hedge):,.0f} shares into 16:00 ET{where} ({book})."
    return CharmClock(
        hours_left=round(hours_left, 2),
        shares_to_close=hedge,
        direction=direction,
        pin_strike=pin,
        spot_vs_pin=side,
        entry_note=note,
    )


def _vanna_play(exposures: Exposures, spot: float) -> VannaPlay:
    up = -exposures.vex / spot if spot > 0 else 0.0
    down = -up
    side = _versus(spot, exposures.vanna_flip)
    distance = ((exposures.vanna_flip - spot) / spot * 100) if exposures.vanna_flip is not None and spot > 0 else None
    if abs(down) < 1.0:
        note = "A one-point IV move does not change the dealer hedge."
    elif down > 0:
        note = (
            f"A 1-point IV drop has dealers buying about {down:,.0f} shares; a 1-point rise has them selling the same. "
            f"Long entries want the crush."
        )
    else:
        note = (
            f"A 1-point IV drop has dealers selling about {abs(down):,.0f} shares; a 1-point rise has them buying the same. "
            f"Long entries are working against a vol crush."
        )
    if exposures.vanna_flip is not None:
        note += f" Vanna flips at {exposures.vanna_flip:g}; spot is {side} it."
    return VannaPlay(
        vanna_flip=exposures.vanna_flip,
        spot_vs_flip=side,
        distance_pct=distance,
        shares_if_iv_down_1pt=down,
        shares_if_iv_up_1pt=up,
        entry_note=note,
    )


# ------------------------------------------------------------------ main entry


def compute_gex(snapshot: ChainSnapshot, r: float, q: float, now: datetime | None = None) -> GexResponse:
    rows = _usable(snapshot.rows)
    spot = snapshot.spot
    inp = _Inputs(rows, spot, r, q)
    gex = inp.gex

    profile = build_profile(inp)
    call_wall, put_wall, call_wall_oi, put_wall_oi = _walls(profile)

    # zero gamma / vanna flip from the BS grid
    curves = _grid_curves(inp, spot, r, q, inp.oi) if rows else None
    curve: list[CurvePoint] = []
    zero: float | None = None
    vanna_flip: float | None = None
    at_spot = up1 = down1 = None
    if curves is not None:
        levels, net_gex, net_vex = curves
        curve = [CurvePoint(level=round(float(lv), 4), net_gex=float(g), net_vex=float(v)) for lv, g, v in zip(levels, net_gex, net_vex)]
        zero = _find_zero_crossing(levels, net_gex, spot)
        vanna_flip = _find_zero_crossing(levels, net_vex, spot)
        at_spot = float(np.interp(spot, levels, net_gex))
        up1 = float(np.interp(spot * 1.01, levels, net_gex))
        down1 = float(np.interp(spot * 0.99, levels, net_gex))
        method = "bs_grid" if zero is not None else "bs_grid_no_flip"
    else:
        zero = zero_gamma_from_profile(profile, spot)
        method = "strike_profile" if zero is not None else "none"

    # per-expiry breakdown
    abs_total = float(np.abs(gex).sum()) if gex.size else 0.0
    by_expiry: list[ExpiryStats] = []
    groups: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        groups[row.expiry].append(i)
    for expiry in sorted(groups):
        idx = np.array(groups[expiry])
        sub_profile = build_profile(inp, idx)
        cw, pw, _, _ = _walls(sub_profile)
        sub_gex = gex[idx]
        call_sum = float(sub_gex[sub_gex > 0].sum())
        put_sum = float(sub_gex[sub_gex < 0].sum())
        wall_row = next((p for p in sub_profile if p.strike == cw), None) if cw is not None else None
        vol_by_strike: dict[float, float] = defaultdict(float)
        for i in idx:
            if inp.rows[int(i)].right == "C":
                vol_by_strike[inp.rows[int(i)].strike] += float(inp.gex_vol[int(i)])
        volume_call_wall = max(vol_by_strike, key=vol_by_strike.get) if any(v > 0 for v in vol_by_strike.values()) else None
        total_expiry = call_sum + put_sum
        by_expiry.append(
            ExpiryStats(
                expiry=expiry,
                dte=round(rows[idx[0]].dte, 3),
                total_gex=total_expiry,
                call_gex=call_sum,
                put_gex=put_sum,
                call_wall=cw,
                put_wall=pw,
                contracts=int(idx.size),
                vex=float(inp.vex[idx].sum()),
                cex=float(inp.cex[idx].sum()),
                abs_gex_share=(float(np.abs(sub_gex).sum()) / abs_total) if abs_total > 0 else 0.0,
                call_wall_gex=wall_row.call_gex if wall_row is not None else 0.0,
                call_wall_oi=wall_row.call_oi if wall_row is not None else 0.0,
                volume_call_wall=volume_call_wall,
                shares_per_1pct=(abs(total_expiry) / spot) if spot > 0 else 0.0,
            )
        )

    total_call_oi = float(sum(p.call_oi for p in profile))
    total_put_oi = float(sum(p.put_oi for p in profile))
    multiplier = float(np.median(inp.mult)) if inp.mult.size else 100.0
    call_gex_total = float(gex[gex > 0].sum()) if gex.size else 0.0
    put_gex_total = float(gex[gex < 0].sum()) if gex.size else 0.0
    total_gex = call_gex_total + put_gex_total

    as_of = now or snapshot.ts
    now = now or datetime.now(tz=snapshot.ts.tzinfo)
    summary = Summary(
        total_gex=total_gex,
        call_gex=call_gex_total,
        put_gex=put_gex_total,
        zero_gamma=zero,
        zero_gamma_method=method,
        call_wall=call_wall,
        put_wall=put_wall,
        call_wall_oi=call_wall_oi,
        put_wall_oi=put_wall_oi,
        max_pain=max_pain(profile, multiplier),
        total_call_oi=total_call_oi,
        total_put_oi=total_put_oi,
        put_call_oi_ratio=(total_put_oi / total_call_oi) if total_call_oi > 0 else None,
        curve_gex_at_spot=at_spot,
    )
    hours_left = _hours_until_close(as_of)
    exposures = Exposures(
        dex=float(inp.dex.sum()) if rows else 0.0,
        dex_shares=(float(inp.dex.sum()) / spot) if rows and spot > 0 else 0.0,
        vex=float(inp.vex.sum()) if rows else 0.0,
        cex=float(inp.cex.sum()) if rows else 0.0,
        vanna_flip=vanna_flip,
        net_gex_up_1pct=up1,
        net_gex_down_1pct=down1,
    )
    return GexResponse(
        symbol=snapshot.symbol,
        sec_type=snapshot.sec_type,
        spot=spot,
        ts=snapshot.ts,
        oi_asof=snapshot.oi_asof,
        params=ParamsOut(
            max_dte=snapshot.params.max_dte,
            strike_range_pct=snapshot.params.strike_range_pct,
            max_contracts=snapshot.params.max_contracts,
            expiries=list(snapshot.params.expiries),
            risk_free_rate=r,
            dividend_yield=q,
        ),
        summary=summary,
        iv_context=_iv_context(snapshot),
        exposures=exposures,
        hedge_flow=_hedge_flow(total_gex, spot, snapshot.adv_shares, zero),
        volume_lens=_volume_lens(inp, spot, r, q) if rows else None,
        implied_move=_implied_move(snapshot.rows, spot),
        concentration=_concentration(profile, inp),
        roll_off=roll_off_scenarios(inp, spot, r, q),
        zero_dte=(zero_book := _zero_dte_book(inp, spot, r, q, hours_left)),
        path_switch=(path_book := _path_switch(zero_book, by_expiry, as_of.astimezone(NY).date())),
        session_path=oi_session_path(path_book, None),
        charm_clock=_charm_clock(inp, spot, r, q, hours_left),
        vanna_play=_vanna_play(exposures, spot),
        profile=profile,
        gamma_curve=curve,
        by_expiry=by_expiry,
        meta=Meta(
            contracts_total=snapshot.contracts_total,
            contracts_used=len(rows),
            dropped_contracts=snapshot.contracts_total - len(rows),
            fetch_duration_s=snapshot.fetch_duration_s,
            data_age_s=max(0.0, (now - snapshot.ts).total_seconds()),
            market_data_type=snapshot.market_data_type,
            spot_source=snapshot.spot_source,
            stale=snapshot.stale,
            warnings=list(snapshot.warnings),
        ),
    )


def chain_response(snapshot: ChainSnapshot, r: float, q: float) -> ChainResponse:
    rows = snapshot.rows
    usable = _usable(rows)
    inp = _Inputs(usable, snapshot.spot, r, q)
    gex_by_id = {row.con_id: float(g) for row, g in zip(usable, inp.gex)}
    return ChainResponse(
        symbol=snapshot.symbol,
        spot=snapshot.spot,
        ts=snapshot.ts,
        oi_asof=snapshot.oi_asof,
        rows=[
            ChainRowOut(
                con_id=row.con_id,
                expiry=row.expiry,
                dte=round(row.dte, 4),
                strike=row.strike,
                right=row.right,
                trading_class=row.trading_class,
                multiplier=row.multiplier,
                bid=row.bid,
                ask=row.ask,
                last=row.last,
                iv=row.iv,
                gamma=row.gamma,
                delta=row.delta,
                oi=row.oi,
                volume=row.volume,
                gex=gex_by_id.get(row.con_id),
            )
            for row in sorted(rows, key=lambda x: (x.expiry, x.strike, x.right))
        ],
    )
