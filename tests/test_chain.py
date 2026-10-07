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
from gex_service.config import Settings
from gex_service.ib_client import IBClient, is_rth_now

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


def test_default_client_id_matches_nautilus():
    assert Settings(_env_file=None).ib_client_id == 1


def test_batch_size_clamped_to_ib_line_ceiling():
    s = Settings(_env_file=None, batch_size=200, max_md_lines=100)
    assert s.batch_size == 100


def test_is_rth_now():
    assert is_rth_now(NOW)
    assert not is_rth_now(datetime(2026, 10, 5, 8, 0, tzinfo=NY))
    assert not is_rth_now(datetime(2026, 10, 4, 12, 0, tzinfo=NY))  # Sunday


def test_ib_client_stays_disconnected_outside_rth():
    client = IBClient(Settings(_env_file=None, ib_rth_only=True))
    assert client.session_allowed(NOW)
    assert not client.session_allowed(datetime(2026, 10, 5, 16, 5, tzinfo=NY))
    assert not client.session_allowed(datetime(2026, 10, 4, 12, 0, tzinfo=NY))
    always = IBClient(Settings(_env_file=None, ib_rth_only=False))
    assert always.session_allowed(datetime(2026, 10, 5, 20, 0, tzinfo=NY))


def test_fill_iv_from_counterpart():
    from gex_service.chain import fill_iv_from_counterpart

    c = ChainRow(1, "20261009", 4.0, 740.0, "C", "X", 100.0, oi=1000)  # deep ITM, no greeks
    p = ChainRow(2, "20261009", 4.0, 740.0, "P", "X", 100.0, oi=500, iv=0.19, gamma=0.001)
    lonely = ChainRow(3, "20261009", 4.0, 700.0, "C", "X", 100.0, oi=10)
    assert fill_iv_from_counterpart([c, p, lonely]) == 1
    assert c.iv == 0.19 and c.gamma is None and c.has_gex_inputs
    assert lonely.iv is None and not lonely.has_gex_inputs


def test_tick_collector_maps_option_ticks():
    from gex_service.ib_client import TickCollector

    c = TickCollector()
    c.on_price(7, 1, 1.2)
    c.on_price(7, 2, 1.4)
    c.on_size(7, 27, 1500)
    c.on_greeks(7, 13, 0.21, 0.55, 0.012)
    snap = c.by_req[7]
    assert snap.bid == 1.2 and snap.ask == 1.4
    assert snap.call_oi == 1500 and snap.iv == 0.21 and snap.gamma == 0.012


def test_option_computation_field_order():
    from gex_service.ib_client import parse_option_computation

    # tickAttrib, impliedVol, delta, optPrice, pvDividend, gamma, vega, theta, undPrice
    implied, delta, gamma = parse_option_computation(0, 0.21, 0.55, 1.2, 0.0, 0.012, 0.08, -0.04, 500.0)
    assert (implied, delta, gamma) == (0.21, 0.55, 0.012)


def test_option_volume_is_not_double_counted():
    from gex_service.ib_client import TickCollector

    trade_first = TickCollector()
    trade_first.on_size(1, 8, 100)
    trade_first.on_size(1, 29, 100)
    assert trade_first.by_req[1].volume == 100

    side_first = TickCollector()
    side_first.on_size(1, 29, 40)
    side_first.on_size(1, 8, 40)
    assert side_first.by_req[1].volume == 40


def test_expiry_uses_last_trade_date_not_new_york_shift():
    from datetime import timezone

    from gex_service.ib_client import option_expiry_yyyymmdd

    # Nautilus fallback is midnight UTC, which is the previous evening in New York.
    midnight_utc = datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)
    assert option_expiry_yyyymmdd("", midnight_utc) == "20261009"
    assert option_expiry_yyyymmdd("20261016", midnight_utc) == "20261016"


def test_market_data_batch_leaves_room_for_open_lines():
    from gex_service.ib_client import market_data_batch_size

    assert market_data_batch_size(100, 100, 0) == 100
    assert market_data_batch_size(100, 100, 12) == 88
    assert market_data_batch_size(100, 100, 100) == 0


def test_daily_hist_bar_date_stays_yyyymmdd():
    from gex_service.ib_client import ib_hist_bar_datetime

    assert ib_hist_bar_datetime("20261005").date().isoformat() == "2026-10-05"


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
