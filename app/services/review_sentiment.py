"""Weekly AI sentiment analysis of competitor Google reviews via Claude API.

Reads review texts accumulated in ReviewSnapshot.snapshot_data (populated
after `reviews` was added to the Places API FIELDS). Deduplicates across the
last 8 weeks, analyzes up to 25 unique reviews per competitor, and writes a
structured result (themes, strengths, weaknesses, summary) to ReviewSentiment.

Uses claude-haiku for speed and cost efficiency — each analysis is ~600 tokens.
Skips competitors with fewer than 3 unique reviews collected so far.
Returns {} / 0 on any failure so the weekly job continues uninterrupted.
"""
import json
import logging
from datetime import datetime, timezone, timedelta, date

logger = logging.getLogger(__name__)

_THEME_GUIDANCE = """
Only flag themes present in these reviews. Common ones for bankruptcy firms:
- Communication & responsiveness
- Process clarity (explaining steps, what to expect)
- Staff warmth & professionalism
- Fee/cost transparency
- Outcome satisfaction
- Office accessibility & wait times
- Filing speed
"""


def _extract_reviews(snapshot_data: dict) -> list:
    if not snapshot_data:
        return []
    return snapshot_data.get("reviews", [])


def _build_prompt(firm_name: str, reviews: list) -> str:
    lines = [
        f"You are analyzing Google reviews for '{firm_name}', a bankruptcy law firm.",
        "Reviews (most recent first):",
        "",
    ]
    for i, r in enumerate(reviews[:25], 1):
        text = (r.get("text") or "").strip()
        rating = r.get("rating", "?")
        if text:
            lines.append(f"{i}. [{rating}★] {text[:400]}")

    lines += [
        "",
        _THEME_GUIDANCE,
        "",
        "Return ONLY valid JSON — no markdown, no explanation:",
        "{",
        '  "themes": [',
        '    {"theme": "Communication", "sentiment": "positive", "mentions": 3, "example": "short verbatim quote"}',
        "  ],",
        '  "strengths": ["Specific strength, max 12 words"],',
        '  "weaknesses": ["Specific weakness, max 12 words"],',
        '  "summary": "One sentence, max 25 words, capturing this firm\'s client experience."',
        "}",
        "",
        "Rules: only themes with 2+ mentions; max 5 themes; max 3 strengths; max 2 weaknesses.",
        'sentiment must be "positive", "negative", or "mixed".',
        "Be specific — use this firm's actual patterns, not generic descriptions.",
    ]
    return "\n".join(lines)


def _parse_response(raw: str) -> dict:
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    data = json.loads(raw)

    themes = []
    for t in data.get("themes", [])[:5]:
        if not isinstance(t, dict) or not t.get("theme"):
            continue
        sentiment = t.get("sentiment", "mixed")
        if sentiment not in ("positive", "negative", "mixed"):
            sentiment = "mixed"
        themes.append({
            "theme": str(t["theme"])[:40],
            "sentiment": sentiment,
            "mentions": int(t.get("mentions", 1)),
            "example": str(t.get("example", ""))[:150],
        })

    strengths = [str(s)[:100] for s in data.get("strengths", [])[:3] if s]
    weaknesses = [str(w)[:100] for w in data.get("weaknesses", [])[:2] if w]
    summary = str(data.get("summary", ""))[:300]
    return {"themes": themes, "strengths": strengths, "weaknesses": weaknesses, "summary": summary}


def analyze_competitor_sentiment(db) -> int:
    """Analyze sentiment for all active competitors. Returns count analyzed."""
    try:
        from app.config import settings
        if not settings.anthropic_api_key:
            logger.info("ANTHROPIC_API_KEY not set — skipping sentiment analysis")
            return 0
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    except Exception as e:
        logger.error(f"Sentiment: failed to init Claude client: {e}", exc_info=True)
        return 0

    from app.models.reviews import ReviewSnapshot
    from app.models.competitor import Competitor
    from app.models.sentiment import ReviewSentiment
    from app.models.base import new_uuid

    cutoff = datetime.now(timezone.utc) - timedelta(weeks=8)
    today = date.today()
    analyzed = 0

    competitors = (
        db.query(Competitor)
        .filter(Competitor.is_own_firm == False, Competitor.active == True)
        .all()
    )

    for comp in competitors:
        snapshots = (
            db.query(ReviewSnapshot)
            .filter(
                ReviewSnapshot.competitor_id == comp.id,
                ReviewSnapshot.snapped_at >= cutoff,
                ReviewSnapshot.snapshot_data.isnot(None),
            )
            .order_by(ReviewSnapshot.snapped_at.desc())
            .all()
        )

        seen: set = set()
        unique_reviews: list = []
        for snap in snapshots:
            for r in _extract_reviews(snap.snapshot_data or {}):
                rid = r.get("review_id") or (r.get("text") or "")[:80]
                if rid and rid not in seen:
                    seen.add(rid)
                    unique_reviews.append(r)

        if len(unique_reviews) < 3:
            logger.debug(f"Sentiment: skipping {comp.name} — {len(unique_reviews)} reviews (need ≥3)")
            continue

        try:
            prompt = _build_prompt(comp.name, unique_reviews)
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            result = _parse_response(message.content[0].text.strip())
        except Exception as e:
            logger.error(f"Sentiment: analysis failed for {comp.name}: {e}", exc_info=True)
            continue

        existing = (
            db.query(ReviewSentiment)
            .filter(ReviewSentiment.competitor_id == comp.id)
            .order_by(ReviewSentiment.analyzed_at.desc())
            .first()
        )
        now = datetime.now(timezone.utc)
        if existing and existing.analyzed_at.date() == today:
            existing.review_count = len(unique_reviews)
            existing.themes = result["themes"]
            existing.strengths = result["strengths"]
            existing.weaknesses = result["weaknesses"]
            existing.summary = result["summary"]
            existing.analyzed_at = now
        else:
            db.add(ReviewSentiment(
                id=new_uuid(),
                competitor_id=comp.id,
                analyzed_at=now,
                review_count=len(unique_reviews),
                themes=result["themes"],
                strengths=result["strengths"],
                weaknesses=result["weaknesses"],
                summary=result["summary"],
            ))
        db.commit()
        analyzed += 1
        logger.info(f"Sentiment: {comp.name} — {len(unique_reviews)} reviews → {len(result['themes'])} themes")

    logger.info(f"Sentiment analysis complete: {analyzed} competitors analyzed")
    return analyzed
