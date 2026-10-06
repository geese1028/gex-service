"""Tests for the extension analytics and endpoints (roll-off, scenarios, surface, drift/EOD,
scan, flow classification, realized/validation, complex, OI estimate, alerts, backtest, features)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from gex_service import analytics as an
from gex_service.alerts import AlertConfig, evaluate
from gex_service.api import AppState, create_app
from gex_service.chain import NY, ChainParams, ChainRow, ChainSnapshot
from gex_service.cli import export_rows
from gex_service.config import Settings
from gex_service.flow import classify, select_flow_contracts
from gex_service.gex import compute_gex
from gex_service.scenarios import scenario_grid
from gex_service.surface import iv_surface

R, Q = 0.045, 0.0


def make_snapshot(symbol: str = "SPY", spot: float = 500.0, ts: datetime | None = None, oi_scale: float = 1.0) -> ChainSnapshot:
    rows = []
    for exp, dte in (("20261009", 3.5), ("20261012", 6.5), ("20261120", 45.0)):
        for k in range(450, 551, 5):
            skew = 0.18 + 0.004 * (500 - k) / 5
            rows.append(ChainRow(hash((exp, k, "C")) % 10**6, exp, dte, float(k), "C", symbol, 100.0, iv=skew, oi=(2000 if k > 500 else 500) * oi_scale, volume=300.0, bid=1.0, ask=1.2))
            rows.append(ChainRow(hash((exp, k, "P")) % 10**6, exp, dte, float(k), "P", symbol, 100.0, iv=skew + 0.02, oi=(2500 if k < 500 else 400) * oi_scale, volume=400.0, bid=1.0, ask=1.2))
    return ChainSnapshot(
        symbol=symbol, sec_type="STK", spot=spot, ts=ts or datetime.now(tz=NY), oi_asof="2026-10-02", rows=rows,
        expirations=["20261009", "20261012", "20261120"], contracts_total=len(rows), contracts_with_data=len(rows),
        fetch_duration_s=1.0, market_data_type=1, params=ChainParams(45, 0.12, 1500), adv_shares=50_000_000.0,
        iv30_history=list(np.linspace(0.12, 0.3, 250)) + [0.2], hv30_history=[0.15] * 10,
    )


# ------------------------------------------------------------------ roll-off


def test_roll_off_scenarios():
    res = compute_gex(make_snapshot(), R, Q)
    names = [s.name for s in res.roll_off]
    assert names == ["drop_nearest", "drop_week"]
    nearest = res.roll_off[0]
    assert nearest.excluded_expiries == ["20261009"]
    assert 0 < nearest.abs_gex_removed_share < 1
    assert nearest.zero_gamma is not None and nearest.call_wall is not None
    week = res.roll_off[1]
    assert week.excluded_expiries == ["20261009", "20261012"]
    assert week.abs_gex_removed_share > nearest.abs_gex_removed_share
    assert res.iv_context is not None and res.iv_context.iv30 == 0.2 and res.iv_context.hv30 == 0.15


def test_roll_off_single_expiry_has_no_scenarios():
    snap = make_snapshot()
    snap.rows = [r for r in snap.rows if r.expiry == "20261012"]
    res = compute_gex(snap, R, Q)
    assert res.roll_off == []


# ----------------------------------------------------------------- scenarios


def test_scenario_grid_shapes_and_monotone_delta():
    grid = scenario_grid(make_snapshot(), R, Q, spot_pct=0.05, spot_steps=11, iv_points=10, iv_steps=3, day_shifts=(0, 1, 5))
    assert len(grid.spot_levels) == 11 and grid.iv_shifts == [-10.0, 0.0, 10.0] and grid.day_shifts == [0, 1, 5]
    assert len(grid.surfaces) == 3
    s0 = grid.surfaces[0]
    assert len(s0.dealer_delta_shares) == 3 and len(s0.dealer_delta_shares[0]) == 11
    # at spot / no shift / no time: equals base
    mid = 5
    assert s0.dealer_delta_shares[1][mid] == pytest.approx(grid.base_dealer_delta_shares, rel=1e-6)
    assert s0.hedge_flow_shares[1][mid] == pytest.approx(0.0, abs=1e-6)
    assert s0.net_gex[1][mid] == pytest.approx(grid.base_net_gex, rel=1e-6)
    # local slope of dealer delta in spot has the sign of net dealer gamma
    deltas = s0.dealer_delta_shares[1]
    assert np.sign(deltas[mid + 1] - deltas[mid - 1]) == np.sign(grid.base_net_gex)
    # 5 days ahead the 3.5-DTE expiry is gone: gamma there collapses, so total |GEX| falls
    assert abs(grid.surfaces[2].net_gex[1][mid]) < abs(grid.base_net_gex)


# ------------------------------------------------------------------- surface


def test_iv_surface_skew_and_rank():
    surf = iv_surface(make_snapshot(), R, Q)
    assert [t["expiry"] for t in surf.term_structure] == ["20261009", "20261012", "20261120"]
    e = surf.expiries[0]
    assert e.atm_iv == pytest.approx(0.18, abs=0.01)
    assert e.iv_put_25d > e.iv_call_25d  # put skew by construction
    assert e.risk_reversal_25d < 0 and e.skew_slope < 0
    assert e.butterfly_25d is not None
    assert surf.stats.iv30_rank_1y == pytest.approx(44.44, abs=0.1)
    assert surf.stats.iv_hv_spread == pytest.approx(0.05)


# -------------------------------------------------------- drift / features


def test_drift_and_feature_row():
    t0 = datetime(2026, 10, 5, 10, 0, tzinfo=NY)
    a = compute_gex(make_snapshot(spot=500.0, ts=t0), R, Q)
    b = compute_gex(make_snapshot(spot=508.0, ts=t0 + timedelta(hours=2)), R, Q)
    d = an.drift("SPY", "2026-10-05", [a, b])
    assert d.points == 2 and d.change["spot"] == pytest.approx(8.0)
    assert d.first.ts == t0 and d.latest.spot == 508.0
    row = an.feature_row(b, "2026-10-05")
    assert list(row) == an.FEATURE_COLUMNS
    assert row["spot"] == 508.0 and row["regime"] in {"positive_gamma", "negative_gamma"}
    assert row["iv30_rank_1y"] is not None and row["roll_off_nearest_removed_share"] is not None
    csv = an.features_csv([row])
    assert csv.splitlines()[0].startswith("symbol,date,ts,spot") and len(csv.splitlines()) == 2


# ------------------------------------------------------------ realized etc.


def _bars(day: date, prices: list[float]) -> list[an.Bar]:
    start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY)
    out = []
    for i, p in enumerate(prices):
        out.append(an.Bar(ts=start + timedelta(minutes=5 * i), open=p, high=p * 1.001, low=p * 0.999, close=p, volume=1000))
    return out


def test_realized_metrics_mean_reversion():
    rng = np.random.default_rng(0)
    # alternating returns -> strongly negative lag-1 autocorrelation
    prices = [100.0]
    for i in range(77):
        prices.append(prices[-1] * (1.002 if i % 2 == 0 else 0.998))
    rv = an.realized_metrics("SPY", _bars(date(2026, 10, 2), prices))
    assert len(rv.days) == 1
    d = rv.days[0]
    assert d.bars == 78 and d.autocorr_lag1 < -0.9 and d.realized_vol > 0
    assert d.last30_vs_rest in {"momentum", "reversal"}
    trend = [100 * (1 + 0.0005 * i) for i in range(78)]
    rv2 = an.realized_metrics("SPY", _bars(date(2026, 10, 5), trend))
    assert rv2.days[0].last30_vs_rest == "momentum"
    _ = rng


def test_validation_and_backtest_from_eod_rows():
    eod = [
        {"date": "2026-10-01", "spot": 500.0, "total_gex": 5e9, "zero_gamma": 495.0, "call_wall": 510.0, "put_wall": 490.0, "call_wall_oi": 505.0, "put_wall_oi": 480.0, "pct_adv": 12.0, "regime": "positive_gamma"},
        {"date": "2026-10-02", "spot": 502.0, "total_gex": -2e9, "zero_gamma": 505.0, "call_wall": 515.0, "put_wall": 495.0, "call_wall_oi": 515.0, "put_wall_oi": 495.0, "pct_adv": 4.0, "regime": "negative_gamma"},
        {"date": "2026-10-05", "spot": 498.0, "total_gex": 1e9, "zero_gamma": 497.0, "call_wall": 505.0, "put_wall": 490.0, "call_wall_oi": 505.0, "put_wall_oi": 490.0, "pct_adv": 7.0, "regime": "positive_gamma"},
    ]
    realized = an.realized_metrics("SPY", _bars(date(2026, 10, 2), [500 + 0.3 * i for i in range(78)]) + _bars(date(2026, 10, 5), [502 - 0.5 * i for i in range(78)]))
    v = an.validation("SPY", eod, realized)
    assert v.n == 2 and v.note is not None and "positive_gamma" in v.by_regime
    daily = [
        an.Bar(ts=datetime(2026, 10, 2, tzinfo=NY), open=501, high=508, low=499, close=507, volume=1),
        an.Bar(ts=datetime(2026, 10, 5, tzinfo=NY), open=503, high=520, low=494, close=496, volume=1),
        an.Bar(ts=datetime(2026, 10, 6, tzinfo=NY), open=497, high=503, low=491, close=500, volume=1),
    ]
    b = an.backtest("SPY", eod, daily)
    assert b.n_days == 3
    assert b.call_wall_hold_rate == pytest.approx(2 / 3)  # 10-02 broke 515 (high 520)
    assert b.put_wall_hold_rate == pytest.approx(2 / 3)  # 10-02 broke 495 (low 494)
    assert b.call_wall_oi_hold_rate == pytest.approx(1 / 3)  # 10-01 OI wall 505 < next high 508 too
    assert b.zero_gamma_side_persistence is not None


# ------------------------------------------------------------------- complex


def test_complex_view_maps_to_anchor_level():
    spx = compute_gex(make_snapshot("SPX", spot=5000.0), R, Q)
    spy = compute_gex(make_snapshot("SPY", spot=500.0), R, Q)
    c = an.complex_view("SPX", {"SPX": spx, "SPY": spy}, ["SPX", "SPY", "XSP"])
    assert c.anchor == "SPX" and c.missing == ["XSP"]
    spy_member = next(m for m in c.members if m.symbol == "SPY")
    assert spy_member.ratio_to_anchor == pytest.approx(10.0)
    assert spy_member.call_wall_anchor_level == pytest.approx(spy.summary.call_wall * 10)
    assert c.total_gex == pytest.approx(spx.summary.total_gex + spy.summary.total_gex)
    assert c.zero_gamma_anchor_level is not None and 4500 < c.zero_gamma_anchor_level < 5500
    assert all(p["level"] % 5 == 0 for p in c.profile)


# ---------------------------------------------------------------------- scan


def test_scan_rows_and_sort():
    res = compute_gex(make_snapshot(), R, Q)
    rows = [an.scan_row("SPY", res), an.scan_row("QQQ", None), an.scan_row("BAD", None, "cannot resolve")]
    assert [r.status for r in rows] == ["ok", "pending", "error"]
    ordered = an.sort_scan(rows, "gamma_imbalance_pct_adv")
    assert ordered[0].symbol == "SPY"  # rows with data sort first


# ----------------------------------------------------------------------- flow


def test_lee_ready_classification_and_selection():
    assert classify(1.20, 1.00, 1.20, None) == 1
    assert classify(1.00, 1.00, 1.20, None) == -1
    assert classify(1.12, 1.00, 1.20, None) == 1  # above mid
    assert classify(1.08, 1.00, 1.20, None) == -1
    assert classify(1.10, 1.00, 1.20, 1.05) == 1  # at mid -> tick test
    assert classify(1.10, None, None, None) == 0
    picked = select_flow_contracts(make_snapshot(), per_side=3)
    assert len(picked) == 12 and {r.expiry for r in picked} == {"20261009"}
    assert sorted({r.strike for r in picked}) == [490.0, 495.0, 500.0, 505.0, 510.0, 515.0]


# --------------------------------------------------------------- OI estimate


def test_oi_estimate_and_calibration():
    snap = make_snapshot()
    est = an.oi_estimate(snap, R, Q, 0.5, [{"date": "2026-10-02", "prev_total_oi": 1000.0, "actual_total_oi": 1300.0, "total_volume": 500.0}])
    assert est.est_total_oi == pytest.approx(est.prev_total_oi + 0.5 * est.total_volume)
    assert est.calibrated_ratio == pytest.approx(0.6)
    assert abs(est.est_total_gex) > abs(compute_gex(snap, R, Q).summary.total_gex) * 0.5
    prev, vol, max_exp = an.oi_totals_for_reconcile(snap, date(2026, 10, 9))  # nearest expiry expires today
    assert max_exp == "20261120" and prev < sum(r.oi for r in snap.rows)
    assert an.actual_oi_up_to(snap, "20261012", date(2026, 10, 9)) == sum(r.oi for r in snap.rows if r.expiry == "20261012")


# -------------------------------------------------------------------- alerts


def test_alert_rules_edge_triggered():
    t0 = datetime(2026, 10, 5, 10, 0, tzinfo=NY)
    a = compute_gex(make_snapshot(spot=500.0, ts=t0), R, Q)
    zg = a.summary.zero_gamma
    assert zg is not None
    # move spot to the other side of zero gamma and far enough to cross a wall
    b = compute_gex(make_snapshot(spot=(zg + 20.0) if zg >= 500 else (zg - 20.0), ts=t0 + timedelta(minutes=5)), R, Q)
    alerts = evaluate(a, b, AlertConfig(gamma_imbalance_pct_adv=0.0))
    rules = {x.rule for x in alerts}
    assert "zero_gamma_cross" in rules
    assert evaluate(None, b, AlertConfig()) == []
    assert evaluate(a, a, AlertConfig()) == []
    only = evaluate(a, b, AlertConfig(rules=["regime_flip"]))
    assert all(x.rule == "regime_flip" for x in only)


# ------------------------------------------------------------------ API layer


class FakeClient:
    is_connected = True
    last_error = None
    market_data_type = 1
    lines_in_use = 0

    async def start(self):
        pass

    async def stop(self):
        pass


@dataclass
class FakeBar:
    date: object
    open: float
    high: float
    low: float
    close: float
    volume: float


class FakeFetcher:
    def __init__(self):
        self.client = FakeClient()
        self.calls = 0
        self.spot = 500.0

    async def fetch(self, symbol: str, params: ChainParams) -> ChainSnapshot:
        self.calls += 1
        return make_snapshot(symbol, spot=self.spot * (10 if symbol in {"SPX"} else 1))

    async def resolve_underlying(self, symbol: str):
        return symbol

    async def fetch_bars(self, underlying, duration: str, bar_size: str, use_rth: bool = True):
        today = datetime.now(tz=NY).date()
        if bar_size == "1 day":
            return [FakeBar(today - timedelta(days=i), 500, 510, 490, 505, 1e6) for i in range(5, 0, -1)]
        out = []
        for d in range(2, 0, -1):
            day = today - timedelta(days=d)
            start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY).astimezone(timezone.utc)
            out += [FakeBar(start + timedelta(minutes=5 * i), 500 + i * 0.1, 500.5 + i * 0.1, 499.5 + i * 0.1, 500 + i * 0.1, 1000) for i in range(78)]
        return out

    def _contract_for(self, underlying, row):
        return row


@pytest.fixture
async def ext_client(tmp_path):
    settings = Settings(_env_file=None, db_path=tmp_path / "t.sqlite", api_key="", refresh_interval_s=3600)
    state = AppState(settings)
    state.client = FakeClient()
    state.fetcher = FakeFetcher()
    state.scheduler.fetcher = state.fetcher
    app = create_app(settings, state)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, state, tmp_path / "t.sqlite"


async def test_extension_endpoints(ext_client):
    client, state, db = ext_client
    r = await client.get("/api/v1/gex/SPY")
    assert r.status_code == 200 and len(r.json()["roll_off"]) == 2

    r = await client.get("/api/v1/gex/SPY?exclude_expiries=20261009")
    assert r.status_code == 200
    body = r.json()
    assert "excluded expiries: 20261009" in body["meta"]["warnings"][-1]
    assert [s["name"] for s in body["roll_off"]] == ["drop_nearest"]
    assert state.fetcher.calls == 1  # recomputed from cache
    assert (await client.get("/api/v1/gex/SPY?exclude_expiries=bad")).status_code == 422

    r = await client.get("/api/v1/gex/SPY/scenarios?spot_steps=5&iv_steps=1&days=0,2")
    assert r.status_code == 200 and r.json()["day_shifts"] == [0, 2] and len(r.json()["surfaces"]) == 2
    assert (await client.get("/api/v1/gex/SPY/scenarios?days=x")).status_code == 422

    r = await client.get("/api/v1/gex/SPY/surface")
    assert r.status_code == 200 and len(r.json()["expiries"]) == 3 and r.json()["stats"]["iv30"] == 0.2

    r = await client.get("/api/v1/gex/SPY/drift")
    assert r.status_code == 200 and r.json()["points"] == 1

    r = await client.get("/api/v1/gex/SPY/realized?days=2")
    assert r.status_code == 200 and len(r.json()["days"]) == 2

    r = await client.get("/api/v1/gex/SPY/oi-estimate?opening_ratio=0.3")
    assert r.status_code == 200 and r.json()["opening_ratio"] == 0.3

    r = await client.get("/api/v1/gex/SPY/features")
    assert r.status_code == 200 and r.json()["symbol"] == "SPY"

    # EOD archive (forced) -> eod rows, features by date, export, backtest, validation, CLI
    r = await client.post("/api/v1/eod/archive")
    assert r.status_code == 200 and r.json()["archived"] == ["SPY"]
    r = await client.get("/api/v1/gex/SPY/eod")
    assert r.status_code == 200 and len(r.json()) == 1
    day = r.json()[0]["date"]
    r = await client.get(f"/api/v1/gex/SPY/features?date={day}")
    assert r.status_code == 200 and r.json()["date"] == day
    r = await client.get("/api/v1/features/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and len(r.text.splitlines()) == 2
    r = await client.get("/api/v1/features/export?format=json&symbols=SPY")
    assert r.status_code == 200 and len(r.json()) == 1
    r = await client.get("/api/v1/gex/SPY/backtest?days=10")
    assert r.status_code == 200 and "n_days" in r.json()
    r = await client.get("/api/v1/gex/SPY/validation?days=5")
    assert r.status_code == 200 and r.json()["n"] >= 0
    rows = export_rows(db, ["SPY"], 10)
    assert len(rows) == 1 and rows[0]["date"] == day

    # OI reconcile row was written by the hook
    hist = (await client.get("/api/v1/gex/SPY/oi-estimate")).json()["reconcile_history"]
    assert len(hist) == 1 and hist[0]["total_volume"] > 0

    # scan: cached SPY ok, QQQ pending then ok
    r = await client.get("/api/v1/scan?symbols=SPY,QQQ")
    assert r.status_code == 200
    statuses = {row["symbol"]: row["status"] for row in r.json()}
    assert statuses == {"SPY": "ok", "QQQ": "pending"}
    await asyncio.sleep(0.05)
    r = await client.get("/api/v1/scan?symbols=SPY,QQQ&sort=total_gex")
    assert {row["status"] for row in r.json()} == {"ok"}
    assert (await client.get("/api/v1/scan?symbols=SPY&sort=nope")).status_code == 422

    # scan with no symbols uses the watch list
    r = await client.get("/api/v1/scan")
    assert r.status_code == 200 and {row["symbol"] for row in r.json()} >= {"SPY", "QQQ"}

    # complex
    complexes = (await client.get("/api/v1/complex")).json()
    assert complexes["SPX"] == ["SPX", "SPY", "XSP"]
    assert complexes["VIX"] == ["VIX"]
    r = await client.get("/api/v1/complex/SPX?wait=true")
    assert r.status_code == 200 and r.json()["anchor"] == "SPX" and r.json()["missing"] == []
    assert (await client.get("/api/v1/complex/NOPE")).status_code == 404

    # alerts config + listing
    r = await client.put("/api/v1/alerts/SPY/config", json={"gamma_imbalance_pct_adv": 3.0, "rules": ["regime_flip"]})
    assert r.status_code == 200 and r.json()["rules"] == ["regime_flip"]
    assert (await client.get("/api/v1/alerts/config")).json()["SPY"]["gamma_imbalance_pct_adv"] == 3.0
    assert (await client.get("/api/v1/alerts")).status_code == 200

    # flow: not started -> 404
    assert (await client.get("/api/v1/flow/SPY")).status_code == 404
    assert (await client.get("/api/v1/flow")).json() == []


async def test_alert_fires_through_scheduler_and_ws(ext_client):
    client, state, _ = ext_client
    await client.get("/api/v1/gex/SPY")
    first = state.scheduler.get("SPY").result
    zg = first.summary.zero_gamma
    state.fetcher.spot = zg + 25 if zg >= 500 else zg - 25
    await client.get("/api/v1/gex/SPY?refresh=true")
    alerts = (await client.get("/api/v1/alerts?symbol=SPY")).json()
    assert any(a["rule"] == "zero_gamma_cross" for a in alerts)
    assert all(a["id"] is not None for a in alerts)
