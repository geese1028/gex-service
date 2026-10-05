from datetime import datetime
from types import SimpleNamespace

from gex_service.chain import (
    NY,
    ChainRow,
    cap_contracts,
    days_to_expiry,
    filter_strikes,
    oi_as_of,
    select_expiries,
    ticker_to_row,
)
from gex_service.ib_client import is_rth_now

NOW = datetime(2026, 10, 5, 10, 30, tzinfo=NY)  # Monday during RTH


def test_select_expiries_window_and_alive():
    expirations = ["20261002", "20261005", "20261009", "20261113", "20270115"]
    chosen = select_expiries(expirations, NOW, max_dte=45)
    assert chosen == ["20261005", "20261009", "20261113"]
    # 0DTE still alive before 16:00 ET
    assert days_to_expiry("20261005", NOW) > 0
    after_close = datetime(2026, 10, 5, 16, 1, tzinfo=NY)
    assert select_expiries(expirations, after_close, max_dte=45) == ["20261009", "20261113"]


def test_select_expiries_explicit_filter():
    expirations = ["20261005", "20261009", "20261113"]
    assert select_expiries(expirations, NOW, 45, explicit=["20261009", "20261231"]) == ["20261009"]


def test_filter_strikes():
    strikes = [400.0, 440.0, 450.0, 500.0, 550.0, 560.0, 600.0]
    assert filter_strikes(strikes, 500.0, 0.10) == [450.0, 500.0, 550.0]


def _rows(n_expiries=3, strikes=range(450, 551, 5)):
    rows = []
    for e in range(n_expiries):
        for k in strikes:
            for right in "CP":
                rows.append(ChainRow(len(rows), f"202610{10 + e:02d}", 5.0 + e, float(k), right, "X", 100.0))
    return rows


def test_cap_contracts_keeps_nearest_strikes_symmetrically():
    rows = _rows()
    assert len(rows) == 3 * 21 * 2
    kept = cap_contracts(rows, 500.0, 60)
    assert len(kept) <= 60
    kept_strikes = sorted({r.strike for r in kept})
    assert kept_strikes == [480.0, 485.0, 490.0, 495.0, 500.0, 505.0, 510.0, 515.0, 520.0]
    # all expiries retained
    assert len({r.expiry for r in kept}) == 3
    # no-op when under the cap
    assert cap_contracts(rows, 500.0, 10_000) == rows


def test_cap_contracts_hard_truncates_when_single_band_exceeds_cap():
    rows = _rows(n_expiries=10, strikes=[500])
    kept = cap_contracts(rows, 500.0, 4)
    assert len(kept) == 4


def test_oi_as_of_is_previous_trading_day():
    assert oi_as_of(NOW) == "2026-10-02"  # Monday -> previous Friday
    tuesday = datetime(2026, 10, 6, 10, 0, tzinfo=NY)
    assert oi_as_of(tuesday) == "2026-10-05"


def test_is_rth_now():
    assert is_rth_now(NOW)
    assert not is_rth_now(datetime(2026, 10, 5, 8, 0, tzinfo=NY))
    assert not is_rth_now(datetime(2026, 10, 4, 12, 0, tzinfo=NY))  # Sunday


def test_fill_iv_from_counterpart():
    from gex_service.chain import fill_iv_from_counterpart

    c = ChainRow(1, "20261009", 4.0, 740.0, "C", "X", 100.0, oi=1000)  # deep ITM, no greeks
    p = ChainRow(2, "20261009", 4.0, 740.0, "P", "X", 100.0, oi=500, iv=0.19, gamma=0.001)
    lonely = ChainRow(3, "20261009", 4.0, 700.0, "C", "X", 100.0, oi=10)
    assert fill_iv_from_counterpart([c, p, lonely]) == 1
    assert c.iv == 0.19 and c.gamma is None and c.has_gex_inputs
    assert lonely.iv is None and not lonely.has_gex_inputs


def test_ticker_to_row_maps_fields_and_oi_by_right():
    nan = float("nan")
    greeks = SimpleNamespace(impliedVol=0.21, gamma=0.012, delta=0.55)
    call_ticker = SimpleNamespace(
        bid=1.0, ask=1.2, last=1.1, volume=30, modelGreeks=greeks, impliedVolatility=nan,
        callOpenInterest=1500, putOpenInterest=nan,
    )
    put_ticker = SimpleNamespace(
        bid=nan, ask=nan, last=nan, volume=nan, modelGreeks=None, impliedVolatility=0.3,
        callOpenInterest=nan, putOpenInterest=800,
    )
    c = ticker_to_row(ChainRow(1, "20261009", 4.0, 500.0, "C", "X", 100.0), call_ticker)
    p = ticker_to_row(ChainRow(2, "20261009", 4.0, 500.0, "P", "X", 100.0), put_ticker)
    assert c.oi == 1500 and c.gamma == 0.012 and c.iv == 0.21 and c.volume == 30
    assert p.oi == 800 and p.gamma is None and p.iv == 0.3 and p.bid is None
    assert c.has_gex_inputs and p.has_gex_inputs
