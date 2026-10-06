"""Second-order analytics built on top of cached GEX results and the store.

Everything here is pure (no IBKR calls); the API layer supplies cached
results, stored EOD rows and historical bars.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Iterable

import numpy as np
from pydantic import BaseModel, Field

from .chain import ChainRow, ChainSnapshot
from .models import GexResponse

# ---------------------------------------------------------------------- drift


class DriftPoint(BaseModel):
    ts: datetime
    spot: float
    total_gex: float
    zero_gamma: float | None
    call_wall: float | None
    put_wall: float | None
    pct_adv: float | None
    regime: str | None


class DriftResponse(BaseModel):
    symbol: str
    date: str
    points: int
    first: DriftPoint | None
    latest: DriftPoint | None
    change: dict[str, float | None]
    regime_flips: int
    call_wall_changes: int
    put_wall_changes: int
    series: list[DriftPoint] = Field(default_factory=list)


def _point(res: GexResponse) -> DriftPoint:
    hf = res.hedge_flow
    return DriftPoint(
        ts=res.ts,
        spot=res.spot,
        total_gex=res.summary.total_gex,
        zero_gamma=res.summary.zero_gamma,
        call_wall=res.summary.call_wall,
        put_wall=res.summary.put_wall,
        pct_adv=hf.pct_adv_per_1pct if hf else None,
        regime=hf.regime if hf else None,
    )


def _delta(a: float | None, b: float | None) -> float | None:
    return (b - a) if a is not None and b is not None else None


def drift(symbol: str, day: str, results: list[GexResponse]) -> DriftResponse:
    pts = [_point(r) for r in results]
    first = pts[0] if pts else None
    latest = pts[-1] if pts else None
    change: dict[str, float | None] = {}
    if first and latest:
        change = {
            "spot": _delta(first.spot, latest.spot),
            "total_gex": _delta(first.total_gex, latest.total_gex),
            "zero_gamma": _delta(first.zero_gamma, latest.zero_gamma),
            "call_wall": _delta(first.call_wall, latest.call_wall),
            "put_wall": _delta(first.put_wall, latest.put_wall),
            "pct_adv": _delta(first.pct_adv, latest.pct_adv),
        }
    flips = sum(1 for a, b in zip(pts, pts[1:]) if a.regime != b.regime)
    cw = sum(1 for a, b in zip(pts, pts[1:]) if a.call_wall != b.call_wall)
    pw = sum(1 for a, b in zip(pts, pts[1:]) if a.put_wall != b.put_wall)
    return DriftResponse(
        symbol=symbol, date=day, points=len(pts), first=first, latest=latest, change=change,
        regime_flips=flips, call_wall_changes=cw, put_wall_changes=pw, series=pts,
    )


# ------------------------------------------------------------------- realized


@dataclass
class Bar:
    ts: datetime  # bar start (tz-aware)
    open: float
    high: float
    low: float
    close: float
    volume: float


class RealizedDay(BaseModel):
    date: str
    bars: int
    open: float
    close: float
    return_pct: float
    range_pct: float  # (high - low) / open
    realized_vol: float  # annualised from 5-min log returns
    autocorr_lag1: float | None  # of 5-min returns (negative = mean reversion)
    last30_return_pct: float | None
    rest_of_day_return_pct: float | None
    last30_vs_rest: str | None  # momentum | reversal


class RealizedResponse(BaseModel):
    symbol: str
    bar_size: str
    days: list[RealizedDay]


def realized_metrics(symbol: str, bars: Iterable[Bar], bars_per_day: int = 78, last_n: int = 6) -> RealizedResponse:
    by_day: dict[str, list[Bar]] = {}
    for b in bars:
        by_day.setdefault(b.ts.date().isoformat(), []).append(b)
    out: list[RealizedDay] = []
    for day, bs in sorted(by_day.items()):
        if len(bs) < 3:
            continue
        closes = np.array([b.close for b in bs], dtype=float)
        rets = np.diff(np.log(closes))
        rv = float(rets.std(ddof=1) * math.sqrt(bars_per_day * 252)) if rets.size > 1 else 0.0
        ac = None
        if rets.size > 3 and rets.std() > 0:
            ac = float(np.corrcoef(rets[:-1], rets[1:])[0, 1])
        hi, lo = max(b.high for b in bs), min(b.low for b in bs)
        o, c = bs[0].open, bs[-1].close
        last30 = rest = tag = None
        if len(bs) > last_n:
            split = closes[-last_n - 1]
            last30 = (c / split - 1) * 100
            rest = (split / o - 1) * 100
            tag = "momentum" if last30 * rest > 0 else "reversal"
        out.append(
            RealizedDay(
                date=day, bars=len(bs), open=o, close=c, return_pct=(c / o - 1) * 100, range_pct=(hi - lo) / o * 100,
                realized_vol=rv, autocorr_lag1=ac, last30_return_pct=last30, rest_of_day_return_pct=rest, last30_vs_rest=tag,
            )
        )
    return RealizedResponse(symbol=symbol, bar_size="5 mins", days=out)


# ----------------------------------------------------------------- validation


class ValidationResponse(BaseModel):
    symbol: str
    n: int
    note: str | None
    corr_gamma_imbalance_vs_realized_vol: float | None
    corr_gamma_imbalance_vs_autocorr: float | None
    corr_total_gex_vs_range: float | None
    by_regime: dict[str, dict[str, float | int | None]]
    rows: list[dict]


def _corr(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3 or np.std(x) == 0 or np.std(y) == 0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def _next_trading_day_map(days: list[str]) -> dict[str, str]:
    """Map each day to the following day present in ``days``."""
    s = sorted(days)
    return {a: b for a, b in zip(s, s[1:])}


def validation(symbol: str, eod_rows: list[dict], realized: RealizedResponse) -> ValidationResponse:
    rd = {d.date: d for d in realized.days}
    nxt = _next_trading_day_map(sorted(set(rd) | {r["date"] for r in eod_rows}))
    rows: list[dict] = []
    for r in eod_rows:
        n = nxt.get(r["date"])
        if n is None or n not in rd or r.get("pct_adv") is None:
            continue
        d = rd[n]
        rows.append(
            {
                "date": r["date"], "next_date": n, "pct_adv": r["pct_adv"], "total_gex": r["total_gex"], "regime": r["regime"],
                "next_realized_vol": d.realized_vol, "next_autocorr": d.autocorr_lag1, "next_range_pct": d.range_pct,
                "next_return_pct": d.return_pct,
            }
        )
    by_regime: dict[str, dict[str, float | int | None]] = {}
    for reg in sorted({r["regime"] for r in rows if r["regime"]}):
        sub = [r for r in rows if r["regime"] == reg]
        acs = [r["next_autocorr"] for r in sub if r["next_autocorr"] is not None]
        by_regime[reg] = {
            "n": len(sub),
            "mean_realized_vol": float(np.mean([r["next_realized_vol"] for r in sub])),
            "mean_autocorr": float(np.mean(acs)) if acs else None,
            "mean_range_pct": float(np.mean([r["next_range_pct"] for r in sub])),
        }
    acr = [r for r in rows if r["next_autocorr"] is not None]
    return ValidationResponse(
        symbol=symbol,
        n=len(rows),
        note=None if len(rows) >= 10 else "fewer than 10 paired days; statistics are indicative only (table fills in as EOD archives accumulate)",
        corr_gamma_imbalance_vs_realized_vol=_corr([r["pct_adv"] for r in rows], [r["next_realized_vol"] for r in rows]),
        corr_gamma_imbalance_vs_autocorr=_corr([r["pct_adv"] for r in acr], [r["next_autocorr"] for r in acr]),
        corr_total_gex_vs_range=_corr([r["total_gex"] for r in rows], [r["next_range_pct"] for r in rows]),
        by_regime=by_regime,
        rows=rows,
    )


# ------------------------------------------------------------------- backtest


class BacktestResponse(BaseModel):
    symbol: str
    n_days: int
    note: str | None
    call_wall_hold_rate: float | None  # next-day high stayed below call wall
    put_wall_hold_rate: float | None
    call_wall_oi_hold_rate: float | None
    put_wall_oi_hold_rate: float | None
    zero_gamma_side_persistence: float | None  # next close on same side of zero gamma as today's close
    by_regime: dict[str, dict[str, float | int | None]]
    rows: list[dict]


def _rate(flags: list[bool]) -> float | None:
    return float(np.mean(flags)) if flags else None


def backtest(symbol: str, eod_rows: list[dict], daily: list[Bar]) -> BacktestResponse:
    bars = {b.ts.date().isoformat(): b for b in daily}
    nxt = _next_trading_day_map(sorted(set(bars) | {r["date"] for r in eod_rows}))
    rows: list[dict] = []
    for r in eod_rows:
        n = nxt.get(r["date"])
        if n is None or n not in bars:
            continue
        b = bars[n]
        spot, zg = r["spot"], r.get("zero_gamma")
        row = {
            "date": r["date"], "next_date": n, "spot": spot, "regime": r["regime"],
            "next_high": b.high, "next_low": b.low, "next_close": b.close,
            "next_return_pct": (b.close / spot - 1) * 100, "next_range_pct": (b.high - b.low) / spot * 100,
            "call_wall_held": (b.high <= r["call_wall"]) if r.get("call_wall") and r["call_wall"] > spot else None,
            "put_wall_held": (b.low >= r["put_wall"]) if r.get("put_wall") and r["put_wall"] < spot else None,
            "call_wall_oi_held": (b.high <= r["call_wall_oi"]) if r.get("call_wall_oi") and r["call_wall_oi"] > spot else None,
            "put_wall_oi_held": (b.low >= r["put_wall_oi"]) if r.get("put_wall_oi") and r["put_wall_oi"] < spot else None,
            "zero_gamma_side_kept": ((b.close - zg) * (spot - zg) > 0) if zg else None,
        }
        rows.append(row)
    by_regime: dict[str, dict[str, float | int | None]] = {}
    for reg in sorted({r["regime"] for r in rows if r["regime"]}):
        sub = [r for r in rows if r["regime"] == reg]
        by_regime[reg] = {
            "n": len(sub),
            "mean_abs_return_pct": float(np.mean([abs(r["next_return_pct"]) for r in sub])),
            "mean_range_pct": float(np.mean([r["next_range_pct"] for r in sub])),
            "call_wall_hold_rate": _rate([r["call_wall_held"] for r in sub if r["call_wall_held"] is not None]),
            "put_wall_hold_rate": _rate([r["put_wall_held"] for r in sub if r["put_wall_held"] is not None]),
        }
    pick = lambda key: [r[key] for r in rows if r[key] is not None]  # noqa: E731
    return BacktestResponse(
        symbol=symbol,
        n_days=len(rows),
        note=None if len(rows) >= 20 else "fewer than 20 days; hold rates are indicative only",
        call_wall_hold_rate=_rate(pick("call_wall_held")),
        put_wall_hold_rate=_rate(pick("put_wall_held")),
        call_wall_oi_hold_rate=_rate(pick("call_wall_oi_held")),
        put_wall_oi_hold_rate=_rate(pick("put_wall_oi_held")),
        zero_gamma_side_persistence=_rate(pick("zero_gamma_side_kept")),
        by_regime=by_regime,
        rows=rows,
    )


# ------------------------------------------------------------------- features

FEATURE_COLUMNS = [
    "symbol", "date", "ts", "spot", "total_gex", "call_gex", "put_gex", "zero_gamma", "zero_gamma_dist_pct",
    "call_wall", "call_wall_dist_pct", "put_wall", "put_wall_dist_pct", "call_wall_oi", "put_wall_oi", "max_pain",
    "put_call_oi_ratio", "total_call_oi", "total_put_oi", "dex", "dex_shares", "vex", "cex", "vanna_flip",
    "net_gex_up_1pct", "net_gex_down_1pct", "gamma_imbalance_pct_adv", "hedge_shares_per_1pct", "regime",
    "volume_total_gex", "volume_zero_gamma", "put_call_volume_ratio", "implied_daily_move_pct", "straddle_move_pct",
    "atm_iv", "absolute_gamma_strike", "gex_hhi", "zero_dte_share", "iv30", "iv30_rank_1y", "iv30_percentile_1y",
    "hv30", "iv_hv_spread", "roll_off_nearest_total_gex", "roll_off_nearest_removed_share", "contracts_used", "stale",
]


def _dist_pct(level: float | None, spot: float) -> float | None:
    return ((level / spot - 1) * 100) if level is not None and spot > 0 else None


def feature_row(res: GexResponse, day: str | None = None) -> dict:
    s, e, hf, vl, im, c, ivc = res.summary, res.exposures, res.hedge_flow, res.volume_lens, res.implied_move, res.concentration, res.iv_context
    nearest = next((r for r in res.roll_off if r.name == "drop_nearest"), None)
    row = {
        "symbol": res.symbol,
        "date": day or res.ts.astimezone().date().isoformat(),
        "ts": res.ts.isoformat(),
        "spot": res.spot,
        "total_gex": s.total_gex, "call_gex": s.call_gex, "put_gex": s.put_gex,
        "zero_gamma": s.zero_gamma, "zero_gamma_dist_pct": _dist_pct(s.zero_gamma, res.spot),
        "call_wall": s.call_wall, "call_wall_dist_pct": _dist_pct(s.call_wall, res.spot),
        "put_wall": s.put_wall, "put_wall_dist_pct": _dist_pct(s.put_wall, res.spot),
        "call_wall_oi": s.call_wall_oi, "put_wall_oi": s.put_wall_oi, "max_pain": s.max_pain,
        "put_call_oi_ratio": s.put_call_oi_ratio, "total_call_oi": s.total_call_oi, "total_put_oi": s.total_put_oi,
        "dex": e.dex if e else None, "dex_shares": e.dex_shares if e else None, "vex": e.vex if e else None,
        "cex": e.cex if e else None, "vanna_flip": e.vanna_flip if e else None,
        "net_gex_up_1pct": e.net_gex_up_1pct if e else None, "net_gex_down_1pct": e.net_gex_down_1pct if e else None,
        "gamma_imbalance_pct_adv": hf.pct_adv_per_1pct if hf else None,
        "hedge_shares_per_1pct": hf.shares_per_1pct if hf else None, "regime": hf.regime if hf else None,
        "volume_total_gex": vl.total_gex if vl else None, "volume_zero_gamma": vl.zero_gamma if vl else None,
        "put_call_volume_ratio": vl.put_call_volume_ratio if vl else None,
        "implied_daily_move_pct": im.iv_daily_move_pct if im else None,
        "straddle_move_pct": im.straddle_move_pct if im else None, "atm_iv": im.atm_iv if im else None,
        "absolute_gamma_strike": c.absolute_gamma_strike if c else None, "gex_hhi": c.gex_hhi if c else None,
        "zero_dte_share": c.zero_dte_share if c else None,
        "iv30": ivc.iv30 if ivc else None, "iv30_rank_1y": ivc.iv30_rank_1y if ivc else None,
        "iv30_percentile_1y": ivc.iv30_percentile_1y if ivc else None, "hv30": ivc.hv30 if ivc else None,
        "iv_hv_spread": ivc.iv_hv_spread if ivc else None,
        "roll_off_nearest_total_gex": nearest.total_gex if nearest else None,
        "roll_off_nearest_removed_share": nearest.abs_gex_removed_share if nearest else None,
        "contracts_used": res.meta.contracts_used, "stale": res.meta.stale,
    }
    return {k: row.get(k) for k in FEATURE_COLUMNS}


def features_csv(rows: list[dict]) -> str:
    import csv
    import io

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FEATURE_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in r.items()})
    return buf.getvalue()


# -------------------------------------------------------------------- complex

COMPLEXES: dict[str, list[str]] = {
    # anchor first; members are converted to anchor index level via spot ratio
    "SPX": ["SPX", "SPY", "XSP"],
    "NDX": ["NDX", "QQQ"],
    "RUT": ["RUT", "IWM"],
    "VIX": ["VIX"],
}


class ComplexMember(BaseModel):
    symbol: str
    spot: float
    ratio_to_anchor: float
    total_gex: float
    share_of_abs_gex: float
    call_wall: float | None
    call_wall_anchor_level: float | None
    put_wall: float | None
    put_wall_anchor_level: float | None
    zero_gamma: float | None
    zero_gamma_anchor_level: float | None
    data_age_s: float
    stale: bool


class ComplexResponse(BaseModel):
    name: str
    anchor: str
    anchor_spot: float
    members: list[ComplexMember]
    missing: list[str]
    total_gex: float
    regime: str
    zero_gamma_anchor_level: float | None
    call_wall_anchor_level: float | None
    put_wall_anchor_level: float | None
    profile: list[dict]  # anchor-level buckets: level, net_gex
    note: str


def _anchor_step(anchor: GexResponse) -> float:
    strikes = sorted({p.strike for p in anchor.profile})
    diffs = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    return min(diffs) if diffs else 1.0


def complex_view(name: str, results: dict[str, GexResponse], members: list[str]) -> ComplexResponse:
    anchor_sym = members[0]
    anchor = results.get(anchor_sym)
    present = [m for m in members if m in results]
    missing = [m for m in members if m not in results]
    if anchor is None:
        # fall back to the first available member as the level reference
        anchor_sym = present[0]
        anchor = results[anchor_sym]
    a_spot = anchor.spot
    step = _anchor_step(anchor)
    abs_total = sum(abs(results[m].summary.total_gex) for m in present) or 1.0
    out_members: list[ComplexMember] = []
    buckets: dict[float, float] = {}
    curve_sum: np.ndarray | None = None
    for m in present:
        r = results[m]
        ratio = a_spot / r.spot if r.spot > 0 else 1.0
        conv = lambda x: (round(x * ratio / step) * step) if x is not None else None  # noqa: E731
        out_members.append(
            ComplexMember(
                symbol=m, spot=r.spot, ratio_to_anchor=ratio, total_gex=r.summary.total_gex,
                share_of_abs_gex=abs(r.summary.total_gex) / abs_total,
                call_wall=r.summary.call_wall, call_wall_anchor_level=conv(r.summary.call_wall),
                put_wall=r.summary.put_wall, put_wall_anchor_level=conv(r.summary.put_wall),
                zero_gamma=r.summary.zero_gamma, zero_gamma_anchor_level=(r.summary.zero_gamma * ratio) if r.summary.zero_gamma else None,
                data_age_s=r.meta.data_age_s, stale=r.meta.stale,
            )
        )
        for p in r.profile:
            lvl = conv(p.strike)
            buckets[lvl] = buckets.get(lvl, 0.0) + p.net_gex
        if r.gamma_curve and len(r.gamma_curve) > 1:
            curve = np.array([pt.net_gex for pt in r.gamma_curve])
            curve_sum = curve if curve_sum is None else (curve_sum + curve if curve_sum.size == curve.size else curve_sum)
    total = sum(results[m].summary.total_gex for m in present)
    profile = [{"level": k, "net_gex": v} for k, v in sorted(buckets.items())]
    cw = max(profile, key=lambda p: p["net_gex"])["level"] if profile and max(p["net_gex"] for p in profile) > 0 else None
    pw = min(profile, key=lambda p: p["net_gex"])["level"] if profile and min(p["net_gex"] for p in profile) < 0 else None
    zg = None
    if curve_sum is not None and anchor.gamma_curve:
        levels = np.array([pt.level for pt in anchor.gamma_curve])
        from .gex import _find_zero_crossing

        zg = _find_zero_crossing(levels, curve_sum, a_spot)
    regime = "flat" if abs(total) < 1e-9 else ("positive_gamma" if total > 0 else "negative_gamma")
    return ComplexResponse(
        name=name, anchor=anchor_sym, anchor_spot=a_spot, members=out_members, missing=missing, total_gex=total,
        regime=regime, zero_gamma_anchor_level=zg, call_wall_anchor_level=cw, put_wall_anchor_level=pw, profile=profile,
        note="Dollar GEX summed across members; strikes mapped to anchor level by spot ratio and bucketed to the anchor's"
        " strike step. Gamma curves are summed on the shared ±10% relative grid. CME futures options (ES/NQ) are not"
        " included: IBKR options on those are a different secType and would need a separate chain path.",
    )


# ----------------------------------------------------------------------- scan


class ScanRow(BaseModel):
    symbol: str
    status: str  # ok | pending | error
    spot: float | None = None
    total_gex: float | None = None
    regime: str | None = None
    gamma_imbalance_pct_adv: float | None = None
    zero_gamma: float | None = None
    zero_gamma_dist_pct: float | None = None
    call_wall: float | None = None
    call_wall_dist_pct: float | None = None
    put_wall: float | None = None
    put_wall_dist_pct: float | None = None
    zero_dte_share: float | None = None
    gex_hhi: float | None = None
    iv30_rank_1y: float | None = None
    implied_daily_move_pct: float | None = None
    data_age_s: float | None = None
    stale: bool | None = None
    error: str | None = None


SCAN_SORT_KEYS = {
    "gamma_imbalance_pct_adv", "total_gex", "zero_gamma_dist_pct", "zero_dte_share", "gex_hhi", "iv30_rank_1y",
    "implied_daily_move_pct", "call_wall_dist_pct", "put_wall_dist_pct",
}


def scan_row(symbol: str, res: GexResponse | None, error: str | None = None) -> ScanRow:
    if res is None:
        return ScanRow(symbol=symbol, status="error" if error else "pending", error=error)
    s, hf, c, ivc, im = res.summary, res.hedge_flow, res.concentration, res.iv_context, res.implied_move
    return ScanRow(
        symbol=symbol, status="ok", spot=res.spot, total_gex=s.total_gex, regime=hf.regime if hf else None,
        gamma_imbalance_pct_adv=hf.pct_adv_per_1pct if hf else None, zero_gamma=s.zero_gamma,
        zero_gamma_dist_pct=_dist_pct(s.zero_gamma, res.spot), call_wall=s.call_wall,
        call_wall_dist_pct=_dist_pct(s.call_wall, res.spot), put_wall=s.put_wall,
        put_wall_dist_pct=_dist_pct(s.put_wall, res.spot), zero_dte_share=c.zero_dte_share if c else None,
        gex_hhi=c.gex_hhi if c else None, iv30_rank_1y=ivc.iv30_rank_1y if ivc else None,
        implied_daily_move_pct=im.iv_daily_move_pct if im else None, data_age_s=res.meta.data_age_s, stale=res.meta.stale,
    )


def sort_scan(rows: list[ScanRow], key: str, descending: bool = True) -> list[ScanRow]:
    def k(row: ScanRow):
        v = getattr(row, key, None)
        if v is None:
            return (1, 0.0)
        return (0, -abs(v) if descending else abs(v))

    return sorted(rows, key=k)


# ---------------------------------------------------------- intraday OI lens


class OIEstimate(BaseModel):
    opening_ratio: float
    calibrated_ratio: float | None
    note: str
    prev_total_oi: float
    total_volume: float
    est_total_oi: float
    est_total_gex: float
    est_zero_gamma: float | None
    est_call_wall: float | None
    est_put_wall: float | None
    reconcile_history: list[dict]


def estimate_oi_rows(rows: list[ChainRow], opening_ratio: float) -> list[ChainRow]:
    """Prior-settlement OI plus the net-opening share of today's volume."""
    out = []
    for row in rows:
        vol = row.volume or 0.0
        out.append(replace(row, oi=max(0.0, (row.oi or 0.0) + opening_ratio * vol)))
    return out


