"""Implied-volatility surface summary and IV rank.

Per expiry: ATM IV, 25-delta call/put IV, risk reversal, butterfly, skew
slope and the smile points. Across expiries: term structure. IV rank and
percentile come from IBKR's 30-day implied volatility history
(``OPTION_IMPLIED_VOLATILITY`` bars) together with 30-day historical
volatility (``HISTORICAL_VOLATILITY``).
"""

from __future__ import annotations

import math

import numpy as np
from pydantic import BaseModel, Field

from .chain import ChainRow, ChainSnapshot
from .greeks import bs_delta, years_from_days


class SmilePoint(BaseModel):
    strike: float
    log_moneyness: float  # ln(K / S)
    iv_call: float | None
    iv_put: float | None
    iv_otm: float | None  # put below spot, call above


class ExpirySmile(BaseModel):
    expiry: str
    dte: float
    atm_iv: float | None
    iv_call_25d: float | None
    iv_put_25d: float | None
    risk_reversal_25d: float | None  # call25 - put25 (negative = put skew)
    butterfly_25d: float | None  # (call25 + put25)/2 - atm
    skew_slope: float | None  # d(IV)/d(ln K/S) on OTM wings
    contracts: int
    points: list[SmilePoint] = Field(default_factory=list)


class IVStats(BaseModel):
    iv30: float | None
    iv30_rank_1y: float | None  # (iv - min) / (max - min) * 100
    iv30_percentile_1y: float | None  # share of days with lower IV * 100
    iv30_1y_low: float | None
    iv30_1y_high: float | None
    hv30: float | None
    iv_hv_spread: float | None  # iv30 - hv30 (vol risk premium proxy)
    history_days: int


class IVSurface(BaseModel):
    symbol: str
    spot: float
    term_structure: list[dict]
    expiries: list[ExpirySmile]
    stats: IVStats | None


def _interp_iv_at_delta(deltas: np.ndarray, ivs: np.ndarray, target: float) -> float | None:
    if deltas.size < 2:
        return None
    order = np.argsort(deltas)
    d, v = deltas[order], ivs[order]
    if not (d[0] <= target <= d[-1]):
        return None
    return float(np.interp(target, d, v))


def _delta_for(rows: list[ChainRow], spot: float, r: float, q: float) -> np.ndarray:
    rep = np.array([row.delta if row.delta is not None else np.nan for row in rows], dtype=float)
    k = np.array([row.strike for row in rows], dtype=float)
    t = years_from_days(np.array([row.dte for row in rows], dtype=float))
    iv = np.array([row.iv if row.iv is not None else 0.2 for row in rows], dtype=float)
    is_call = np.array([row.right == "C" for row in rows])
    bs = bs_delta(spot, k, t, iv, is_call, r, q)
    return np.where(np.isnan(rep), bs, rep)


def expiry_smile(rows: list[ChainRow], spot: float, r: float, q: float) -> ExpirySmile:
    rows = [row for row in rows if row.iv is not None and row.iv > 0]
    expiry = rows[0].expiry
    dte = rows[0].dte
    by_strike: dict[float, dict[str, float]] = {}
    for row in rows:
        by_strike.setdefault(row.strike, {})[row.right] = row.iv
    points: list[SmilePoint] = []
    for k in sorted(by_strike):
        c, p = by_strike[k].get("C"), by_strike[k].get("P")
        otm = p if k < spot else c
        if otm is None:
            otm = c if c is not None else p
        points.append(SmilePoint(strike=k, log_moneyness=math.log(k / spot), iv_call=c, iv_put=p, iv_otm=otm))

    # ATM: interpolate OTM IV at ln-moneyness 0
    lm = np.array([pt.log_moneyness for pt in points if pt.iv_otm is not None])
    otm_iv = np.array([pt.iv_otm for pt in points if pt.iv_otm is not None])
    atm = float(np.interp(0.0, lm, otm_iv)) if lm.size >= 2 and lm.min() <= 0 <= lm.max() else (float(otm_iv[np.argmin(np.abs(lm))]) if lm.size else None)

    deltas = _delta_for(rows, spot, r, q)
    calls = np.array([row.right == "C" for row in rows])
    ivs = np.array([row.iv for row in rows], dtype=float)
    c25 = _interp_iv_at_delta(deltas[calls], ivs[calls], 0.25)
    p25 = _interp_iv_at_delta(deltas[~calls], ivs[~calls], -0.25)

    slope = None
    if lm.size >= 3:
        slope = float(np.polyfit(lm, otm_iv, 1)[0])

    return ExpirySmile(
        expiry=expiry,
        dte=round(dte, 3),
        atm_iv=atm,
        iv_call_25d=c25,
        iv_put_25d=p25,
        risk_reversal_25d=(c25 - p25) if c25 is not None and p25 is not None else None,
        butterfly_25d=((c25 + p25) / 2 - atm) if c25 is not None and p25 is not None and atm is not None else None,
        skew_slope=slope,
        contracts=len(rows),
        points=points,
    )


def iv_stats(iv_history: list[float] | None, hv_history: list[float] | None) -> IVStats | None:
    if not iv_history:
        return None
    arr = np.array([x for x in iv_history if x is not None and math.isfinite(x) and x > 0], dtype=float)
    if arr.size == 0:
        return None
    cur = float(arr[-1])
    lo, hi = float(arr.min()), float(arr.max())
    rank = ((cur - lo) / (hi - lo) * 100) if hi > lo else None
    pct = float((arr[:-1] < cur).mean() * 100) if arr.size > 1 else None
    hv = None
    if hv_history:
        hv_arr = [x for x in hv_history if x is not None and math.isfinite(x) and x > 0]
        hv = float(hv_arr[-1]) if hv_arr else None
    return IVStats(
        iv30=cur,
        iv30_rank_1y=rank,
        iv30_percentile_1y=pct,
        iv30_1y_low=lo,
        iv30_1y_high=hi,
        hv30=hv,
        iv_hv_spread=(cur - hv) if hv is not None else None,
        history_days=int(arr.size),
    )


def iv_surface(snapshot: ChainSnapshot, r: float, q: float) -> IVSurface:
    groups: dict[str, list[ChainRow]] = {}
    for row in snapshot.rows:
        if row.iv is not None and row.iv > 0 and row.dte > 0:
            groups.setdefault(row.expiry, []).append(row)
    smiles = [expiry_smile(rows, snapshot.spot, r, q) for _, rows in sorted(groups.items()) if len(rows) >= 2]
    term = [{"expiry": s.expiry, "dte": s.dte, "atm_iv": s.atm_iv, "risk_reversal_25d": s.risk_reversal_25d} for s in smiles]
    return IVSurface(
        symbol=snapshot.symbol,
        spot=snapshot.spot,
        term_structure=term,
        expiries=smiles,
        stats=iv_stats(snapshot.iv30_history, snapshot.hv30_history),
    )
