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

    async def prune(self) -> int:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=self.retention_days)
        cursor = await self._conn().execute("DELETE FROM snapshots WHERE ts < ?", (cutoff.isoformat(),))
        await self._conn().commit()
        deleted = cursor.rowcount or 0
        if deleted:
            log.info("pruned %d snapshots older than %d days", deleted, self.retention_days)
        return deleted
