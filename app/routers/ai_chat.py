"""Ask AI — conversational endpoint for on-demand data analysis.

POST /ai/ask  { "question": "..." }
Gathers the full live data context (same as the weekly digest), builds a
prompt, and streams a Claude response back as plain text.
"""
import logging
from fastapi import APIRouter, Request, Depends
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


def _require_session(request: Request):
    user = request.session.get("user")
    if not user:
        return None
    return user


def _build_ask_prompt(question: str, ctx: dict) -> str:
    """Build a data-rich prompt for a free-form question about the business."""
    from app.services.ai_recommendations import (
        _MARKET_DISPLAY,
        _build_narrative_prompt,
    )

    # Reuse the narrative data-assembly (it already serialises everything nicely)
    data_block = _build_narrative_prompt(ctx)

    # Replace the narrative instruction at the end with the user's question
    marker = "Write the briefing now."
    if marker in data_block:
        data_block = data_block[: data_block.index(marker)].rstrip()

    return "\n".join([
        "You are a strategic advisor with full access to the live Market Pulse data for",
        "Duncan Law LLP — a bankruptcy law firm in North Carolina with offices in",
        "Greensboro, Winston-Salem, High Point, Charlotte, Salisbury, and Asheville.",
        "",
        "Answer the following question based on the data below.",
        "Be direct, specific, and actionable. Use the actual numbers and competitor names",
        "from the data. Keep the response focused — do not summarise data the question",
        "did not ask about. Aim for 150–400 words unless the question calls for more.",
        "Write in flowing prose (no bullet lists unless the question specifically calls",
        "for a list). If the data does not contain enough information to fully answer the",
        "question, say so plainly and explain what data would be needed.",
        "",
        f"QUESTION: {question}",
        "",
        "--- LIVE DATA ---",
        "",
        data_block,
    ])


@router.post("/ai/ask")
async def ask_ai(request: Request, db: Session = Depends(get_db)):
    user = _require_session(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    question = (body.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": "No question provided"}, status_code=400)
    if len(question) > 1000:
        return JSONResponse({"error": "Question too long (max 1000 characters)"}, status_code=400)

    from app.config import settings
    if not settings.anthropic_api_key:
        return JSONResponse(
            {"error": "Anthropic API key not configured — add ANTHROPIC_API_KEY to Railway environment variables."},
            status_code=503,
        )

    # Gather live data (same pipeline as the weekly digest)
    try:
        from app.services.email_digest import _gather_data
        ctx = _gather_data(db)
    except Exception as e:
        logger.error(f"Ask AI: data gathering failed: {e}", exc_info=True)
        return JSONResponse({"error": "Failed to gather data — try again."}, status_code=500)

    prompt = _build_ask_prompt(question, ctx)

    async def stream_response():
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            async with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=1200,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except Exception as e:
            logger.error(f"Ask AI: Claude stream failed: {e}", exc_info=True)
            yield f"\n\n[Error: {str(e)[:120]}]"

    return StreamingResponse(stream_response(), media_type="text/plain; charset=utf-8")
