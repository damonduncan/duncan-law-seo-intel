"""Seed historical DocuSign attorney-client agreement counts into discovery_cache.

Counts were pulled from DocuSign via MCP on 2026-06-11, filtering completed envelopes
with subject "PLEASE SIGN: Bankruptcy Attorney-Client Agreement" by completedDateTime.
DocuSign was adopted for attorney-client agreements in December 2022.

Run once:
    DATABASE_URL="postgresql://postgres:rLoHLAmRsbjtuCOIBmryExOwGXUfZRUM@kodama.proxy.rlwy.net:39353/railway" \
    python scripts/seed_contracts.py
"""

import os
import sys
import json
import uuid
from datetime import datetime, timezone

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: Set DATABASE_URL env var before running.")
    sys.exit(1)

try:
    import sqlalchemy as sa
except ImportError:
    print("ERROR: pip install sqlalchemy psycopg2-binary")
    sys.exit(1)

MONTHS = [
    {"year": 2022, "month": 12, "count": 3},
    {"year": 2023, "month": 1,  "count": 30},
    {"year": 2023, "month": 2,  "count": 34},
    {"year": 2023, "month": 3,  "count": 42},
    {"year": 2023, "month": 4,  "count": 44},
    {"year": 2023, "month": 5,  "count": 45},
    {"year": 2023, "month": 6,  "count": 35},
    {"year": 2023, "month": 7,  "count": 36},
    {"year": 2023, "month": 8,  "count": 35},
    {"year": 2023, "month": 9,  "count": 40},
    {"year": 2023, "month": 10, "count": 45},
    {"year": 2023, "month": 11, "count": 51},
    {"year": 2023, "month": 12, "count": 48},
    {"year": 2024, "month": 1,  "count": 46},
    {"year": 2024, "month": 2,  "count": 41},
    {"year": 2024, "month": 3,  "count": 49},
    {"year": 2024, "month": 4,  "count": 55},
    {"year": 2024, "month": 5,  "count": 55},
    {"year": 2024, "month": 6,  "count": 56},
    {"year": 2024, "month": 7,  "count": 57},
    {"year": 2024, "month": 8,  "count": 53},
    {"year": 2024, "month": 9,  "count": 65},
    {"year": 2024, "month": 10, "count": 65},
    {"year": 2024, "month": 11, "count": 50},
    {"year": 2024, "month": 12, "count": 30},
    {"year": 2025, "month": 1,  "count": 50},
    {"year": 2025, "month": 2,  "count": 61},
    {"year": 2025, "month": 3,  "count": 55},
    {"year": 2025, "month": 4,  "count": 44},
    {"year": 2025, "month": 5,  "count": 40},
    {"year": 2025, "month": 6,  "count": 46},
    {"year": 2025, "month": 7,  "count": 36},
    {"year": 2025, "month": 8,  "count": 40},
    {"year": 2025, "month": 9,  "count": 70},
    {"year": 2025, "month": 10, "count": 78},
    {"year": 2025, "month": 11, "count": 71},
    {"year": 2025, "month": 12, "count": 79},
    {"year": 2026, "month": 1,  "count": 82},
    {"year": 2026, "month": 2,  "count": 79},
    {"year": 2026, "month": 3,  "count": 90},
    {"year": 2026, "month": 4,  "count": 83},
    {"year": 2026, "month": 5,  "count": 76},
]

CACHE_KEY = "docusign_monthly_contracts"

engine = sa.create_engine(DATABASE_URL)
meta   = sa.MetaData()
cache  = sa.Table("discovery_cache", meta, autoload_with=engine)

data = {
    "months": MONTHS,
    "notes":  [
        "DocuSign attorney-client agreements (subject: 'PLEASE SIGN: Bankruptcy Attorney-Client Agreement').",
        "Data available from Dec 2022 (DocuSign adoption date). Seeded 2026-06-11.",
    ],
    "updated_at": datetime.now(timezone.utc).isoformat(),
}

with engine.begin() as conn:
    existing = conn.execute(
        sa.select(cache).where(cache.c.key == CACHE_KEY)
    ).first()

    if existing:
        conn.execute(
            cache.update()
            .where(cache.c.key == CACHE_KEY)
            .values(value=json.dumps(data), updated_at=datetime.now(timezone.utc))
        )
        print(f"Updated existing '{CACHE_KEY}' row.")
    else:
        conn.execute(
            cache.insert().values(
                id=str(uuid.uuid4()),
                key=CACHE_KEY,
                value=json.dumps(data),
                updated_at=datetime.now(timezone.utc),
            )
        )
        print(f"Inserted new '{CACHE_KEY}' row.")

total = sum(m["count"] for m in MONTHS)
print(f"Seeded {len(MONTHS)} months, {total} total contracts (Dec 2022 – May 2026).")
