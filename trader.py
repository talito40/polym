import logging
from typing import Optional

import config
import market_fetcher

logger = logging.getLogger(__name__)


def compute_order_params(
    market: dict,
    analysis: dict,
    midpoint: float,
    book: Optional[dict],
) -> Optional[dict]:
    claude_prob = analysis["probability"]
    edge = claude_prob - midpoint

    if abs(edge) < config.MIN_EDGE_PCT:
        logger.debug(
            "Edge %.3f below threshold %.3f — skipping '%s'",
            abs(edge), config.MIN_EDGE_PCT, market.get("question", "")[:60],
        )
        return None

    tokens = market.get("tokens", [])
    if not tokens:
        logger.warning("No tokens found for market: %s", market.get("condition_id"))
        return None

    if edge > 0:
        # Claude thinks YES is underpriced → buy YES token
        side = "BUY"
        token_id = next(
            (t["token_id"] for t in tokens if t.get("outcome", "").upper() == "YES"),
            tokens[0]["token_id"],
        )
        # Place limit slightly below midpoint to get filled
        limit_price = round(midpoint - 0.01, 3)
    else:
        # Claude thinks YES is overpriced → buy NO token
        side = "BUY"
        token_id = next(
            (t["token_id"] for t in tokens if t.get("outcome", "").upper() == "NO"),
            tokens[-1]["token_id"] if len(tokens) > 1 else tokens[0]["token_id"],
        )
        # NO token price is 1 - YES_mid; place limit slightly below that
        no_mid = round(1.0 - midpoint, 3)
        limit_price = round(no_mid - 0.01, 3)

    # Ensure price is valid
    limit_price = max(0.01, min(0.99, limit_price))

    return {
        "token_id": token_id,
        "price": limit_price,
        "size_usdc": config.MAX_ORDER_SIZE_USDC,
        "side": side,
        "edge": round(edge, 4),
    }


def execute_trade(
    market: dict,
    order_params: dict,
) -> Optional[str]:
    question = market.get("question", "")[:60]
    logger.info(
        "Executing trade: '%s' | token=%s | price=%.3f | size=$%.2f | edge=%.3f",
        question,
        order_params["token_id"][:12],
        order_params["price"],
        order_params["size_usdc"],
        order_params["edge"],
    )

    order_id = market_fetcher.place_limit_order(
        token_id=order_params["token_id"],
        price=order_params["price"],
        size_usdc=order_params["size_usdc"],
        side=order_params["side"],
    )

    if order_id:
        logger.info("Trade submitted — order_id: %s", order_id)
    else:
        logger.error("Trade failed for '%s'", question)

    return order_id
