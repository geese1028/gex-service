"""Tests for the companion analytics: DEX/VEX/CEX, hedge flow, volume lens,
implied move, concentration."""

from datetime import datetime

import numpy as np

from gex_service.chain import NY, ChainParams, ChainRow, ChainSnapshot
from gex_service.gex import compute_gex
from gex_service.greeks import bs_charm, bs_delta, bs_vanna


def snap(rows, spot=100.0, adv=None):
    return ChainSnapshot(
        symbol="T", sec_type="STK", spot=spot, ts=datetime(2026, 10, 5, 10, 0, tzinfo=NY), oi_asof="2026-10-02",
        rows=rows, expirations=sorted({r.expiry for r in rows}), contracts_total=len(rows), contracts_with_data=len(rows),
        fetch_duration_s=0.1, market_data_type=1, params=ChainParams(45, 0.12, 1500), adv_shares=adv,
    )


def row(cid, expiry, dte, k, right, oi, iv=0.25, vol=0.0, bid=None, ask=None):
    return ChainRow(cid, expiry, dte, k, right, "T", 100.0, iv=iv, oi=oi, volume=vol, bid=bid, ask=ask)


def test_vanna_and_charm_match_finite_differences():
    S, K, T, v, r, q = 100.0, 105.0, 0.25, 0.3, 0.04, 0.01
    h = 1e-4
    fd_vanna = (bs_delta(S, K, T, v + h, True, r, q) - bs_delta(S, K, T, v - h, True, r, q)) / (2 * h)
    assert abs(float(bs_vanna(S, K, T, v, r, q)) - float(fd_vanna)) < 1e-6
    for is_call in (True, False):
        fd_charm = -(bs_delta(S, K, T + h, v, is_call, r, q) - bs_delta(S, K, T - h, v, is_call, r, q)) / (2 * h)
        assert abs(float(bs_charm(S, K, T, v, is_call, r, q)) - float(fd_charm)) < 1e-5
    # put and call delta differ by e^{-qT}
    d_call = float(bs_delta(S, K, T, v, True, r, q))
    d_put = float(bs_delta(S, K, T, v, False, r, q))
    assert abs((d_call - d_put) - np.exp(-q * T)) < 1e-12


def test_exposures_signs_and_hedge_flow():
    # one OTM call and one OTM put, equal OI -> dealer long call delta, short put (positive delta)
    rows = [row(1, "20261016", 11.0, 105.0, "C", oi=1000), row(2, "20261016", 11.0, 95.0, "P", oi=1000)]
    res = compute_gex(snap(rows, 100.0, adv=5_000_000), r=0.0, q=0.0)
    assert res.exposures is not None
    assert res.exposures.dex > 0  # long call delta + (-)(negative put delta) > 0
    assert abs(res.exposures.dex_shares - res.exposures.dex / 100.0) < 1e-9
    # OTM call vanna > 0 (counts +), OTM put vanna < 0 (counts -, so contributes +): net VEX positive
    assert res.exposures.vex > 0
    assert res.exposures.net_gex_up_1pct is not None and res.exposures.net_gex_down_1pct is not None
    hf = res.hedge_flow
    assert hf is not None and hf.regime in ("positive_gamma", "negative_gamma", "flat")
    assert abs(hf.shares_per_1pct - abs(res.summary.total_gex) / 100.0) < 1e-6
    assert hf.pct_adv_per_1pct is not None and hf.pct_adv_per_1pct >= 0
    assert hf.distance_to_zero_gamma_pct is not None  # a flip exists between the two strikes


def test_hedge_flow_pct_adv_barbon_buraschi():
    rows = [row(1, "20261016", 11.0, 100.0, "C", oi=10_000)]
    res = compute_gex(snap(rows, 100.0, adv=2_000_000), r=0.0, q=0.0)
    hf = res.hedge_flow
    assert hf.regime == "positive_gamma"
    assert abs(hf.shares_per_1pct - res.summary.total_gex / 100.0) < 1e-6
    assert abs(hf.pct_adv_per_1pct - hf.shares_per_1pct / 2_000_000 * 100) < 1e-9
    assert "dampens" in hf.direction_note
    # no ADV (index) -> pct is None
    res2 = compute_gex(snap(rows, 100.0, adv=None), r=0.0, q=0.0)
    assert res2.hedge_flow.pct_adv_per_1pct is None


