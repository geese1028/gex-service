"""NautilusTrader IBKR session used by GEX.

This service does not open its own TWS socket. It connects through
``HistoricInteractiveBrokersClient`` as documented by NautilusTrader: the
instrument provider loads option chains, the data client streams market data,
and ``request_bars`` pulls historical series.

Availability rules:

* one ``HistoricInteractiveBrokersClient`` session (Nautilus default client_id=1)
* option chains via ``IBContract(build_options_chain=True, min/max_expiry_days=...)``
* concurrent ``reqMktData`` lines capped at IB's own simultaneous-line ceiling
* historical requests use Nautilus ``request_bars``, spaced for IB pacing only
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from nautilus_trader.adapters.interactive_brokers.common import IBContract
from nautilus_trader.adapters.interactive_brokers.config import (
    InteractiveBrokersInstrumentProviderConfig,
)
from nautilus_trader.adapters.interactive_brokers.parsing.instruments import (
    ib_contract_to_instrument_id,
)
from nautilus_trader.model.data import BarSpecification, BarType
from nautilus_trader.model.enums import AggregationSource
from nautilus_trader.model.identifiers import InstrumentId

from .config import Settings

log = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")

# Error codes that mean the request itself is broken rather than the session.
NON_FATAL_ERROR_CODES = {
    162,  # historical data service error / pacing
    200,  # no security definition found
    354,  # requested market data is not subscribed
    10167,  # requested market data is not subscribed; delayed available
    10089,  # requested market data requires additional subscription
    10090,  # part of requested market data is not subscribed
    10197,  # no market data during competing live session
    2104, 2106, 2107, 2108, 2158,  # farm connection status notices
    2100, 2101, 2102, 2103, 2105, 2109, 2110, 2119, 2137,  # info notices
}

# IB tick types we care about for option snapshots.
_BID, _ASK, _LAST, _VOLUME, _CLOSE = 1, 2, 4, 8, 9
_CALL_OI, _PUT_OI = 27, 28
_CALL_VOL, _PUT_VOL = 29, 30
_MODEL_OPTION = 13
_DELAYED_BID, _DELAYED_ASK, _DELAYED_LAST = 66, 67, 68
_DELAYED_VOLUME, _DELAYED_CLOSE = 74, 75
_DELAYED_CALL_OI, _DELAYED_PUT_OI = 101, 102  # not always present
_DELAYED_MODEL_OPTION = 86
_RT_VOLUME = 48


def is_rth_now(now: datetime | None = None) -> bool:
    """True during US cash RTH (09:30-16:00 ET, Mon-Fri). Holidays ignored."""
    now = now or datetime.now(tz=NY)
    now = now.astimezone(NY)
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() < dtime(16, 0)


def _ib_num(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or abs(number) >= 1e100:
        return None
    return number


def parse_option_computation(*fields: object) -> tuple[object, object, object]:
    """Map ``tickOptionComputation`` args after ``(reqId, tickType)``.

    Order is ``tickAttrib, impliedVol, delta, optPrice, pvDividend, gamma, ...``.
    """
    implied = fields[1] if len(fields) > 1 else None
    delta = fields[2] if len(fields) > 2 else None
    gamma = fields[5] if len(fields) > 5 else None
    return implied, delta, gamma


def ib_hist_bar_datetime(value: object) -> datetime:
    """IB ``formatDate=2`` daily bars are ``YYYYMMDD``; intraday bars are unix seconds."""
    text = str(value).strip()
    if len(text) >= 8 and text[:8].isdigit() and (len(text) == 8 or not text[8].isdigit()):
        if len(text) == 8:
            return datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone.utc)
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=timezone.utc)
    return datetime.now(tz=timezone.utc)


def option_expiry_yyyymmdd(last_trade: str, expiration_utc: datetime) -> str:
    """Expiry as ``YYYYMMDD``.

    Prefer the IB last-trade date. The Nautilus fallback timestamp is midnight UTC,
    which is the previous evening in New York, so the calendar date is taken in UTC.
    """
    text = (last_trade or "").replace("-", "")[:8]
    if len(text) == 8 and text.isdigit():
        return text
    moment = expiration_utc if expiration_utc.tzinfo else expiration_utc.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%d")


def market_data_batch_size(configured: int, max_lines: int, lines_in_use: int) -> int:
    """How many new ``reqMktData`` lines fit under IB's simultaneous-line ceiling."""
    room = max_lines - max(lines_in_use, 0)
    if room < 1:
        return 0
    return min(configured, room)


