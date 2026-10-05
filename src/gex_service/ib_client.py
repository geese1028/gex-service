"""Connection management for one ib_async ``IB`` session.

Responsibilities:

* connect with a bounded timeout and reconnect with exponential backoff
* track market data type and switch to frozen outside regular trading hours
* count concurrently subscribed market data lines so callers can stay under
  the account limit, which is shared with every other client on the Gateway
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from ib_async import IB, Contract, Ticker

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


def is_rth_now(now: datetime | None = None) -> bool:
    """True during US cash RTH (09:30-16:00 ET, Mon-Fri). Holidays ignored."""
    now = now or datetime.now(tz=NY)
    now = now.astimezone(NY)
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() < dtime(16, 0)


class IBClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ib = IB()
        self.ib.RequestTimeout = settings.fetch_timeout_s
        self._connected_since: float | None = None
        self._last_error: str | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._stopping = False
        self._market_data_type: int | None = None
        self._lines: set[int] = set()
        self._connect_lock = asyncio.Lock()
        self.ib.errorEvent += self._on_error
        self.ib.disconnectedEvent += self._on_disconnected

    # ------------------------------------------------------------------ state
    @property
    def is_connected(self) -> bool:
        return self.ib.isConnected()

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

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        """Connect once; on failure schedule the reconnect loop."""
        self._stopping = False
        try:
            await self.connect()
        except Exception as exc:  # noqa: BLE001
            log.warning("initial IB connect failed: %s", exc)
            self._schedule_reconnect()

    async def stop(self) -> None:
        self._stopping = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        if self.ib.isConnected():
            self.ib.disconnect()

    async def connect(self) -> None:
        async with self._connect_lock:
            if self.ib.isConnected():
                return
            s = self.settings
            log.info("connecting to IB Gateway %s:%s clientId=%s", s.ib_host, s.ib_port, s.ib_client_id)
            await self.ib.connectAsync(
                s.ib_host,
                s.ib_port,
                clientId=s.ib_client_id,
                timeout=s.ib_connect_timeout_s,
                readonly=True,
            )
            self._connected_since = time.time()
            self._last_error = None
            self._lines.clear()
            self.apply_market_data_type()
            log.info("connected; server version %s", self.ib.client.serverVersion())

    def _on_disconnected(self) -> None:
        self._connected_since = None
        self._lines.clear()
        if not self._stopping:
            log.warning("IB Gateway disconnected")
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        delay = self.settings.ib_reconnect_min_s
        while not self._stopping and not self.ib.isConnected():
            await asyncio.sleep(delay)
            try:
                await self.connect()
                log.info("reconnected to IB Gateway")
                return
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"reconnect failed: {exc}"
                log.warning("reconnect failed (%s); retrying in %.0fs", exc, delay)
                delay = min(delay * 2, self.settings.ib_reconnect_max_s)

    def _on_error(self, reqId: int, errorCode: int, errorString: str, contract: Contract | None) -> None:
        if errorCode in NON_FATAL_ERROR_CODES:
            log.debug("IB notice %s reqId=%s: %s", errorCode, reqId, errorString)
            return
        if errorCode == 326:
            self._last_error = f"clientId {self.settings.ib_client_id} already in use"
        elif errorCode in (1100, 2110):
            self._last_error = f"gateway lost connectivity: {errorString}"
        elif errorCode == 1102:
            self._last_error = None
        else:
            self._last_error = f"{errorCode}: {errorString}"
        log.warning("IB error %s reqId=%s: %s", errorCode, reqId, errorString)

    # ------------------------------------------------------------ market data
    def desired_market_data_type(self) -> int:
        base = self.settings.market_data_type
        if self.settings.auto_frozen and base == 1 and not is_rth_now():
            return 2
        return base

    def apply_market_data_type(self) -> int:
        """Set the market data type if it differs from the current one."""
        wanted = self.desired_market_data_type()
        if wanted != self._market_data_type and self.ib.isConnected():
            self.ib.reqMarketDataType(wanted)
            self._market_data_type = wanted
            log.info("market data type -> %s", wanted)
        return wanted

    def subscribe(self, contract: Contract, generic_ticks: str = "") -> Ticker:
        ticker = self.ib.reqMktData(contract, genericTickList=generic_ticks, snapshot=False)
        self._lines.add(contract.conId)
        return ticker

    def unsubscribe(self, contract: Contract) -> None:
        try:
            self.ib.cancelMktData(contract)
        finally:
            self._lines.discard(contract.conId)

    def require_connected(self) -> None:
        if not self.ib.isConnected():
            raise ConnectionError(self._last_error or "IB Gateway not connected")
