from datetime import datetime

import numpy as np

from gex_service.chain import NY, ChainParams, ChainRow, ChainSnapshot
from gex_service.gex import (
    _find_zero_crossing,
    chain_response,
    compute_gex,
    contract_gex,
    max_pain,
)
from gex_service.models import StrikeLevel


def make_snapshot(rows, spot=500.0, params=None):
    return ChainSnapshot(
        symbol="TEST",
        sec_type="STK",
        spot=spot,
        ts=datetime(2026, 10, 5, 10, 0, tzinfo=NY),
        oi_asof="2026-10-02",
        rows=rows,
        expirations=sorted({r.expiry for r in rows}),
        contracts_total=len(rows),
        contracts_with_data=len(rows),
        fetch_duration_s=1.0,
        market_data_type=1,
        params=params or ChainParams(45, 0.12, 1500),
    )


def row(con_id, expiry, dte, strike, right, oi, iv=0.2, gamma=None):
    return ChainRow(con_id, expiry, dte, strike, right, "TEST", 100.0, iv=iv, gamma=gamma, oi=oi)


def test_contract_gex_sign_and_scale():
    assert contract_gex(100.0, 0.01, 10, 100, "C") == 0.01 * 10 * 100 * 100 * 100 * 0.01
    assert contract_gex(100.0, 0.01, 10, 100, "P") < 0


def test_find_zero_crossing_prefers_upward_flip_nearest_spot():
    levels = np.array([90.0, 95.0, 100.0, 105.0, 110.0])
    values = np.array([-2.0, -1.0, 1.0, 2.0, 3.0])
    zero = _find_zero_crossing(levels, values, 100.0)
    assert abs(zero - 97.5) < 1e-9
    assert _find_zero_crossing(levels, np.array([1.0, 2.0, 3.0, 4.0, 5.0]), 100.0) is None


def test_compute_gex_walls_and_zero_gamma():
    rows = []
    spot = 500.0
    for k in range(470, 531, 5):
        call_oi = 10_000 if k == 520 else 1_000
        put_oi = 12_000 if k == 480 else 1_000
        rows.append(row(k * 10 + 1, "20261016", 11.0, float(k), "C", call_oi))
        rows.append(row(k * 10 + 2, "20261016", 11.0, float(k), "P", put_oi))
    result = compute_gex(make_snapshot(rows, spot), r=0.04, q=0.0)

    assert result.summary.call_wall == 520.0
    assert result.summary.put_wall == 480.0
    assert result.summary.call_wall_oi == 520.0
    assert result.summary.put_wall_oi == 480.0
    assert result.summary.zero_gamma is not None
    assert result.summary.zero_gamma_method == "bs_grid"
    # With a heavier put wall below spot, the flip should sit between the walls
    assert 480.0 < result.summary.zero_gamma < 520.0
    assert len(result.gamma_curve) == 161
    assert result.meta.contracts_used == len(rows)
    assert result.meta.dropped_contracts == 0
    assert result.summary.put_call_oi_ratio is not None and result.summary.put_call_oi_ratio > 1
    assert len(result.by_expiry) == 1 and result.by_expiry[0].contracts == len(rows)
    # net at spot from the curve should match the direct aggregate closely
    assert abs(result.summary.curve_gex_at_spot - result.summary.total_gex) / abs(result.summary.total_gex) < 0.05


def test_compute_gex_uses_reported_gamma_when_present_and_drops_bad_rows():
    rows = [
        row(1, "20261016", 11.0, 500.0, "C", oi=100, iv=None, gamma=0.02),
        row(2, "20261016", 11.0, 500.0, "P", oi=0, iv=0.2),  # zero OI -> dropped
        row(3, "20261016", 11.0, 505.0, "P", oi=50, iv=None, gamma=None),  # no gamma, no iv -> dropped
    ]
    result = compute_gex(make_snapshot(rows, 500.0), r=0.0, q=0.0)
    assert result.meta.contracts_used == 1
    assert result.meta.dropped_contracts == 2
    assert result.summary.call_gex == 0.02 * 100 * 100 * 500 * 500 * 0.01
    # no IV anywhere -> grid not possible, single strike -> no profile crossing
    assert result.summary.zero_gamma is None
    assert result.summary.zero_gamma_method == "none"


def test_zero_gamma_profile_fallback_when_no_iv():
    rows = [
        row(1, "20261016", 11.0, 490.0, "P", oi=1000, iv=None, gamma=0.03),
        row(2, "20261016", 11.0, 510.0, "C", oi=1000, iv=None, gamma=0.03),
    ]
    result = compute_gex(make_snapshot(rows, 500.0), r=0.0, q=0.0)
    assert result.summary.zero_gamma_method == "strike_profile"
    assert abs(result.summary.zero_gamma - 500.0) < 1e-6


def test_max_pain_simple():
    profile = [
        StrikeLevel(strike=90, call_gex=0, put_gex=0, net_gex=0, call_oi=0, put_oi=100),
        StrikeLevel(strike=100, call_gex=0, put_gex=0, net_gex=0, call_oi=100, put_oi=100),
        StrikeLevel(strike=110, call_gex=0, put_gex=0, net_gex=0, call_oi=100, put_oi=0),
    ]
    assert max_pain(profile) == 100.0


def test_by_expiry_split_and_chain_response():
    rows = [
        row(1, "20261009", 4.0, 500.0, "C", 100),
        row(2, "20261016", 11.0, 500.0, "P", 100),
        ChainRow(3, "20261016", 11.0, 505.0, "C", "TEST", 100.0),  # no data at all
    ]
    snap = make_snapshot(rows, 500.0)
    result = compute_gex(snap, r=0.0, q=0.0)
    assert [e.expiry for e in result.by_expiry] == ["20261009", "20261016"]
    assert result.meta.dropped_contracts == 1

    chain = chain_response(snap, 0.0, 0.0)
    assert len(chain.rows) == 3
    by_id = {r.con_id: r for r in chain.rows}
    assert by_id[1].gex is not None and by_id[1].gex > 0
    assert by_id[2].gex is not None and by_id[2].gex < 0
    assert by_id[3].gex is None
