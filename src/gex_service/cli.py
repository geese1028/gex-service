"""``gex-export``: dump archived EOD feature rows to CSV without the service running.

    gex-export --db data/gex.sqlite --symbols SPY,QQQ --days 250 > features.csv
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import zlib
from pathlib import Path

from .analytics import feature_row, features_csv
from .models import GexResponse


def export_rows(db: Path, symbols: list[str] | None, days: int) -> list[dict]:
    conn = sqlite3.connect(str(db))
    try:
        if not symbols:
            symbols = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM eod_snapshots ORDER BY symbol")]
        rows: list[dict] = []
        for sym in symbols:
            cur = conn.execute(
                "SELECT date, payload FROM eod_snapshots WHERE symbol = ? ORDER BY date DESC LIMIT ?", (sym.upper(), days)
            )
            for day, payload in reversed(cur.fetchall()):
                res = GexResponse.model_validate(json.loads(zlib.decompress(payload).decode()))
                rows.append(feature_row(res, day))
        return rows
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gex-export", description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="data/gex.sqlite", help="path to the service SQLite file")
    parser.add_argument("--symbols", default="", help="comma separated; default all archived symbols")
    parser.add_argument("--days", type=int, default=250)
    parser.add_argument("--format", choices=("csv", "json"), default="csv")
    parser.add_argument("--out", default="-", help="output file, '-' for stdout")
    args = parser.parse_args(argv)
    db = Path(args.db)
    if not db.exists():
        print(f"database not found: {db}", file=sys.stderr)
        return 2
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or None
    rows = export_rows(db, symbols, args.days)
    text = features_csv(rows) if args.format == "csv" else json.dumps(rows, indent=2, default=str)
    if args.out == "-":
        sys.stdout.write(text)
    else:
        Path(args.out).write_text(text)
    print(f"{len(rows)} rows", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
