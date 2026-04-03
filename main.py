#!/usr/bin/env python3
"""
Polymarket Trading Bot
======================
Entry point with CLI modes for running, analyzing, and managing positions.

Usage:
  python main.py --run        # Full cycle: analyze + manage + trade
  python main.py --analyze    # Analyze markets only (no trades)
  python main.py --manage     # Manage open positions only
  python main.py --status     # Print portfolio summary
  python main.py --cancel-all # Emergency: cancel all open orders
"""

import argparse
import logging
import os
import sys

import config
import market_analyzer
import market_fetcher
import position_manager
import state as state_module
import trader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("polymarket_bot")


def run_full_cycle() -> None:
    logger.info("=== FULL CYCLE START (DRY_RUN=%s) ===", config.DRY_RUN)

    st = state_module.load_state()

    # Step 1: Manage existing positions
    logger.info("--- Step 1: Managing open positions ---")
    st = position_manager.check_and_manage_positions(st)
    state_module.save_state(st)

    # Step 2: Check position cap
    open_count = len(st.get("positions", {}))
    if open_count >= config.MAX_OPEN_POSITIONS:
        logger.info(
            "At max open positions (%d/%d) — skipping new trades",
            open_count, config.MAX_OPEN_POSITIONS,
        )
        return

    slots_available = config.MAX_OPEN_POSITIONS - open_count

    # Step 3: Fetch and filter markets
    logger.info("--- Step 2: Fetching markets ---")
    markets = market_fetcher.get_filtered_markets()
    if not markets:
        logger.warning("No markets passed filters — nothing to analyze")
        return

    # Step 4: Gather price data
    logger.info("--- Step 3: Gathering price data ---")
    markets_with_data = []
    for m in markets[:30]:  # cap to avoid hitting rate limits
        tokens = m.get("tokens", [])
        if not tokens:
            continue
        yes_token = next(
            (t["token_id"] for t in tokens if t.get("outcome", "").upper() == "YES"),
            tokens[0]["token_id"],
        )
        midpoint = market_fetcher.get_market_midpoint(yes_token)
        if midpoint is None or not (0.05 <= midpoint <= 0.95):
            continue  # skip near-certain markets
        book = market_fetcher.get_order_book_summary(yes_token)
        markets_with_data.append({"market": m, "midpoint": midpoint, "book": book})

    if not markets_with_data:
        logger.warning("No usable market data found")
        return

    # Step 5: AI analysis
    logger.info("--- Step 4: Running Claude AI analysis on %d markets ---", len(markets_with_data))
    analyzed = market_analyzer.analyze_markets_batch(markets_with_data)

    if not analyzed:
        logger.info("No markets passed AI analysis — no trades to place")
        return

    # Step 6: Place trades
    logger.info("--- Step 5: Evaluating %d opportunities ---", len(analyzed))
    trades_placed = 0
    for item in analyzed:
        if trades_placed >= slots_available:
            break

        market = item["market"]
        midpoint = item["midpoint"]
        book = item.get("book")
        analysis = item["analysis"]

        params = trader.compute_order_params(market, analysis, midpoint, book)
        if params is None:
            continue

        # Skip if we already have a position in this market
        existing_tokens = {p["token_id"] for p in st["positions"].values()}
        if params["token_id"] in existing_tokens:
            logger.debug("Already have position in token %s — skipping", params["token_id"][:12])
            continue

        order_id = trader.execute_trade(market, params)
        if order_id:
            st = state_module.add_position(
                st,
                order_id=order_id,
                market_id=market.get("condition_id", ""),
                token_id=params["token_id"],
                question=market.get("question", ""),
                entry_price=params["price"],
                size_usdc=params["size_usdc"],
                side=params["side"],
            )
            state_module.save_state(st)
            trades_placed += 1

    logger.info("=== FULL CYCLE COMPLETE — %d new trade(s) placed ===", trades_placed)


def run_analyze_only() -> None:
    logger.info("=== ANALYZE ONLY MODE ===")
    markets = market_fetcher.get_filtered_markets()
    if not markets:
        logger.warning("No markets passed filters")
        return

    markets_with_data = []
    for m in markets[:20]:
        tokens = m.get("tokens", [])
        if not tokens:
            continue
        yes_token = next(
            (t["token_id"] for t in tokens if t.get("outcome", "").upper() == "YES"),
            tokens[0]["token_id"],
        )
        midpoint = market_fetcher.get_market_midpoint(yes_token)
        if midpoint is None or not (0.05 <= midpoint <= 0.95):
            continue
        book = market_fetcher.get_order_book_summary(yes_token)
        markets_with_data.append({"market": m, "midpoint": midpoint, "book": book})

    analyzed = market_analyzer.analyze_markets_batch(markets_with_data)

    print(f"\n{'='*60}")
    print(f"MARKET ANALYSIS RESULTS ({len(analyzed)} opportunities)")
    print(f"{'='*60}")
    for item in analyzed:
        m = item["market"]
        a = item["analysis"]
        mid = item["midpoint"]
        edge = a["probability"] - mid
        print(
            f"\n  Q: {m.get('question', '')[:70]}"
            f"\n     market={mid:.3f}  claude={a['probability']:.3f}  edge={edge:+.3f}"
            f"  confidence={a['confidence']}"
            f"\n     reason: {a['reasoning']}"
        )
    print(f"\n{'='*60}\n")


def run_manage_only() -> None:
    logger.info("=== MANAGE POSITIONS ONLY ===")
    st = state_module.load_state()
    st = position_manager.check_and_manage_positions(st)
    state_module.save_state(st)
    position_manager.summarize_portfolio(st)


def run_status() -> None:
    st = state_module.load_state()
    position_manager.summarize_portfolio(st)


def run_cancel_all() -> None:
    logger.info("=== EMERGENCY CANCEL ALL ===")
    market_fetcher.cancel_all_orders()
    logger.info("Done")


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Trading Bot")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run", action="store_true", help="Full cycle: analyze + manage + trade")
    group.add_argument("--analyze", action="store_true", help="Analyze markets only (no trades)")
    group.add_argument("--manage", action="store_true", help="Manage open positions only")
    group.add_argument("--status", action="store_true", help="Print portfolio summary")
    group.add_argument("--cancel-all", action="store_true", help="Emergency: cancel all open orders")

    args = parser.parse_args()

    if args.run:
        run_full_cycle()
    elif args.analyze:
        run_analyze_only()
    elif args.manage:
        run_manage_only()
    elif args.status:
        run_status()
    elif args.cancel_all:
        run_cancel_all()


if __name__ == "__main__":
    main()