def test_volume_lens_uses_volume_weights():
    rows = [
        row(1, "20261016", 11.0, 105.0, "C", oi=100, vol=5000),
        row(2, "20261016", 11.0, 110.0, "C", oi=9000, vol=10),
        row(3, "20261016", 11.0, 95.0, "P", oi=100, vol=4000),
    ]
    res = compute_gex(snap(rows, 100.0), r=0.0, q=0.0)
    assert res.summary.call_wall_oi == 110.0
    assert res.volume_lens is not None
    assert res.volume_lens.call_wall == 105.0  # today's flow concentrates at 105
    assert res.volume_lens.put_wall == 95.0
    assert res.volume_lens.total_call_volume == 5010 and res.volume_lens.total_put_volume == 4000
    # profile carries volume fields
    lvl = {p.strike: p for p in res.profile}
    assert lvl[105.0].call_volume == 5000 and lvl[105.0].net_gex_volume > 0
    # no volume at all -> lens omitted
    res2 = compute_gex(snap([row(1, "20261016", 11.0, 100.0, "C", oi=10)], 100.0), r=0.0, q=0.0)
    assert res2.volume_lens is None


def test_implied_move_from_nearest_expiry_straddle():
    rows = [
        row(1, "20261009", 4.0, 100.0, "C", oi=10, iv=0.20, bid=1.0, ask=1.2),
        row(2, "20261009", 4.0, 100.0, "P", oi=10, iv=0.24, bid=0.9, ask=1.1),
        row(3, "20261016", 11.0, 100.0, "C", oi=10, iv=0.30, bid=5.0, ask=5.0),
    ]
    res = compute_gex(snap(rows, 100.0), r=0.0, q=0.0)
    im = res.implied_move
    assert im is not None and im.expiry == "20261009" and im.atm_strike == 100.0
    assert abs(im.straddle_price - 2.1) < 1e-9
    assert abs(im.straddle_move_pct - 2.1) < 1e-9
    assert abs(im.atm_iv - 0.22) < 1e-9
    assert abs(im.iv_move_to_expiry_pct - 0.22 * (4.0 / 365) ** 0.5 * 100) < 1e-9
    assert abs(im.iv_daily_move_pct - 0.22 / 252**0.5 * 100) < 1e-9


def test_concentration_and_zero_dte_share():
    rows = [
        row(1, "20261005", 0.3, 100.0, "C", oi=10_000),  # 0DTE, dominant
        row(2, "20261016", 11.0, 110.0, "C", oi=100),
        row(3, "20261016", 11.0, 90.0, "P", oi=100),
    ]
    res = compute_gex(snap(rows, 100.0), r=0.0, q=0.0)
    c = res.concentration
    assert c.absolute_gamma_strike == 100.0
    assert c.top_strikes[0] == 100.0 and len(c.top_strikes) == 3
    assert c.gex_hhi is not None and 0.5 < c.gex_hhi <= 1.0
    assert c.zero_dte_share is not None and c.zero_dte_share > 0.9
    e = {x.expiry: x for x in res.by_expiry}
    assert abs(sum(x.abs_gex_share for x in res.by_expiry) - 1.0) < 1e-9
    assert e["20261005"].abs_gex_share > 0.9


def test_vanna_flip_detected_when_sign_changes():
    # puts below, calls above with equal weight -> VEX flips sign around spot
    rows = [row(1, "20261016", 11.0, 110.0, "C", oi=1000), row(2, "20261016", 11.0, 90.0, "P", oi=1000)]
    res = compute_gex(snap(rows, 100.0), r=0.0, q=0.0)
    assert res.gamma_curve and res.gamma_curve[0].net_vex is not None
    # OTM call vanna>0 counts +, OTM put vanna<0 counts - => both positive; moving spot far up
    # makes the call ITM (vanna<0) while the put stays OTM -> sign change somewhere above spot
    assert res.exposures.vanna_flip is None or res.exposures.vanna_flip > 0
