from datetime import date, datetime, timedelta

from gex_service.chain import NY, ChainParams, ChainRow, ChainSnapshot
from gex_service.gex import compute_gex, front_week_expiry
from gex_service.models import ExpiryStats
from gex_service.store import SnapshotStore


def _row(con_id, expiry, dte, strike, right, oi):
    return ChainRow(con_id, expiry, dte, strike, right, "TEST", 100.0, iv=0.2, oi=oi)


def _snap(rows, spot, ts):
    return ChainSnapshot(
        symbol="TEST", sec_type="STK", spot=spot, ts=ts, oi_asof="2026-10-05", rows=rows,
        expirations=sorted({r.expiry for r in rows}), contracts_total=len(rows), contracts_with_data=len(rows),
        fetch_duration_s=1.0, market_data_type=1, params=ChainParams(45, 0.12, 1500), adv_shares=1_000_000,
    )


def test_front_week_picks_friday_not_the_same_day_book():
    day = date(2026, 10, 6)  # Tuesday; Friday is the 9th
    stats = [
        ExpiryStats(expiry="20261006", dte=0.3, total_gex=1, call_gex=1, put_gex=0, call_wall=100, put_wall=90, contracts=2),
        ExpiryStats(expiry="20261009", dte=3.3, total_gex=1, call_gex=1, put_gex=0, call_wall=105, put_wall=95, contracts=2),
        ExpiryStats(expiry="20261016", dte=10, total_gex=1, call_gex=1, put_gex=0, call_wall=110, put_wall=90, contracts=2),
    ]
    assert front_week_expiry(stats, day).expiry == "20261009"


async def test_expiry_days_replace_and_wall_touch_is_throttled(tmp_path):
    store = SnapshotStore(tmp_path / "t.sqlite", retention_days=14)
    await store.open()
    ts = datetime(2026, 10, 6, 11, 0, tzinfo=NY)
    rows = [
        _row(1, "20261009", 3.2, 100.0, "C", oi=2000),
        _row(2, "20261009", 3.2, 90.0, "P", oi=2000),
    ]
    snap = _snap(rows, 100.0, ts)
    result = compute_gex(snap, r=0.0, q=0.0, now=snap.ts)
    await store.upsert_expiry_days(result)
    result.summary.total_gex = 1.0
    await store.upsert_expiry_days(result)
    front, days, touches = await store.wall_trend("TEST")
    assert front == "20261009"
    assert len(days) == 1
    assert days[0].call_wall is not None
    assert days[0].finalized is False

    week = next(item for item in result.by_expiry if item.expiry == "20261009")
    result.spot = week.call_wall
    assert await store.record_wall_touch(result, min_interval_s=900) is True
    assert await store.record_wall_touch(result, min_interval_s=900) is False
    later = result.model_copy(deep=True)
    later.ts = result.ts + timedelta(minutes=20)
    assert await store.record_wall_touch(later, min_interval_s=900) is True
    _, _, touches = await store.wall_trend("TEST")
    assert len(touches) == 2

    await store.save_watch("MSFT", ChainParams(21, 0.12, 400))
    saved = await store.list_watches()
    assert saved[0][0] == "MSFT"
    await store.delete_watch("MSFT")
    assert await store.list_watches() == []
    await store.close()
