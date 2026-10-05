"""Watch list, cache and background refresh loop.

One ``ChainFetcher`` can only talk to the Gateway for one symbol at a time
(market data lines are a shared, scarce resource), so refreshes are run
serially. Each watched symbol keeps its most recent snapshot and result in
memory; WebSocket subscribers are notified after every refresh.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .chain import ChainError, ChainFetcher, ChainParams, ChainSnapshot
from .config import Settings
from .gex import compute_gex
from .models import GexResponse, WatchEntry
from .store import SnapshotStore

log = logging.getLogger(__name__)

Listener = Callable[[GexResponse], Awaitable[None]]


@dataclass
class WatchState:
    symbol: str
    params: ChainParams
    pinned: bool = False
    last_access: float = field(default_factory=time.monotonic)
    last_refresh_ts: datetime | None = None
    last_error: str | None = None
    snapshot: ChainSnapshot | None = None
    result: GexResponse | None = None
    refreshing: asyncio.Lock = field(default_factory=asyncio.Lock)
    listeners: set[Listener] = field(default_factory=set)

    def touch(self) -> None:
        self.last_access = time.monotonic()


class Scheduler:
    def __init__(self, fetcher: ChainFetcher, store: SnapshotStore, settings: Settings) -> None:
        self.fetcher = fetcher
        self.store = store
        self.settings = settings
        self._watch: dict[str, WatchState] = {}
        self._task: asyncio.Task | None = None
        self._stopping = False

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="gex-refresh-loop")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ------------------------------------------------------------ watch list
    def default_params(self) -> ChainParams:
        s = self.settings
        return ChainParams(max_dte=s.max_dte, strike_range_pct=s.strike_range_pct, max_contracts=s.max_contracts)

    def watched(self) -> list[str]:
        return sorted(self._watch)

    def entries(self) -> list[WatchEntry]:
        return [
            WatchEntry(
                symbol=w.symbol,
                pinned=w.pinned,
                last_access_ts=datetime.fromtimestamp(time.time() - (time.monotonic() - w.last_access), tz=timezone.utc),
                last_refresh_ts=w.last_refresh_ts,
                last_error=w.last_error,
            )
            for w in self._watch.values()
        ]

    def get(self, symbol: str) -> WatchState | None:
        return self._watch.get(symbol.upper())

    def ensure(self, symbol: str, params: ChainParams | None = None, pinned: bool | None = None) -> WatchState:
        symbol = symbol.upper()
        state = self._watch.get(symbol)
        if state is None:
            state = WatchState(symbol=symbol, params=params or self.default_params())
            self._watch[symbol] = state
            log.info("watching %s (%s)", symbol, state.params.key())
        elif params is not None and params.key() != state.params.key():
            state.params = params
            state.result = None
            state.snapshot = None
        if pinned is not None:
            state.pinned = pinned
        state.touch()
        return state

    def remove(self, symbol: str) -> bool:
        return self._watch.pop(symbol.upper(), None) is not None

    def subscribe(self, symbol: str, listener: Listener) -> WatchState:
        state = self.ensure(symbol)
        state.listeners.add(listener)
        return state

    def unsubscribe(self, symbol: str, listener: Listener) -> None:
        state = self._watch.get(symbol.upper())
        if state:
            state.listeners.discard(listener)

    # -------------------------------------------------------------- refresh
    async def refresh(self, state: WatchState) -> GexResponse:
        async with state.refreshing:
            s = self.settings
            try:
                snapshot = await asyncio.wait_for(
                    self.fetcher.fetch(state.symbol, state.params), timeout=s.fetch_timeout_s
                )
                result = compute_gex(snapshot, s.risk_free_rate, s.dividend_yield)
            except asyncio.TimeoutError:
                state.last_error = f"fetch timed out after {s.fetch_timeout_s:.0f}s"
                raise
            except (ChainError, ConnectionError) as exc:
                state.last_error = str(exc)
                raise
            except Exception as exc:  # noqa: BLE001
                state.last_error = f"{type(exc).__name__}: {exc}"
                raise
            state.snapshot = snapshot
            state.result = result
            state.last_refresh_ts = snapshot.ts
            state.last_error = None
            try:
                await self.store.save(result, state.params.key())
            except Exception as exc:  # noqa: BLE001
                log.warning("snapshot save failed for %s: %s", state.symbol, exc)
            await self._notify(state, result)
            return result

    async def get_or_fetch(self, symbol: str, params: ChainParams | None, force: bool = False) -> GexResponse:
        state = self.ensure(symbol, params)
        if state.result is not None and not force:
            return self._with_age(state)
        if state.refreshing.locked():
            # Another request is already fetching; wait for it rather than piling on.
            async with state.refreshing:
                pass
            if state.result is not None and not force:
                return self._with_age(state)
        return await self.refresh(state)

    def _with_age(self, state: WatchState) -> GexResponse:
        result = state.result
        assert result is not None
        age = (datetime.now(tz=timezone.utc) - result.ts).total_seconds()
        stale = (not self.fetcher.client.is_connected) or age > self.settings.refresh_interval_s * 3
        result.meta.data_age_s = max(0.0, age)
        result.meta.stale = stale
        return result

    async def _notify(self, state: WatchState, result: GexResponse) -> None:
        dead: list[Listener] = []
        for listener in list(state.listeners):
            try:
                await listener(result)
            except Exception as exc:  # noqa: BLE001
                log.debug("listener for %s failed: %s", state.symbol, exc)
                dead.append(listener)
        for listener in dead:
            state.listeners.discard(listener)

    # ----------------------------------------------------------------- loop
    def _evict_idle(self) -> None:
        now = time.monotonic()
        for symbol, state in list(self._watch.items()):
            if state.pinned or state.listeners:
                continue
            if now - state.last_access > self.settings.idle_ttl_s:
                log.info("evicting idle symbol %s", symbol)
                del self._watch[symbol]

    async def _loop(self) -> None:
        interval = self.settings.refresh_interval_s
        last_prune = 0.0
        while not self._stopping:
            cycle_start = time.monotonic()
            self._evict_idle()
            if self.fetcher.client.is_connected:
                for symbol in self.watched():
                    state = self._watch.get(symbol)
                    if state is None or state.refreshing.locked():
                        continue
                    due = state.last_refresh_ts is None or (
                        (datetime.now(tz=timezone.utc) - state.last_refresh_ts).total_seconds() >= interval * 0.9
                    )
                    if not due:
                        continue
                    try:
                        await self.refresh(state)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("refresh %s failed: %s", symbol, exc)
                    if self._stopping:
                        return
            if time.monotonic() - last_prune > 3600:
                try:
                    await self.store.prune()
                except Exception as exc:  # noqa: BLE001
                    log.warning("prune failed: %s", exc)
                last_prune = time.monotonic()
            elapsed = time.monotonic() - cycle_start
            await asyncio.sleep(max(5.0, interval - elapsed) if self._watch else 5.0)
