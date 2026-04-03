import json
import logging
import os
import tempfile
from datetime import datetime, timezone

from config import STATE_FILE

logger = logging.getLogger(__name__)


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"positions": {}, "closed_positions": []}
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    dir_name = os.path.dirname(STATE_FILE) or "."
    with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, suffix=".tmp") as tmp:
        json.dump(state, tmp, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, STATE_FILE)


def add_position(
    state: dict,
    order_id: str,
    market_id: str,
    token_id: str,
    question: str,
    entry_price: float,
    size_usdc: float,
    side: str,
) -> dict:
    state["positions"][order_id] = {
        "order_id": order_id,
        "market_id": market_id,
        "token_id": token_id,
        "question": question,
        "entry_price": entry_price,
        "size_usdc": size_usdc,
        "side": side,
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("Position added: %s @ %.3f (%s)", order_id, entry_price, side)
    return state


def close_position(state: dict, order_id: str, exit_price: float, reason: str = "") -> dict:
    position = state["positions"].pop(order_id, None)
    if position is None:
        logger.warning("close_position: order_id %s not found in state", order_id)
        return state

    entry = position["entry_price"]
    if position["side"] == "BUY":
        pnl_pct = (exit_price - entry) / entry
    else:
        pnl_pct = (entry - exit_price) / entry

    closed = {
        **position,
        "exit_price": exit_price,
        "pnl_pct": round(pnl_pct, 4),
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }
    state.setdefault("closed_positions", []).append(closed)
    logger.info(
        "Position closed: %s, exit=%.3f, pnl=%.1f%% (%s)",
        order_id, exit_price, pnl_pct * 100, reason,
    )
    return state
