import json
import logging
from typing import Optional

import anthropic

import config

logger = logging.getLogger(__name__)

_anthropic_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        if not config.ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        _anthropic_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _anthropic_client


def analyze_market(
    market_data: dict,
    midpoint: float,
    book_summary: Optional[dict],
    kalshi_info: Optional[dict] = None,
) -> Optional[dict]:
    question = market_data.get("question", "")
    days_to_close = market_data.get("_days_to_close", "?")
    volume = market_data.get("volume", "?")
    spread = book_summary.get("spread") if book_summary else None
    best_bid = book_summary.get("best_bid") if book_summary else None
    best_ask = book_summary.get("best_ask") if book_summary else None

    # Build Kalshi cross-platform context block if available
    kalshi_context = ""
    if kalshi_info:
        k_mid = kalshi_info.get("kalshi_mid", kalshi_info.get("mid"))
        k_title = kalshi_info.get("kalshi_title", kalshi_info.get("title", ""))
        if k_mid is not None:
            divergence = k_mid - midpoint
            direction = "agrees with Polymarket" if abs(divergence) < 0.05 else (
                "prices this HIGHER than Polymarket" if divergence > 0 else "prices this LOWER than Polymarket"
            )
            kalshi_context = (
                f"\nKalshi cross-platform signal: A closely related market on Kalshi "
                f"({k_title[:60]}) is priced at {k_mid:.3f} — Kalshi {direction} "
                f"(divergence: {divergence:+.3f}). Large divergence suggests one platform "
                f"may be mispriced. Factor this into your estimate."
            )

    prompt = f"""You are analyzing a prediction market on Polymarket. Based only on your knowledge and reasoning, estimate the true probability of this outcome occurring.

Market question: {question}
Current market price (mid): {midpoint:.3f} (this represents the implied probability, 0-1)
Best bid: {best_bid}
Best ask: {best_ask}
Spread: {spread}
Volume (USDC): {volume}
Days until market closes: {days_to_close}{kalshi_context}

Respond ONLY with a JSON object in this exact format (no markdown, no extra text):
{{
  "probability": <float between 0 and 1>,
  "confidence": "<high|medium|low>",
  "reasoning": "<one sentence explanation>"
}}

Rules:
- probability must be your honest estimate, not anchored to the market price
- confidence=high means you have strong knowledge about this topic
- confidence=low means you have very little basis to estimate
- Keep reasoning concise (under 20 words)"""

    try:
        client = _get_client()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in response.content if hasattr(b, "text")), "").strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        result = json.loads(text)

        prob = float(result["probability"])
        confidence = result.get("confidence", "low")
        reasoning = result.get("reasoning", "")

        if not (0.0 <= prob <= 1.0):
            logger.warning("Claude returned out-of-range probability %.3f for: %s", prob, question[:60])
            return None

        logger.info(
            "Analysis: '%s' | market=%.3f | claude=%.3f | conf=%s",
            question[:60], midpoint, prob, confidence,
        )
        return {"probability": prob, "confidence": confidence, "reasoning": reasoning}

    except json.JSONDecodeError as e:
        logger.warning("Failed to parse Claude response for '%s': %s", question[:60], e)
        return None
    except Exception as e:
        logger.error("Claude analysis failed for '%s': %s", question[:60], e)
        return None


def analyze_markets_batch(markets_with_data: list[dict]) -> list[dict]:
    results = []
    for item in markets_with_data:
        market = item["market"]
        midpoint = item["midpoint"]
        book = item.get("book")

        analysis = analyze_market(market, midpoint, book)
        if analysis is None:
            continue
        if analysis["confidence"] == "low":
            logger.debug("Skipping low-confidence market: %s", market.get("question", "")[:60])
            continue

        results.append({
            "market": market,
            "midpoint": midpoint,
            "book": book,
            "analysis": analysis,
        })

    logger.info("Batch analysis: %d/%d markets passed", len(results), len(markets_with_data))
    return results
