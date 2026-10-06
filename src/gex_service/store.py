"""SQLite snapshot store for intraday GEX history."""

from __future__ import annotations

import json
import logging
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from .chain import NY, ChainParams
from .gex import front_week_expiry
from .models import ExpiryDay, GexResponse, HistoryPoint, WallTouch

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    spot REAL NOT NULL,
    total_gex REAL NOT NULL,
    zero_gamma REAL,
    call_wall REAL,
    put_wall REAL,
    params_key TEXT NOT NULL,
    payload BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_ts ON snapshots(symbol, ts);
CREATE TABLE IF NOT EXISTS watches (
    symbol TEXT PRIMARY KEY,
    max_dte INTEGER NOT NULL,
    strike_range_pct REAL NOT NULL,
    max_contracts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS expiry_days (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    expiry TEXT NOT NULL,
    dte REAL,
    ts TEXT NOT NULL,
    spot REAL NOT NULL,
    call_wall REAL,
    call_wall_gex REAL,
    call_wall_oi REAL,
    put_wall REAL,
    abs_gex_share REAL,
    total_gex REAL,
    shares_per_1pct REAL,
    pct_adv REAL,
    volume_call_wall REAL,
    oi_asof TEXT,
    finalized INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, date, expiry)
);
CREATE TABLE IF NOT EXISTS wall_touches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    expiry TEXT NOT NULL,
    ts TEXT NOT NULL,
    spot REAL NOT NULL,
    call_wall REAL,
    distance_pct REAL NOT NULL,
    volume_call_wall REAL,
    call_wall_gex REAL
);
CREATE INDEX IF NOT EXISTS idx_wall_touches_symbol_ts ON wall_touches(symbol, ts);
CREATE TABLE IF NOT EXISTS eod_snapshots (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    ts TEXT NOT NULL,
    spot REAL NOT NULL,
    total_gex REAL NOT NULL,
    zero_gamma REAL,
    call_wall REAL,
    put_wall REAL,
    call_wall_oi REAL,
    put_wall_oi REAL,
    pct_adv REAL,
    regime TEXT,
    payload BLOB NOT NULL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    rule TEXT NOT NULL,
    message TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
CREATE TABLE IF NOT EXISTS oi_reconcile (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    est_total_oi REAL NOT NULL,
    prev_total_oi REAL NOT NULL,
    actual_total_oi REAL,
    opening_ratio REAL NOT NULL,
    total_volume REAL NOT NULL DEFAULT 0,
    max_expiry TEXT,
    PRIMARY KEY (symbol, date)
);
"""


class SnapshotStore:
    def __init__(self, path: Path, retention_days: int) -> None:
        self.path = path
        self.retention_days = retention_days
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        await self._db.executescript(SCHEMA)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store not open")
        return self._db

    async def save(self, result: GexResponse, params_key: str) -> None:
        payload = zlib.compress(result.model_dump_json().encode())
        await self._conn().execute(
            "INSERT INTO snapshots(symbol, ts, spot, total_gex, zero_gamma, call_wall, put_wall, params_key, payload)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                result.symbol,
                result.ts.isoformat(),
                result.spot,
                result.summary.total_gex,
                result.summary.zero_gamma,
                result.summary.call_wall,
                result.summary.put_wall,
                params_key,
                payload,
            ),
        )
        await self._conn().commit()

    async def history(
        self,
        symbol: str,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 500,
    ) -> list[HistoryPoint]:
        clauses = ["symbol = ?"]
        args: list[object] = [symbol.upper()]
        if since is not None:
            clauses.append("ts >= ?")
            args.append(since.isoformat())
        if until is not None:
            clauses.append("ts <= ?")
            args.append(until.isoformat())
        sql = (
            "SELECT ts, spot, total_gex, zero_gamma, call_wall, put_wall FROM snapshots WHERE "
            + " AND ".join(clauses)
            + " ORDER BY ts DESC LIMIT ?"
        )
        args.append(limit)
        async with self._conn().execute(sql, args) as cursor:
            rows = await cursor.fetchall()
        points = [
            HistoryPoint(
                ts=datetime.fromisoformat(r[0]),
                spot=r[1],
                total_gex=r[2],
                zero_gamma=r[3],
                call_wall=r[4],
                put_wall=r[5],
            )
            for r in rows
        ]
        points.reverse()
        return points

    async def latest_payload(self, symbol: str) -> GexResponse | None:
        async with self._conn().execute(
            "SELECT payload FROM snapshots WHERE symbol = ? ORDER BY ts DESC LIMIT 1", (symbol.upper(),)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return GexResponse.model_validate(json.loads(zlib.decompress(row[0]).decode()))

    # ------------------------------------------------------------- intraday
    async def day_payloads(self, symbol: str, day_start: datetime, day_end: datetime) -> list[GexResponse]:
        """All intraday payloads for one session, oldest first."""
        async with self._conn().execute(
            "SELECT payload FROM snapshots WHERE symbol = ? AND ts >= ? AND ts <= ? ORDER BY ts ASC",
            (symbol.upper(), day_start.isoformat(), day_end.isoformat()),
        ) as cursor:
            rows = await cursor.fetchall()
        return [GexResponse.model_validate(json.loads(zlib.decompress(r[0]).decode())) for r in rows]

    async def last_payload_before(self, symbol: str, cutoff: datetime) -> GexResponse | None:
        async with self._conn().execute(
            "SELECT payload FROM snapshots WHERE symbol = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
            (symbol.upper(), cutoff.isoformat()),
        ) as cursor:
            row = await cursor.fetchone()
        return GexResponse.model_validate(json.loads(zlib.decompress(row[0]).decode())) if row else None

    # ------------------------------------------------------------------ EOD
    async def save_eod(self, result: GexResponse, day: str) -> None:
        payload = zlib.compress(result.model_dump_json().encode())
        hf = result.hedge_flow
        await self._conn().execute(
            "INSERT OR REPLACE INTO eod_snapshots(symbol, date, ts, spot, total_gex, zero_gamma, call_wall, put_wall,"
            " call_wall_oi, put_wall_oi, pct_adv, regime, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                result.symbol, day, result.ts.isoformat(), result.spot, result.summary.total_gex,
                result.summary.zero_gamma, result.summary.call_wall, result.summary.put_wall,
                result.summary.call_wall_oi, result.summary.put_wall_oi,
                hf.pct_adv_per_1pct if hf else None, hf.regime if hf else None, payload,
            ),
        )
        await self._conn().commit()

    async def eod_rows(self, symbol: str, limit: int = 250) -> list[dict]:
        async with self._conn().execute(
            "SELECT date, ts, spot, total_gex, zero_gamma, call_wall, put_wall, call_wall_oi, put_wall_oi, pct_adv, regime"
            " FROM eod_snapshots WHERE symbol = ? ORDER BY date DESC LIMIT ?",
            (symbol.upper(), limit),
        ) as cursor:
            rows = await cursor.fetchall()
        keys = ["date", "ts", "spot", "total_gex", "zero_gamma", "call_wall", "put_wall", "call_wall_oi", "put_wall_oi", "pct_adv", "regime"]
        out = [dict(zip(keys, r)) for r in rows]
        out.reverse()
        return out

    async def eod_payload(self, symbol: str, day: str) -> GexResponse | None:
        async with self._conn().execute(
            "SELECT payload FROM eod_snapshots WHERE symbol = ? AND date = ?", (symbol.upper(), day)
        ) as cursor:
            row = await cursor.fetchone()
        return GexResponse.model_validate(json.loads(zlib.decompress(row[0]).decode())) if row else None

    async def eod_symbols(self) -> list[str]:
        async with self._conn().execute("SELECT DISTINCT symbol FROM eod_snapshots ORDER BY symbol") as cursor:
            return [r[0] for r in await cursor.fetchall()]

    # --------------------------------------------------------------- alerts
    async def save_alert(self, ts: datetime, symbol: str, rule: str, message: str, payload: dict) -> int:
        cursor = await self._conn().execute(
            "INSERT INTO alerts(ts, symbol, rule, message, payload) VALUES (?,?,?,?,?)",
            (ts.isoformat(), symbol, rule, message, json.dumps(payload)),
        )
        await self._conn().commit()
        return int(cursor.lastrowid or 0)

    async def recent_alerts(self, symbol: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT id, ts, symbol, rule, message, payload FROM alerts"
        args: list[object] = []
        if symbol:
            sql += " WHERE symbol = ?"
            args.append(symbol.upper())
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        async with self._conn().execute(sql, args) as cursor:
            rows = await cursor.fetchall()
        return [
            {"id": r[0], "ts": r[1], "symbol": r[2], "rule": r[3], "message": r[4], "payload": json.loads(r[5])}
            for r in rows
        ]

    # ------------------------------------------------------- OI reconcile
    async def save_oi_estimate(
        self, symbol: str, day: str, prev_total_oi: float, total_volume: float, opening_ratio: float, max_expiry: str | None
    ) -> None:
        """Upsert today's estimate (keeps an already-reconciled actual value)."""
        await self._conn().execute(
            "INSERT INTO oi_reconcile(symbol, date, est_total_oi, prev_total_oi, actual_total_oi, opening_ratio, total_volume, max_expiry)"
            " VALUES (?,?,?,?,NULL,?,?,?)"
            " ON CONFLICT(symbol, date) DO UPDATE SET est_total_oi = excluded.est_total_oi, prev_total_oi = excluded.prev_total_oi,"
            " opening_ratio = excluded.opening_ratio, total_volume = excluded.total_volume, max_expiry = excluded.max_expiry",
            (symbol.upper(), day, prev_total_oi + opening_ratio * total_volume, prev_total_oi, opening_ratio, total_volume, max_expiry),
        )
        await self._conn().commit()

    async def pending_oi_reconcile(self, symbol: str, before_day: str) -> list[dict]:
        async with self._conn().execute(
            "SELECT date, max_expiry FROM oi_reconcile WHERE symbol = ? AND date < ? AND actual_total_oi IS NULL ORDER BY date DESC LIMIT 5",
            (symbol.upper(), before_day),
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"date": r[0], "max_expiry": r[1]} for r in rows]

    async def reconcile_oi(self, symbol: str, day: str, actual_total_oi: float) -> None:
        await self._conn().execute(
            "UPDATE oi_reconcile SET actual_total_oi = ? WHERE symbol = ? AND date = ?", (actual_total_oi, symbol.upper(), day)
        )
        await self._conn().commit()

    async def oi_reconcile_rows(self, symbol: str, limit: int = 60) -> list[dict]:
        async with self._conn().execute(
            "SELECT date, est_total_oi, prev_total_oi, actual_total_oi, opening_ratio, total_volume, max_expiry FROM oi_reconcile"
            " WHERE symbol = ? ORDER BY date DESC LIMIT ?",
            (symbol.upper(), limit),
        ) as cursor:
            rows = await cursor.fetchall()
        keys = ["date", "est_total_oi", "prev_total_oi", "actual_total_oi", "opening_ratio", "total_volume", "max_expiry"]
        out = [dict(zip(keys, r)) for r in rows]
        out.reverse()
        return out

    # --------------------------------------------------------------- watches
    async def save_watch(self, symbol: str, params: ChainParams) -> None:
        await self._conn().execute(
            "INSERT INTO watches(symbol, max_dte, strike_range_pct, max_contracts) VALUES (?,?,?,?)"
            " ON CONFLICT(symbol) DO UPDATE SET max_dte=excluded.max_dte,"
            " strike_range_pct=excluded.strike_range_pct, max_contracts=excluded.max_contracts",
            (symbol.upper(), params.max_dte, params.strike_range_pct, params.max_contracts),
        )
        await self._conn().commit()

    async def delete_watch(self, symbol: str) -> None:
        await self._conn().execute("DELETE FROM watches WHERE symbol = ?", (symbol.upper(),))
        await self._conn().commit()

    async def list_watches(self) -> list[tuple[str, ChainParams]]:
        async with self._conn().execute(
            "SELECT symbol, max_dte, strike_range_pct, max_contracts FROM watches ORDER BY symbol"
        ) as cursor:
            rows = await cursor.fetchall()
        return [(row[0], ChainParams(int(row[1]), float(row[2]), int(row[3]))) for row in rows]

    # ---------------------------------------------------------- weekly walls
    def _week_rows(self, result: GexResponse) -> list:
        week = [stats for stats in result.by_expiry if stats.dte <= 7]
        if week:
            return week
        return result.by_expiry[:1]

    async def upsert_expiry_days(self, result: GexResponse, finalized: bool = False) -> None:
        """Keep one row per symbol, session date, and expiry. Later refreshes replace today."""
        day = result.ts.astimezone(NY).date().isoformat()
        adv = result.hedge_flow.adv_shares if result.hedge_flow else None
        for stats in self._week_rows(result):
            pct = (stats.shares_per_1pct / adv * 100) if adv and adv > 0 else None
            await self._conn().execute(
                "INSERT INTO expiry_days(symbol, date, expiry, dte, ts, spot, call_wall, call_wall_gex, call_wall_oi,"
                " put_wall, abs_gex_share, total_gex, shares_per_1pct, pct_adv, volume_call_wall, oi_asof, finalized)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(symbol, date, expiry) DO UPDATE SET"
                " dte=excluded.dte, ts=excluded.ts, spot=excluded.spot, call_wall=excluded.call_wall,"
                " call_wall_gex=excluded.call_wall_gex, call_wall_oi=excluded.call_wall_oi, put_wall=excluded.put_wall,"
                " abs_gex_share=excluded.abs_gex_share, total_gex=excluded.total_gex,"
                " shares_per_1pct=excluded.shares_per_1pct, pct_adv=excluded.pct_adv,"
                " volume_call_wall=excluded.volume_call_wall, oi_asof=excluded.oi_asof,"
                " finalized=MAX(expiry_days.finalized, excluded.finalized)",
                (
                    result.symbol, day, stats.expiry, stats.dte, result.ts.isoformat(), result.spot,
                    stats.call_wall, stats.call_wall_gex, stats.call_wall_oi, stats.put_wall,
                    stats.abs_gex_share, stats.total_gex, stats.shares_per_1pct, pct,
                    stats.volume_call_wall, result.oi_asof, 1 if finalized else 0,
                ),
            )
        await self._conn().commit()

    async def finalize_expiry_days(self, symbol: str, day: str) -> None:
        await self._conn().execute(
            "UPDATE expiry_days SET finalized = 1 WHERE symbol = ? AND date = ?",
            (symbol.upper(), day),
        )
        await self._conn().commit()

    async def record_wall_touch(self, result: GexResponse, near_pct: float = 0.02, min_interval_s: float = 900) -> bool:
        """Record spot while it is within ``near_pct`` of the front-week call wall. At most once per interval."""
        day = result.ts.astimezone(NY).date()
        front = front_week_expiry(result.by_expiry, day)
        if front is None or front.call_wall is None or result.spot <= 0:
            return False
        distance = (result.spot - front.call_wall) / result.spot
        if abs(distance) > near_pct:
            return False
        async with self._conn().execute(
            "SELECT ts FROM wall_touches WHERE symbol = ? AND expiry = ? ORDER BY ts DESC LIMIT 1",
            (result.symbol, front.expiry),
        ) as cursor:
            last = await cursor.fetchone()
        if last is not None:
            from datetime import datetime as dt

            prev = dt.fromisoformat(last[0])
            if prev.tzinfo is None:
                prev = prev.replace(tzinfo=result.ts.tzinfo)
            if (result.ts - prev).total_seconds() < min_interval_s:
                return False
        await self._conn().execute(
            "INSERT INTO wall_touches(symbol, expiry, ts, spot, call_wall, distance_pct, volume_call_wall, call_wall_gex)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                result.symbol, front.expiry, result.ts.isoformat(), result.spot, front.call_wall,
                distance * 100, front.volume_call_wall, front.call_wall_gex,
            ),
        )
        await self._conn().commit()
        return True

    async def wall_trend(self, symbol: str, limit_days: int = 15, limit_touches: int = 40) -> tuple[str | None, list[ExpiryDay], list[WallTouch]]:
        symbol = symbol.upper()
        async with self._conn().execute(
            "SELECT date, expiry, dte, ts, spot, call_wall, call_wall_gex, call_wall_oi, put_wall, abs_gex_share,"
            " total_gex, shares_per_1pct, pct_adv, volume_call_wall, oi_asof, finalized"
            " FROM expiry_days WHERE symbol = ? ORDER BY date ASC, expiry ASC",
            (symbol,),
        ) as cursor:
            raw_days = await cursor.fetchall()
        days = [
            ExpiryDay(
                date=row[0], expiry=row[1], dte=row[2], ts=datetime.fromisoformat(row[3]), spot=row[4],
                call_wall=row[5], call_wall_gex=row[6] or 0.0, call_wall_oi=row[7] or 0.0, put_wall=row[8],
                abs_gex_share=row[9] or 0.0, total_gex=row[10] or 0.0, shares_per_1pct=row[11] or 0.0,
                pct_adv=row[12], volume_call_wall=row[13], oi_asof=row[14] or "", finalized=bool(row[15]),
            )
            for row in raw_days
        ]
        if limit_days and days:
            keep_dates = sorted({item.date for item in days})[-limit_days:]
            days = [item for item in days if item.date in keep_dates]
        async with self._conn().execute(
            "SELECT ts, expiry, spot, call_wall, distance_pct, volume_call_wall, call_wall_gex"
            " FROM wall_touches WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
            (symbol, limit_touches),
        ) as cursor:
            raw_touches = await cursor.fetchall()
        touches = [
            WallTouch(
                ts=datetime.fromisoformat(row[0]), expiry=row[1], spot=row[2], call_wall=row[3],
                distance_pct=row[4], volume_call_wall=row[5], call_wall_gex=row[6] or 0.0,
            )
            for row in reversed(raw_touches)
        ]
        front = None
        if days:
            latest = days[-1].date
            same_day = [item for item in days if item.date == latest]
            front = max(same_day, key=lambda item: item.expiry).expiry
        return front, days, touches

    async def prune(self) -> int:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=self.retention_days)
        cursor = await self._conn().execute("DELETE FROM snapshots WHERE ts < ?", (cutoff.isoformat(),))
        await self._conn().commit()
        deleted = cursor.rowcount or 0
        if deleted:
            log.info("pruned %d snapshots older than %d days", deleted, self.retention_days)
        return deleted
