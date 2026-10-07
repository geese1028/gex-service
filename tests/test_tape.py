"""Signed call-wall tape, the day's path, and the index vol-control echo."""

from datetime import date, timedelta

from gex_service.chain import ChainParams, ChainRow, ChainSnapshot
from gex_service.gex import compute_gex
from gex_service.models import PathSwitch, WallTape
from gex_service.tape import select_wall_rows, session_path, wall_tape_from_legs
from gex_service.vol_control import vol_control_from_closes
from tests.test_gex import make_snapshot, row


def _closes(start: float, returns: list[float], first: date) -> list[tuple[date, float]]:
    px = start
    day = first
    out = [(day, px)]
    for ret in returns:
        day += timedelta(days=1)
        px *= 1.0 + ret
        out.append((day, px))
    return out


def test_wall_rows_sit_on_the_call_wall():
    rows = []
    for i, strike in enumerate((80, 90, 100, 110, 120)):
        rows.append(ChainRow(i * 2 + 1, "20261009", 3.0, float(strike), "C", "SPY", 100.0, oi=10))
        rows.append(ChainRow(i * 2 + 2, "20261009", 3.0, float(strike), "P", "SPY", 100.0, oi=10))
    snap = ChainSnapshot(
        symbol="SPY", sec_type="STK", spot=101.0, ts=make_snapshot([]).ts, oi_asof="2026-10-05",
        rows=rows, expirations=["20261009"], contracts_total=len(rows), contracts_with_data=len(rows),
        fetch_duration_s=0.1, market_data_type=1, params=ChainParams(7, 0.12, 100),
    )
    picked = select_wall_rows(snap, "20261009", 100.0, neighbors=1)
    assert {row.strike for row in picked} == {90.0, 100.0, 110.0}
    assert {row.right for row in picked} == {"C", "P"}


def test_customer_call_buys_weaken_the_fade():
    tape = wall_tape_from_legs(
        expiry="20261009", call_wall=100.0, strikes=[100.0],
        call_buy=40, call_sell=5, put_buy=0, put_sell=0, gex=-1.0, shares=100.0,
    )
    assert tape.tape == "shorter_gamma" and tape.wall_read == "fade_weaker"
    path = PathSwitch(
        book="front_week", regime="positive_gamma", path="fade", expiry="20261009",
        strike=100.0, shares_per_1pct=1.0, note="oi fade",
    )
    decided = session_path(path, tape)
    assert decided.source == "tape" and decided.path == "chase"


def test_customer_selling_keeps_the_fade():
    tape = wall_tape_from_legs(
        expiry="20261009", call_wall=100.0, strikes=[100.0],
        call_buy=5, call_sell=40, put_buy=0, put_sell=10, gex=2.0, shares=-50.0,
    )
    assert tape.tape == "longer_gamma" and tape.wall_read == "fade_stands"
    path = PathSwitch(
        book="front_week", regime="negative_gamma", path="chase", expiry="20261009",
        strike=100.0, shares_per_1pct=1.0, note="oi chase",
    )
    decided = session_path(path, tape)
    assert decided.source == "tape" and decided.path == "fade"


def test_small_tape_leaves_the_open_interest_path():
    tape = wall_tape_from_legs(
        expiry="20261009", call_wall=100.0, strikes=[100.0],
        call_buy=3, call_sell=1, put_buy=0, put_sell=0, gex=-1.0, shares=10.0,
    )
    assert tape.tape == "quiet" and tape.wall_read == "unchanged"
    path = PathSwitch(
        book="front_week", regime="positive_gamma", path="fade", expiry="20261009",
        strike=100.0, shares_per_1pct=1.0, note="oi fade",
    )
    decided = session_path(path, tape)
    assert decided.source == "oi" and decided.path == "fade"


def test_zero_dte_path_is_the_expiring_book():
    rows = [
        row(1, "20261005", 0.2, 105.0, "C", oi=5000),
        row(2, "20261005", 0.2, 95.0, "P", oi=5000),
        row(3, "20261016", 11.0, 80.0, "C", oi=50000),
    ]
    res = compute_gex(make_snapshot(rows, spot=100.0), r=0.0, q=0.0)
    assert res.path_switch is not None and res.path_switch.book == "zero_dte"
    assert res.path_switch.expiry == "20261005"
    assert res.session_path is not None and res.session_path.source == "oi"
    assert res.session_path.path == res.path_switch.path


def test_vol_control_echo_after_a_shock_then_goes_quiet():
    small = [0.01, -0.01] * 10
    shock = _closes(100.0, small + [-0.04], date(2026, 1, 1))
    as_of = shock[-1][0] + timedelta(days=1)
    selling = vol_control_from_closes("SPY", shock, as_of=as_of, sec_type="STK")
    assert selling.applies and selling.state == "selling"
    assert selling.echo_left == 5 and selling.sessions_since_shock == 0

    later = _closes(shock[-1][1], [0.001] * 5, shock[-1][0])
    # later includes the shock close as its first print; stitch the tail only.
    extended = shock + later[1:]
    done = vol_control_from_closes("SPY", extended, as_of=extended[-1][0] + timedelta(days=1))
    assert done.state != "selling"


def test_vol_control_full_after_a_quiet_stretch_and_off_for_a_single_name():
    noisy = [0.02, -0.02] * 15
    quiet = [0.002, -0.002] * 8
    closes = _closes(100.0, noisy + quiet, date(2026, 1, 1))
    full = vol_control_from_closes("SPY", closes, as_of=closes[-1][0] + timedelta(days=1))
    assert full.state == "full"
    off = vol_control_from_closes("RKLB", closes, as_of=closes[-1][0] + timedelta(days=1), sec_type="STK")
    assert off.applies is False and off.state == "off"


def test_unavailable_tape_type_is_a_wall_tape():
    tape = WallTape(expiry=None, call_wall=None, tape="unavailable", wall_read="unchanged", note="n")
    assert tape.classified_volume == 0.0
