"""Pydantic response models shared by the calculator, store and API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ParamsOut(BaseModel):
    max_dte: int
    strike_range_pct: float
    max_contracts: int
    expiries: list[str] = Field(default_factory=list)
    risk_free_rate: float
    dividend_yield: float


class StrikeLevel(BaseModel):
    strike: float
    call_gex: float
    put_gex: float
    net_gex: float
    call_oi: float
    put_oi: float
    call_volume: float = 0.0
    put_volume: float = 0.0
    # Volume-weighted GEX at this strike (today's flow lens; see VolumeLens).
    net_gex_volume: float = 0.0
    # Companion dealer exposures at this strike (dollar delta terms).
    net_dex: float = 0.0
    net_vex: float = 0.0
    net_cex: float = 0.0


class CurvePoint(BaseModel):
    level: float
    net_gex: float
    net_vex: float | None = None


class ExpiryStats(BaseModel):
    expiry: str
    dte: float
    total_gex: float
    call_gex: float
    put_gex: float
    call_wall: float | None
    put_wall: float | None
    contracts: int
    vex: float = 0.0
    cex: float = 0.0
    # Share of the chain's absolute GEX sitting in this expiry (0..1).
    abs_gex_share: float = 0.0


class Exposures(BaseModel):
    """Companion dealer exposures (dealer long calls / short puts convention)."""

    dex: float  # dealer net dollar delta
    dex_shares: float  # dex / spot: underlying shares dealers are long (they hedge by shorting this many)
    vex: float  # dollar delta change for +1 implied-vol point
    cex: float  # dollar delta change per calendar day
    vanna_flip: float | None  # spot level where net VEX changes sign (None if no flip in range)
    net_gex_up_1pct: float | None  # net GEX if spot rises 1% (from the BS curve)
    net_gex_down_1pct: float | None


class HedgeFlow(BaseModel):
    """Delta-hedging flow implications, following Barbon & Buraschi (2021)."""

    shares_per_1pct: float  # |GEX| / spot: shares dealers trade per 1% move
    pct_adv_per_1pct: float | None  # gamma imbalance Γ^IB: shares_per_1pct / ADV, in percent
    adv_shares: float | None
    regime: str  # positive_gamma | negative_gamma | flat
    distance_to_zero_gamma_pct: float | None  # (zero_gamma - spot) / spot * 100
    direction_note: str


class VolumeLens(BaseModel):
    """Same GEX machinery weighted by today's volume instead of open interest."""

    total_gex: float
    call_wall: float | None
    put_wall: float | None
    zero_gamma: float | None
    total_call_volume: float
    total_put_volume: float
    put_call_volume_ratio: float | None


class ImpliedMove(BaseModel):
    expiry: str
    dte: float
    atm_strike: float
    straddle_price: float | None
    straddle_move_pct: float | None  # straddle / spot * 100
    atm_iv: float | None
    iv_move_to_expiry_pct: float | None  # atm_iv * sqrt(T) * 100
    iv_daily_move_pct: float | None  # atm_iv / sqrt(252) * 100


class Concentration(BaseModel):
    absolute_gamma_strike: float | None  # strike with the largest |call_gex| + |put_gex|
    top_strikes: list[float]  # top 5 strikes by absolute gamma
    gex_hhi: float | None  # Herfindahl index of |GEX| across strikes (1 = all at one strike)
    zero_dte_share: float | None  # share of |GEX| in expiries with dte < 1


class Summary(BaseModel):
    total_gex: float
    call_gex: float
    put_gex: float
    zero_gamma: float | None
    zero_gamma_method: str
    call_wall: float | None
    put_wall: float | None
    call_wall_oi: float | None
    put_wall_oi: float | None
    max_pain: float | None
    total_call_oi: float
    total_put_oi: float
    put_call_oi_ratio: float | None
    # Net GEX at the current spot from the Black-Scholes curve, when available.
    curve_gex_at_spot: float | None = None


class Meta(BaseModel):
    contracts_total: int
    contracts_used: int
    dropped_contracts: int
    fetch_duration_s: float
    data_age_s: float
    market_data_type: int | None
    spot_source: str
    stale: bool = False
    warnings: list[str] = Field(default_factory=list)


class GexResponse(BaseModel):
    symbol: str
    sec_type: str
    spot: float
    ts: datetime
    oi_asof: str
    params: ParamsOut
    summary: Summary
    exposures: Exposures | None = None
    hedge_flow: HedgeFlow | None = None
    volume_lens: VolumeLens | None = None
    implied_move: ImpliedMove | None = None
    concentration: Concentration | None = None
    profile: list[StrikeLevel]
    gamma_curve: list[CurvePoint]
    by_expiry: list[ExpiryStats]
    meta: Meta


class ChainRowOut(BaseModel):
    con_id: int
    expiry: str
    dte: float
    strike: float
    right: str
    trading_class: str
    multiplier: float
    bid: float | None
    ask: float | None
    last: float | None
    iv: float | None
    gamma: float | None
    delta: float | None
    oi: float | None
    volume: float | None
    gex: float | None


class ChainResponse(BaseModel):
    symbol: str
    spot: float
    ts: datetime
    oi_asof: str
    rows: list[ChainRowOut]


class HistoryPoint(BaseModel):
    ts: datetime
    spot: float
    total_gex: float
    zero_gamma: float | None
    call_wall: float | None
    put_wall: float | None


class HealthResponse(BaseModel):
    ok: bool
    ib_connected: bool
    ib_last_error: str | None
    market_data_type: int | None
    lines_in_use: int
    watched_symbols: list[str]
    uptime_s: float
    rss_mb: float | None
    version: str


class WatchEntry(BaseModel):
    symbol: str
    pinned: bool
    last_access_ts: datetime | None
    last_refresh_ts: datetime | None
    last_error: str | None
