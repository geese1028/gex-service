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
    # Call-wall inventory for this expiry alone.
    call_wall_gex: float = 0.0
    call_wall_oi: float = 0.0
    volume_call_wall: float | None = None
    shares_per_1pct: float = 0.0


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


class RollOffScenario(BaseModel):
    """Profile after a set of expiries drops off (what the book looks like post-expiry)."""

    name: str  # drop_nearest | drop_week | custom
    excluded_expiries: list[str]
    abs_gex_removed_share: float  # share of |GEX| that expires with them (0..1)
    total_gex: float
    zero_gamma: float | None
    call_wall: float | None
    put_wall: float | None
    regime: str


class ZeroDteBook(BaseModel):
    """Walls and zero gamma of the contracts that expire today."""

    expiry: str | None
    dte: float | None
    hours_left: float
    total_gex: float
    abs_gex_share: float
    regime: str
    call_wall: float | None
    put_wall: float | None
    zero_gamma: float | None
    spot_vs_zero_gamma: str  # above | below | at | unknown
    in_walls: bool | None
    shares_per_1pct: float
    hedge_shares_to_call_wall: float | None  # signed; positive = dealers buy on the way there
    hedge_shares_to_put_wall: float | None
    entry_note: str


class CharmClock(BaseModel):
    """Dealer hedge from time decay between now and the 16:00 ET cash close."""

    hours_left: float
    shares_to_close: float  # positive = dealers buy the underlying into the close
    direction: str  # buy | sell | flat
    pin_strike: float | None
    spot_vs_pin: str
    entry_note: str


class PathSwitch(BaseModel):
    """Sign of the expiring book: dampen or amplify. The strike is where that hedge is largest."""

    book: str  # zero_dte | front_week
    regime: str  # positive_gamma | negative_gamma | flat
    path: str  # fade | chase | flat
    expiry: str | None
    strike: float | None
    shares_per_1pct: float
    note: str


class WallTape(BaseModel):
    """Classified customer prints near the open-interest call wall since the tracker started."""

    expiry: str | None
    call_wall: float | None
    strikes: list[float] = Field(default_factory=list)
    customer_call_buy: float = 0.0
    customer_call_sell: float = 0.0
    customer_put_buy: float = 0.0
    customer_put_sell: float = 0.0
    classified_volume: float = 0.0
    dealer_gex: float = 0.0  # positive = customers net sold, dealers longer gamma
    dealer_shares: float = 0.0  # positive = dealers buy the underlying to hedge the new inventory
    tape: str  # longer_gamma | shorter_gamma | quiet | unavailable
    wall_read: str  # fade_stands | fade_weaker | unchanged
    note: str


class SessionPath(BaseModel):
    """Today's path. A live tape with enough size overrides the open-interest sign."""

    source: str  # oi | tape | none
    path: str  # fade | chase | flat
    note: str


class VolControl(BaseModel):
    """Trailing-vol echo for index products. Not a measured fund flow and not a strike."""

    applies: bool
    state: str  # off | selling | full | quiet | unavailable
    rv_20d: float | None = None  # annualized close-to-close
    last_return: float | None = None
    shock_date: str | None = None
    shock_return: float | None = None
    sessions_since_shock: int | None = None
    echo_left: int | None = None
    note: str


class VannaPlay(BaseModel):
    """Dealer hedge if implied vol moves one point, and where that sign flips."""

    vanna_flip: float | None
    spot_vs_flip: str
    distance_pct: float | None
    shares_if_iv_down_1pt: float  # positive = dealers buy
    shares_if_iv_up_1pt: float
    entry_note: str


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


class IVContext(BaseModel):
    iv30: float | None
    iv30_rank_1y: float | None
    iv30_percentile_1y: float | None
    hv30: float | None
    iv_hv_spread: float | None


class GexResponse(BaseModel):
    symbol: str
    sec_type: str
    spot: float
    ts: datetime
    oi_asof: str
    params: ParamsOut
    summary: Summary
    iv_context: IVContext | None = None
    exposures: Exposures | None = None
    hedge_flow: HedgeFlow | None = None
    volume_lens: VolumeLens | None = None
    implied_move: ImpliedMove | None = None
    concentration: Concentration | None = None
    roll_off: list[RollOffScenario] = Field(default_factory=list)
    zero_dte: ZeroDteBook | None = None
    path_switch: PathSwitch | None = None
    wall_tape: WallTape | None = None
    session_path: SessionPath | None = None
    vol_control: VolControl | None = None
    charm_clock: CharmClock | None = None
    vanna_play: VannaPlay | None = None
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


class ExpiryDay(BaseModel):
    """One session's reading of a single expiry's call wall."""

    date: str
    expiry: str
    dte: float | None = None
    ts: datetime
    spot: float
    call_wall: float | None
    call_wall_gex: float = 0.0
    call_wall_oi: float = 0.0
    put_wall: float | None = None
    abs_gex_share: float = 0.0
    total_gex: float = 0.0
    shares_per_1pct: float = 0.0
    pct_adv: float | None = None
    volume_call_wall: float | None = None
    oi_asof: str = ""
    finalized: bool = False


class WallTouch(BaseModel):
    """Intraday print taken while spot is near the front-week call wall."""

    ts: datetime
    expiry: str
    spot: float
    call_wall: float | None
    distance_pct: float
    volume_call_wall: float | None = None
    call_wall_gex: float = 0.0


class WallTrend(BaseModel):
    symbol: str
    front_expiry: str | None
    days: list[ExpiryDay]
    touches: list[WallTouch]


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
