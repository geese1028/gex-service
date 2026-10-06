"""SQLite snapshot store for intraday GEX history."""

from __future__ import annotations

import json
import logging
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from .models import GexResponse, HistoryPoint

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

    async def prune(self) -> int:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=self.retention_days)
        cursor = await self._conn().execute("DELETE FROM snapshots WHERE ts < ?", (cutoff.isoformat(),))
        await self._conn().commit()
        deleted = cursor.rowcount or 0
        if deleted:
            log.info("pruned %d snapshots older than %d days", deleted, self.retention_days)
        return deleted
