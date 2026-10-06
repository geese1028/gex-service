"""FastAPI application: REST + WebSocket surface."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .alerts import AlertManager
from .api_ext import register_extensions
from .analytics import actual_oi_up_to, oi_totals_for_reconcile
from .chain import NY, ChainError, ChainFetcher, ChainParams
from .config import Settings, get_settings
from .flow import FlowTracker
from .gex import chain_response
from .ib_client import IBClient
from .models import ChainResponse, GexResponse, HealthResponse, HistoryPoint, WatchEntry
from .scheduler import Scheduler
from .store import SnapshotStore

log = logging.getLogger(__name__)


class AppState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = IBClient(settings)
        self.fetcher = ChainFetcher(self.client, settings)
        self.store = SnapshotStore(settings.db_path, settings.snapshot_retention_days)
        self.scheduler = Scheduler(self.fetcher, self.store, settings)
        self.alerts = AlertManager(self.store, settings.alert_webhook_url)
        self.flow = FlowTracker(self.client, settings.flow_max_symbols, settings.flow_strikes_per_side)
        self.started_at = time.time()
        self.scheduler.result_hooks.append(self._alert_hook)
        self.scheduler.result_hooks.append(self._oi_hook)
        self.scheduler.result_hooks.append(self._flow_hook)

    async def start(self) -> None:
        await self.store.open()
        for symbol, params in await self.store.list_watches():
            self.scheduler.ensure(symbol, params, pinned=True)
        await self.client.start()
        self.scheduler.start()

    async def stop(self) -> None:
        await self.scheduler.stop()
        await self.flow.stop_all()
        await self.alerts.close()
        await self.client.stop()
        await self.store.close()

    # ------------------------------------------------------------ hooks
    async def _alert_hook(self, _state, result: GexResponse) -> None:
        await self.alerts.on_result(result)

    async def _oi_hook(self, state, result: GexResponse) -> None:
        snapshot = state.snapshot
        if snapshot is None:
            return
        today = result.ts.astimezone(NY).date()
        day = today.isoformat()
        for pending in await self.store.pending_oi_reconcile(result.symbol, day):
            if pending["max_expiry"]:
                actual = actual_oi_up_to(snapshot, pending["max_expiry"], date.fromisoformat(pending["date"]))
                await self.store.reconcile_oi(result.symbol, pending["date"], actual)
        prev_oi, volume, max_expiry = oi_totals_for_reconcile(snapshot, today)
        if max_expiry:
            await self.store.save_oi_estimate(result.symbol, day, prev_oi, volume, self.settings.oi_opening_ratio, max_expiry)

    async def _flow_hook(self, _state, result: GexResponse) -> None:
        flow = self.flow.get(result.symbol)
        if flow is not None:
            flow.update_spot(result.spot)


def _rss_mb() -> float | None:
    try:
        import psutil

        return round(psutil.Process().memory_info().rss / (1024 * 1024), 1)
    except Exception:  # noqa: BLE001
        return None


def create_app(settings: Settings | None = None, state: AppState | None = None) -> FastAPI:
    settings = settings or get_settings()
    app_state = state or AppState(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await app_state.start()
        try:
            yield
        finally:
            await app_state.stop()

    app = FastAPI(title="gex-service", version=__version__, lifespan=lifespan)
    app.state.gex = app_state

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )

    # ------------------------------------------------------------ security
    async def require_api_key(request: Request) -> None:
        if not settings.api_key:
            return
        provided = request.headers.get("x-api-key") or request.query_params.get("api_key")
        if provided != settings.api_key:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")

    api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])

    def st() -> AppState:
        return app_state

    def parse_params(
        max_dte: int | None,
        strike_range_pct: float | None,
        max_contracts: int | None,
        expiries: str | None,
    ) -> ChainParams:
        s = settings
        exp: tuple[str, ...] = ()
        if expiries:
            parts = tuple(p.strip() for p in expiries.split(",") if p.strip())
            for p in parts:
                if len(p) != 8 or not p.isdigit():
                    raise HTTPException(status_code=422, detail=f"expiry {p!r} must be YYYYMMDD")
            exp = parts
        md = max_dte if max_dte is not None else s.max_dte
        rng = strike_range_pct if strike_range_pct is not None else s.strike_range_pct
        cap = max_contracts if max_contracts is not None else s.max_contracts
        if md <= 0 or md > 400:
            raise HTTPException(status_code=422, detail="max_dte must be in 1..400")
        if not 0 < rng <= 0.5:
            raise HTTPException(status_code=422, detail="strike_range_pct must be in (0, 0.5]")
        if cap <= 0 or cap > s.max_contracts * 2:
            raise HTTPException(status_code=422, detail=f"max_contracts must be in 1..{s.max_contracts * 2}")
        return ChainParams(max_dte=md, strike_range_pct=rng, max_contracts=cap, expiries=exp)

    def _symbol(symbol: str) -> str:
        sym = symbol.strip().upper()
        if not sym or len(sym) > 12 or not all(c.isalnum() or c in "-." for c in sym):
            raise HTTPException(status_code=422, detail="invalid symbol")
        return sym

    # ------------------------------------------------------------ handlers
    @app.exception_handler(ChainError)
    async def _chain_error(_: Request, exc: ChainError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConnectionError)
    async def _conn_error(_: Request, exc: ConnectionError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(asyncio.TimeoutError)
    async def _timeout(_: Request, __: asyncio.TimeoutError) -> JSONResponse:
        return JSONResponse(status_code=504, content={"detail": "chain fetch timed out"})

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        s = st()
        return HealthResponse(
            ok=s.client.is_connected,
            ib_connected=s.client.is_connected,
            ib_last_error=s.client.last_error,
            market_data_type=s.client.market_data_type,
            lines_in_use=s.client.lines_in_use,
            watched_symbols=s.scheduler.watched(),
            uptime_s=round(time.time() - s.started_at, 1),
            rss_mb=_rss_mb(),
            version=__version__,
        )

    @api.get("/gex/{symbol}", response_model=GexResponse)
    async def get_gex(
        symbol: str,
        max_dte: int | None = Query(None),
        strike_range_pct: float | None = Query(None),
        max_contracts: int | None = Query(None),
        expiries: str | None = Query(None, description="comma separated YYYYMMDD"),
        refresh: bool = Query(False),
        wait: bool = Query(True, description="block until data is ready; false returns 202 while a fetch runs"),
        exclude_expiries: str | None = Query(None, description="comma separated YYYYMMDD to drop from the cached chain"),
    ):
        params = parse_params(max_dte, strike_range_pct, max_contracts, expiries)
        sym = _symbol(symbol)
        scheduler = st().scheduler
        if exclude_expiries:
            excl = tuple(p.strip() for p in exclude_expiries.split(",") if p.strip())
            for p in excl:
                if len(p) != 8 or not p.isdigit():
                    raise HTTPException(status_code=422, detail=f"expiry {p!r} must be YYYYMMDD")
            await scheduler.get_or_fetch(sym, params, force=refresh)
            result = scheduler.filtered_result(scheduler.ensure(sym, params), excl)
            if result is None:
                raise HTTPException(status_code=404, detail="no chain snapshot")
            return result
        if not wait:
            state = scheduler.ensure(sym, params)
            if state.result is not None and not refresh:
                return await scheduler.get_or_fetch(sym, params)
            if not state.refreshing.locked():
                asyncio.create_task(_safe_refresh(scheduler, sym, params, force=refresh))
            return JSONResponse(
                status_code=202,
                content={"status": "pending", "symbol": sym, "detail": "chain fetch in progress; poll again", "last_error": state.last_error},
            )
        return await scheduler.get_or_fetch(sym, params, force=refresh)

    @api.get("/gex/{symbol}/chain", response_model=ChainResponse)
    async def get_chain(symbol: str, refresh: bool = Query(False)) -> ChainResponse:
        s = st()
        sym = _symbol(symbol)
        state = s.scheduler.ensure(sym)
        if state.snapshot is None or refresh:
            await s.scheduler.get_or_fetch(sym, None, force=refresh)
            state = s.scheduler.ensure(sym)
        if state.snapshot is None:
            raise HTTPException(status_code=404, detail="no chain snapshot")
        return chain_response(state.snapshot, settings.risk_free_rate, settings.dividend_yield)

    @api.get("/gex/{symbol}/history", response_model=list[HistoryPoint])
    async def get_history(
        symbol: str,
        since: datetime | None = Query(None, alias="from"),
        until: datetime | None = Query(None, alias="to"),
        limit: int = Query(500, ge=1, le=5000),
    ) -> list[HistoryPoint]:
        return await st().store.history(_symbol(symbol), since, until, limit)

    @api.get("/watch", response_model=list[WatchEntry])
    async def list_watch() -> list[WatchEntry]:
        return st().scheduler.entries()

    @api.put("/watch/{symbol}", response_model=WatchEntry, status_code=201)
    async def add_watch(
        symbol: str,
        max_dte: int | None = Query(None),
        strike_range_pct: float | None = Query(None),
        max_contracts: int | None = Query(None),
        expiries: str | None = Query(None),
    ) -> WatchEntry:
        params = parse_params(max_dte, strike_range_pct, max_contracts, expiries)
        sym = _symbol(symbol)
        state = st().scheduler.ensure(sym, params, pinned=True)
        await st().store.save_watch(sym, state.params)
        return next(e for e in st().scheduler.entries() if e.symbol == sym)

    @api.delete("/watch/{symbol}", status_code=204)
    async def remove_watch(symbol: str) -> None:
        sym = _symbol(symbol)
        if not st().scheduler.remove(sym):
            raise HTTPException(status_code=404, detail="not watched")
        await st().store.delete_watch(sym)

    @app.websocket("/ws/gex/{symbol}")
    async def ws_gex(websocket: WebSocket, symbol: str) -> None:
        if settings.api_key:
            provided = websocket.headers.get("x-api-key") or websocket.query_params.get("api_key")
            if provided != settings.api_key:
                await websocket.close(code=4401)
                return
        try:
            sym = _symbol(symbol)
        except HTTPException:
            await websocket.close(code=4422)
            return
        await websocket.accept()
        s = st()
        queue: asyncio.Queue[GexResponse] = asyncio.Queue(maxsize=4)

        async def listener(result: GexResponse) -> None:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(result)

        state = s.scheduler.subscribe(sym, listener)
        try:
            if state.result is not None:
                await websocket.send_text(state.result.model_dump_json())
            elif not state.refreshing.locked():
                asyncio.create_task(_safe_refresh(s.scheduler, sym))
            while True:
                sender = asyncio.create_task(queue.get())
                receiver = asyncio.create_task(websocket.receive_text())
                done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if receiver in done:
                    try:
                        receiver.result()  # raises on disconnect
                    except WebSocketDisconnect:
                        return
                    except Exception:  # noqa: BLE001
                        return
                if sender in done:
                    await websocket.send_text(sender.result().model_dump_json())
        except WebSocketDisconnect:
            return
        finally:
            s.scheduler.unsubscribe(sym, listener)

    register_extensions(api, app, settings, st, _symbol, _safe_refresh)
    app.include_router(api)
    return app


async def _safe_refresh(scheduler: Scheduler, symbol: str, params: ChainParams | None = None, force: bool = False) -> None:
    try:
        await scheduler.get_or_fetch(symbol, params, force=force)
    except Exception as exc:  # noqa: BLE001
        log.warning("websocket-triggered refresh of %s failed: %s", symbol, exc)
