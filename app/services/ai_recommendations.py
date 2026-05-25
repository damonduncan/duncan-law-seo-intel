"""Weekly AI-generated 4-week SEO roadmap via Claude API.

Called once during the Monday digest build. Returns a sequential week-by-week
action plan grounded in that week's actual performance data.
Returns {} on any failure so the digest still sends without this section.
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
        "KEY SEO PRINCIPLE: Google reviews are the #1 ranking lever for local pack positions.",
        "Review outreach this week produces ranking improvements in 2-4 weeks, not immediately.",
        "This means the roadmap must sequence reviews BEFORE expecting ranking gains.",
        "",
    ]

    # Rankings
    lines.append("## Current Google 3-Pack Rankings")
    for market, label in _MARKET_DISPLAY.items():
        data = ctx.get("rankings_by_market", {}).get(market)
        if data:
            status = f"{data['in_pack']}/{data['total']} keywords in pack"
            gaps = data.get("gaps", [])
            gap_str = f" — missing: {', '.join(gaps[:3])}" if gaps else " — fully in pack"
            lines.append(f"  {label}: {status}{gap_str}")
        else:
            lines.append(f"  {label}: no data yet")

    # Reviews with competitor comparison
    lines.append("")
    lines.append("## Google Reviews — Duncan Law vs. Top Market Competitor")
    reviews = ctx.get("reviews_by_market", {})
    own_deltas = ctx.get("own_review_deltas", {})
    velocity_map = {v["display"]: v for v in ctx.get("market_velocity", [])}
    for market, label in _MARKET_DISPLAY.items():
        data = reviews.get(market)
        if not data:
            lines.append(f"  {label}: no data")
            continue
        count = data.get("review_count", 0)
        delta = own_deltas.get(market, 0)
        vel = velocity_map.get(label)
        if vel and vel["rival_count"] > count:
            gap = vel["rival_count"] - count
            rd = vel["rival_delta"]
            rival_str = (
                f" | rival: {vel['rival_name']} has {vel['rival_count']} "
                f"(+{rd}/wk) — gap: {gap} reviews"
            )
            proj = vel.get("proj_text", "")
            rival_str += f" | projection: {proj}"
        elif vel:
            rival_str = f" | leading {vel['rival_name']} ({vel['rival_count']} reviews)"
        else:
            rival_str = ""
        delta_str = f"+{delta}" if delta >= 0 else str(delta)
        lines.append(f"  {label}: Duncan Law {count} reviews ({delta_str}/wk){rival_str}")

    # Gap to rank #1 (review count needed to match the firm currently holding #1)
    g1 = ctx.get("gap_to_1_by_market", {})
    if g1:
        lines.append("")
        lines.append("## Reviews Needed to Match Rank-#1 Firm (per market)")
        for market, label in _MARKET_DISPLAY.items():
            info = g1.get(market)
            if not info:
                continue
            if info.get("is_leading"):
                lines.append(f"  {label}: Duncan Law leads in reviews (Review leader)")
            elif info.get("gap") is not None:
                rival = info.get("rank1_name", "—")
                r1rev = info.get("rank1_reviews", "—")
                lines.append(
                    f"  {label}: need +{info['gap']} reviews to match #1 "
                    f"({rival}, {r1rev} reviews)"
                )

    # Pack activity this week
    pack_entries = ctx.get("pack_entries_by_market", {})
    if pack_entries:
        lines.append("")
        lines.append("## New Competitor 3-Pack Entries This Week")
        for market, entries in pack_entries.items():
            label = _MARKET_DISPLAY.get(market, market.replace("_", " ").title())
            for e in entries[:4]:
                pos = f"#{e['position']}" if e.get("position") else "unknown position"
                lines.append(f"  {label}: {e['competitor']} entered at {pos} for '{e['keyword']}'")

    # Alerts
    open_alerts = ctx.get("open_alerts", [])
    if open_alerts:
        lines.append("")
        lines.append("## Open Alerts")
        for a in open_alerts[:5]:
            msg = ""
            if hasattr(a, "detail") and a.detail:
                msg = a.detail.get("message", "")
            if not msg and hasattr(a, "alert_type"):
                msg = a.alert_type
            lines.append(f"  - {str(msg)[:120]}")

    # PACER
    pacer = ctx.get("pacer_standings", {})
    if any(pacer.values()):
        lines.append("")
        lines.append("## PACER Bankruptcy Filings — Most Recent Month")
        for dist, rows in pacer.items():
            if not rows:
                continue
            top = rows[0]
            own_row = next((r for r in rows if r.get("is_own")), None)
            own_str = f"Duncan Law: {own_row['count']} cases" if own_row else "Duncan Law: not in top 6"
            lines.append(f"  {dist}: leader {top['name']} ({top['count']} cases) | {own_str}")

    lines += [
        "",
        "---",
        "TASK: Create a 4-week sequential SEO action roadmap for Duncan Law.",
        "",
        "Sequencing rules (follow these strictly):",
        "1. Week 1 = highest-urgency actions only. Max 3 tasks. Focus on the 1-2 markets",
        "   with the biggest review gaps AND active pack issues.",
        "2. Week 2 = follow up on week 1 + expand to next-priority market.",
        "   Never put the same market+action combo in consecutive weeks.",
        "3. Week 3 = secondary markets, GBP profile checks, monitoring tasks.",
        "4. Week 4 = progress review, sustained cadence, forward-looking adjustments.",
        "5. Reviews always precede ranking expectations by 2-4 weeks — reflect this.",
        "6. Each task must be completable in under 30 minutes by a law firm staff member.",
        "7. Be specific: use actual competitor names, review counts, and keywords from the data.",
        "",
        "Return ONLY valid JSON. No markdown, no explanation.",
        "Schema:",
        '{',
        '  "weeks": [',
        '    {',
        '      "week": 1,',
        '      "theme": "6 words max describing this week\'s focus",',
        '      "tasks": [',
        '        {',
        '          "task": "Specific action step, max 40 words",',
        '          "market": "Market name or All Markets",',
        '          "why": "The data point driving this, max 20 words",',
        '          "minutes": 15',
        '        }',
        '      ]',
        '    }',
        '  ]',
        '}',
    ]

    return "\n".join(lines)


def _parse_response(raw: str) -> dict:
    """Parse and validate Claude's JSON response. Returns {} if invalid."""
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    data = json.loads(raw)
    if not isinstance(data, dict) or "weeks" not in data:
        logger.warning("AI roadmap: unexpected response shape")
        return {}

    weeks = data["weeks"]
    if not isinstance(weeks, list) or len(weeks) == 0:
        return {}

    out_weeks = []
    for w in weeks[:4]:
        if not isinstance(w, dict):
            continue
        tasks = []
        for t in w.get("tasks", [])[:4]:
            if not isinstance(t, dict) or not t.get("task"):
                continue
            tasks.append({
                "task":    str(t.get("task", ""))[:200],
                "market":  str(t.get("market", ""))[:40],
                "why":     str(t.get("why", ""))[:150],
                "minutes": int(t.get("minutes", 20)),
            })
        if tasks:
            out_weeks.append({
                "week":  int(w.get("week", len(out_weeks) + 1)),
                "theme": str(w.get("theme", ""))[:60],
                "tasks": tasks,
            })

    if not out_weeks:
        return {}

    return {"weeks": out_weeks}


def generate_recommendations(ctx: dict) -> dict:
    """Build and return a 4-week roadmap dict. Returns {} on any failure."""
    try:
        from app.config import settings
        if not settings.anthropic_api_key:
            logger.info("ANTHROPIC_API_KEY not configured — skipping AI roadmap")
            return {}

        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        prompt = _build_prompt(ctx)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1800,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = message.content[0].text.strip()
        result = _parse_response(raw)

        if result:
            total_tasks = sum(len(w["tasks"]) for w in result["weeks"])
            logger.info(f"AI roadmap generated: {len(result['weeks'])} weeks, {total_tasks} tasks")
        else:
            logger.warning("AI roadmap: parse returned empty result")

        return result

    except Exception as e:
        logger.error(f"AI roadmap generation failed: {e}", exc_info=True)
        return {}
