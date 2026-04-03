import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderArgs, OrderType

import config

logger = logging.getLogger(__name__)

_client: Optional[ClobClient] = None


def get_clob_client() -> ClobClient:
    global _client
    if _client is not None:
        return _client

    if not config.POLYMARKET_PRIVATE_KEY:
        raise ValueError("POLYMARKET_PRIVATE_KEY is not set in environment")

    client = ClobClient(
        host=config.CLOB_HOST,
        key=config.POLYMARKET_PRIVATE_KEY,
        chain_id=config.CHAIN_ID,
        signature_type=0,
    )

    if config.POLYMARKET_API_KEY:
        client.set_api_creds(ApiCreds(
            api_key=config.POLYMARKET_API_KEY,
            api_secret=config.POLYMARKET_SECRET,
            api_passphrase=config.POLYMARKET_PASSPHRASE,
        ))
        logger.info("CLOB client initialized with existing API credentials")
    else:
        logger.info("No API credentials found — deriving them now (one-time setup)")
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        logger.info(
            "API credentials derived. Add these to your .env:\n"
            "  POLYMARKET_API_KEY=%s\n"
            "  POLYMARKET_SECRET=%s\n"
            "  POLYMARKET_PASSPHRASE=%s",
            creds.api_key, creds.api_secret, creds.api_passphrase,
        )

    _client = client
    return _client


def get_filtered_markets() -> list[dict]:
    client = get_clob_client()
    now = datetime.now(timezone.utc)

    try:
        raw = client.get_markets()
    except Exception as e:
        logger.error("Failed to fetch markets: %s", e)
        return []

    markets = raw.get("data", raw) if isinstance(raw, dict) else raw
    filtered = []

    for m in markets:
        try:
            volume = float(m.get("volume", 0) or 0)
            if volume < config.MIN_VOLUME_USDC:
                continue

            end_date_iso = m.get("end_date_iso") or m.get("endDateIso")
            if not end_date_iso:
                continue

            end_dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
            days_to_close = (end_dt - now).total_seconds() / 86400

            if not (config.MIN_DAYS_TO_CLOSE <= days_to_close <= config.MAX_DAYS_TO_CLOSE):
                continue

            tokens = m.get("tokens", [])
            if not tokens:
                continue

            filtered.append({**m, "_days_to_close": round(days_to_close, 2)})

        except Exception as e:
            logger.debug("Skipping market %s: %s", m.get("condition_id", "?"), e)

    logger.info("Fetched %d markets, %d passed filters", len(markets), len(filtered))
    return filtered


def get_market_midpoint(token_id: str) -> Optional[float]:
    client = get_clob_client()
    try:
        result = client.get_midpoint(token_id)
        mid = result.get("mid") if isinstance(result, dict) else result
        return float(mid)
    except Exception as e:
        logger.warning("get_midpoint failed for %s: %s", token_id, e)
        return None


def get_order_book_summary(token_id: str) -> Optional[dict]:
    client = get_clob_client()
    try:
        book = client.get_order_book(token_id)
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        spread = round(best_ask - best_bid, 4) if (best_bid and best_ask) else None
        return {"best_bid": best_bid, "best_ask": best_ask, "spread": spread}
    except Exception as e:
        logger.warning("get_order_book failed for %s: %s", token_id, e)
        return None


def place_limit_order(
    token_id: str,
    price: float,
    size_usdc: float,
    side: str = "BUY",
) -> Optional[str]:
    if config.DRY_RUN:
        logger.info(
            "[DRY RUN] Would place %s limit order: token=%s price=%.3f size=$%.2f",
            side, token_id, price, size_usdc,
        )
        return f"dry-run-{token_id[:8]}-{side.lower()}"

    client = get_clob_client()
    try:
        size_shares = round(size_usdc / price, 2)
        order = client.create_order(OrderArgs(
            token_id=token_id,
            price=price,
            size=size_shares,
            side=side,
        ))
        response = client.post_order(order, OrderType.GTC)
        order_id = response.get("orderID") or response.get("order_id")
        logger.info(
            "Order placed: %s | token=%s price=%.3f shares=%.2f",
            order_id, token_id, price, size_shares,
        )
        return order_id
    except Exception as e:
        logger.error("Failed to place order for %s: %s", token_id, e)
        return None


def cancel_order(order_id: str) -> bool:
    if config.DRY_RUN:
        logger.info("[DRY RUN] Would cancel order: %s", order_id)
        return True

    client = get_clob_client()
    try:
        client.cancel(order_id)
        logger.info("Cancelled order: %s", order_id)
        return True
    except Exception as e:
        logger.error("Failed to cancel order %s: %s", order_id, e)
        return False


def cancel_all_orders() -> bool:
    if config.DRY_RUN:
        logger.info("[DRY RUN] Would cancel all orders")
        return True

    client = get_clob_client()
    try:
        client.cancel_all()
        logger.info("All orders cancelled")
        return True
    except Exception as e:
        logger.error("Failed to cancel all orders: %s", e)
        return False


def get_last_trade_price(token_id: str) -> Optional[float]:
    client = get_clob_client()
    try:
        result = client.get_last_trade_price(token_id)
        price = result.get("price") if isinstance(result, dict) else result
        return float(price) if price is not None else None
    except Exception as e:
        logger.debug("get_last_trade_price failed for %s: %s", token_id, e)
        return None
