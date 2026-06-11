"""Weekly AI-generated content for the Monday digest via Claude API.

Two functions:
  generate_narrative(ctx)      — written strategic briefing, returned as plain text
  generate_recommendations(ctx) — 4-week task roadmap, returned as structured dict

Both return a safe empty value ({} or "") on any failure so the digest still sends.
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

    # Intake funnel
    funnel = ctx.get("funnel", {})
    if funnel and funnel.get("combined_ytd"):
        f = funnel
        lines.append("")
        lines.append(f"## Intake Funnel — {f.get('period_label', 'YTD')}")
        lines.append(f"  Consultations: {f.get('combined_ytd', 0):,} (Damon {f.get('damon_ytd',0)}, Anne {f.get('anne_ytd',0)})")
        lines.append(f"  Contracts signed: {f.get('contract_ytd', 0):,} ({f.get('consult_to_contract','?')}% consult→contract)")
        lines.append(f"  Cases filed (PACER): {f.get('pacer_ytd', 0)} ({f.get('consult_to_filed','?')}% consult→filed, {f.get('contract_to_filed','?')}% contract→filed)")
        lines.append(f"  Est. revenue YTD: ~${f.get('est_revenue_ytd', 0):,} (at $1,500/case blended avg)")
        if f.get("pacer_prev_ytd"):
            delta = f["pacer_ytd"] - f["pacer_prev_ytd"]
            lines.append(f"  vs same period prior year: {f['pacer_prev_ytd']} filings ({'+'if delta>=0 else ''}{delta} YoY)")

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


# ── Narrative briefing ────────────────────────────────────────────────────────

def _build_narrative_prompt(ctx: dict) -> str:
    """Build the prompt for the written strategic narrative."""
    lines = [
        "You are a strategic advisor writing a weekly intelligence briefing for Damon Duncan,",
        "the owner of Duncan Law LLP — a bankruptcy law firm with offices in Greensboro,",
        "Winston-Salem, High Point, Charlotte, Salisbury, and Asheville, North Carolina.",
        "",
        "Damon is a busy attorney, not an SEO technician. Write as a trusted advisor who has",
        "studied this week's data and is giving him a direct, honest read on where things stand",
        "and exactly what he needs to do. Be specific — name competitors, cite actual numbers,",
        "call out risks clearly. Do not pad with generic SEO advice he already knows.",
        "",
        "Tone: direct, confident, conversational. Think 'smart partner memo', not marketing copy.",
        "No bullet points. No headers. No markdown. Write in flowing paragraphs only.",
        "Target length: 450–600 words across 4–5 paragraphs.",
        "",
        "Structure your briefing in this order:",
        "1. Opening paragraph: Overall state of play this week — one honest summary sentence",
        "   per district/market cluster. Is this a good week or a concerning one? Be direct.",
        "2. Rankings & visibility: Where is Duncan Law winning in the 3-pack? Where are the",
        "   gaps? Name the keywords and markets where positions are at risk.",
        "3. Reviews & competition: This is the most important paragraph. Describe the review",
        "   velocity situation — is Duncan Law gaining or losing ground vs. competitors?",
        "   Name specific firms that are pulling ahead and by how much.",
        "4. PACER / bankruptcy filings: How does Duncan Law's case volume compare to competitors",
        "   in each district? Are they gaining or losing market share? Be frank.",
        "5. Closing action paragraph: End with 2–3 concrete things Damon or his staff should",
        "   do THIS WEEK — not generic advice, but specific actions tied to the data above.",
        "   Make the stakes clear: what happens if he does these things vs. if he doesn't.",
        "",
        "Here is this week's live data:",
        "",
    ]

    # Rankings
    lines.append("GOOGLE 3-PACK RANKINGS:")
    for market, label in _MARKET_DISPLAY.items():
        data = ctx.get("rankings_by_market", {}).get(market)
        if data:
            status = f"{data['in_pack']}/{data['total']} keywords in pack"
            gaps = data.get("gaps", [])
            pos = data.get("positions", [])
            pos_str = ", ".join(f"#{p}" for p in sorted(pos)) if pos else "none"
            gap_str = f", missing: {', '.join(gaps[:3])}" if gaps else ", fully covered"
            lines.append(f"  {label}: {status} (positions: {pos_str}{gap_str})")
        else:
            lines.append(f"  {label}: no data this week")

    # Reviews
    lines.append("")
    lines.append("GOOGLE REVIEWS:")
    reviews = ctx.get("reviews_by_market", {})
    own_deltas = ctx.get("own_review_deltas", {})
    velocity_map = {v["display"]: v for v in ctx.get("market_velocity", [])}
    for market, label in _MARKET_DISPLAY.items():
        data = reviews.get(market)
        if not data:
            lines.append(f"  {label}: no data")
            continue
        count = data.get("review_count", 0)
        rating = data.get("rating")
        delta = own_deltas.get(market, 0)
        rating_str = f"{rating:.1f}★" if rating else "no rating"
        vel = velocity_map.get(label)
        if vel:
            if vel["rival_count"] > count:
                gap = vel["rival_count"] - count
                rd = round(vel["rival_delta"], 1)
                od = round(vel["own_delta"], 1)
                proj = vel.get("proj_text", "unknown")
                lines.append(
                    f"  {label}: Duncan Law {count} reviews ({rating_str}, +{delta} this week, "
                    f"+{od}/wk avg) | BEHIND: {vel['rival_name']} has {vel['rival_count']} reviews "
                    f"(+{rd}/wk avg) — gap: {gap} reviews, projection: {proj}"
                )
            else:
                lines.append(
                    f"  {label}: Duncan Law {count} reviews ({rating_str}, +{delta} this week) "
                    f"| LEADING vs. {vel['rival_name']} ({vel['rival_count']} reviews)"
                )
        else:
            lines.append(f"  {label}: Duncan Law {count} reviews ({rating_str}, +{delta} this week)")

    # Gap to #1 in pack
    g1 = ctx.get("gap_to_1_by_market", {})
    if g1:
        lines.append("")
        lines.append("REVIEWS NEEDED TO MATCH CURRENT RANK-#1 FIRM:")
        for market, label in _MARKET_DISPLAY.items():
            info = g1.get(market)
            if not info:
                continue
            if info.get("is_leading"):
                lines.append(f"  {label}: Duncan Law is the review leader")
            elif info.get("gap") is not None:
                lines.append(
                    f"  {label}: need +{info['gap']} more reviews to match "
                    f"{info.get('rank1_name','—')} ({info.get('rank1_reviews','—')} reviews)"
                )

    # Competitor pack entries
    pack_entries = ctx.get("pack_entries_by_market", {})
    if pack_entries:
        lines.append("")
        lines.append("COMPETITORS THAT ENTERED THE 3-PACK THIS WEEK:")
        for market, entries in pack_entries.items():
            label = _MARKET_DISPLAY.get(market, market.replace("_", " ").title())
            for e in entries[:4]:
                pos = f"#{e['position']}" if e.get("position") else "unknown position"
                lines.append(f"  {label}: {e['competitor']} at {pos} for '{e['keyword']}'")

    # Competitor review velocity leaders
    leaders = ctx.get("velocity_leaders", [])
    if leaders:
        lines.append("")
        lines.append("COMPETITORS WITH HIGHEST REVIEW GAINS THIS WEEK:")
        for v in leaders:
            lines.append(f"  {v['name']}: +{v['delta']} reviews (total: {v['total']:,})")

    # PACER
    pacer = ctx.get("pacer_standings", {})
    if any(pacer.values()):
        lines.append("")
        lines.append("PACER BANKRUPTCY FILING STANDINGS (most recent month):")
        for dist, rows in pacer.items():
            if not rows:
                continue
            own_row = next((r for r in rows if r.get("is_own")), None)
            top = rows[0]
            rank = next((i + 1 for i, r in enumerate(rows) if r.get("is_own")), None)
            own_str = (
                f"Duncan Law: #{rank} with {own_row['count']} cases"
                if own_row else "Duncan Law: not in top 6"
            )
            lines.append(
                f"  {dist}: #1 is {top['name']} ({top['count']} cases) | {own_str}"
            )

    # Open alerts
    open_alerts = ctx.get("open_alerts", [])
    if open_alerts:
        lines.append("")
        lines.append("OPEN ALERTS (unacknowledged):")
        for a in open_alerts[:5]:
            msg = (a.detail.get("message", "") if hasattr(a, "detail") and a.detail else "")
            if not msg and hasattr(a, "alert_type"):
                msg = a.alert_type
            lines.append(f"  - {str(msg)[:120]}")

    # Intake funnel
    funnel = ctx.get("funnel", {})
    if funnel and funnel.get("combined_ytd"):
        f = funnel
        lines.append("")
        lines.append(f"INTAKE FUNNEL — {f.get('period_label', 'YTD')}:")
        lines.append(f"  Consultations: {f.get('combined_ytd', 0):,} (Damon {f.get('damon_ytd',0)}, Anne {f.get('anne_ytd',0)})")
        lines.append(f"  Contracts signed: {f.get('contract_ytd', 0):,} ({f.get('consult_to_contract','?')}% consult→contract conversion rate)")
        lines.append(f"  Cases filed (PACER): {f.get('pacer_ytd', 0)} ({f.get('consult_to_filed','?')}% of consultations → filed, {f.get('contract_to_filed','?')}% of contracts → filed)")
        lines.append(f"  Est. revenue YTD: ~${f.get('est_revenue_ytd', 0):,} at $1,500/case blended average")
        if f.get("pacer_prev_ytd"):
            delta = f["pacer_ytd"] - f["pacer_prev_ytd"]
            sign  = "+" if delta >= 0 else ""
            lines.append(f"  Prior year same period: {f['pacer_prev_ytd']} filings ({sign}{delta} YoY change)")

    lines.append("")
    lines.append("Write the briefing now. Remember: no bullet points, no headers, flowing paragraphs only.")

    return "\n".join(lines)


def generate_narrative(ctx: dict) -> str:
    """Generate a written strategic briefing paragraph. Returns '' on any failure."""
    try:
        from app.config import settings
        if not settings.anthropic_api_key:
            logger.info("ANTHROPIC_API_KEY not configured — skipping narrative")
            return ""

        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        prompt = _build_narrative_prompt(ctx)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )

        text = message.content[0].text.strip()
        logger.info(f"AI narrative generated: {len(text)} chars")
        return text

    except Exception as e:
        logger.error(f"AI narrative generation failed: {e}", exc_info=True)
        return ""
