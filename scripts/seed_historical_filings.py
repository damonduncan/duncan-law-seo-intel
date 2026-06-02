#!/usr/bin/env python3
"""One-time seed: load Duncan Law historical filing data into DiscoveryCache.

Source: Duncan Law Annual Bankruptcy Filings.xlsx
Run once (or re-run to update):
    DATABASE_URL="..." python scripts/seed_historical_filings.py
"""
import os
import sys
from datetime import datetime, timezone

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("Error: DATABASE_URL environment variable is not set.")
    sys.exit(1)

# ── Historical data extracted from Excel ──────────────────────────────────────
# Monthly filings: 12 values Jan–Dec. None = month not yet completed.
MONTHLY = {
    2009: [None, None, None, None, None, None, None, None, None, 1, 1, 5],  # Oct–Dec only (partial year)
    2010: [10, 9, 10, 6, 5, 12, 8, 12, 17, 13, 10, 7],
    2011: [13, 5, 16, 4, 18,  5, 20, 21, 14, 13, 17, 14],
    2012: [ 8, 18, 23, 21, 13,  9, 15,  8, 13,  7, 11, 17],
    2013: [14,  7, 12, 10,  8, 15, 16, 17, 12, 12,  7,  6],
    2014: [11,  5, 14, 11, 13, 14, 14,  8, 17, 20, 18, 20],
    2015: [ 9, 16, 10, 14, 14, 12, 17,  9, 23, 12, 19, 13],
    2016: [ 9, 15, 21, 14, 13, 15, 10, 18, 17, 12, 18, 17],
    2017: [10, 15, 11, 21,  8, 18, 21, 17, 16, 22, 22, 12],
    2018: [25, 23, 17, 23, 24, 12, 14, 26, 16, 18, 15, 22],
    2019: [11, 15, 14, 16, 20, 16, 10, 14, 16, 17, 14, 21],
    2020: [11, 15, 20, 18, 11, 10, 13,  3, 12,  7, 12, 16],
    2021: [16,  6,  7,  6, 11,  4,  4,  8,  9, 10, 15, 22],
    2022: [13,  7, 14, 16, 14, 13, 13, 18, 12, 21, 13, 25],
    2023: [21, 10, 27, 18, 22, 26, 17, 24, 22, 13, 13, 27],
    2024: [27, 27, 38, 23, 29, 29, 12, 28, 29, 20, 24, 29],
    2025: [18, 35, 30, 31, 32, 18, 15, 18, 29, 46, 26, 38],
    2026: [40, 32, 44, 40, 42, None, None, None, None, None, None, None],  # YTD through May
}

# MDNC/WDNC split available from 2024 onward
DISTRICT_SPLIT = {
    2024: {"mdnc": 251, "wdnc": 64},
    2025: {"mdnc": 231, "wdnc": 105},
    2026: {"mdnc": 143, "wdnc": 55},  # YTD through May
}

NOTES = {
    2009: "Partial year — firm opened Oct 2009",
    2026: "YTD through May 2026",
}

# ── Build annual summary ───────────────────────────────────────────────────────
annual = []
for year in sorted(MONTHLY):
    monthly = MONTHLY[year]
    total = sum(v for v in monthly if v is not None)
    entry = {
        "year":    year,
        "total":   total,
        "monthly": monthly,
        "ytd":     any(v is None for v in monthly),
        "partial": year == 2009,
    }
    if year in DISTRICT_SPLIT:
        entry.update(DISTRICT_SPLIT[year])
    if year in NOTES:
        entry["note"] = NOTES[year]
    annual.append(entry)

payload = {
    "annual":     annual,
    "source":     "Duncan Law Annual Bankruptcy Filings.xlsx",
    "seeded_at":  datetime.now(timezone.utc).isoformat(),
}

# ── Write to DiscoveryCache ───────────────────────────────────────────────────
try:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
except ImportError:
    print("Error: sqlalchemy not installed. Run: pip install sqlalchemy psycopg2-binary")
    sys.exit(1)

engine = create_engine(DATABASE_URL)

import json
with engine.begin() as conn:
    existing = conn.execute(
        text("SELECT id FROM discovery_cache WHERE key = 'duncan_law_filing_history'")
    ).fetchone()
    if existing:
        conn.execute(
            text("UPDATE discovery_cache SET value = :v, updated_at = :t WHERE key = 'duncan_law_filing_history'"),
            {"v": json.dumps(payload), "t": datetime.now(timezone.utc)},
        )
        print(f"Updated duncan_law_filing_history ({len(annual)} years)")
    else:
        import uuid
        conn.execute(
            text("INSERT INTO discovery_cache (id, key, value, updated_at) VALUES (:id, :k, :v, :t)"),
            {
                "id": str(uuid.uuid4()),
                "k":  "duncan_law_filing_history",
                "v":  json.dumps(payload),
                "t":  datetime.now(timezone.utc),
            },
        )
        print(f"Inserted duncan_law_filing_history ({len(annual)} years)")

print("Done.")
