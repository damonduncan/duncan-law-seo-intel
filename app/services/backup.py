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
  <p style="margin:0 0 8px;"><strong>File:</strong> {filename} ({size_kb:.1f} KB)</p>
  <p style="margin:0 0 20px;color:#6b7280;font-size:14px;">
    To restore: download the attachment and run<br>
    <code style="background:#f3f4f6;padding:2px 6px;border-radius:4px;">
      python scripts/restore_backup.py market-pulse-backup-YYYY-MM-DD.json.gz
    </code>
  </p>
  <p style="margin:0;font-size:12px;color:#9ca3af;">
    This backup is sent automatically on the 1st of each month by Market Pulse.
  </p>
</div>
"""

    try:
        import resend as resend_sdk
        resend_sdk.api_key = settings.resend_api_key
        resend_sdk.Emails.send({
            "from":    settings.resend_from_address,
            "to":      ["damonduncan@duncanlawonline.com"],
            "subject": f"Market Pulse Database Backup — {exported_at.strftime('%B %Y')}",
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
