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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .chain import NY, ChainError, ChainFetcher, ChainParams, ChainSnapshot
from .config import Settings
from .gex import compute_gex
from .models import GexResponse, WatchEntry
from .store import SnapshotStore

log = logging.getLogger(__name__)

Listener = Callable[[GexResponse], Awaitable[None]]
ResultHook = Callable[["WatchState", GexResponse], Awaitable[None]]


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
        # Called after every successful refresh (alerts, OI reconciliation, ...).
        self.result_hooks: list[ResultHook] = []
        self._eod_done: set[tuple[str, str]] = set()

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
                # The fetcher applies fetch_timeout_s to the fetch itself (not to queueing).
                snapshot = await self.fetcher.fetch(state.symbol, state.params)
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
                await self.store.upsert_expiry_days(result)
                await self.store.record_wall_touch(result)
            except Exception as exc:  # noqa: BLE001
                log.warning("snapshot save failed for %s: %s", state.symbol, exc)
            await self._notify(state, result)
            for hook in list(self.result_hooks):
                try:
                    await hook(state, result)
                except Exception as exc:  # noqa: BLE001
                    log.warning("result hook %s failed for %s: %s", getattr(hook, "__name__", hook), state.symbol, exc)
            return result

    def filtered_result(self, state: WatchState, exclude_expiries: tuple[str, ...]) -> GexResponse | None:
        """Recompute from the cached snapshot without the given expiries (no Gateway round-trip)."""
        if state.snapshot is None:
            return None
        rows = [r for r in state.snapshot.rows if r.expiry not in exclude_expiries]
        snap = replace(state.snapshot, rows=rows, expirations=[e for e in state.snapshot.expirations if e not in exclude_expiries])
        s = self.settings
        result = compute_gex(snap, s.risk_free_rate, s.dividend_yield)
        result.meta.warnings = list(result.meta.warnings) + [f"excluded expiries: {', '.join(exclude_expiries)}"]
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

    # ------------------------------------------------------------------ EOD
    def _eod_times(self, now_ny: datetime) -> tuple[datetime, datetime]:
        hh, mm = (int(x) for x in self.settings.eod_archive_time.split(":"))
        archive_at = now_ny.replace(hour=hh, minute=mm, second=0, microsecond=0)
        close = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)
        return archive_at, close

    async def archive_eod(self, now: datetime | None = None, force: bool = False) -> list[str]:
        """Archive the last pre-close snapshot of every watched symbol once per session."""
        now_ny = (now or datetime.now(tz=timezone.utc)).astimezone(NY)
        archive_at, close = self._eod_times(now_ny)
        if now_ny.weekday() >= 5 or (now_ny < archive_at and not force):
            return []
        day = now_ny.date().isoformat()
        archived: list[str] = []
        for symbol in self.watched():
            key = (symbol, day)
            if key in self._eod_done:
                continue
            if await self.store.eod_payload(symbol, day) is not None:
                self._eod_done.add(key)
                continue
            payload = await self.store.last_payload_before(symbol, close if not force else now_ny)
            day_start = now_ny.replace(hour=0, minute=0, second=0, microsecond=0)
            if payload is None or payload.ts.astimezone(NY) < day_start:
                continue  # nothing from today's session
            await self.store.save_eod(payload, day)
            await self.store.upsert_expiry_days(payload, finalized=True)
            self._eod_done.add(key)
            archived.append(symbol)
            log.info("EOD archived %s for %s (snapshot %s)", symbol, day, payload.ts.isoformat())
        return archived

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
            try:
                await self.archive_eod()
            except Exception as exc:  # noqa: BLE001
                log.warning("EOD archive failed: %s", exc)
            if time.monotonic() - last_prune > 3600:
                try:
                    await self.store.prune()
                except Exception as exc:  # noqa: BLE001
                    log.warning("prune failed: %s", exc)
                last_prune = time.monotonic()
            elapsed = time.monotonic() - cycle_start
            await asyncio.sleep(max(5.0, interval - elapsed) if self._watch else 5.0)
