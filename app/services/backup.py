"""Monthly database backup — exports all tables as gzipped JSON, emails via Resend.

Runs on the 1st of each month alongside the PACER collection. The attachment
is a .json.gz file that can be fully restored using scripts/restore_backup.py.

Table export order respects foreign-key dependencies so the restore script can
INSERT rows in the correct sequence.
"""
import base64
import gzip
import io
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings

logger = logging.getLogger(__name__)

# Export in FK-safe order: parents before children
BACKUP_TABLES = [
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


def run_backup(db: Session) -> bool:
    """Export all tables to gzipped JSON and email to admin. Returns True on success."""
    if not settings.resend_api_key:
        logger.warning("Backup: Resend not configured — skipping")
        return False

    exported_at = datetime.now(timezone.utc)
    backup = {
        "exported_at": exported_at.isoformat(),
        "version": 1,
        "tables": {},
        "row_counts": {},
    }

    for table in BACKUP_TABLES:
        try:
            result = db.execute(text(f"SELECT * FROM {table}"))
            cols = list(result.keys())
            rows = [dict(zip(cols, row)) for row in result.fetchall()]
            backup["tables"][table] = _serialize(rows)
            backup["row_counts"][table] = len(rows)
            logger.info(f"Backup: {table} — {len(rows):,} rows")
        except Exception as e:
            logger.warning(f"Backup: could not export {table}: {e}")

    total_rows = sum(backup["row_counts"].values())

    # Compress
    raw = json.dumps(backup, indent=None, separators=(",", ":")).encode("utf-8")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9) as gz:
        gz.write(raw)
    compressed = buf.getvalue()
    size_kb = len(compressed) / 1024
    logger.info(f"Backup: {total_rows:,} rows → {size_kb:.1f} KB compressed")

    filename = f"market-pulse-backup-{exported_at.strftime('%Y-%m-%d')}.json.gz"
    date_str  = exported_at.strftime("%B %d, %Y at %H:%M UTC")
    table_summary = "".join(
        f"<tr><td style='padding:3px 12px 3px 0;color:#374151;'>{t}</td>"
        f"<td style='padding:3px 0;color:#6b7280;text-align:right;'>{backup['row_counts'].get(t, 0):,}</td></tr>"
        for t in BACKUP_TABLES if t in backup["row_counts"]
    )

    html = f"""
<div style="font-family:sans-serif;max-width:560px;color:#111827;">
  <h2 style="margin:0 0 4px;">Market Pulse Monthly Backup</h2>
  <p style="margin:0 0 20px;color:#6b7280;">Automated database export — {date_str}</p>
  <table style="border-collapse:collapse;margin-bottom:20px;width:100%;">
    <tr style="border-bottom:2px solid #e5e7eb;">
      <th style="text-align:left;padding:4px 12px 8px 0;font-size:12px;color:#9ca3af;text-transform:uppercase;">Table</th>
      <th style="text-align:right;padding:4px 0 8px;font-size:12px;color:#9ca3af;text-transform:uppercase;">Rows</th>
    </tr>
    {table_summary}
    <tr style="border-top:2px solid #e5e7eb;">
      <td style="padding:8px 12px 4px 0;font-weight:700;">Total</td>
      <td style="padding:8px 0 4px;font-weight:700;text-align:right;">{total_rows:,}</td>
    </tr>
  </table>
  <p style="margin:0 0 20px;"><strong>Attachment:</strong> {filename} ({size_kb:.1f} KB)</p>

  <hr style="border:none;border-top:1px solid #e5e7eb;margin:0 0 20px;">

  <h3 style="margin:0 0 12px;font-size:15px;color:#111827;">How to restore this backup</h3>
  <p style="margin:0 0 12px;color:#374151;font-size:14px;">
    These instructions are intentionally detailed so anyone — not just the original developer —
    can restore the database from scratch using only this email.
  </p>

  <p style="margin:0 0 6px;font-weight:600;font-size:14px;">Step 1 — Download the attachment</p>
  <p style="margin:0 0 16px;color:#374151;font-size:14px;">
    Save the <code style="background:#f3f4f6;padding:1px 5px;border-radius:3px;">{filename}</code>
    file from this email to your computer. Remember where you put it (e.g. your Downloads folder).
  </p>

  <p style="margin:0 0 6px;font-weight:600;font-size:14px;">Step 2 — Get the source code</p>
  <p style="margin:0 0 16px;color:#374151;font-size:14px;">
    The restore script lives in the GitHub repository. Clone it if you don't already have it:<br>
    <code style="background:#f3f4f6;padding:2px 6px;border-radius:4px;display:inline-block;margin-top:4px;">
      git clone https://github.com/damonduncan/duncan-law-seo-intel.git<br>
      cd duncan-law-seo-intel
    </code>
  </p>

  <p style="margin:0 0 6px;font-weight:600;font-size:14px;">Step 3 — Install Python dependencies</p>
  <p style="margin:0 0 16px;color:#374151;font-size:14px;">
    You need Python 3.10+ and the packages the app uses. From inside the repository folder:<br>
    <code style="background:#f3f4f6;padding:2px 6px;border-radius:4px;display:inline-block;margin-top:4px;">
      pip install sqlalchemy psycopg2-binary
    </code><br>
    (If you're restoring to the live Railway database you only need these two packages —
    you do <em>not</em> need to install the full requirements.txt.)
  </p>

  <p style="margin:0 0 6px;font-weight:600;font-size:14px;">Step 4 — Get the database connection string</p>
  <p style="margin:0 0 6px;color:#374151;font-size:14px;">
    The connection string (called <strong>DATABASE_URL</strong>) tells the script where the
    database lives and how to authenticate. There are two ways to find it:
  </p>
  <ul style="margin:0 0 16px;padding-left:20px;color:#374151;font-size:14px;line-height:1.7;">
    <li><strong>Railway dashboard (preferred):</strong> Log in at
      <a href="https://railway.app" style="color:#2563EB;">railway.app</a>,
      open the <em>duncan-law-seo-intel</em> project, click the PostgreSQL service,
      then click <em>Variables</em>. Copy the value of <code style="background:#f3f4f6;padding:1px 4px;border-radius:3px;">DATABASE_URL</code>.
    </li>
    <li><strong>Already deployed app:</strong> In the same Railway project, open the web
      service → Variables → copy <code style="background:#f3f4f6;padding:1px 4px;border-radius:3px;">DATABASE_URL</code> from there.
    </li>
  </ul>

  <p style="margin:0 0 6px;font-weight:600;font-size:14px;">Step 5 — Run the restore script</p>
  <p style="margin:0 0 6px;color:#374151;font-size:14px;">
    Open a terminal in the repository folder and run the command below, replacing
    <code style="background:#f3f4f6;padding:1px 4px;border-radius:3px;">YOUR_DATABASE_URL</code>
    with the value you copied in Step 4 and
    <code style="background:#f3f4f6;padding:1px 4px;border-radius:3px;">/path/to/{filename}</code>
    with the full path to the file you downloaded in Step 1:
  </p>
  <p style="margin:0 0 16px;">
    <code style="background:#f3f4f6;padding:6px 10px;border-radius:4px;display:block;font-size:13px;line-height:1.6;">
      DATABASE_URL="YOUR_DATABASE_URL" python scripts/restore_backup.py /path/to/{filename}
    </code>
  </p>
  <p style="margin:0 0 16px;color:#374151;font-size:14px;">
    The script will show you how many rows are in each table and ask you to type
    <strong>YES</strong> before it touches anything. If you type anything else it exits
    without making any changes.
  </p>

  <p style="margin:0 0 6px;font-weight:600;font-size:14px;">Step 6 — Verify the restore</p>
  <p style="margin:0 0 20px;color:#374151;font-size:14px;">
    After the script finishes, open the Market Pulse web app. You should see all your
    competitor data, PACER filings, rankings, and reviews exactly as they were at the time
    this backup was taken ({date_str}).
    If anything looks wrong, the backup file itself is untouched and you can run the restore
    script again.
  </p>

  <div style="background:#fef3c7;border:1px solid #fcd34d;border-radius:6px;padding:12px 16px;margin-bottom:20px;">
    <p style="margin:0;font-size:13px;color:#92400e;">
      <strong>Important:</strong> The restore script <em>overwrites</em> all existing data.
      If the live database has newer data you want to keep, export it first (trigger a manual
      backup from the admin panel) before running a restore from an older file.
    </p>
  </div>

  <p style="margin:0;font-size:12px;color:#9ca3af;">
    This backup is sent automatically on the 1st of each month by Market Pulse.
    Questions? Contact the developer or reply to this email.
  </p>
</div>
"""

    try:
        import resend as resend_sdk
        resend_sdk.api_key = settings.resend_api_key
        resend_sdk.Emails.send({
            "from":    settings.resend_from_address,
            "to":      ["damonduncan@duncanlawonline.com"],
            "subject": f"Backup for Market Pulse — No action needed ({exported_at.strftime('%B %Y')})",
            "html":    html,
            "attachments": [{
                "filename": filename,
                "content":  base64.b64encode(compressed).decode("ascii"),
            }],
        })
        logger.info(f"Backup email sent: {filename} ({size_kb:.1f} KB, {total_rows:,} rows)")
        return True
    except Exception as e:
        logger.error(f"Backup email failed: {e}", exc_info=True)
        return False


def _serialize(rows: list) -> list:
    """Convert all values to JSON-safe types."""
    out = []
    for row in rows:
        out.append({
            k: v.isoformat() if hasattr(v, "isoformat") else v
            for k, v in row.items()
        })
    return out
