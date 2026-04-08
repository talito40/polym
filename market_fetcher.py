import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderArgs, OrderType, AssetType

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
        signature_type=config.SIGNATURE_TYPE,
        funder=config.POLYMARKET_PROXY_WALLET,
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


GAMMA_API = "https://gamma-api.polymarket.com"


def get_filtered_markets() -> list[dict]:
    now = datetime.now(timezone.utc)
    filtered = []
    offset = 0
    limit = 100
    total_fetched = 0

    while True:
        try:
            resp = __import__("requests").get(
                f"{GAMMA_API}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "order": "volume24hr",
                    "ascending": "false",
                    "limit": limit,
                    "offset": offset,
                },
                timeout=15,
            )
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            logger.error("Failed to fetch markets from Gamma API: %s", e)
            break

        if not batch:
            break

        total_fetched += len(batch)

        for m in batch:
            try:
                if not m.get("active") or m.get("closed"):
                    continue
                if not m.get("acceptingOrders"):
                    continue

                volume = float(m.get("volumeClob") or m.get("volume") or 0)
                if volume < config.MIN_VOLUME_USDC:
                    # Markets are sorted by volume desc — once below threshold, stop paging
                    break

                end_date_iso = m.get("endDate") or m.get("endDateIso")
                if not end_date_iso:
                    continue

                end_dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
                days_to_close = (end_dt - now).total_seconds() / 86400
                if not (config.MIN_DAYS_TO_CLOSE <= days_to_close <= config.MAX_DAYS_TO_CLOSE):
                    continue

                clob_token_ids = m.get("clobTokenIds", [])
                if isinstance(clob_token_ids, str):
                    import json as _json
                    clob_token_ids = _json.loads(clob_token_ids)
                if not clob_token_ids:
                    continue

                outcomes = m.get("outcomes") or ["YES", "NO"]
                if isinstance(outcomes, str):
                    import json as _json
                    outcomes = _json.loads(outcomes)

                # Normalise to the shape the rest of the bot expects
                tokens = [{"token_id": tid, "outcome": out} for tid, out in zip(
                    clob_token_ids,
                    outcomes,
                )]
                filtered.append({
                    **m,
                    "condition_id": m.get("conditionId", ""),
                    "tokens": tokens,
                    "_days_to_close": round(days_to_close, 2),
                })

            except Exception as e:
                logger.debug("Skipping market %s: %s", m.get("conditionId", "?"), e)
        else:
            # Inner loop completed without break — keep paging
            offset += limit
            continue
        break  # Inner loop broke (volume threshold hit)

    logger.info("Fetched %d markets from Gamma API, %d passed filters", total_fetched, len(filtered))
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


def place_market_order(
    token_id: str,
    size_usdc: float,
    side: str = "BUY",
    stop_loss_pct: Optional[float] = None,
) -> Optional[str]:
    if config.DRY_RUN:
        logger.info(
            "[DRY RUN] Would place %s market order: token=%s size=$%.2f",
            side, token_id, size_usdc,
        )
        return f"dry-run-{token_id[:8]}-{side.lower()}-mkt"

    client = get_clob_client()
    try:
        order = client.create_market_order(MarketOrderArgs(
            token_id=token_id,
            amount=size_usdc,
            side=side,
        ))
        response = client.post_order(order, OrderType.FOK)
        order_id = response.get("orderID") or response.get("order_id")
        logger.info(
            "Market order placed: %s | token=%s size=$%.2f",
            order_id, token_id, size_usdc,
        )
        if order_id and side.upper() == "BUY":
            register_stop_loss(token_id, stop_loss_pct=stop_loss_pct)
        return order_id
    except Exception as e:
        logger.error("Failed to place market order for %s: %s", token_id, e)
        return None


def get_token_balance(token_id: str) -> Optional[float]:
    client = get_clob_client()
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams
        params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        resp = client.get_balance_allowance(params=params)
        balance = resp.get("balance") if isinstance(resp, dict) else getattr(resp, "balance", resp)
        # balance is returned in 6-decimal fixed-point (like USDC), convert to shares
        return float(balance) / 1e6 if balance is not None else None
    except Exception as e:
        logger.warning("get_token_balance failed for %s: %s", token_id, e)
        return None


def get_stop_loss_pct(mid: float) -> float:
    """Return stop-loss % based on token price tier."""
    if mid > 0.50:
        return 0.15   # e.g. buy @ 0.80 → stop @ 0.68
    elif mid >= 0.20:
        return 0.25   # e.g. buy @ 0.35 → stop @ 0.26
    else:
        return 0.40   # e.g. buy @ 0.10 → stop @ 0.06


def register_stop_loss(
    token_id: str,
    stop_loss_pct: Optional[float] = None,
    question: str = "",
) -> None:
    """Register a soft stop-loss in stop_losses.json (monitored by stop_loss_monitor.py)."""
    import json as _json
    from pathlib import Path
    sl_file = Path(__file__).parent / "stop_losses.json"
    positions = _json.loads(sl_file.read_text()) if sl_file.exists() else []

    mid = get_market_midpoint(token_id)
    if mid is None:
        logger.warning("register_stop_loss: no midpoint for %s, skipping", token_id)
        return

    if stop_loss_pct is None:
        stop_loss_pct = get_stop_loss_pct(mid)

    stop_price = round(mid * (1 - stop_loss_pct), 4)
    entry = {
        "token_id": token_id,
        "entry_price": mid,
        "stop_loss_price": stop_price,
        "stop_loss_pct": stop_loss_pct,
        "question": question or token_id[:20],
    }
    positions.append(entry)
    sl_file.write_text(_json.dumps(positions, indent=2))
    logger.info(
        "Stop-loss registered: token=%s mid=%.4f stop=%.4f (-%d%%)",
        token_id, mid, stop_price, int(stop_loss_pct * 100),
    )


def place_stop_loss(
    token_id: str,
    stop_loss_pct: float = 0.15,
) -> Optional[str]:
    """Place a SELL limit order at (mid * (1 - stop_loss_pct)) for the full token balance."""
    mid = get_market_midpoint(token_id)
    if mid is None:
        logger.error("Cannot place stop-loss: no midpoint for %s", token_id)
        return None

    balance = get_token_balance(token_id)
    if not balance or balance < 0.01:
        logger.error("Cannot place stop-loss: no balance for %s (balance=%s)", token_id, balance)
        return None

    stop_price = round(mid * (1 - stop_loss_pct), 3)
    stop_price = max(0.01, min(0.99, stop_price))

    if config.DRY_RUN:
        logger.info(
            "[DRY RUN] Would place stop-loss SELL: token=%s mid=%.3f stop=%.3f shares=%.2f",
            token_id, mid, stop_price, balance,
        )
        return f"dry-run-{token_id[:8]}-stoploss"

    client = get_clob_client()
    try:
        import math
        size = math.floor(balance * 100) / 100  # floor to 2dp to never exceed balance
        order = client.create_order(OrderArgs(
            token_id=token_id,
            price=stop_price,
            size=size,
            side="SELL",
        ))
        response = client.post_order(order, OrderType.GTC)
        sl_order_id = response.get("orderID") or response.get("order_id")
        logger.info(
            "Stop-loss placed: %s | token=%s mid=%.3f stop=%.3f shares=%.2f",
            sl_order_id, token_id, mid, stop_price, balance,
        )
        return sl_order_id
    except Exception as e:
        logger.error("Failed to place stop-loss for %s: %s", token_id, e)
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
