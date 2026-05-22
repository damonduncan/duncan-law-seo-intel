"""Weekly email digest — Phase 5.

Sends a Monday morning intelligence summary to damonduncan@duncanlawonline.com
via Resend covering:
  • Duncan Law's current Google 3-pack positions across all 6 markets
  • Review counts by market with action flags for thin listings
  • All unacknowledged alerts from the past week
  • Quick links back to the full dashboard
"""
import logging
from datetime import date, timedelta, datetime, timezone

from sqlalchemy import cast, Date
from sqlalchemy.orm import Session

from app.config import settings
from app.models.alerts import Alert, DigestLog
from app.models.base import new_uuid
from app.models.competitor import Competitor
from app.models.rankings import LocalPackRanking
from app.models.reviews import ReviewSnapshot

logger = logging.getLogger(__name__)

MARKET_DISPLAY = {
    "greensboro":    "Greensboro",
    "winston_salem": "Winston-Salem",
    "high_point":    "High Point",
    "charlotte":     "Charlotte",
    "salisbury":     "Salisbury",
    "asheville":     "Asheville",
}
MARKET_ORDER = list(MARKET_DISPLAY.keys())


def build_and_send_digest(db: Session) -> None:
    """Build the weekly digest and send via Resend. Logs result to digest_log."""
    if not settings.resend_api_key:
        raise ValueError("RESEND_API_KEY not configured — add it to Railway environment variables")

    today    = date.today()
    week_str = today.strftime("%B %d, %Y")
    subject  = f"Duncan Law SEO Intelligence — Week of {week_str}"

    ctx  = _gather_data(db)
    html = _build_html(ctx, week_str)

    log = DigestLog(
        id=new_uuid(),
        sent_at=datetime.now(timezone.utc),
        recipient=settings.digest_recipient,
        subject=subject,
        status="failed",
    )
    db.add(log)

    try:
        import resend as resend_sdk
        resend_sdk.api_key = settings.resend_api_key
        result = resend_sdk.Emails.send({
            "from":    settings.resend_from_address,
            "to":      settings.digest_recipient,
            "subject": subject,
            "html":    html,
        })
        log.status            = "sent"
        log.resend_message_id = result.get("id") if isinstance(result, dict) else str(result)
        logger.info(f"Weekly digest sent to {settings.digest_recipient}")
    except Exception as e:
        log.error_detail = str(e)
        logger.error(f"Digest send failed: {e}")
    finally:
        db.commit()


# ── Data gathering ────────────────────────────────────────────────────────────

def _gather_data(db: Session) -> dict:
    own_firm = db.query(Competitor).filter(Competitor.is_own_firm == True).first()
    own_id   = own_firm.id if own_firm else None

    # Latest own-firm rankings — most recent date available
    latest_date = None
    if own_id:
        row = (
            db.query(LocalPackRanking.scraped_at)
            .filter(LocalPackRanking.competitor_id == own_id)
            .order_by(LocalPackRanking.scraped_at.desc())
            .first()
        )
        if row:
            latest_date = row[0].date()

    rankings_by_market: dict = {}
    if own_id and latest_date:
        rows = (
            db.query(LocalPackRanking)
            .filter(
                LocalPackRanking.competitor_id == own_id,
                LocalPackRanking.is_own_firm == True,
                cast(LocalPackRanking.scraped_at, Date) == latest_date,
            )
            .all()
        )
        for r in rows:
            m = rankings_by_market.setdefault(
                r.market, {"in_pack": 0, "total": 0, "gaps": [], "positions": []}
            )
            m["total"] += 1
            if r.in_pack:
                m["in_pack"] += 1
                if r.rank_position:
                    m["positions"].append(r.rank_position)
            else:
                m["gaps"].append(r.keyword)

    # Latest own-firm review snapshot per market
    reviews_by_market: dict = {}
    if own_id:
        snaps = (
            db.query(ReviewSnapshot)
            .filter(
                ReviewSnapshot.competitor_id == own_id,
                ReviewSnapshot.source == "google",
                ReviewSnapshot.market != None,
            )
            .order_by(ReviewSnapshot.snapped_at.desc())
            .all()
        )
        for s in snaps:
            if s.market not in reviews_by_market:
                reviews_by_market[s.market] = {
                    "rating":       float(s.rating) if s.rating else None,
                    "review_count": s.review_count or 0,
                }

    # Unacknowledged alerts
    open_alerts = (
        db.query(Alert)
        .filter(Alert.acknowledged_at == None)
        .order_by(Alert.triggered_at.desc())
        .limit(10)
        .all()
    )

    priority_action = _generate_priority(rankings_by_market, reviews_by_market)

    return {
        "rankings_by_market":  rankings_by_market,
        "reviews_by_market":   reviews_by_market,
        "open_alerts":         open_alerts,
        "rankings_as_of":      latest_date,
        "base_url":            settings.app_base_url,
        "priority_action":     priority_action,
    }


