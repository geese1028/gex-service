"""Option chain discovery and market data collection.

Flow for one symbol, all through the Nautilus IB adapter:

1. resolve the underlying via ``request_instruments``
2. read the spot price (Nautilus ``subscribe_market_data`` / index feed)
3. load the chain with ``IBContract(build_options_chain=True, min/max_expiry_days=...)``
4. keep strikes within ``spot * (1 ± strike_range_pct)`` and cap contract count
5. subscribe in IB-sized batches with generic ticks 100/101/106; cancel; next batch

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

from nautilus_trader.adapters.interactive_brokers.common import IBContract
from nautilus_trader.model.enums import OptionKind
from nautilus_trader.model.instruments import OptionContract

from .config import Settings
from .ib_client import NY, IBClient, QuoteSnapshot, market_data_batch_size, option_expiry_yyyymmdd

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
    # One year of IBKR 30-day implied / historical volatility (daily closes), oldest first.
    iv30_history: list[float] | None = None
    hv30_history: list[float] | None = None


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


def quote_to_row(row: ChainRow, quote: QuoteSnapshot) -> ChainRow:
    """Copy a Nautilus-collected option snapshot onto a ``ChainRow``."""
    row.bid = _finite(quote.bid)
    row.ask = _finite(quote.ask)
    row.last = _finite(quote.last)
    row.volume = _finite(quote.volume)
    row.iv = _finite(quote.iv)
    row.gamma = _finite(quote.gamma)
    row.delta = _finite(quote.delta)
    primary = quote.call_oi if row.right == "C" else quote.put_oi
    secondary = quote.put_oi if row.right == "C" else quote.call_oi
    row.oi = _finite(primary)
    if row.oi is None:
        row.oi = _finite(secondary)
    return row


def ticker_to_row(row: ChainRow, ticker: object) -> ChainRow:
    """Accept ``QuoteSnapshot`` or a duck-typed ticker (tests / old callers)."""
    if isinstance(ticker, QuoteSnapshot):
        return quote_to_row(row, ticker)
    greeks = getattr(ticker, "modelGreeks", None)
    quote = QuoteSnapshot(
        bid=_finite(getattr(ticker, "bid", None)),
        ask=_finite(getattr(ticker, "ask", None)),
        last=_finite(getattr(ticker, "last", None)),
        volume=_finite(getattr(ticker, "volume", None)),
        iv=_finite(getattr(greeks, "impliedVol", None) if greeks is not None else getattr(ticker, "impliedVolatility", None)),
        gamma=_finite(getattr(greeks, "gamma", None) if greeks is not None else None),
        delta=_finite(getattr(greeks, "delta", None) if greeks is not None else None),
        call_oi=_finite(getattr(ticker, "callOpenInterest", None)),
        put_oi=_finite(getattr(ticker, "putOpenInterest", None)),
    )
    return quote_to_row(row, quote)


def _row_ready(row: ChainRow, quote: QuoteSnapshot | object, allow_quote_only: bool) -> bool:
    """A row is ready once OI has arrived together with greeks (or, after a
    grace period, just a quote: IB never publishes model greeks for some deep
    ITM / illiquid contracts, so waiting longer would not help)."""
    if not isinstance(quote, QuoteSnapshot):
        quote = QuoteSnapshot(
            bid=_finite(getattr(quote, "bid", None)),
            last=_finite(getattr(quote, "last", None)),
            gamma=_finite(getattr(getattr(quote, "modelGreeks", None), "gamma", None)),
            call_oi=_finite(getattr(quote, "callOpenInterest", None)),
            put_oi=_finite(getattr(quote, "putOpenInterest", None)),
        )
    oi = quote.call_oi if row.right == "C" else quote.put_oi
    if _finite(oi) is None:
        return False
    if _finite(quote.gamma) is not None:
        return True
    return allow_quote_only and (_finite(quote.bid) is not None or _finite(quote.last) is not None)


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
        self._underlyings: dict[str, IBContract] = {}
        # (symbol, max_dte, expiries) -> (trading day, option instruments)
        self._chain_cache: dict[tuple[str, int, str], tuple[date, list]] = {}
        # (symbol, expiry) -> (trading day, raw ibapi contract details)
        self._details_cache: dict[tuple[str, str], tuple[date, list]] = {}
        self._adv_cache: dict[str, tuple[date, float]] = {}
        self._vol_cache: dict[str, tuple[date, list[float] | None, list[float] | None]] = {}
        self._bar_cache: dict[tuple[str, str, str], tuple[date, list]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ underlying
    async def resolve_underlying(self, symbol: str) -> IBContract:
        symbol = symbol.upper()
        if symbol in self._underlyings:
            return self._underlyings[symbol]
        self.client.require_connected()
        ib_symbol = symbol.replace("-", " ").replace(".", " ")
        candidates: list[IBContract] = []
        if symbol in INDEX_OVERRIDES:
            exchange, currency = INDEX_OVERRIDES[symbol]
            candidates.append(IBContract(secType="IND", symbol=symbol, exchange=exchange, currency=currency))
        else:
            candidates.append(IBContract(secType="STK", symbol=ib_symbol, exchange="SMART", currency="USD"))
            candidates.append(IBContract(secType="IND", symbol=symbol, exchange="CBOE", currency="USD"))
        for candidate in candidates:
            try:
                contract = await self.client.qualify(candidate)
            except Exception as exc:  # noqa: BLE001
                log.debug("qualify %s failed: %s", candidate, exc)
                continue
            if contract is None or not contract.conId:
                continue
            self._underlyings[symbol] = contract
            log.info("resolved %s -> %s conId=%s", symbol, contract.secType, contract.conId)
            return contract
        raise ChainError(f"cannot resolve underlying for {symbol!r}")

    async def fetch_spot(self, contract: IBContract, wait_s: float = 4.0) -> tuple[float, str]:
        self.client.require_connected()
        quote = await self.client.subscribe(contract, "")
        try:
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline:
                price = _finite(quote.market_price())
                if price is not None and price > 0:
                    return price, "last" if quote.last else "mid"
                await asyncio.sleep(0.2)
            for attr, source in ((quote.last, "last"), (quote.close, "close"), (quote.mid(), "mid")):
                price = _finite(attr)
                if price is not None and price > 0:
                    return price, source
        finally:
            await self.client.unsubscribe(contract)
        raise ChainError(f"no spot price for {contract.symbol}")

    async def fetch_adv(self, underlying: IBContract, today: date, days: int = 21) -> float | None:
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
            bars = await self.client.request_bars(underlying, "1-DAY-LAST", f"{days + 10} D")
        except Exception as exc:  # noqa: BLE001
            log.warning("ADV request failed for %s: %s", underlying.symbol, exc)
            return None
        vols = [
            float(b.volume)
            for b in bars
            if b.volume and b.volume > 0 and b.date.astimezone(NY).date() != today
        ]
        if not vols:
            return None
        adv = sum(vols[-days:]) / len(vols[-days:])
        self._adv_cache[underlying.symbol] = (today, adv)
        return adv

    def _bar_spec(self, bar_size: str) -> str:
        mapping = {
            "1 day": "1-DAY-LAST",
            "1 days": "1-DAY-LAST",
            "5 mins": "5-MINUTE-LAST",
            "5 min": "5-MINUTE-LAST",
            "1 min": "1-MINUTE-LAST",
            "1 mins": "1-MINUTE-LAST",
        }
        return mapping.get(bar_size, "1-DAY-LAST")

    def _duration_spec(self, duration: str) -> str:
        text = duration.strip()
        if " " in text:
            return text
        # "5 D" is the Nautilus Historic client format; accept "5D" too.
        for suffix in ("D", "W", "M", "Y", "S"):
            if text.upper().endswith(suffix) and text[:-1].isdigit():
                return f"{text[:-1]} {suffix}"
        return text

    async def fetch_bars(self, underlying: IBContract, duration: str, bar_size: str, use_rth: bool = True) -> list:
        """TRADES/MID bars for the underlying through Nautilus (cached per day)."""
        today = datetime.now(tz=NY).date()
        key = (underlying.symbol, duration, bar_size)
        cached = self._bar_cache.get(key)
        if cached and cached[0] == today:
            return cached[1]
        bars = await self.client.request_bars(
            underlying, self._bar_spec(bar_size), self._duration_spec(duration), use_rth=use_rth
        )
        self._bar_cache[key] = (today, bars)
        return bars

    async def fetch_vol_history(self, underlying: IBContract, today: date) -> tuple[list[float] | None, list[float] | None]:
        """One year of IBKR 30-day implied and historical volatility (cached per day)."""
        cached = self._vol_cache.get(underlying.symbol)
        if cached and cached[0] == today:
            return cached[1], cached[2]
        out: list[list[float] | None] = []
        for what in ("OPTION_IMPLIED_VOLATILITY", "HISTORICAL_VOLATILITY"):
            try:
                bars = await self.client.request_what_bars(underlying, "1 Y", what)
                vals = [float(b.close) for b in bars if b.close is not None and b.close > 0]
                out.append(vals or None)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s history failed for %s: %s", what, underlying.symbol, exc)
                out.append(None)
        self._vol_cache[underlying.symbol] = (today, out[0], out[1])
        return out[0], out[1]

    # ----------------------------------------------------------------- chain
    async def option_expiries(self, underlying: IBContract) -> list[str]:
        chains = await self.client.get_option_chains(underlying)
        if not chains:
            raise ChainError(f"no option chain for {underlying.symbol}")
        expirations: set[str] = set()
        smart = [c for c in chains if c and c[0] == "SMART"]
        chosen = smart or list(chains)
        for chain in chosen:
            for expiry in chain[1] or ():
                text = str(expiry).replace("-", "")[:8]
                if len(text) == 8 and text.isdigit():
                    expirations.add(text)
        if not expirations:
            raise ChainError(f"no SMART option expiries for {underlying.symbol}")
        return sorted(expirations)

    async def contract_details(self, underlying: IBContract, expiry: str, today: date) -> list:
        key = (underlying.symbol, expiry)
        cached = self._details_cache.get(key)
        if cached and cached[0] == today:
            return cached[1]
        details = await self.client.get_option_chain_details(underlying, expiry, "SMART")
        self._details_cache[key] = (today, details or [])
        return details or []

    def _row_from_instrument(self, instrument: OptionContract, now: datetime) -> ChainRow | None:
        provider = self.client.instrument_provider
        ib_contract = provider.contract.get(instrument.id) if provider is not None else None
        con_id = int(ib_contract.conId) if ib_contract is not None and ib_contract.conId else 0
        if not con_id:
            return None
        last_trade = ""
        if ib_contract is not None:
            last_trade = getattr(ib_contract, "lastTradeDateOrContractMonth", "") or ""
        expiry = option_expiry_yyyymmdd(last_trade, instrument.expiration_utc)
        kind = instrument.option_kind
        right = "C" if kind == OptionKind.CALL else "P"
        trading_class = ""
        if ib_contract is not None:
            trading_class = ib_contract.tradingClass or ""
        return ChainRow(
            con_id=con_id,
            expiry=expiry,
            dte=days_to_expiry(expiry, now),
            strike=float(instrument.strike_price),
            right=right,
            trading_class=trading_class,
            multiplier=float(instrument.multiplier),
        )

    async def _load_chain_instruments(
        self,
        underlying: IBContract,
        params: ChainParams,
        today: date,
    ) -> list:
        cache_key = (underlying.symbol, params.max_dte, ",".join(params.expiries))
        cached = self._chain_cache.get(cache_key)
        if cached and cached[0] == today:
            return cached[1]
        started = time.monotonic()
        if params.expiries:
            sem = asyncio.Semaphore(self.settings.details_concurrency)

            async def _one(expiry: str):
                async with sem:
                    return await self.client.load_options_chain(underlying, max_expiry_days=params.max_dte, expiry=expiry)

            batches = await asyncio.gather(*(_one(e) for e in params.expiries))
            instruments = [inst for batch in batches for inst in (batch or [])]
        else:
            instruments = await self.client.load_options_chain(underlying, max_expiry_days=params.max_dte) or []
        options = [inst for inst in instruments if isinstance(inst, OptionContract)]
        log.info(
            "%s: Nautilus options chain loaded %d contracts in %.1fs",
            underlying.symbol, len(options), time.monotonic() - started,
        )
        self._chain_cache[cache_key] = (today, options)
        return options

    async def build_rows(
        self,
        underlying: IBContract,
        spot: float,
        params: ChainParams,
        now: datetime,
    ) -> tuple[list[ChainRow], list[str]]:
        today = now.astimezone(NY).date()
        instruments = await self._load_chain_instruments(underlying, params, today)
        lo, hi = spot * (1 - params.strike_range_pct), spot * (1 + params.strike_range_pct)
        allow = set(params.expiries)
        rows: list[ChainRow] = []
        seen: set[int] = set()
        for instrument in instruments:
            row = self._row_from_instrument(instrument, now)
            if row is None or row.con_id in seen:
                continue
            if allow and row.expiry not in allow:
                continue
            if row.dte < 0 or row.dte > params.max_dte:
                continue
            if row.strike < lo or row.strike > hi:
                continue
            seen.add(row.con_id)
            rows.append(row)
        if not rows:
            # Provider range load can miss a SMART class; fall back to per-expiry details.
            rows, expirations = await self._build_rows_from_details(underlying, spot, params, now)
            return rows, expirations
        rows = cap_contracts(rows, spot, params.max_contracts)
        return rows, sorted({r.expiry for r in rows})

    async def _build_rows_from_details(
        self,
        underlying: IBContract,
        spot: float,
        params: ChainParams,
        now: datetime,
    ) -> tuple[list[ChainRow], list[str]]:
        all_expiries = await self.option_expiries(underlying)
        expiries = select_expiries(all_expiries, now, params.max_dte, params.expiries)
        if not expiries:
            return [], []
        today = now.astimezone(NY).date()
        lo, hi = spot * (1 - params.strike_range_pct), spot * (1 + params.strike_range_pct)
        rows: list[ChainRow] = []
        seen_con_ids: set[int] = set()
        sem = asyncio.Semaphore(self.settings.details_concurrency)

        async def _details(expiry: str) -> tuple[str, list]:
            async with sem:
                return expiry, await self.contract_details(underlying, expiry, today)

        results = await asyncio.gather(*(_details(e) for e in expiries))
        for expiry, details in results:
            for item in details:
                c = getattr(item, "contract", item)
                if c is None or getattr(c, "conId", 0) in seen_con_ids:
                    continue
                right = getattr(c, "right", "")
                strike = float(getattr(c, "strike", 0) or 0)
                last_trade = str(getattr(c, "lastTradeDateOrContractMonth", "") or "")[:8]
                if right not in ("C", "P") or last_trade != expiry:
                    continue
                if strike < lo or strike > hi:
                    continue
                seen_con_ids.add(int(c.conId))
                rows.append(
                    ChainRow(
                        con_id=int(c.conId),
                        expiry=expiry,
                        dte=days_to_expiry(expiry, now),
                        strike=strike,
                        right=right,
                        trading_class=getattr(c, "tradingClass", "") or "",
                        multiplier=float(getattr(c, "multiplier", None) or 100),
                    )
                )
        return cap_contracts(rows, spot, params.max_contracts), sorted({r.expiry for r in rows})

    def _contract_for(self, underlying: IBContract, row: ChainRow) -> IBContract:
        return IBContract(
            secType="OPT",
            conId=row.con_id,
            symbol=underlying.symbol,
            lastTradeDateOrContractMonth=row.expiry,
            strike=row.strike,
            right=row.right,
            exchange="SMART",
            multiplier=str(int(row.multiplier)) if row.multiplier.is_integer() else str(row.multiplier),
            currency="USD",
            tradingClass=row.trading_class,
        )

    async def collect_market_data(self, underlying: IBContract, rows: list[ChainRow]) -> int:
        """Fill ``rows`` in place with quotes / greeks / OI. Returns rows with usable data."""
        s = self.settings
        filled = 0
        start = 0
        while start < len(rows):
            self.client.require_connected()
            step = market_data_batch_size(s.batch_size, s.max_md_lines, self.client.lines_in_use)
            if step <= 0:
                raise ChainError(
                    f"no free market-data lines ({self.client.lines_in_use}/{s.max_md_lines} in use)"
                )
            batch = rows[start : start + step]
            contracts = [self._contract_for(underlying, r) for r in batch]
            quotes: list[QuoteSnapshot] = []
            try:
                for contract in contracts:
                    quotes.append(await self.client.subscribe(contract, GENERIC_TICKS))
                started = time.monotonic()
                deadline = started + s.batch_wait_s
                grace = started + s.batch_min_wait_s
                while time.monotonic() < deadline:
                    quote_only_ok = time.monotonic() >= grace
                    if all(_row_ready(r, q, quote_only_ok) for r, q in zip(batch, quotes)):
                        break
                    await asyncio.sleep(0.25)
                for row, quote in zip(batch, quotes):
                    quote_to_row(row, quote)
                    if row.has_gex_inputs:
                        filled += 1
            finally:
                for contract in contracts:
                    await self.client.unsubscribe(contract)
            start += step
            if start < len(rows):
                await asyncio.sleep(s.batch_sleep_s)
        return filled

    # ------------------------------------------------------------------ main
    async def fetch(self, symbol: str, params: ChainParams) -> ChainSnapshot:
        """Build a complete chain snapshot for ``symbol``.

        Serialised per fetcher; the ``fetch_timeout_s`` budget applies to the
        fetch itself, not to time spent queueing behind other symbols.
        """
        async with self._lock:
            return await asyncio.wait_for(self._fetch(symbol, params), timeout=self.settings.fetch_timeout_s)

    async def _fetch(self, symbol: str, params: ChainParams) -> ChainSnapshot:
        started = time.monotonic()
        self.client.require_connected()
        await self.client.apply_market_data_type()
        now = datetime.now(tz=NY)
        underlying = await self.resolve_underlying(symbol)
        spot, spot_source = await self.fetch_spot(underlying)
        adv = await self.fetch_adv(underlying, now.astimezone(NY).date())
        iv_hist, hv_hist = await self.fetch_vol_history(underlying, now.astimezone(NY).date())
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
            iv30_history=iv_hist,
            hv30_history=hv_hist,
        )
