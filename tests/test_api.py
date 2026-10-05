"""API tests with the IB-facing fetcher replaced by a synthetic one."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient

from gex_service.api import AppState, create_app
from gex_service.chain import NY, ChainError, ChainParams, ChainRow, ChainSnapshot
from gex_service.config import Settings


class FakeClient:
    is_connected = True
    last_error = None
    market_data_type = 1
    lines_in_use = 0

    async def start(self):
        pass

    async def stop(self):
        pass


class FakeFetcher:
    def __init__(self):
        self.client = FakeClient()
        self.calls = 0

    async def fetch(self, symbol: str, params: ChainParams) -> ChainSnapshot:
        self.calls += 1
        if symbol == "NOPE":
            raise ChainError("cannot resolve underlying for 'NOPE'")
        rows = []
        for k in range(480, 521, 5):
            rows.append(ChainRow(k * 10 + 1, "20261016", 11.0, float(k), "C", symbol, 100.0, iv=0.2, oi=1000 if k != 515 else 8000))
            rows.append(ChainRow(k * 10 + 2, "20261016", 11.0, float(k), "P", symbol, 100.0, iv=0.22, oi=1000 if k != 485 else 9000))
        return ChainSnapshot(
            symbol=symbol, sec_type="STK", spot=500.0, ts=datetime.now(tz=NY), oi_asof="2026-10-02",
            rows=rows, expirations=["20261016"], contracts_total=len(rows), contracts_with_data=len(rows),
            fetch_duration_s=0.1, market_data_type=1, params=params,
        )


@pytest.fixture
async def app_client(tmp_path):
    settings = Settings(_env_file=None, db_path=tmp_path / "t.sqlite", api_key="secret", refresh_interval_s=3600)
    state = AppState(settings)
    state.client = FakeClient()
    state.fetcher = FakeFetcher()
    state.scheduler.fetcher = state.fetcher
    app = create_app(settings, state)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", headers={"x-api-key": "secret"}) as client:
            yield client, state


async def test_health_and_auth(app_client):
    client, _ = app_client
    r = await client.get("/health")
    assert r.status_code == 200 and r.json()["ib_connected"] is True
    r = await client.get("/api/v1/gex/SPY", headers={"x-api-key": "wrong"})
    assert r.status_code == 401


async def test_gex_endpoint_caches_and_refreshes(app_client):
    client, state = app_client
    r = await client.get("/api/v1/gex/spy")
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "SPY"
    assert body["summary"]["call_wall"] == 515.0
    assert body["summary"]["put_wall"] == 485.0
    assert body["summary"]["zero_gamma"] is not None
    assert len(body["profile"]) == 9
    assert state.fetcher.calls == 1

    r = await client.get("/api/v1/gex/SPY")
    assert r.status_code == 200 and state.fetcher.calls == 1  # served from cache
    r = await client.get("/api/v1/gex/SPY?refresh=true")
    assert r.status_code == 200 and state.fetcher.calls == 2
    # changing params invalidates the cache
    r = await client.get("/api/v1/gex/SPY?max_dte=7&expiries=20261016")
    assert r.status_code == 200 and state.fetcher.calls == 3
    assert r.json()["params"]["expiries"] == ["20261016"]


async def test_non_blocking_wait_false(app_client):
    client, state = app_client
    r = await client.get("/api/v1/gex/DIA?wait=false")
    assert r.status_code == 202 and r.json()["status"] == "pending"
    await asyncio.sleep(0.05)  # let the background fetch finish
    r = await client.get("/api/v1/gex/DIA?wait=false")
    assert r.status_code == 200 and r.json()["symbol"] == "DIA"
    assert state.fetcher.calls == 1


async def test_validation_and_errors(app_client):
    client, _ = app_client
    assert (await client.get("/api/v1/gex/SPY?expiries=2026-10-16")).status_code == 422
    assert (await client.get("/api/v1/gex/SPY?strike_range_pct=0.9")).status_code == 422
    assert (await client.get("/api/v1/gex/NOPE")).status_code == 404
    assert (await client.get("/api/v1/gex/bad$sym")).status_code == 422


async def test_chain_history_and_watch(app_client):
    client, state = app_client
    r = await client.get("/api/v1/gex/QQQ/chain")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert len(rows) == 18 and all("gex" in row for row in rows)

    r = await client.get("/api/v1/gex/QQQ/history")
    assert r.status_code == 200 and len(r.json()) == 1

    r = await client.put("/api/v1/watch/IWM?max_dte=10")
    assert r.status_code == 201 and r.json()["pinned"] is True
    r = await client.get("/api/v1/watch")
    assert {e["symbol"] for e in r.json()} == {"QQQ", "IWM"}
    assert (await client.delete("/api/v1/watch/IWM")).status_code == 204
    assert (await client.delete("/api/v1/watch/IWM")).status_code == 404


async def test_websocket_pushes_on_refresh(app_client):
    _, state = app_client
    received: list = []

    async def listener(result):
        received.append(result)

    ws_state = state.scheduler.subscribe("SPY", listener)
    await state.scheduler.refresh(ws_state)
    assert len(received) == 1 and received[0].symbol == "SPY"
    state.scheduler.unsubscribe("SPY", listener)
    await state.scheduler.refresh(ws_state)
    assert len(received) == 1


def test_websocket_endpoint_sends_cached_then_pushed(tmp_path):
    from starlette.testclient import TestClient

    settings = Settings(_env_file=None, db_path=tmp_path / "ws.sqlite", refresh_interval_s=3600)
    state = AppState(settings)
    state.client = FakeClient()
    state.fetcher = FakeFetcher()
    state.scheduler.fetcher = state.fetcher
    app = create_app(settings, state)
    with TestClient(app) as client:
        # first connection has no cache: the server triggers a fetch and pushes the result
        with client.websocket_connect("/ws/gex/SPY") as ws:
            first = ws.receive_json()
            assert first["symbol"] == "SPY" and first["summary"]["call_wall"] == 515.0
        # second connection gets the cached payload immediately
        with client.websocket_connect("/ws/gex/SPY") as ws:
            cached = ws.receive_json()
            assert cached["ts"] == first["ts"]


async def test_idle_eviction_respects_pins_and_listeners(app_client):
    _, state = app_client
    sched = state.scheduler
    sched.settings.idle_ttl_s = 0.0
    sched.ensure("AAA")
    sched.ensure("BBB", pinned=True)
    sched.subscribe("CCC", lambda r: asyncio.sleep(0))
    await asyncio.sleep(0.01)
    sched._evict_idle()
    assert sched.watched() == ["BBB", "CCC"]