def _generate_priority(rankings: dict, reviews: dict) -> dict:
    """Synthesise rankings and review data into a single priority recommendation."""
    # Critical: listings with fewer than 5 reviews — directly limits neutral search rankings
    thin = [
        (MARKET_DISPLAY.get(m, m), d["review_count"])
        for m, d in reviews.items()
        if d["review_count"] < 5
    ]
    if thin:
        markets = ", ".join(f"{name} ({count})" for name, count in thin)
        return {
            "level": "high",
            "headline": "Review building — immediate priority",
            "body": (
                f"{markets} {'review' if sum(c for _, c in thin) == 1 else 'reviews'} on "
                f"{'that listing' if len(thin) == 1 else 'those listings'}. "
                "Competitors in those markets have 37–60 reviews. Ask every satisfied client "
                "from those offices to leave a Google review this week."
            ),
        }

    # Pack gaps — not in 3-pack for a keyword
    gaps = [
        (MARKET_DISPLAY.get(m, m), d["gaps"][0])
        for m, d in rankings.items()
        if d["gaps"]
    ]
    if gaps:
        market_name, kw = gaps[0]
        return {
            "level": "medium",
            "headline": f"Pack gap in {market_name}",
            "body": (
                f'Duncan Law is not in the 3-pack for "{kw}". '
                "Compare your GBP listing categories and review count against the two firms "
                "holding the top spots — review count is the most likely lever."
            ),
        }

    # Review count below 20 — yellow flag
    low = [
        (MARKET_DISPLAY.get(m, m), d["review_count"])
        for m, d in reviews.items()
        if d["review_count"] < 20
    ]
    if low:
        markets = ", ".join(f"{name} ({count})" for name, count in low)
        return {
            "level": "medium",
            "headline": "Build review volume in smaller markets",
            "body": (
                f"{markets}. Aim for 30+ in each market to strengthen ranking stability."
            ),
        }

    return {
        "level": "good",
        "headline": "Strong week across all markets",
        "body": "Duncan Law is in the 3-pack for all tracked keywords. Maintain review request cadence to protect your positions.",
    }


# ── HTML builder ──────────────────────────────────────────────────────────────