@dataclass
class QuoteSnapshot:
    """Market-data fields collected from the Nautilus IB wrapper."""

    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    close: float | None = None
    volume: float | None = None
    iv: float | None = None
    gamma: float | None = None
    delta: float | None = None
    call_oi: float | None = None
    put_oi: float | None = None
    saw_trade_volume: bool = False
    rt_prints: list[tuple[float, float, float]] = field(default_factory=list)

    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return None

    def market_price(self) -> float | None:
        for value in (self.last, self.mid(), self.close):
            if value is not None and value > 0:
                return value
        return None


@dataclass
class HistBar:
    date: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class TickCollector:
    """Accumulate option quotes / greeks / OI / RTVolume from the Nautilus wrapper."""

    def __init__(self) -> None:
        self.by_req: dict[int, QuoteSnapshot] = {}
        self._listeners: dict[int, list[Callable[[QuoteSnapshot], None]]] = {}

    def track(self, req_id: int) -> QuoteSnapshot:
        snap = self.by_req.get(req_id)
        if snap is None:
            snap = QuoteSnapshot()
            self.by_req[req_id] = snap
        return snap

    def drop(self, req_id: int) -> None:
        self.by_req.pop(req_id, None)
        self._listeners.pop(req_id, None)

    def listen(self, req_id: int, callback: Callable[[QuoteSnapshot], None]) -> None:
        self._listeners.setdefault(req_id, []).append(callback)

    def on_price(self, req_id: int, tick_type: int, price: float) -> None:
        value = _ib_num(price)
        if value is None or value == -1.0:
            return
        snap = self.track(req_id)
        if tick_type in (_BID, _DELAYED_BID):
            snap.bid = value
        elif tick_type in (_ASK, _DELAYED_ASK):
            snap.ask = value
        elif tick_type in (_LAST, _DELAYED_LAST):
            snap.last = value
        elif tick_type in (_CLOSE, _DELAYED_CLOSE):
            snap.close = value
        self._notify(req_id)

    def on_size(self, req_id: int, tick_type: int, size: object) -> None:
        value = _ib_num(size)
        if value is None or value < 0:
            return
        snap = self.track(req_id)
        if tick_type in (_VOLUME, _DELAYED_VOLUME):
            snap.volume = value
            snap.saw_trade_volume = True
        elif tick_type in (_CALL_VOL, _PUT_VOL):
            # Generic ticks 29/30 repeat the contract volume. Keep tick 8 when both arrive.
            if not snap.saw_trade_volume:
                snap.volume = value
        elif tick_type in (_CALL_OI, _DELAYED_CALL_OI):
            snap.call_oi = value
        elif tick_type in (_PUT_OI, _DELAYED_PUT_OI):
            snap.put_oi = value
        self._notify(req_id)

    def on_greeks(
        self,
        req_id: int,
        tick_type: int,
        implied_vol: float,
        delta: float,
        gamma: float,
    ) -> None:
        if tick_type not in (_MODEL_OPTION, _DELAYED_MODEL_OPTION, 10, 11, 12, 80, 81, 82, 83):
            return
        snap = self.track(req_id)
        iv = _ib_num(implied_vol)
        if iv is not None and iv > 0:
            snap.iv = iv
        g = _ib_num(gamma)
        if g is not None:
            snap.gamma = g
        d = _ib_num(delta)
        if d is not None:
            snap.delta = d
        self._notify(req_id)

    def on_string(self, req_id: int, tick_type: int, value: str) -> None:
        if tick_type != _RT_VOLUME or not value:
            return
        # RTVolume: price;size;time;total;vwap;single
        parts = value.split(";")
        if len(parts) < 3:
            return
        price = _ib_num(parts[0])
        size = _ib_num(parts[1])
        try:
            ts = float(parts[2]) / (1000.0 if float(parts[2]) > 1e11 else 1.0)
        except (TypeError, ValueError):
            ts = time.time()
        if price is None or size is None or price <= 0 or size <= 0:
            return
        snap = self.track(req_id)
        snap.rt_prints.append((ts, price, size))
        self._notify(req_id)

    def _notify(self, req_id: int) -> None:
        snap = self.by_req.get(req_id)
        if snap is None:
            return
        for callback in self._listeners.get(req_id, ()):
            try:
                callback(snap)
            except Exception as exc:  # noqa: BLE001
                log.debug("quote listener failed: %s", exc)


