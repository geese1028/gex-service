"""Option chain discovery and market data collection.

Flow for one symbol:

1. resolve the underlying (Stock on SMART, else Index with a small override map)
2. read the spot price
3. ``reqSecDefOptParams`` -> expirations per trading class (SPX and SPXW merged)
4. pick expirations / strikes within the requested window, cap contract count
5. ``reqContractDetails`` per (expiry, tradingClass) to get real, qualified
   contracts (cached per trading day)
6. subscribe in batches with generic ticks 100 (volume), 101 (open interest),
   106 (model greeks); wait for data; cancel; next batch

The pure selection helpers (``select_expiries``, ``filter_strikes``,
``cap_contracts``) have no IB dependency so they can be unit tested.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Iterable, Sequence

from ib_async import Contract, ContractDetails, Index, Option, Stock, Ticker

from .config import Settings
from .ib_client import NY, IBClient

log = logging.getLogger(__name__)

GENERIC_TICKS = "100,101,106"

# Symbols that are indices rather than SMART-routed stocks/ETFs.
INDEX_OVERRIDES: dict[str, tuple[str, str]] = {
    "SPX": ("CBOE", "USD"),
    "XSP": ("CBOE", "USD"),
    "NDX": ("NASDAQ", "USD"),
    "VIX": ("CBOE", "USD"),
    "RUT": ("RUSSELL", "USD"),
    "DJX": ("CBOE", "USD"),
    "OEX": ("CBOE", "USD"),
}


class ChainError(RuntimeError):
    """Raised when a chain cannot be built for a symbol."""


@dataclass(slots=True)
class ChainParams:
    max_dte: int
    strike_range_pct: float
    max_contracts: int
    expiries: tuple[str, ...] = ()  # explicit YYYYMMDD filter; empty = all within max_dte

    def key(self) -> str:
        return f"dte={self.max_dte}|rng={self.strike_range_pct:.4f}|cap={self.max_contracts}|exp={','.join(self.expiries)}"


@dataclass(slots=True)
class ChainRow:
    con_id: int
    expiry: str  # YYYYMMDD
    dte: float  # calendar days to 16:00 ET expiry, fractional
    strike: float
    right: str  # "C" or "P"
    trading_class: str
    multiplier: float
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    iv: float | None = None
    gamma: float | None = None
    delta: float | None = None
    oi: float | None = None
    volume: float | None = None

    @property
    def has_gex_inputs(self) -> bool:
        return self.oi is not None and self.oi > 0 and (self.gamma is not None or self.iv is not None)


@dataclass(slots=True)
class ChainSnapshot:
    symbol: str
    sec_type: str
    spot: float
    ts: datetime
    oi_asof: str  # ISO date the open interest reflects
    rows: list[ChainRow]
    expirations: list[str]
    contracts_total: int
    contracts_with_data: int
    fetch_duration_s: float
    market_data_type: int | None
    params: ChainParams
    spot_source: str = "last"
    stale: bool = False
    warnings: list[str] = field(default_factory=list)
    # 21-day average daily share volume of the underlying (None for indices).
    adv_shares: float | None = None


# ---------------------------------------------------------------- pure helpers


def expiry_datetime(expiry: str) -> datetime:
    """Expiry instant used for time-to-expiry: 16:00 ET on the expiry date."""
    d = datetime.strptime(expiry, "%Y%m%d").date()
    return datetime.combine(d, dtime(16, 0), tzinfo=NY)


def days_to_expiry(expiry: str, now: datetime) -> float:
    return (expiry_datetime(expiry) - now.astimezone(NY)).total_seconds() / 86400.0


def previous_trading_day(today: date) -> date:
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def oi_as_of(now: datetime) -> str:
    """IBKR open interest is the prior session's settlement figure."""
    return previous_trading_day(now.astimezone(NY).date()).isoformat()


def select_expiries(
    expirations: Iterable[str],
    now: datetime,
    max_dte: int,
    explicit: Sequence[str] = (),
) -> list[str]:
    """Expirations still alive (16:00 ET not passed) within ``max_dte`` days.

    If ``explicit`` is given it is intersected with the alive set instead.
    """
    alive = sorted({e for e in expirations if days_to_expiry(e, now) > 0})
    if explicit:
        wanted = set(explicit)
        return [e for e in alive if e in wanted]
    return [e for e in alive if days_to_expiry(e, now) <= max_dte]


