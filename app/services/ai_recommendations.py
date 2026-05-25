"""Weekly AI-generated action recommendations via Claude API.

Called once during the Monday digest build. Returns a list of 4-6 prioritized,
specific action items grounded in that week's actual performance data.
Returns [] on any failure so the digest still sends.
"""
import json
import logging

logger = logging.getLogger(__name__)

_MARKET_DISPLAY = {
    "greensboro":    "Greensboro",
    "winston_salem": "Winston-Salem",
    "high_point":    "High Point",
    "charlotte":     "Charlotte",
    "salisbury":     "Salisbury",
    "asheville":     "Asheville",
}


def _build_prompt(ctx: dict) -> str:
    lines = [
        "You are a local SEO advisor for Duncan Law LLP, a bankruptcy law firm in North Carolina.",
        "Offices: Greensboro, Winston-Salem, High Point, Charlotte, Salisbury, and Asheville.",
        "Tracked federal court districts: MDNC (Middle NC) and WDNC (Western NC).",
        "",
    ]

    # Rankings
    lines.append("## Google 3-Pack Rankings")
    for market, label in _MARKET_DISPLAY.items():
        data = ctx.get("rankings_by_market", {}).get(market)
        if data:
            status = f"{data['in_pack']}/{data['total']} keywords in pack"
            gaps = data.get("gaps", [])
            gap_str = f", missing keywords: {', '.join(gaps[:3])}" if gaps else ""
            lines.append(f"  {label}: {status}{gap_str}")
        else:
            lines.append(f"  {label}: no ranking data")

    # Reviews vs competitors
    lines.append("")
    lines.append("## Google Reviews — Duncan Law vs. Top Competitor per Market")
    reviews = ctx.get("reviews_by_market", {})
    own_deltas = ctx.get("own_review_deltas", {})
    velocity_map = {v["display"]: v for v in ctx.get("market_velocity", [])}
    for market, label in _MARKET_DISPLAY.items():
        data = reviews.get(market)
        if not data:
            lines.append(f"  {label}: no review data")
            continue
        count = data.get("review_count", 0)
        delta = own_deltas.get(market, 0)
        vel = velocity_map.get(label)
        rival_str = ""
        if vel:
            rd = vel["rival_delta"]
            rival_str = (
                f" | top rival: {vel['rival_name']} "
                f"({vel['rival_count']} reviews, +{rd}/wk)"
            )
        lines.append(f"  {label}: Duncan Law {count} reviews (+{delta}/wk){rival_str}")

    # Pack entries this week
    pack_entries = ctx.get("pack_entries_by_market", {})
    if pack_entries:
        lines.append("")
        lines.append("## Competitors That Entered Our 3-Pack This Week")
        for market, entries in pack_entries.items():
            label = _MARKET_DISPLAY.get(market, market.replace("_", " ").title())
            for e in entries[:4]:
                pos = f"#{e['position']}" if e.get("position") else "unknown position"
                lines.append(
                    f"  {label}: {e['competitor']} entered at {pos} "
                    f"for keyword '{e['keyword']}'"
                )

    # Open alerts (non-pack-entry)
    open_alerts = ctx.get("open_alerts", [])
    if open_alerts:
        lines.append("")
        lines.append("## Open Alerts")
        for a in open_alerts[:6]:
            msg = ""
            if hasattr(a, "detail") and a.detail:
                msg = a.detail.get("message", "")
            if not msg and hasattr(a, "alert_type"):
                msg = a.alert_type
            lines.append(f"  - {str(msg)[:120]}")

    # PACER standings
    pacer = ctx.get("pacer_standings", {})
    if any(pacer.values()):
        lines.append("")
        lines.append("## PACER Bankruptcy Filing Standings (most recent month)")
        for dist, rows in pacer.items():
            if not rows:
                continue
            top = rows[0]
            own_row = next((r for r in rows if r.get("is_own")), None)
            own_str = f"Duncan Law: {own_row['count']} cases" if own_row else "Duncan Law: not in top 6"
            lines.append(
                f"  {dist}: leader {top['name']} ({top['count']} cases). {own_str}."
            )

    lines += [
        "",
        "---",
        "Based on the data above, generate exactly 5 prioritized action items for this week.",
        "Rules:",
        "- Be specific: name actual competitors, actual review counts, actual keywords.",
        "- Every action must be something a lawyer or their staff can do this week.",
        "- Rank by expected impact on Google 3-pack visibility.",
        "- Do not repeat the same advice for multiple markets — consolidate where possible.",
        "",
        "Return ONLY a valid JSON array. No markdown fences, no explanation, no preamble.",
        'Schema: [{"priority":1,"category":"reviews|rankings|gbp|pacer","market":"Market Name or All Markets","headline":"Max 8 words","action":"Specific step, max 40 words","why":"Data point, max 25 words","impact":"high|medium|low"}]',
    ]

    return "\n".join(lines)


def generate_recommendations(ctx: dict) -> list:
    """Return list of recommendation dicts. Always safe to call — returns [] on failure."""
    try:
        from app.config import settings
        if not settings.anthropic_api_key:
            logger.info("ANTHROPIC_API_KEY not configured — skipping AI recommendations")
            return []

        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        prompt = _build_prompt(ctx)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = message.content[0].text.strip()

        # Strip markdown code fence if the model wraps output anyway
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        recs = json.loads(raw)
        if not isinstance(recs, list):
            logger.warning("AI recommendations: unexpected response shape")
            return []

        out = []
        for r in recs[:6]:
            if not isinstance(r, dict):
                continue
            out.append({
                "priority": int(r.get("priority", 9)),
                "category": str(r.get("category", "rankings")),
                "market":   str(r.get("market", "")),
                "headline": str(r.get("headline", ""))[:80],
                "action":   str(r.get("action", ""))[:200],
                "why":      str(r.get("why", ""))[:150],
                "impact":   str(r.get("impact", "medium")),
            })

        out.sort(key=lambda x: x["priority"])
        logger.info(f"AI recommendations generated: {len(out)} items")
        return out

    except Exception as e:
        logger.error(f"AI recommendations failed: {e}", exc_info=True)
        return []
