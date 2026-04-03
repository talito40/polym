import logging

import config
import market_fetcher
import state as state_module

logger = logging.getLogger(__name__)


def check_and_manage_positions(st: dict) -> dict:
    positions = st.get("positions", {})
    if not positions:
        logger.info("No open positions to manage")
        return st

    to_close = []

    for order_id, pos in positions.items():
        token_id = pos["token_id"]
        entry_price = pos["entry_price"]

        current_price = market_fetcher.get_last_trade_price(token_id)
        if current_price is None:
            current_price = market_fetcher.get_market_midpoint(token_id)
        if current_price is None:
            logger.warning("Cannot get price for position %s — skipping", order_id)
            continue

        if pos["side"] == "BUY":
            pnl_pct = (current_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - current_price) / entry_price

        logger.info(
            "Position %s: entry=%.3f current=%.3f pnl=%.1f%%",
            order_id, entry_price, current_price, pnl_pct * 100,
        )

        if pnl_pct <= -config.STOP_LOSS_PCT:
            to_close.append((order_id, current_price, "stop_loss"))
        elif pnl_pct >= config.TAKE_PROFIT_PCT:
            to_close.append((order_id, current_price, "take_profit"))

    for order_id, exit_price, reason in to_close:
        market_fetcher.cancel_order(order_id)
        st = state_module.close_position(st, order_id, exit_price, reason)

    return st


def summarize_portfolio(st: dict) -> None:
    positions = st.get("positions", {})
    closed = st.get("closed_positions", [])

    print(f"\n{'='*50}")
    print(f"PORTFOLIO SUMMARY")
    print(f"{'='*50}")
    print(f"Open positions: {len(positions)}")

    for order_id, pos in positions.items():
        print(
            f"  [{pos['side']}] {pos['question'][:50]}"
            f"\n       entry={pos['entry_price']:.3f}  size=${pos['size_usdc']:.2f}"
            f"  opened={pos['opened_at'][:10]}"
        )

    if closed:
        total_pnl = sum(p.get("pnl_pct", 0) for p in closed) / len(closed)
        winners = sum(1 for p in closed if p.get("pnl_pct", 0) > 0)
        print(f"\nClosed positions: {len(closed)}")
        print(f"  Win rate: {winners}/{len(closed)} ({100*winners//len(closed)}%)")
        print(f"  Avg return: {total_pnl*100:.1f}%")
    else:
        print("\nNo closed positions yet")

    print(f"{'='*50}\n")
