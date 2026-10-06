"""Extension endpoints: scenarios, surface, drift/EOD, scan, flow, realized/validation,
complex, OI estimate, alerts, backtest, features."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Callable

from fastapi import APIRouter, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

from . import analytics as an
from .alerts import Alert, AlertConfig
from .chain import NY, ChainSnapshot
from .config import Settings
from .flow import FlowResponse
from .models import WallTrend
from .scenarios import ScenarioGrid, scenario_grid
from .surface import IVSurface, iv_surface

log = logging.getLogger(__name__)


def _to_bar(b) -> an.Bar:
    d = b.date
    if isinstance(d, datetime):
        ts = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(NY)
    else:
        ts = datetime(d.year, d.month, d.day, tzinfo=NY)
    return an.Bar(ts=ts, open=float(b.open), high=float(b.high), low=float(b.low), close=float(b.close), volume=float(b.volume or 0))


def register_extensions(api: APIRouter, app: FastAPI, settings: Settings, st: Callable, _symbol: Callable, _safe_refresh: Callable) -> None:
    r, q = settings.risk_free_rate, settings.dividend_yield

    async def _snapshot(sym: str, refresh: bool = False) -> ChainSnapshot:
        s = st()
        await s.scheduler.get_or_fetch(sym, None, force=refresh)
        state = s.scheduler.ensure(sym)
        if state.snapshot is None:
            raise HTTPException(status_code=404, detail="no chain snapshot")
        return state.snapshot

    def _today_ny() -> date:
        return datetime.now(tz=timezone.utc).astimezone(NY).date()

    # ------------------------------------------------------------ scenarios
    @api.get("/gex/{symbol}/scenarios", response_model=ScenarioGrid)
    async def get_scenarios(
        symbol: str,
        spot_pct: float = Query(0.05, gt=0, le=0.3),
        spot_steps: int = Query(21, ge=3, le=201),
        iv_points: float = Query(10.0, ge=0, le=100),
        iv_steps: int = Query(5, ge=1, le=21),
        days: str = Query("0,1", description="comma separated day shifts"),
        refresh: bool = Query(False),
    ) -> ScenarioGrid:
        try:
            day_shifts = tuple(sorted({int(x) for x in days.split(",") if x.strip()}))
        except ValueError:
            raise HTTPException(status_code=422, detail="days must be comma separated integers") from None
        if not day_shifts or len(day_shifts) > 10 or min(day_shifts) < 0 or max(day_shifts) > 60:
            raise HTTPException(status_code=422, detail="days must contain 1..10 values in 0..60")
        snap = await _snapshot(_symbol(symbol), refresh)
        return scenario_grid(snap, r, q, spot_pct, spot_steps, iv_points, iv_steps, day_shifts)

    # -------------------------------------------------------------- surface
    @api.get("/gex/{symbol}/surface", response_model=IVSurface)
    async def get_surface(symbol: str, refresh: bool = Query(False)) -> IVSurface:
        snap = await _snapshot(_symbol(symbol), refresh)
        return iv_surface(snap, r, q)

    # ---------------------------------------------------------- drift / EOD
    @api.get("/gex/{symbol}/drift", response_model=an.DriftResponse)
    async def get_drift(symbol: str, day: date | None = Query(None, alias="date")) -> an.DriftResponse:
        sym = _symbol(symbol)
        d = day or _today_ny()
        start = datetime(d.year, d.month, d.day, tzinfo=NY)
        end = start.replace(hour=23, minute=59, second=59)
        results = await st().store.day_payloads(sym, start.astimezone(timezone.utc), end.astimezone(timezone.utc))
        return an.drift(sym, d.isoformat(), results)

    @api.get("/gex/{symbol}/eod")
    async def get_eod(symbol: str, days: int = Query(60, ge=1, le=1000)) -> list[dict]:
        return await st().store.eod_rows(_symbol(symbol), days)

    @api.get("/gex/{symbol}/walls", response_model=WallTrend)
    async def get_walls(symbol: str) -> WallTrend:
        """Daily front-week wall series plus intraday prints taken near the call wall."""
        sym = _symbol(symbol)
        front, days, touches = await st().store.wall_trend(sym)
        return WallTrend(symbol=sym, front_expiry=front, days=days, touches=touches)

    @api.post("/eod/archive")
    async def force_eod_archive() -> dict:
        """Archive the latest snapshot of every watched symbol now (normally automatic after the close)."""
        archived = await st().scheduler.archive_eod(force=True)
        return {"archived": archived}

    # ------------------------------------------------------------- realized
    async def _realized(sym: str, days: int) -> an.RealizedResponse:
        s = st()
        underlying = await s.fetcher.resolve_underlying(sym)
        bars = await s.fetcher.fetch_bars(underlying, f"{days} D", "5 mins")
        return an.realized_metrics(sym, [_to_bar(b) for b in bars])

    @api.get("/gex/{symbol}/realized", response_model=an.RealizedResponse)
    async def get_realized(symbol: str, days: int = Query(5, ge=1, le=30)) -> an.RealizedResponse:
        return await _realized(_symbol(symbol), days)

    @api.get("/gex/{symbol}/validation", response_model=an.ValidationResponse)
    async def get_validation(symbol: str, days: int = Query(30, ge=2, le=30)) -> an.ValidationResponse:
        sym = _symbol(symbol)
        eod = await st().store.eod_rows(sym, days + 5)
        realized = await _realized(sym, days)
        return an.validation(sym, eod, realized)

    # ------------------------------------------------------------- backtest
    @api.get("/gex/{symbol}/backtest", response_model=an.BacktestResponse)
    async def get_backtest(symbol: str, days: int = Query(120, ge=2, le=365)) -> an.BacktestResponse:
        sym = _symbol(symbol)
        s = st()
        eod = await s.store.eod_rows(sym, days + 5)
        underlying = await s.fetcher.resolve_underlying(sym)
        bars = await s.fetcher.fetch_bars(underlying, f"{days + 10} D", "1 day")
        return an.backtest(sym, eod, [_to_bar(b) for b in bars])

    # ---------------------------------------------------------- OI estimate
    @api.get("/gex/{symbol}/oi-estimate", response_model=an.OIEstimate)
    async def get_oi_estimate(
        symbol: str, opening_ratio: float | None = Query(None, ge=-1, le=1), refresh: bool = Query(False)
    ) -> an.OIEstimate:
        sym = _symbol(symbol)
        snap = await _snapshot(sym, refresh)
        hist = await st().store.oi_reconcile_rows(sym)
        ratio = opening_ratio if opening_ratio is not None else settings.oi_opening_ratio
        return an.oi_estimate(snap, r, q, ratio, hist)

    # ------------------------------------------------------------- features
    @api.get("/gex/{symbol}/features")
    async def get_features(symbol: str, day: date | None = Query(None, alias="date")) -> dict:
        sym = _symbol(symbol)
        s = st()
        if day is not None:
            payload = await s.store.eod_payload(sym, day.isoformat())
            if payload is None:
                raise HTTPException(status_code=404, detail="no EOD archive for that date")
            return an.feature_row(payload, day.isoformat())
        result = await s.scheduler.get_or_fetch(sym, None)
        return an.feature_row(result)

    @api.get("/features/export")
    async def export_features(
        symbols: str | None = Query(None, description="comma separated; default all archived symbols"),
        days: int = Query(250, ge=1, le=2000),
        fmt: str = Query("csv", alias="format", pattern="^(csv|json)$"),
    ):
        s = st()
        syms = [_symbol(x) for x in symbols.split(",") if x.strip()] if symbols else await s.store.eod_symbols()
        rows: list[dict] = []
        for sym in syms:
            for eod in await s.store.eod_rows(sym, days):
                payload = await s.store.eod_payload(sym, eod["date"])
                if payload is not None:
                    rows.append(an.feature_row(payload, eod["date"]))
        if fmt == "json":
            return rows
        return PlainTextResponse(an.features_csv(rows), media_type="text/csv")

    # ----------------------------------------------------------------- scan
    @api.get("/scan", response_model=list[an.ScanRow])
    async def scan(
        symbols: str | None = Query(None, description="comma separated; default = currently watched symbols"),
        sort: str = Query("gamma_imbalance_pct_adv"),
        refresh: bool = Query(False),
        pin: bool = Query(False, description="keep the symbols on the watch list"),
    ) -> list[an.ScanRow]:
        if sort not in an.SCAN_SORT_KEYS:
            raise HTTPException(status_code=422, detail=f"sort must be one of {sorted(an.SCAN_SORT_KEYS)}")
        if symbols:
            syms = [_symbol(x) for x in symbols.split(",") if x.strip()]
        else:
            syms = st().scheduler.watched()
        if not syms or len(syms) > settings.scan_max_symbols:
            raise HTTPException(status_code=422, detail=f"1..{settings.scan_max_symbols} symbols (or watch at least one)")
        sched = st().scheduler
        rows: list[an.ScanRow] = []
        for sym in syms:
            state = sched.ensure(sym, None, pinned=True if pin else None)
            if state.result is not None and not refresh:
                rows.append(an.scan_row(sym, sched._with_age(state)))
                continue
            if not state.refreshing.locked():
                asyncio.create_task(_safe_refresh(sched, sym, None, force=refresh))
            rows.append(an.scan_row(sym, None, state.last_error))
        return an.sort_scan(rows, sort)

    # -------------------------------------------------------------- complex
    @api.get("/complex")
    async def list_complexes() -> dict[str, list[str]]:
        return an.COMPLEXES

    @api.get("/complex/{name}", response_model=an.ComplexResponse)
    async def get_complex(name: str, wait: bool = Query(False, description="fetch missing members before answering")) -> an.ComplexResponse:
        key = name.upper()
        members = an.COMPLEXES.get(key)
        if members is None:
            raise HTTPException(status_code=404, detail=f"unknown complex; known: {sorted(an.COMPLEXES)}")
        sched = st().scheduler
        results = {}
        for m in members:
            state = sched.ensure(m, None)
            if state.result is None:
                if wait:
                    try:
                        await sched.get_or_fetch(m, None)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("complex %s: fetch %s failed: %s", key, m, exc)
                elif not state.refreshing.locked():
                    asyncio.create_task(_safe_refresh(sched, m))
            state = sched.ensure(m, None)
            if state.result is not None:
                results[m] = sched._with_age(state)
        if not results:
            raise HTTPException(status_code=202, detail="no member data yet; fetches started, poll again")
        return an.complex_view(key, results, members)

    # ----------------------------------------------------------------- flow
    @api.get("/flow")
    async def list_flow() -> list[str]:
        return st().flow.symbols()

    @api.put("/flow/{symbol}", response_model=FlowResponse, status_code=201)
    async def start_flow(symbol: str) -> FlowResponse:
        sym = _symbol(symbol)
        s = st()
        snap = await _snapshot(sym)
        underlying = await s.fetcher.resolve_underlying(sym)
        s.scheduler.ensure(sym, None, pinned=True)
        try:
            flow = await s.flow.start(snap, lambda row: s.fetcher._contract_for(underlying, row), r, q)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return flow.response()

    @api.get("/flow/{symbol}", response_model=FlowResponse)
    async def get_flow(symbol: str) -> FlowResponse:
        flow = st().flow.get(_symbol(symbol))
        if flow is None:
            raise HTTPException(status_code=404, detail="flow tracking not started; PUT first")
        return flow.response()

    @api.delete("/flow/{symbol}", status_code=204)
    async def stop_flow(symbol: str) -> None:
        if not await st().flow.stop(_symbol(symbol)):
            raise HTTPException(status_code=404, detail="flow tracking not started")

    # --------------------------------------------------------------- alerts
    @api.get("/alerts", response_model=list[Alert])
    async def list_alerts(symbol: str | None = Query(None), limit: int = Query(100, ge=1, le=1000)) -> list[Alert]:
        rows = await st().store.recent_alerts(_symbol(symbol) if symbol else None, limit)
        return [Alert(**row) for row in rows]

    @api.get("/alerts/config", response_model=dict[str, AlertConfig])
    async def alert_configs() -> dict[str, AlertConfig]:
        return st().alerts.configs()

    @api.get("/alerts/{symbol}/config", response_model=AlertConfig)
    async def get_alert_config(symbol: str) -> AlertConfig:
        return st().alerts.config(_symbol(symbol))

    @api.put("/alerts/{symbol}/config", response_model=AlertConfig)
    async def set_alert_config(symbol: str, cfg: AlertConfig) -> AlertConfig:
        st().alerts.set_config(_symbol(symbol), cfg)
        return cfg

    @app.websocket("/ws/alerts")
    async def ws_alerts(websocket: WebSocket) -> None:
        if settings.api_key:
            provided = websocket.headers.get("x-api-key") or websocket.query_params.get("api_key")
            if provided != settings.api_key:
                await websocket.close(code=4401)
                return
        await websocket.accept()
        queue: asyncio.Queue[Alert] = asyncio.Queue(maxsize=100)

        async def listener(alert: Alert) -> None:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(alert)

        mgr = st().alerts
        mgr.subscribe(listener)
        try:
            while True:
                sender = asyncio.create_task(queue.get())
                receiver = asyncio.create_task(websocket.receive_text())
                done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if receiver in done:
                    return
                if sender in done:
                    await websocket.send_text(sender.result().model_dump_json())
        except WebSocketDisconnect:
            return
        finally:
            mgr.unsubscribe(listener)
