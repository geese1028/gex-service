"""Scenario surface: dealer delta and net GEX over (spot shift, IV shift, time shift).

Generalises the one-dimensional ``gamma_curve``. For every grid point we
re-price each contract's delta and gamma with Black-Scholes (contract IV plus
the shift, time to expiry minus the shift) and aggregate under the dealer
convention. The difference in dealer delta versus now is the hedging flow
dealers would have to execute to get there.
"""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, Field

from .chain import ChainRow, ChainSnapshot
from .greeks import MIN_T_YEARS, bs_delta, bs_gamma, years_from_days


class ScenarioSurface(BaseModel):
    days: int
    # Matrices indexed [iv_shift][spot_level]
    dealer_delta_shares: list[list[float]]
    hedge_flow_shares: list[list[float]]  # -(dealer_delta - base): positive = dealers must buy
    net_gex: list[list[float]]


class ScenarioGrid(BaseModel):
    symbol: str
    spot: float
    spot_levels: list[float]
    spot_pct: list[float]
    iv_shifts: list[float]  # vol points added to every contract's IV
    day_shifts: list[int]
    base_dealer_delta_shares: float
    base_net_gex: float
    contracts: int
    surfaces: list[ScenarioSurface] = Field(default_factory=list)


def _usable(rows: list[ChainRow]) -> list[ChainRow]:
    return [r for r in rows if r.has_gex_inputs and r.dte > 0]


def scenario_grid(
    snapshot: ChainSnapshot,
    r: float,
    q: float,
    spot_pct: float = 0.05,
    spot_steps: int = 21,
    iv_points: float = 10.0,
    iv_steps: int = 5,
    day_shifts: tuple[int, ...] = (0, 1),
) -> ScenarioGrid:
    rows = _usable(snapshot.rows)
    spot = snapshot.spot
    levels = spot * np.linspace(1 - spot_pct, 1 + spot_pct, spot_steps)
    iv_shifts = np.linspace(-iv_points, iv_points, iv_steps) / 100.0

    n = len(rows)
    k = np.array([row.strike for row in rows], dtype=float)
    t0 = years_from_days(np.array([row.dte for row in rows], dtype=float)) if n else np.array([])
    iv = np.array([row.iv if row.iv is not None else np.nan for row in rows], dtype=float)
    has_iv = np.isfinite(iv) & (iv > 0)
    oi = np.array([row.oi or 0.0 for row in rows], dtype=float)
    mult = np.array([row.multiplier for row in rows], dtype=float)
    sign = np.array([1.0 if row.right == "C" else -1.0 for row in rows], dtype=float)
    is_call = sign > 0
    rep_delta = np.array([row.delta if row.delta is not None else 0.0 for row in rows], dtype=float)
    rep_gamma = np.array([row.gamma if row.gamma is not None else 0.0 for row in rows], dtype=float)
    w = sign * oi * mult

    def delta_gamma(level_col: np.ndarray, iv_shift: float, days: int) -> tuple[np.ndarray, np.ndarray]:
        """Delta and gamma matrices (levels x contracts)."""
        if n == 0:
            return np.zeros((level_col.size, 0)), np.zeros((level_col.size, 0))
        t = t0 - days / 365.0
        expired = t <= 0
        t_safe = np.maximum(t, MIN_T_YEARS)
        iv_s = np.maximum(np.where(has_iv, iv, 0.2) + iv_shift, 0.01)
        d = bs_delta(level_col, k[None, :], t_safe[None, :], iv_s[None, :], is_call[None, :], r, q)
        g = bs_gamma(level_col, k[None, :], t_safe[None, :], iv_s[None, :], r, q)
        # contracts without IV keep their reported greeks
        d = np.where(has_iv[None, :], d, rep_delta[None, :])
        g = np.where(has_iv[None, :], g, rep_gamma[None, :])
        # expired contracts: step delta, zero gamma
        if expired.any():
            itm_call = (level_col > k[None, :]) & is_call[None, :]
            itm_put = (level_col < k[None, :]) & ~is_call[None, :]
            step = np.where(itm_call, 1.0, np.where(itm_put, -1.0, 0.0))
            d = np.where(expired[None, :], step, d)
            g = np.where(expired[None, :], 0.0, g)
        return d, g

    base_d, base_g = delta_gamma(np.array([[spot]]), 0.0, 0)
    base_delta_shares = float((base_d * w[None, :]).sum()) if n else 0.0
    base_net_gex = float((base_g * w[None, :]).sum() * spot * spot * 0.01) if n else 0.0

    surfaces: list[ScenarioSurface] = []
    level_col = levels[:, None]
    for days in day_shifts:
        dd: list[list[float]] = []
        hf: list[list[float]] = []
        ng: list[list[float]] = []
        for shift in iv_shifts:
            d, g = delta_gamma(level_col, float(shift), int(days))
            delta_shares = (d * w[None, :]).sum(axis=1) if n else np.zeros(levels.size)
            gex = (g * w[None, :]).sum(axis=1) * levels**2 * 0.01 if n else np.zeros(levels.size)
            dd.append([float(x) for x in delta_shares])
            hf.append([float(-(x - base_delta_shares)) for x in delta_shares])
            ng.append([float(x) for x in gex])
        surfaces.append(ScenarioSurface(days=int(days), dealer_delta_shares=dd, hedge_flow_shares=hf, net_gex=ng))

    return ScenarioGrid(
        symbol=snapshot.symbol,
        spot=spot,
        spot_levels=[round(float(x), 4) for x in levels],
        spot_pct=[round(float(x / spot - 1) * 100, 3) for x in levels],
        iv_shifts=[round(float(x * 100), 3) for x in iv_shifts],
        day_shifts=[int(d) for d in day_shifts],
        base_dealer_delta_shares=base_delta_shares,
        base_net_gex=base_net_gex,
        contracts=n,
        surfaces=surfaces,
    )