def calibrated_opening_ratio(reconcile_rows: list[dict]) -> float | None:
    ratios = []
    for r in reconcile_rows:
        if r.get("actual_total_oi") is None or not r.get("total_volume"):
            continue
        ratios.append((r["actual_total_oi"] - r["prev_total_oi"]) / r["total_volume"])
    return float(np.median(ratios)) if ratios else None


def oi_estimate(snapshot: ChainSnapshot, r: float, q: float, opening_ratio: float, reconcile_rows: list[dict]) -> OIEstimate:
    from .gex import compute_gex

    est_rows = estimate_oi_rows(snapshot.rows, opening_ratio)
    est_snap = replace(snapshot, rows=est_rows)
    res = compute_gex(est_snap, r, q)
    prev_oi = float(sum(row.oi or 0.0 for row in snapshot.rows))
    vol = float(sum(row.volume or 0.0 for row in snapshot.rows))
    return OIEstimate(
        opening_ratio=opening_ratio,
        calibrated_ratio=calibrated_opening_ratio(reconcile_rows),
        note="Heuristic: est_OI = settlement_OI + opening_ratio x today's volume per contract. IBKR does not publish"
        " intraday OI; opening_ratio is a guess (default 0.5) that the next-day reconciliation table lets you calibrate.",
        prev_total_oi=prev_oi,
        total_volume=vol,
        est_total_oi=prev_oi + opening_ratio * vol,
        est_total_gex=res.summary.total_gex,
        est_zero_gamma=res.summary.zero_gamma,
        est_call_wall=res.summary.call_wall,
        est_put_wall=res.summary.put_wall,
        reconcile_history=reconcile_rows,
    )


def oi_totals_for_reconcile(snapshot: ChainSnapshot, today: date) -> tuple[float, float, str | None]:
    """(prev OI, volume, max expiry) over contracts that survive today's session."""
    today_s = today.strftime("%Y%m%d")
    rows = [row for row in snapshot.rows if row.expiry > today_s]
    if not rows:
        return 0.0, 0.0, None
    return (
        float(sum(row.oi or 0.0 for row in rows)),
        float(sum(row.volume or 0.0 for row in rows)),
        max(row.expiry for row in rows),
    )


def actual_oi_up_to(snapshot: ChainSnapshot, max_expiry: str, after: date) -> float:
    """Total OI for contracts with expiry in (after, max_expiry]."""
    after_s = after.strftime("%Y%m%d")
    return float(sum(row.oi or 0.0 for row in snapshot.rows if after_s < row.expiry <= max_expiry))