class IBClient:
    """Nautilus-backed IBKR connection. Public surface stays close to the old helper."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.collector = TickCollector()
        self._historic: Any = None
        self._connected_since: float | None = None
        self._last_error: str | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._stopping = False
        self._market_data_type: int | None = None
        self._lines: dict[int, InstrumentId] = {}
        self._req_by_con: dict[int, int] = {}
        self._connect_lock = asyncio.Lock()
        self._hist_lock = asyncio.Lock()
        self._last_hist_at = 0.0
        self._hooked_wrapper: object | None = None

    # ------------------------------------------------------------------ state
    @property
    def is_connected(self) -> bool:
        ib = self._ib
        if ib is None:
            return False
        try:
            return bool(ib._eclient.isConnected() and ib._is_ib_connected.is_set())
        except Exception:  # noqa: BLE001
            return False

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def market_data_type(self) -> int | None:
        return self._market_data_type

    @property
    def lines_in_use(self) -> int:
        return len(self._lines)

    @property
    def connected_since(self) -> float | None:
        return self._connected_since

    @property
    def _ib(self):
        return None if self._historic is None else getattr(self._historic, "_client", None)

    @property
    def instrument_provider(self):
        if self._historic is None:
            return None
        return self._historic._data_client.instrument_provider

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._stopping = False
        try:
            await self.connect()
        except Exception as exc:  # noqa: BLE001
            log.warning("initial Nautilus IB connect failed: %s", exc)
            self._schedule_reconnect()

    async def stop(self) -> None:
        self._stopping = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        await self._disconnect()

    async def connect(self) -> None:
        async with self._connect_lock:
            if self.is_connected:
                return
            try:
                await self._connect_nautilus()
            except Exception:
                await self._disconnect()
                raise

    async def _connect_nautilus(self) -> None:
        from ibapi.common import MarketDataTypeEnum
        from nautilus_trader.adapters.interactive_brokers.historical.client import (
            HistoricInteractiveBrokersClient,
        )

        s = self.settings
        md_type = {
            1: MarketDataTypeEnum.REALTIME,
            2: MarketDataTypeEnum.FROZEN,
            3: MarketDataTypeEnum.DELAYED,
            4: MarketDataTypeEnum.DELAYED_FROZEN,
        }.get(self.desired_market_data_type(), MarketDataTypeEnum.REALTIME)
        pickle_path = str(s.instrument_cache_path) if s.instrument_cache_path else None
        if pickle_path:
            Path(pickle_path).parent.mkdir(parents=True, exist_ok=True)
        log.info(
            "connecting via Nautilus HistoricInteractiveBrokersClient %s:%s clientId=%s",
            s.ib_host,
            s.ib_port,
            s.ib_client_id,
        )
        self._historic = HistoricInteractiveBrokersClient(
            host=s.ib_host,
            port=s.ib_port,
            client_id=s.ib_client_id,
            market_data_type=md_type,
            log_level="WARNING",
            instrument_provider_config=InteractiveBrokersInstrumentProviderConfig(
                load_all=False,
                cache_validity_days=1,
                pickle_path=pickle_path,
            ),
        )
        try:
            asyncio.get_running_loop().set_debug(False)
        except RuntimeError:
            pass
        ib = self._ib
        if ib is not None:
            ib._request_timeout_secs = s.ib_request_timeout_s
            ib._max_connection_attempts = 0
        try:
            await asyncio.wait_for(self._historic.connect(), timeout=s.ib_connect_timeout_s)
        except TimeoutError as exc:
            raise ConnectionError(self._last_error or "Nautilus IB Gateway connect timed out") from exc
        if not self.is_connected:
            raise ConnectionError(self._last_error or "Nautilus IB Gateway not ready")
        self._install_wrapper_hooks()
        self._connected_since = time.time()
        self._last_error = None
        self._lines.clear()
        self._req_by_con.clear()
        await self.apply_market_data_type()
        log.info("Nautilus IB client ready (clientId=%s)", s.ib_client_id)

    async def _disconnect(self) -> None:
        historic = self._historic
        self._historic = None
        self._hooked_wrapper = None
        self._connected_since = None
        self._lines.clear()
        self._req_by_con.clear()
        if historic is None:
            return
        client = getattr(historic, "_client", None)
        stop = getattr(client, "_stop_async", None)
        try:
            if stop is not None:
                await stop()
            elif client is not None and hasattr(client, "_disconnect"):
                await client._disconnect()
        except Exception as exc:  # noqa: BLE001
            log.debug("Nautilus disconnect: %s", exc)

    def _schedule_reconnect(self) -> None:
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        delay = self.settings.ib_reconnect_min_s
        while not self._stopping and not self.is_connected:
            await asyncio.sleep(delay)
            try:
                await self.connect()
                log.info("reconnected to IB Gateway via Nautilus")
                return
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"reconnect failed: {exc}"
                log.warning("reconnect failed (%s); retrying in %.0fs", exc, delay)
                delay = min(delay * 2, self.settings.ib_reconnect_max_s)

    def _install_wrapper_hooks(self) -> None:
        ib = self._ib
        if ib is None:
            return
        wrapper = ib._eclient.wrapper
        if self._hooked_wrapper is wrapper:
            return
        collector = self.collector

        orig_price = wrapper.tickPrice
        orig_size = wrapper.tickSize
        orig_string = getattr(wrapper, "tickString", None)
        orig_greeks = getattr(wrapper, "tickOptionComputation", None)
        orig_error = ib.process_error

        def tick_price(reqId, tickType, price, attrib):
            collector.on_price(reqId, int(tickType), price)
            return orig_price(reqId, tickType, price, attrib)

        def tick_size(reqId, tickType, size):
            collector.on_size(reqId, int(tickType), size)
            return orig_size(reqId, tickType, size)

        def tick_string(reqId, tickType, value):
            collector.on_string(reqId, int(tickType), value)
            if orig_string is not None:
                return orig_string(reqId, tickType, value)
            return None

        def tick_option_computation(reqId, tickType, *rest):
            implied, delta, gamma = parse_option_computation(*rest)
            collector.on_greeks(reqId, int(tickType), implied, delta, gamma)
            if orig_greeks is not None:
                return orig_greeks(reqId, tickType, *rest)
            return None

        async def process_error(*, req_id, error_time, error_code, error_string, advanced_order_reject_json=""):
            self._on_ib_error(req_id, error_code, error_string)
            return await orig_error(
                req_id=req_id,
                error_time=error_time,
                error_code=error_code,
                error_string=error_string,
                advanced_order_reject_json=advanced_order_reject_json,
            )

        wrapper.tickPrice = tick_price
        wrapper.tickSize = tick_size
        wrapper.tickString = tick_string
        wrapper.tickOptionComputation = tick_option_computation
        ib.process_error = process_error
        self._hooked_wrapper = wrapper

    def _on_ib_error(self, req_id: int, error_code: int, error_string: str) -> None:
        if error_code in NON_FATAL_ERROR_CODES:
            log.debug("IB notice %s reqId=%s: %s", error_code, req_id, error_string)
            return
        if error_code == 326:
            self._last_error = f"clientId {self.settings.ib_client_id} already in use"
        elif error_code in (1100, 2110):
            self._last_error = f"gateway lost connectivity: {error_string}"
            self._connected_since = None
            if not self._stopping:
                self._schedule_reconnect()
        elif error_code == 1102:
            self._last_error = None
        else:
            self._last_error = f"{error_code}: {error_string}"
        log.warning("IB error %s reqId=%s: %s", error_code, req_id, error_string)

    # ------------------------------------------------------------ market data
    def desired_market_data_type(self) -> int:
        base = self.settings.market_data_type
        if self.settings.auto_frozen and base == 1 and not is_rth_now():
            return 2
        return base

    async def apply_market_data_type(self) -> int:
        from ibapi.common import MarketDataTypeEnum

        wanted = self.desired_market_data_type()
        ib = self._ib
        if wanted != self._market_data_type and ib is not None and self.is_connected:
            enum = {
                1: MarketDataTypeEnum.REALTIME,
                2: MarketDataTypeEnum.FROZEN,
                3: MarketDataTypeEnum.DELAYED,
                4: MarketDataTypeEnum.DELAYED_FROZEN,
            }.get(wanted, MarketDataTypeEnum.REALTIME)
            await ib.set_market_data_type(enum)
            self._market_data_type = wanted
            log.info("market data type -> %s", wanted)
        return wanted

    def _instrument_id_for(self, contract: IBContract) -> InstrumentId:
        if contract.conId:
            return InstrumentId.from_str(f"C{contract.conId}.SMART")
        provider = self.instrument_provider
        if provider is not None:
            venue = provider.determine_venue_from_contract(contract)
            return ib_contract_to_instrument_id(contract, venue, provider.config.symbology_method)
        symbol = contract.localSymbol or contract.symbol or "UNK"
        return InstrumentId.from_str(f"{symbol}.SMART")

    async def subscribe(self, contract: IBContract, generic_ticks: str = "") -> QuoteSnapshot:
        self.require_connected()
        if len(self._lines) >= self.settings.max_md_lines:
            raise ConnectionError(
                f"refusing subscribe: {len(self._lines)} lines already open (cap {self.settings.max_md_lines})"
            )
        ib = self._ib
        instrument_id = self._instrument_id_for(contract)
        if contract.secType == "IND":
            await ib.subscribe_index_market_data(instrument_id, contract, generic_ticks)
            name = (str(instrument_id), "index_market_data")
        else:
            await ib.subscribe_market_data(instrument_id, contract, generic_ticks)
            name = (str(instrument_id), "market_data")
        subscription = ib._subscriptions.get(name=name)
        if subscription is None:
            raise ConnectionError(f"Nautilus did not register a subscription for {instrument_id}")
        snap = self.collector.track(subscription.req_id)
        self._lines[contract.conId or id(contract)] = instrument_id
        self._req_by_con[contract.conId or id(contract)] = subscription.req_id
        return snap

    async def unsubscribe(self, contract: IBContract) -> None:
        key = contract.conId or id(contract)
        instrument_id = self._lines.pop(key, None)
        req_id = self._req_by_con.pop(key, None)
        if req_id is not None:
            self.collector.drop(req_id)
        ib = self._ib
        if ib is None or instrument_id is None:
            return
        try:
            if contract.secType == "IND":
                await ib.unsubscribe_index_market_data(instrument_id)
            else:
                await ib.unsubscribe_market_data(instrument_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("unsubscribe failed: %s", exc)

    def on_quote(self, contract: IBContract, callback: Callable[[QuoteSnapshot], None]) -> None:
        req_id = self._req_by_con.get(contract.conId or id(contract))
        if req_id is not None:
            self.collector.listen(req_id, callback)

    def require_connected(self) -> None:
        if not self.is_connected:
            raise ConnectionError(self._last_error or "IB Gateway not connected")

    # ---------------------------------------------------------- historical
    async def _pace_historical(self) -> None:
        delay = self.settings.historical_request_delay_s
        async with self._hist_lock:
            wait = delay - (time.monotonic() - self._last_hist_at)
            if wait > 0:
                log.info("pacing historical request for %.1fs", wait)
                await asyncio.sleep(wait)
            self._last_hist_at = time.monotonic()

    async def request_instruments(self, contracts: list[IBContract]):
        self.require_connected()
        return await self._historic.request_instruments(contracts=contracts)

    async def load_options_chain(
        self,
        underlying: IBContract,
        *,
        max_expiry_days: int,
        min_expiry_days: int = 0,
        expiry: str | None = None,
    ):
        """Official Nautilus chain load: ``IBContract(build_options_chain=True)``."""
        seed = IBContract(
            secType=underlying.secType,
            conId=underlying.conId,
            symbol=underlying.symbol,
            exchange=underlying.exchange,
            primaryExchange=underlying.primaryExchange,
            currency=underlying.currency or "USD",
            build_options_chain=True,
            min_expiry_days=None if expiry else min_expiry_days,
            max_expiry_days=None if expiry else max_expiry_days,
            lastTradeDateOrContractMonth=expiry or "",
        )
        return await self.request_instruments(contracts=[seed])

    async def get_option_chains(self, underlying: IBContract):
        self.require_connected()
        return await self._ib.get_option_chains(underlying)

    async def get_option_chain_details(self, underlying: IBContract, expiry: str, exchange: str = "SMART"):
        self.require_connected()
        provider = self.instrument_provider
        return await provider.get_option_chain_details_by_expiry(underlying, expiry, exchange)

    async def get_contract_details(self, contract: IBContract):
        self.require_connected()
        return await self._ib.get_contract_details(contract)

    async def qualify(self, contract: IBContract) -> IBContract | None:
        loaded = await self.request_instruments(contracts=[contract])
        if not loaded:
            return None
        provider = self.instrument_provider
        for instrument in loaded:
            ib_contract = provider.contract.get(instrument.id) if provider is not None else None
            if ib_contract is None or ib_contract.secType in {"OPT", "FOP"}:
                continue
            if ib_contract.conId:
                return ib_contract
        details = await self.get_contract_details(contract)
        if not details:
            return None
        first = details[0]
        raw = getattr(first, "contract", first)
        if raw is None:
            return None
        return IBContract(
            secType=raw.secType,
            conId=int(raw.conId or 0),
            symbol=raw.symbol,
            exchange=raw.exchange or contract.exchange,
            primaryExchange=getattr(raw, "primaryExchange", "") or "",
            currency=raw.currency or contract.currency or "USD",
            localSymbol=getattr(raw, "localSymbol", "") or "",
            tradingClass=getattr(raw, "tradingClass", "") or "",
        )

    async def request_bars(
        self,
        contract: IBContract,
        bar_spec: str,
        duration: str,
        use_rth: bool = True,
    ) -> list[HistBar]:
        """TRADES/MID bars through Nautilus ``request_bars`` (paced)."""
        self.require_connected()
        await self._pace_historical()
        spec = bar_spec if contract.secType == "STK" else bar_spec.replace("LAST", "MID")
        now = datetime.now(tz=timezone.utc)
        bars = await self._historic.request_bars(
            bar_specifications=[spec],
            end_date_time=now,
            tz_name="UTC",
            duration=duration,
            contracts=[contract],
            use_rth=use_rth,
            timeout=self.settings.historical_timeout_s,
        )
        out: list[HistBar] = []
        for bar in bars or []:
            ts = datetime.fromtimestamp(bar.ts_event / 1_000_000_000, tz=timezone.utc)
            out.append(
                HistBar(
                    date=ts,
                    open=float(bar.open),
                    high=float(bar.high),
                    low=float(bar.low),
                    close=float(bar.close),
                    volume=float(bar.volume),
                )
            )
        return out

    async def request_what_bars(
        self,
        contract: IBContract,
        duration: str,
        what: str,
        bar_size: str = "1 day",
    ) -> list[HistBar]:
        """Historical bars with an IB ``whatToShow`` on the Nautilus session."""
        self.require_connected()
        await self._pace_historical()
        ib = self._ib
        await self.request_instruments(contracts=[contract])
        provider = self.instrument_provider
        venue = provider.determine_venue_from_contract(contract)
        instrument_id = ib_contract_to_instrument_id(contract, venue, provider.config.symbology_method)
        bar_type = BarType(
            instrument_id,
            BarSpecification.from_str("1-DAY-LAST"),
            AggregationSource.EXTERNAL,
        )
        end = datetime.now(tz=timezone.utc).strftime("%Y%m%d %H:%M:%S UTC")
        name = (str(bar_type), f"{end}-{what}")
        req_id = ib._next_req_id()
        request = ib._requests.add(
            req_id=req_id,
            name=name,
            handle=functools.partial(
                ib._eclient.reqHistoricalData,
                reqId=req_id,
                contract=contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow=what,
                useRTH=True,
                formatDate=2,
                keepUpToDate=False,
                chartOptions=[],
            ),
            cancel=functools.partial(ib._eclient.cancelHistoricalData, reqId=req_id),
        )
        if not request:
            return []
        # Capture raw IB bars. Nautilus converts them with the underlying price tick,
        # which rounds a 0.184 IV print to 0.18 and distorts IV rank.
        raw: list = []
        wrapper = ib._eclient.wrapper
        orig_historical = wrapper.historicalData

        def historical_data(reqId, bar):
            if reqId == req_id:
                raw.append(bar)
            return orig_historical(reqId, bar)

        wrapper.historicalData = historical_data
        try:
            request.handle()
            await ib._await_request(request, self.settings.historical_timeout_s, default_value=[])
        finally:
            wrapper.historicalData = orig_historical
        out: list[HistBar] = []
        for bar in raw:
            out.append(
                HistBar(
                    date=ib_hist_bar_datetime(bar.date),
                    open=float(bar.open),
                    high=float(bar.high),
                    low=float(bar.low),
                    close=float(bar.close),
                    volume=float(bar.volume) if bar.volume not in (None, -1) else 0.0,
                )
            )
        return out
