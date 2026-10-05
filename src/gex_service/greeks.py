"""Black-Scholes helpers (vectorised with numpy).

Gamma drives GEX; delta, vanna and charm drive the companion exposures
(DEX / VEX / CEX). All functions broadcast over numpy arrays and return 0.0
for invalid inputs (non-positive spot, strike or sigma) so aggregates stay
finite.
"""

from __future__ import annotations

import numpy as np
from scipy.stats import norm

# Floor on time to expiry: 15 minutes expressed in years. Prevents greeks from
# blowing up for 0DTE contracts in the final minutes.
MIN_T_YEARS = 15.0 / (60.0 * 24.0 * 365.0)

ArrayLike = float | np.ndarray


def _prep(spot, strike, t_years, sigma):
    s = np.asarray(spot, dtype=float)
    k = np.asarray(strike, dtype=float)
    t = np.maximum(np.asarray(t_years, dtype=float), MIN_T_YEARS)
    v = np.asarray(sigma, dtype=float)
    valid = (s > 0) & (k > 0) & (v > 0) & np.isfinite(s) & np.isfinite(k) & np.isfinite(v)
    s = np.where(valid, s, 1.0)
    k = np.where(valid, k, 1.0)
    v = np.where(valid, v, 1.0)
    return s, k, t, v, valid


def _d1_d2(s, k, t, v, r, q):
    sqrt_t = np.sqrt(t)
    d1 = (np.log(s / k) + (r - q + 0.5 * v**2) * t) / (v * sqrt_t)
    return d1, d1 - v * sqrt_t, sqrt_t


def bs_gamma(spot: ArrayLike, strike: ArrayLike, t_years: ArrayLike, sigma: ArrayLike, r: float = 0.0, q: float = 0.0) -> np.ndarray:
    """Gamma (identical for calls and puts)."""
    s, k, t, v, valid = _prep(spot, strike, t_years, sigma)
    d1, _, sqrt_t = _d1_d2(s, k, t, v, r, q)
    gamma = np.exp(-q * t) * norm.pdf(d1) / (s * v * sqrt_t)
    return np.where(valid, gamma, 0.0)


def bs_delta(spot: ArrayLike, strike: ArrayLike, t_years: ArrayLike, sigma: ArrayLike, is_call: ArrayLike, r: float = 0.0, q: float = 0.0) -> np.ndarray:
    """Delta: e^{-qT} N(d1) for calls, e^{-qT} (N(d1) - 1) for puts."""
    s, k, t, v, valid = _prep(spot, strike, t_years, sigma)
    d1, _, _ = _d1_d2(s, k, t, v, r, q)
    call = np.asarray(is_call, dtype=bool)
    delta = np.exp(-q * t) * np.where(call, norm.cdf(d1), norm.cdf(d1) - 1.0)
    return np.where(valid, delta, 0.0)


def bs_vanna(spot: ArrayLike, strike: ArrayLike, t_years: ArrayLike, sigma: ArrayLike, r: float = 0.0, q: float = 0.0) -> np.ndarray:
    """Vanna = d(delta)/d(sigma) per unit of sigma (1.0 = 100 vol points). Same for calls and puts."""
    s, k, t, v, valid = _prep(spot, strike, t_years, sigma)
    d1, d2, _ = _d1_d2(s, k, t, v, r, q)
    vanna = -np.exp(-q * t) * norm.pdf(d1) * d2 / v
    return np.where(valid, vanna, 0.0)


def bs_charm(spot: ArrayLike, strike: ArrayLike, t_years: ArrayLike, sigma: ArrayLike, is_call: ArrayLike, r: float = 0.0, q: float = 0.0) -> np.ndarray:
    """Charm = d(delta)/d(t) per year (delta decay; divide by 365 for per day)."""
    s, k, t, v, valid = _prep(spot, strike, t_years, sigma)
    d1, d2, sqrt_t = _d1_d2(s, k, t, v, r, q)
    call = np.asarray(is_call, dtype=bool)
    common = -np.exp(-q * t) * norm.pdf(d1) * (2.0 * (r - q) * t - d2 * v * sqrt_t) / (2.0 * t * v * sqrt_t)
    charm = np.where(
        call,
        q * np.exp(-q * t) * norm.cdf(d1) + common,
        -q * np.exp(-q * t) * norm.cdf(-d1) + common,
    )
    return np.where(valid, charm, 0.0)


def years_from_days(days: ArrayLike) -> np.ndarray:
    return np.maximum(np.asarray(days, dtype=float) / 365.0, MIN_T_YEARS)