def filter_strikes(strikes: Iterable[float], spot: float, strike_range_pct: float) -> list[float]:
    lo, hi = spot * (1 - strike_range_pct), spot * (1 + strike_range_pct)
    return sorted(k for k in strikes if lo <= k <= hi)


def cap_contracts(rows: Sequence[ChainRow], spot: float, max_contracts: int) -> list[ChainRow]:
    """Keep at most ``max_contracts`` rows, dropping the strikes furthest from spot first.

    Strikes are removed symmetrically across all expiries so every expiry keeps
    the same relative strike window.
    """
    if len(rows) <= max_contracts:
        return list(rows)
    by_distance = sorted({abs(r.strike - spot) for r in rows})
    # Find the largest distance threshold that fits the cap.
    kept: list[ChainRow] = list(rows)
    for threshold in reversed(by_distance):
        kept = [r for r in rows if abs(r.strike - spot) < threshold]
        if len(kept) <= max_contracts:
            break
    if not kept:
        # Even the nearest strike band exceeds the cap: hard-truncate by distance.
        kept = sorted(rows, key=lambda r: (abs(r.strike - spot), r.expiry, r.right))[:max_contracts]
    return kept


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def ticker_to_row(row: ChainRow, ticker: Ticker) -> ChainRow:
    """Copy the relevant fields of an ib_async ``Ticker`` onto a ``ChainRow``."""
    row.bid = _finite(ticker.bid)
    row.ask = _finite(ticker.ask)
    row.last = _finite(ticker.last)
    row.volume = _finite(ticker.volume)
    greeks = ticker.modelGreeks
    if greeks is not None:
        row.iv = _finite(greeks.impliedVol)
        row.gamma = _finite(greeks.gamma)
        row.delta = _finite(greeks.delta)
    if row.iv is None:
        row.iv = _finite(ticker.impliedVolatility)
    primary = ticker.callOpenInterest if row.right == "C" else ticker.putOpenInterest
    secondary = ticker.putOpenInterest if row.right == "C" else ticker.callOpenInterest
    row.oi = _finite(primary)
    if row.oi is None:
        row.oi = _finite(secondary)
    return row


def _row_ready(row: ChainRow, ticker: Ticker, allow_quote_only: bool) -> bool:
    """A row is ready once OI has arrived together with greeks (or, after a
    grace period, just a quote: IB never publishes model greeks for some deep
    ITM / illiquid contracts, so waiting longer would not help)."""
    oi = ticker.callOpenInterest if row.right == "C" else ticker.putOpenInterest
    if _finite(oi) is None:
        return False
    greeks = ticker.modelGreeks
    if greeks is not None and _finite(greeks.gamma) is not None:
        return True
    return allow_quote_only and (_finite(ticker.bid) is not None or _finite(ticker.last) is not None)


def fill_iv_from_counterpart(rows: list[ChainRow]) -> int:
    """Contracts without model greeks borrow the IV of the same-strike
    opposite right (put-call parity implies identical IV). Returns count filled."""
    by_key: dict[tuple[str, float, str], ChainRow] = {(r.expiry, r.strike, r.right): r for r in rows}
    filled = 0
    for row in rows:
        if row.iv is not None or row.gamma is not None:
            continue
        other = by_key.get((row.expiry, row.strike, "P" if row.right == "C" else "C"))
        if other is not None and other.iv is not None:
            row.iv = other.iv
            filled += 1
    return filled


# ------------------------------------------------------------- IB operations