def _build_html(ctx: dict, week_str: str) -> str:
    base_url = ctx["base_url"].rstrip("/")
    sections = []

    # ── Priority card ─────────────────────────────────────────────────────────
    pa = ctx.get("priority_action", {})
    level_colors = {
        "high":   ("#fee2e2", "#991b1b", "#dc2626"),
        "medium": ("#fef3c7", "#92400e", "#d97706"),
        "good":   ("#d1fae5", "#065f46", "#059669"),
    }
    bg, fg, accent = level_colors.get(pa.get("level", "good"), level_colors["good"])
    sections.append(f"""<tr><td style="padding:20px 24px;border-bottom:1px solid #e5e7eb;background:{bg};">
      <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:{accent};margin-bottom:6px;">
        This Week's Priority
      </div>
      <div style="font-size:14px;font-weight:600;color:{fg};margin-bottom:6px;">{pa.get("headline", "")}</div>
      <div style="font-size:13px;color:{fg};opacity:.85;line-height:1.6;">{pa.get("body", "")}</div>
    </td></tr>""")

    # ── Rankings section ──────────────────────────────────────────────────────
    rank_rows = ""
    as_of = ctx["rankings_as_of"]
    for market in MARKET_ORDER:
        label = MARKET_DISPLAY.get(market, market)
        data  = ctx["rankings_by_market"].get(market)
        if not data:
            rank_rows += _tr(label, "—", "—")
            continue
        in_pack   = data["in_pack"]
        total     = data["total"]
        gaps      = data["gaps"]
        positions = sorted(data.get("positions", []))

        # Position display e.g. "#1 #1 #3"
        pos_str = " ".join(f"#{p}" for p in positions) if positions else "—"

        if in_pack == total and total > 0:
            badge = _badge("green", f"{in_pack}/{total} in pack")
        elif in_pack > 0:
            badge = _badge("yellow", f"{in_pack}/{total} in pack")
        else:
            badge = _badge("red", "Not in pack")

        gap_text = ""
        if gaps:
            short_kw = gaps[0].replace(" Greensboro", "").replace(" Winston-Salem", "") \
                               .replace(" High Point", "").replace(" Charlotte", "") \
                               .replace(" Salisbury", "").replace(" Asheville", "")
            gap_label = f"{len(gaps)} gap{'s' if len(gaps) > 1 else ''}: {short_kw}"
            gap_text = f'<div style="font-size:11px;color:#dc2626;margin-top:2px;">{gap_label}</div>'
        rank_rows += _tr(label, pos_str, badge, gap_text)

    sections.append(_section(
        f'3-Pack Positions' + (f' — as of {as_of.strftime("%b %d")}' if as_of else ''),
        f'''<table width="100%" cellpadding="8" cellspacing="0" style="border-collapse:collapse;">
          <tr>
            <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Market</th>
            <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Positions</th>
            <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Status</th>
            <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Note</th>
          </tr>
          {rank_rows}
        </table>
        <p style="margin-top:12px;font-size:12px;color:#6b7280;">
          <a href="{base_url}/rankings" style="color:#3b82f6;">View full rankings →</a>
        </p>''',
    ))

    # ── Reviews section ───────────────────────────────────────────────────────
    review_rows = ""
    for market in MARKET_ORDER:
        label = MARKET_DISPLAY.get(market, market)
        data  = ctx["reviews_by_market"].get(market)
        if not data:
            review_rows += _tr(label, "—", "", "")
            continue
        count  = data["review_count"]
        rating = data["rating"]
        stars  = f"{rating:.1f} ★" if rating else "—"
        if count < 5:
            badge = _badge("red", f"{count} reviews")
            note  = "Priority: review building needed"
        elif count < 20:
            badge = _badge("yellow", f"{count} reviews")
            note  = "Build toward 30+"
        else:
            badge = _badge("green", f"{count} reviews")
            note  = ""
        review_rows += _tr(label, f"{stars}", badge, note)

    sections.append(_section(
        "Google Review Counts by Market",
        f'''<table width="100%" cellpadding="8" cellspacing="0" style="border-collapse:collapse;">
          <tr>
            <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Market</th>
            <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Rating</th>
            <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Reviews</th>
            <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Action</th>
          </tr>
          {review_rows}
        </table>
        <p style="margin-top:12px;font-size:12px;color:#6b7280;">
          <a href="{base_url}/reviews" style="color:#3b82f6;">View full review data →</a>
        </p>''',
    ))

    # ── Alerts section ────────────────────────────────────────────────────────
    if ctx["open_alerts"]:
        alert_items = ""
        type_labels = {
            "pack_drop":             "Pack drop",
            "competitor_pack_entry": "Competitor entered pack",
            "review_gap":            "Review gap",
            "pacer_volume_spike":    "PACER volume spike",
        }
        for a in ctx["open_alerts"]:
            label = type_labels.get(a.alert_type, a.alert_type)
            msg   = a.detail.get("message", "") if a.detail else ""
            sev_color = "#dc2626" if a.severity == "immediate" else "#d97706"
            alert_items += f'''
              <div style="border-left:3px solid {sev_color};background:#fafafa;padding:10px 12px;margin-bottom:8px;border-radius:0 4px 4px 0;">
                <div style="font-size:12px;font-weight:600;color:{sev_color};">{label}</div>
                <div style="font-size:12px;color:#374151;margin-top:2px;">{msg[:140]}</div>
              </div>'''
        sections.append(_section(
            f"{len(ctx['open_alerts'])} Open Alert{'s' if len(ctx['open_alerts']) != 1 else ''}",
            f'''{alert_items}
            <p style="margin-top:12px;font-size:12px;color:#6b7280;">
              <a href="{base_url}/alerts" style="color:#3b82f6;">View all alerts →</a>
            </p>''',
        ))
    else:
        sections.append(_section(
            "Alerts",
            '<p style="font-size:13px;color:#059669;">✓ No open alerts — all clear</p>',
        ))

    body = "\n".join(sections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Duncan Law SEO Intelligence</title>
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 0;">
    <tr><td>
      <table width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1);">
        <!-- Header -->
        <tr><td style="background:#0f1117;padding:24px;">
          <div style="font-size:18px;font-weight:700;color:#e2e8f0;">Duncan Law SEO Intelligence</div>
          <div style="font-size:13px;color:#8892a4;margin-top:4px;">Week of {week_str}</div>
        </td></tr>
        <!-- Body -->
        {body}
        <!-- Footer -->
        <tr><td style="background:#f9fafb;padding:16px 24px;border-top:1px solid #e5e7eb;">
          <p style="margin:0;font-size:12px;color:#9ca3af;">
            <a href="{base_url}/dashboard" style="color:#3b82f6;">Open full dashboard</a>
            &nbsp;·&nbsp; Sent every Monday at 7 AM ET
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _section(title: str, content: str) -> str:
    return f"""<tr><td style="padding:20px 24px;border-bottom:1px solid #e5e7eb;">
      <div style="font-size:14px;font-weight:600;color:#111827;margin-bottom:12px;">{title}</div>
      {content}
    </td></tr>"""


def _badge(color: str, text: str) -> str:
    colors = {
        "green":  ("background:#d1fae5", "color:#065f46"),
        "yellow": ("background:#fef3c7", "color:#92400e"),
        "red":    ("background:#fee2e2", "color:#991b1b"),
    }
    bg, fg = colors.get(color, ("background:#f3f4f6", "color:#374151"))
    return (
        f'<span style="{bg};{fg};padding:2px 8px;border-radius:4px;'
        f'font-size:11px;font-weight:600;">{text}</span>'
    )


def _tr(*cells) -> str:
    tds = "".join(
        f'<td style="padding:8px;font-size:13px;border-bottom:1px solid #f3f4f6;vertical-align:top;">{c}</td>'
        for c in cells if c != ""
    )
    return f"<tr>{tds}</tr>"
