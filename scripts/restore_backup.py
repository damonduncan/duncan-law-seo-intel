#!/usr/bin/env python3
"""Restore a Market Pulse database backup from a .json.gz file.

Usage:
    python scripts/restore_backup.py market-pulse-backup-YYYY-MM-DD.json.gz

Reads DATABASE_URL from the environment (same variable used by the app).
Truncates each table and re-inserts all rows in FK-safe order.

WARNING: This is a full destructive restore — existing data is overwritten.
"""
import gzip
import json
import os
import sys
from datetime import datetime


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/restore_backup.py <backup-file.json.gz>")
        sys.exit(1)

    backup_path = sys.argv[1]
    if not os.path.exists(backup_path):
        print(f"Error: file not found: {backup_path}")
        sys.exit(1)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("Error: DATABASE_URL environment variable is not set.")
        sys.exit(1)

    print(f"Loading backup: {backup_path}")
    with gzip.open(backup_path, "rb") as f:
        backup = json.loads(f.read().decode("utf-8"))

    version = backup.get("version", 0)
    exported_at = backup.get("exported_at", "unknown")
    tables = backup.get("tables", {})
    row_counts = backup.get("row_counts", {})
    total_rows = sum(row_counts.values())

    print(f"Backup version : {version}")
    print(f"Exported at    : {exported_at}")
    print(f"Tables         : {len(tables)}")
    print(f"Total rows     : {total_rows:,}")
    print()

    for tbl, count in row_counts.items():
        print(f"  {tbl:<35} {count:>7,} rows")
    print()

    confirm = input("Type YES to proceed with restore (this will overwrite existing data): ")
    if confirm.strip() != "YES":
        print("Aborted.")
        sys.exit(0)

    # Use SQLAlchemy so we get the same dialect support as the app
    try:
        from sqlalchemy import create_engine, text
    except ImportError:
        print("Error: sqlalchemy is not installed. Run: pip install sqlalchemy psycopg2-binary")
        sys.exit(1)

    engine = create_engine(database_url)

    # Restore in the same FK-safe order used during export
    restore_order = [
        "users",
        "competitors",
        "competitor_attorneys",
        "attorney_aliases",
        "competitor_locations",
        "discovery_cache",
        "job_runs",
        "digest_log",
        "alerts",
        "local_pack_rankings",
        "review_snapshots",
        "review_sentiment",
        "filing_snapshots",
    ]

    with engine.begin() as conn:
        # Disable FK checks for the duration of restore (PostgreSQL)
        conn.execute(text("SET session_replication_role = 'replica'"))

        for tbl in restore_order:
            rows = tables.get(tbl)
            if rows is None:
                print(f"  SKIP  {tbl} (not in backup)")
                continue

            conn.execute(text(f"TRUNCATE TABLE {tbl} CASCADE"))

            if not rows:
                print(f"  OK    {tbl} — 0 rows (empty)")
                continue

            # Build parameterised INSERT from the first row's keys
            cols = list(rows[0].keys())
            col_list = ", ".join(cols)
            val_list = ", ".join(f":{c}" for c in cols)
            insert_sql = text(f"INSERT INTO {tbl} ({col_list}) VALUES ({val_list})")

            # Cast ISO strings back to datetimes where needed
            conn.execute(insert_sql, [_cast_row(r) for r in rows])
            print(f"  OK    {tbl} — {len(rows):,} rows restored")

        conn.execute(text("SET session_replication_role = 'origin'"))

    print()
    print(f"Restore complete. {total_rows:,} rows written.")


def _cast_row(row: dict) -> dict:
    """Leave values as-is; PostgreSQL driver handles ISO strings for timestamp columns."""
    return row


if __name__ == "__main__":
    main()