class ChainFetcher:
    def __init__(self, client: IBClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self._underlyings: dict[str, Contract] = {}
        # (symbol, expiry, tradingClass) -> (trading day, details)
        self._details_cache: dict[tuple[str, str, str], tuple[date, list[ContractDetails]]] = {}
        self._adv_cache: dict[str, tuple[date, float]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ underlying
    async def resolve_underlying(self, symbol: str) -> Contract:
        symbol = symbol.upper()
        if symbol in self._underlyings:
            return self._underlyings[symbol]
        self.client.require_connected()
        ib = self.client.ib
        candidates: list[Contract] = []
        if symbol in INDEX_OVERRIDES:
            exchange, currency = INDEX_OVERRIDES[symbol]
            candidates.append(Index(symbol, exchange, currency))
        else:
            candidates.append(Stock(symbol.replace("-", " ").replace(".", " "), "SMART", "USD"))
            candidates.append(Index(symbol, "CBOE", "USD"))
        for candidate in candidates:
            qualified = await ib.qualifyContractsAsync(candidate)
            if qualified and qualified[0] is not None and isinstance(qualified[0], Contract):
                contract = qualified[0]
                self._underlyings[symbol] = contract
                log.info("resolved %s -> %s conId=%s", symbol, contract.secType, contract.conId)
                return contract
        raise ChainError(f"cannot resolve underlying for {symbol!r}")

    async def fetch_spot(self, contract: Contract, wait_s: float = 4.0) -> tuple[float, str]:
        self.client.require_connected()
        ticker = self.client.subscribe(contract, "")
        try:
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline:
                price = _finite(ticker.marketPrice())
                if price is not None and price > 0:
                    return price, "last"
                await asyncio.sleep(0.2)
            for attr in ("last", "close"):
                price = _finite(getattr(ticker, attr))
                if price is not None and price > 0:
                    return price, attr
            mid = _finite(ticker.midpoint())
            if mid is not None and mid > 0:
                return mid, "mid"
        finally:
            self.client.unsubscribe(contract)
        raise ChainError(f"no spot price for {contract.symbol}")

    async def fetch_adv(self, underlying: Contract, today: date, days: int = 21) -> float | None:
        """Average daily share volume over the last ``days`` sessions (cached per day).

        Used for the Barbon-Buraschi gamma imbalance (hedge flow as % of ADV).
        Indices have no volume; returns None for them.
        """
        if underlying.secType != "STK":
            return None
        cached = self._adv_cache.get(underlying.symbol)
        if cached and cached[0] == today:
            return cached[1]
        try:
            bars = await self.client.ib.reqHistoricalDataAsync(
                underlying,
                endDateTime="",
                durationStr=f"{days + 10} D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=2,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("ADV request failed for %s: %s", underlying.symbol, exc)
            return None
        vols = [float(b.volume) for b in bars if b.volume and b.volume > 0 and b.date != today]
        if not vols:
            return None
        adv = sum(vols[-days:]) / len(vols[-days:])
        self._adv_cache[underlying.symbol] = (today, adv)
        return adv

    # ----------------------------------------------------------------- chain
    async def option_params(self, underlying: Contract) -> list:
        ib = self.client.ib
        chains = await ib.reqSecDefOptParamsAsync(
            underlying.symbol, "", underlying.secType, underlying.conId
        )
        smart = [c for c in chains if c.exchange == "SMART"]
        if not smart:
            raise ChainError(f"no SMART option chain for {underlying.symbol}")
        return smart

    async def contract_details(self, symbol: str, expiry: str, trading_class: str, today: date) -> list[ContractDetails]:
        key = (symbol, expiry, trading_class)
        cached = self._details_cache.get(key)
        if cached and cached[0] == today:
            return cached[1]
        option = Option(symbol, expiry, exchange="SMART", tradingClass=trading_class)
        details = await self.client.ib.reqContractDetailsAsync(option)
        self._details_cache[key] = (today, details)
        return details

    async def build_rows(
        self,
        underlying: Contract,
        spot: float,
        params: ChainParams,
        now: datetime,
    ) -> tuple[list[ChainRow], list[str]]:
        chains = await self.option_params(underlying)
        today = now.astimezone(NY).date()
        rows: list[ChainRow] = []
        expirations_used: set[str] = set()
        seen_con_ids: set[int] = set()
        for chain in chains:
            expiries = select_expiries(chain.expirations, now, params.max_dte, params.expiries)
            strikes = set(filter_strikes(chain.strikes, spot, params.strike_range_pct))
            if not expiries or not strikes:
                continue
            # Contract details per expiry are large responses; run a few in parallel.
            sem = asyncio.Semaphore(self.settings.details_concurrency)

            async def _details(expiry: str) -> tuple[str, list[ContractDetails]]:
                async with sem:
                    return expiry, await self.contract_details(underlying.symbol, expiry, chain.tradingClass, today)

            details_started = time.monotonic()
            results = await asyncio.gather(*(_details(e) for e in expiries))
            log.info(
                "%s %s: contract details for %d expiries in %.1fs",
                underlying.symbol, chain.tradingClass, len(expiries), time.monotonic() - details_started,
            )
            for expiry, details in results:
                for d in details:
                    c = d.contract
                    if c is None or c.conId in seen_con_ids:
                        continue
                    if c.strike not in strikes or c.right not in ("C", "P"):
                        continue
                    if c.lastTradeDateOrContractMonth[:8] != expiry:
                        continue
                    seen_con_ids.add(c.conId)
                    expirations_used.add(expiry)
                    rows.append(
                        ChainRow(
                            con_id=c.conId,
                            expiry=expiry,
                            dte=days_to_expiry(expiry, now),
                            strike=float(c.strike),
                            right=c.right,
                            trading_class=c.tradingClass or chain.tradingClass,
                            multiplier=float(c.multiplier or chain.multiplier or 100),
                        )
                    )
        rows = cap_contracts(rows, spot, params.max_contracts)
        return rows, sorted({r.expiry for r in rows})

    def _contract_for(self, underlying: Contract, row: ChainRow) -> Contract:
        return Option(
            symbol=underlying.symbol,
            lastTradeDateOrContractMonth=row.expiry,
            strike=row.strike,
            right=row.right,
            exchange="SMART",
            multiplier=str(int(row.multiplier)) if row.multiplier.is_integer() else str(row.multiplier),
            currency="USD",
            tradingClass=row.trading_class,
            conId=row.con_id,
        )

    async def collect_market_data(self, underlying: Contract, rows: list[ChainRow]) -> int:
        """Fill ``rows`` in place with quotes / greeks / OI. Returns rows with usable data."""
        s = self.settings
        filled = 0
        for start in range(0, len(rows), s.batch_size):
            self.client.require_connected()
            batch = rows[start : start + s.batch_size]
            contracts = [self._contract_for(underlying, r) for r in batch]
            tickers = [self.client.subscribe(c, GENERIC_TICKS) for c in contracts]
            try:
                started = time.monotonic()
                deadline = started + s.batch_wait_s
                grace = started + s.batch_min_wait_s
                while time.monotonic() < deadline:
                    quote_only_ok = time.monotonic() >= grace
                    if all(_row_ready(r, t, quote_only_ok) for r, t in zip(batch, tickers)):
                        break
                    await asyncio.sleep(0.25)
                for r, t in zip(batch, tickers):
                    ticker_to_row(r, t)
                    if r.has_gex_inputs:
                        filled += 1
            finally:
                for c in contracts:
                    self.client.unsubscribe(c)
            if start + s.batch_size < len(rows):
                await asyncio.sleep(s.batch_sleep_s)
        return filled

    # ------------------------------------------------------------------ main
    async def fetch(self, symbol: str, params: ChainParams) -> ChainSnapshot:
        """Build a complete chain snapshot for ``symbol``. Serialised per fetcher."""
        async with self._lock:
            started = time.monotonic()
            self.client.require_connected()
            self.client.apply_market_data_type()
            now = datetime.now(tz=NY)
            underlying = await self.resolve_underlying(symbol)
            spot, spot_source = await self.fetch_spot(underlying)
            adv = await self.fetch_adv(underlying, now.astimezone(NY).date())
            rows, expirations = await self.build_rows(underlying, spot, params, now)
            if not rows:
                raise ChainError(f"no option contracts for {symbol} within params {params.key()}")
            md_started = time.monotonic()
            with_data = await self.collect_market_data(underlying, rows)
            borrowed = fill_iv_from_counterpart(rows)
            with_data = sum(1 for r in rows if r.has_gex_inputs)
            log.info(
                "%s: market data for %d contracts in %.1fs (%d usable, %d borrowed IV)",
                symbol, len(rows), time.monotonic() - md_started, with_data, borrowed,
            )
            warnings: list[str] = []
            if with_data == 0:
                warnings.append("no contracts returned open interest and greeks; check OPRA subscription / market data type")
            elif with_data < len(rows) * 0.5:
                warnings.append(f"only {with_data}/{len(rows)} contracts returned usable data")
            return ChainSnapshot(
                symbol=symbol.upper(),
                sec_type=underlying.secType,
                spot=spot,
                ts=now,
                oi_asof=oi_as_of(now),
                rows=rows,
                expirations=expirations,
                contracts_total=len(rows),
                contracts_with_data=with_data,
                fetch_duration_s=round(time.monotonic() - started, 2),
                market_data_type=self.client.market_data_type,
                params=params,
                spot_source=spot_source,
                warnings=warnings,
                adv_shares=adv,
            )
