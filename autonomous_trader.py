"""
Unified Autonomous Trader
=========================
Goal: grow portfolio to $50,000 starting from ~$2,000.

Combines three strategies under shared risk controls. Runs every 2 minutes via cron.

Strategy 1 — Copy Trading (every run, HIGHEST PRIORITY)
  - Monitors top-scored wallets (refreshed daily by wallet_scanner.py)
  - Pinned wallets (PINNED_WALLETS in .env) are copied without Claude validation
  - Regular wallets require Claude to independently agree before copying
  - Dynamic sizing: 3% of balance ($5-$100 cap)

Strategy 2 — Sports Arbitrage (every 15 min)
  - Scans sports markets closing within 72h
  - Compares Polymarket prices against bookmaker consensus via The Odds API
  - Executes automatically when edge > 5% (speed is the advantage)
  - Smaller sizing: 2% of balance ($10-$75 cap)

Strategy 3 — Opportunity Scanning (every 30 min, requires user confirmation)
  - Fetches top 500 Polymarket markets by 24h volume
  - Runs top 50 candidates through Claude AI analysis
  - Requires edge >= 3% AND Claude confidence >= 70%
  - Half-Kelly sizing ($5 min, $200 max, 8% of balance cap)
  - Sends Telegram confirmation request before executing

Shared risk controls (all strategies):
  - Max open positions: MAX_OPEN_POSITIONS (config, default 12)
  - Daily loss guard: pauses all trading if portfolio drops >20% in a day
  - Min USDC balance: $5
"""

import sys, json, time, requests
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config
import market_fetcher
import market_analyzer
import state as state_module
import telegram_notify as tg
try:
    import kalshi_client
    KALSHI_ENABLED = True
except Exception:
    KALSHI_ENABLED = False

LOG_FILE          = Path(__file__).parent / "autonomous_trader.log"
DAILY_GUARD_FILE  = Path(__file__).parent / "daily_guard.json"
COPY_STATE_FILE        = Path(__file__).parent / "copy_state.json"
SCAN_STATE_FILE        = Path(__file__).parent / "scan_state.json"
SCAN_REJECT_FILE       = Path(__file__).parent / "scan_rejected.json"
STOP_LOSS_FILE         = Path(__file__).parent / "stop_losses.json"

SCAN_REJECT_HOURS      = 8   # don't re-suggest a skipped market for 8 hours

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
ODDS_API  = "https://api.the-odds-api.com/v4"

# ── Strategy 1: Copy trading tunables ─────────────────────────────────────────
# Pinned wallets (PINNED_WALLETS in .env) bypass Claude validation — trusted unconditionally.
# Regular wallets require Claude to independently agree before copying.
COPY_SIZE_PCT        = 0.03   # 3% of balance per copied trade
COPY_MIN_USDC        = 5.0
COPY_MAX_USDC        = 100.0
COPY_MAX_PRICE       = 0.93
COPY_MIN_PRICE       = 0.07
COPY_MIN_CLAUDE_PROB = 0.60

# ── Strategy 2: Sports Arbitrage tunables ──────────────────────────────────────
SPORTS_ARB_INTERVAL_MIN  = 15    # check every 15 min
SPORTS_ARB_ODDS_CACHE    = 60    # re-fetch bookmaker odds every 60 min (API quota)
SPORTS_ARB_MAX_HOURS     = 72    # only markets closing within 72h
SPORTS_ARB_MIN_EDGE      = 0.08  # 8% edge vs bookmaker consensus (raised from 5%)
SPORTS_ARB_SIZE_PCT      = 0.02  # 2% of balance per arb trade
SPORTS_ARB_MIN_USDC      = 10.0
SPORTS_ARB_MAX_USDC      = 75.0
SPORTS_ARB_BOOKMAKER_ONLY = True  # Never trade on claude-estimated odds (src=claude only allowed as fallback, not for execution)
SPORTS_ARB_MAX_YES_PRICE  = 0.80  # Don't BUY NO against a market priced >80% YES (too risky)
SPORTS_KEYWORDS = [
    'vs.', ' vs ', ' win ', 'cover', 'over/under', 'o/u', 'beats',
    'nba', 'nhl', 'mlb', 'nfl', 'ufc', 'lol:', 'cs2:', 'cblol',
    'soccer', 'football', 'basketball', 'baseball', 'hockey',
    'esport', 'playoff', 'series', 'champion', 'match', 'game',
]

# ── Strategy 3: Opportunity scanning tunables ──────────────────────────────────
MIN_EDGE          = 0.03
MIN_CLAUDE_PROB   = 0.70
MAX_TRADE_PCT     = 0.08
MIN_TRADE_USDC    = 5.0
MAX_TRADE_USDC    = 200.0
SCAN_INTERVAL_MIN = 30
SCAN_PAGES        = 5

# ── Shared risk controls ───────────────────────────────────────────────────────
DAILY_LOSS_LIMIT   = 0.20
REVIEW_AUTO_EXIT   = False  # Set True to auto-exit flagged positions; False = notify only


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ── Balance ───────────────────────────────────────────────────────────────────

def get_usdc_balance() -> float:
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    client = market_fetcher.get_clob_client()
    params = BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL,
        signature_type=config.SIGNATURE_TYPE,
    )
    b = client.get_balance_allowance(params)
    return float(b.get("balance", 0)) / 1e6


# ── Shared: portfolio value for daily guard ───────────────────────────────────

def get_open_positions_value() -> float:
    """Sum currentValue of all live positions from Polymarket data API.
    This includes manually placed trades and is accurate to current market price."""
    try:
        r = requests.get(
            f"{DATA_API}/positions",
            params={"user": config.POLYMARKET_PROXY_WALLET, "sizeThreshold": "0"},
            timeout=10,
        )
        if r.status_code != 200:
            return 0.0
        positions = r.json()
        total = sum(float(p.get("currentValue", 0)) for p in positions if isinstance(p, dict))
        return round(total, 2)
    except Exception:
        return 0.0


# ── Shared: daily loss guard ──────────────────────────────────────────────────

def check_daily_guard(current_balance: float) -> bool:
    """Return True if safe to trade. Tracks portfolio (cash + open positions)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    guard = {}
    if DAILY_GUARD_FILE.exists():
        try:
            guard = json.loads(DAILY_GUARD_FILE.read_text())
        except Exception:
            guard = {}

    open_value = get_open_positions_value()
    portfolio = current_balance + open_value

    if guard.get("date") != today:
        guard = {"date": today, "starting_portfolio": portfolio, "paused": False}
        DAILY_GUARD_FILE.write_text(json.dumps(guard, indent=2))
        log(f"Daily guard reset. Starting portfolio: ${portfolio:.2f} (cash ${current_balance:.2f} + positions ${open_value:.2f})")
        return True

    if guard.get("paused"):
        log("Daily loss guard active — all trading paused for today.")
        return False

    starting = guard.get("starting_portfolio", portfolio)
    daily_loss = starting - portfolio
    loss_pct = daily_loss / starting if starting > 0 else 0

    log(f"Portfolio: ${portfolio:.2f} (cash ${current_balance:.2f} + positions ~${open_value:.2f}) | Daily P&L: {-loss_pct:.1%}")

    if loss_pct >= DAILY_LOSS_LIMIT:
        guard["paused"] = True
        DAILY_GUARD_FILE.write_text(json.dumps(guard, indent=2))
        log(f"Daily loss limit hit ({loss_pct:.1%}). Pausing all trading today.")
        tg.send_message(
            config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
            f"*Autonomous Trader - Daily Pause*\nPortfolio down {loss_pct:.1%} today "
            f"(${daily_loss:.2f} loss). Both strategies paused until midnight UTC."
        )
        return False

    return True


# ── Shared: stop-loss registration ───────────────────────────────────────────

def register_stop_loss(token_id: str, entry_price: float, sl_pct: float, question: str, manual: bool = False):
    """Add a position to stop_losses.json so stop_loss_monitor.py can watch it.
    Manual positions are excluded — user manages their own stop-losses."""
    if manual or "[MANUAL]" in question:
        log(f"[SL] Skipping stop-loss for manual position: '{question[:55]}'")
        return
    try:
        if STOP_LOSS_FILE.exists():
            entries = json.loads(STOP_LOSS_FILE.read_text())
        else:
            entries = []
        # Avoid duplicate registrations for same token
        existing_tokens = {e.get("token_id") for e in entries}
        if token_id in existing_tokens:
            return
        stop_price = round(entry_price * (1 - sl_pct), 4)
        entries.append({
            "token_id": token_id,
            "entry_price": entry_price,
            "stop_loss_price": stop_price,
            "stop_loss_pct": sl_pct,
            "question": question[:80],
        })
        STOP_LOSS_FILE.write_text(json.dumps(entries, indent=2))
        log(f"[SL] Registered stop-loss: '{question[:55]}' stop={stop_price:.4f} (-{int(sl_pct*100)}%)")
    except Exception as e:
        log(f"[SL] Failed to register stop-loss: {e}")


# ── Shared: position sync + slots ────────────────────────────────────────────

def sync_positions():
    """
    Sync state.json against actual Polymarket positions.
    - Removes positions that were sold/resolved (size < 0.001)
    - Imports new positions found on Polymarket but not in state (manually added)
      Manual positions are tagged [MANUAL] and excluded from stop-loss tracking.
    Runs every cycle so slots reflect reality immediately.
    """
    st = state_module.load_state()
    positions = st.get("positions", {})

    try:
        r = requests.get(
            f"{DATA_API}/positions",
            params={"user": config.POLYMARKET_PROXY_WALLET, "sizeThreshold": "0"},
            timeout=10,
        )
        if r.status_code != 200:
            return
        live = r.json()
        live_tokens = {p.get("asset") or p.get("token_id"): float(p.get("size") or 0)
                       for p in live if isinstance(p, dict)}
    except Exception as e:
        log(f"[SYNC] Could not fetch live positions: {e}")
        return

    # ── Remove closed positions ───────────────────────────────────────────────
    removed = []
    for order_id, pos in list(positions.items()):
        token_id = pos.get("token_id", "")
        live_size = live_tokens.get(token_id, 0)
        if live_size < 0.001:
            removed.append((order_id, pos.get("question", "")[:55]))
            del positions[order_id]

    # ── Import manually added positions not yet in state ──────────────────────
    tracked_tokens = {pos.get("token_id", "") for pos in positions.values()}
    added = []
    for p in live:
        tid = p.get("asset") or p.get("token_id", "")
        if not tid or tid not in live_tokens or live_tokens[tid] < 0.001:
            continue
        if tid in tracked_tokens:
            continue
        # New position found on Polymarket not tracked in state — must be manual
        title   = str(p.get("title") or "")
        outcome = str(p.get("outcome") or "YES").upper()
        question = f"{title} (BUY {outcome}) [MANUAL]"
        key = f"manual_{tid[:16]}"
        positions[key] = {
            "token_id":    tid,
            "question":    question,
            "side":        "BUY",
            "entry_price": float(p.get("avgPrice") or 0.5),
            "size":        float(p.get("size") or 0),
            "usdc_spent":  float(p.get("initialValue") or 0),
            "strategy":    "MANUAL",
            "manual":      True,   # flag: exclude from stop-loss tracking
        }
        tracked_tokens.add(tid)
        added.append(question[:60])

    changed = removed or added
    if changed:
        state_module.save_state(st)
        for oid, q in removed:
            log(f"[SYNC] Cleared closed position: '{q}'")
        if removed:
            log(f"[SYNC] Freed {len(removed)} slot(s)")
        for q in added:
            log(f"[SYNC] Imported manual position: '{q}'")
    else:
        log(f"[SYNC] All {len(positions)} positions still active")


def get_available_slots() -> int:
    st = state_module.load_state()
    open_count = len(st.get("positions", {}))
    max_pos = config.MAX_OPEN_POSITIONS
    slots = max_pos - open_count
    log(f"Open positions: {open_count}/{max_pos} — {max(0, slots)} slot(s) available")
    return max(0, slots)


def already_holds_market(*token_ids: str) -> bool:
    """True if we already hold ANY of the given token_ids (checks live API, not just state)."""
    try:
        r = requests.get(
            f"{DATA_API}/positions",
            params={"user": config.POLYMARKET_PROXY_WALLET, "sizeThreshold": "0.01"},
            timeout=8,
        )
        live = {p["asset"] for p in r.json() if isinstance(p, dict) and p.get("asset")}
        return any(tid in live for tid in token_ids)
    except Exception:
        # Fall back to state.json if API fails
        st = state_module.load_state()
        held = {pos.get("token_id") for pos in st.get("positions", {}).values()}
        return any(tid in held for tid in token_ids)


# ── Strategy 1: Copy Trading ──────────────────────────────────────────────────

def load_copy_state() -> dict:
    if COPY_STATE_FILE.exists():
        try:
            return json.loads(COPY_STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_copy_state(state: dict):
    COPY_STATE_FILE.write_text(json.dumps(state, indent=2))


def get_recent_trades(wallet: str, limit: int = 20) -> list:
    try:
        r = requests.get(
            f"{DATA_API}/activity",
            params={"user": wallet, "limit": limit},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else data.get("data", [])
    except Exception as e:
        log(f"  Error fetching trades for {wallet[:10]}: {e}")
    return []


def is_market_open(condition_id: str) -> bool:
    try:
        r = requests.get(
            f"{GAMMA_API}/markets",
            params={"conditionId": condition_id},
            timeout=10,
        )
        data = r.json()
        m = data[0] if isinstance(data, list) and data else {}
        return m.get("acceptingOrders", False) and not m.get("closed", True)
    except Exception:
        pass
    return False


def fetch_market_data(condition_id: str) -> dict | None:
    """Fetch full market object from Gamma API for Claude analysis."""
    try:
        r = requests.get(
            f"{GAMMA_API}/markets",
            params={"conditionId": condition_id},
            timeout=10,
        )
        data = r.json()
        return data[0] if isinstance(data, list) and data else None
    except Exception as e:
        log(f"  [COPY] Failed to fetch market data: {e}")
    return None


def validate_copy_trade(market: dict, token_id: str, outcome: str, mid_yes: float) -> tuple[bool, dict | None]:
    """
    Run Claude analysis on a potential copy trade.
    Returns (should_copy, analysis) where analysis contains probability and reasoning.
    We trust the copied trader's direction but only execute if Claude independently agrees.
    """
    try:
        analysis = market_analyzer.analyze_market(market, mid_yes, None)
        if analysis is None:
            log("  [COPY] Claude analysis returned None — skipping")
            return False, None

        claude_p   = analysis["probability"]
        confidence = analysis["confidence"]
        reasoning  = analysis["reasoning"][:120]

        if confidence == "low":
            log(f"  [COPY] Claude low confidence — skipping | reasoning: {reasoning}")
            return False, analysis

        if outcome.upper() == "YES":
            edge = claude_p - mid_yes
            agrees = claude_p >= COPY_MIN_CLAUDE_PROB and edge > 0
            log(f"  [COPY] Claude: YES prob={claude_p:.3f} market={mid_yes:.3f} edge={edge:+.3f} — {'AGREE' if agrees else 'DISAGREE'}")
        else:
            # Trader bought NO token — Claude must see low YES probability
            no_prob = 1.0 - claude_p
            no_market = 1.0 - mid_yes
            edge = no_prob - no_market
            agrees = no_prob >= COPY_MIN_CLAUDE_PROB and edge > 0
            log(f"  [COPY] Claude: NO prob={no_prob:.3f} market={no_market:.3f} edge={edge:+.3f} — {'AGREE' if agrees else 'DISAGREE'}")

        if not agrees:
            log(f"  [COPY] Claude disagrees with trade — skipping | {reasoning}")
            return False, analysis

        return True, analysis

    except Exception as e:
        log(f"  [COPY] Analysis error: {e} — skipping to be safe")
        return False, None


def run_copy_strategy(balance: float, slots: int) -> int:
    """Mirror new BUY trades from watched wallets. Returns number of trades placed."""
    wallets_raw = config.COPY_WALLETS
    if not wallets_raw:
        log("[COPY] No COPY_WALLETS configured.")
        return 0

    wallets = [w.strip() for w in wallets_raw.split(",") if w.strip()]
    copy_state = load_copy_state()
    copy_size = round(max(COPY_MIN_USDC, min(balance * COPY_SIZE_PCT, COPY_MAX_USDC)), 2)
    log(f"[COPY] Checking {len(wallets)} wallet(s) | copy size: ${copy_size:.2f}")

    trades_placed = 0

    for wallet in wallets:
        if trades_placed >= slots:
            break

        trades = get_recent_trades(wallet)
        if not trades:
            continue

        seen_ids = set(copy_state.get(wallet, {}).get("seen_ids", []))
        new_trades = []
        for trade in trades:
            trade_id = (trade.get("transactionHash") or
                        f"{trade.get('timestamp', '')}_{trade.get('asset', '')}")
            if trade_id and trade_id not in seen_ids:
                new_trades.append((trade_id, trade))

        for trade_id, trade in new_trades:
            seen_ids.add(trade_id)

            side = (trade.get("side") or "").upper()
            if side != "BUY":
                continue

            token_id  = trade.get("asset")
            cond_id   = trade.get("conditionId", "")
            question  = trade.get("title", "")
            outcome   = trade.get("outcome", "YES")

            if not token_id:
                continue

            # Price sanity check — skip if near resolution
            mid = market_fetcher.get_market_midpoint(token_id)
            if mid is not None and (mid > COPY_MAX_PRICE or mid < COPY_MIN_PRICE):
                log(f"  [COPY] Skip '{question[:45]}' — price {mid:.3f} out of range")
                continue

            # Don't double-up on same market
            if already_holds_market(token_id):
                log(f"  [COPY] Skip '{question[:45]}' — already holding")
                continue

            if not is_market_open(cond_id):
                log(f"  [COPY] Skip '{question[:45]}' — market closed")
                continue

            if trades_placed >= slots:
                log("  [COPY] No more slots available.")
                break

            # ── Claude validation gate (skipped for pinned wallets) ──────────
            pinned_raw = config.COPY_WALLETS  # reuse config attr; pinned stored separately
            pinned_wallets = [w.strip().lower() for w in
                              __import__('os').getenv('PINNED_WALLETS','').split(',') if w.strip()]
            is_pinned = wallet.lower() in pinned_wallets

            if is_pinned:
                log(f"  [COPY] Pinned wallet — skipping Claude validation, copying directly.")
                analysis = None
            else:
                log(f"  [COPY] Validating '{question[:50]}' ({outcome}) with Claude...")
                market_data = fetch_market_data(cond_id)
                if market_data is None:
                    log(f"  [COPY] Could not fetch market data — skipping")
                    continue
                should_copy, analysis = validate_copy_trade(market_data, token_id, outcome, mid or 0.5)
                if not should_copy:
                    continue
            # ─────────────────────────────────────────────────────────────────

            log(f"  [COPY] Copying BUY {outcome} on '{question[:50]}' | ${copy_size}")
            order_id = market_fetcher.place_market_order(
                token_id=token_id,
                size_usdc=copy_size,
                side="BUY",
            )

            if order_id:
                entry = mid or 0.5
                sl_pct = market_fetcher.get_stop_loss_pct(entry)
                st = state_module.load_state()
                st = state_module.add_position(
                    st, order_id=order_id,
                    market_id=cond_id,
                    token_id=token_id,
                    question=f"{question} ({outcome}) [COPY]",
                    entry_price=entry,
                    size_usdc=copy_size,
                    side="BUY",
                )
                state_module.save_state(st)
                register_stop_loss(token_id, entry, sl_pct, f"{question} ({outcome}) [COPY]")

                reasoning = analysis["reasoning"][:120] if analysis else ""
                msg = (
                    f"*Copy Trade Executed*\n"
                    f"Copied: `{wallet[:12]}...`\n"
                    f"Market: {question[:70]}\n"
                    f"Outcome: {outcome} | Amount: ${copy_size:.2f}\n"
                    f"Entry: {entry:.3f} | Stop: {round(entry*(1-sl_pct),4)} (-{int(sl_pct*100)}%)\n"
                    f"Claude: {analysis['probability']:.3f} confidence | {reasoning}"
                )
                tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, msg)
                log(f"    Order placed: {order_id}")
                trades_placed += 1
                slots -= 1
            else:
                log(f"    [COPY] Order FAILED for '{question[:45]}'")

        copy_state[wallet] = {"seen_ids": list(seen_ids)[-200:]}

    save_copy_state(copy_state)
    return trades_placed


# ── Strategy 2: Sports Arbitrage ──────────────────────────────────────────────

SPORTS_ARB_STATE_FILE = Path(__file__).parent / "sports_arb_state.json"

def load_arb_state() -> dict:
    if SPORTS_ARB_STATE_FILE.exists():
        try:
            return json.loads(SPORTS_ARB_STATE_FILE.read_text())
        except Exception:
            pass
    return {}

def save_arb_state(state: dict):
    SPORTS_ARB_STATE_FILE.write_text(json.dumps(state, indent=2))

def should_run_sports_arb() -> bool:
    state = load_arb_state()
    last = state.get("last_arb_ts", 0)
    elapsed = (datetime.now(timezone.utc).timestamp() - last) / 60
    return elapsed >= SPORTS_ARB_INTERVAL_MIN

def mark_arb_done():
    state = load_arb_state()
    state["last_arb_ts"] = datetime.now(timezone.utc).timestamp()
    save_arb_state(state)

def fetch_bookmaker_odds() -> list:
    """Fetch live odds from The Odds API (caches for SPORTS_ARB_ODDS_CACHE min)."""
    arb_state = load_arb_state()
    cache_ts = arb_state.get("odds_cache_ts", 0)
    cached   = arb_state.get("odds_cache", [])
    elapsed  = (datetime.now(timezone.utc).timestamp() - cache_ts) / 60

    if cached and elapsed < SPORTS_ARB_ODDS_CACHE:
        log(f"[SARB] Using cached odds ({elapsed:.0f}m old, refresh at {SPORTS_ARB_ODDS_CACHE}m)")
        return cached

    api_key = __import__('os').getenv("ODDS_API_KEY", "")
    if not api_key:
        log("[SARB] No ODDS_API_KEY set — skipping bookmaker comparison")
        return []

    sports = ["basketball_nba", "icehockey_nhl", "baseball_mlb",
              "soccer_epl", "soccer_uefa_champs_league", "soccer_spain_la_liga",
              "soccer_italy_serie_a", "soccer_germany_bundesliga", "soccer_france_ligue_one",
              "americanfootball_nfl", "mma_mixed_martial_arts"]

    all_odds = []
    for sport in sports:
        try:
            r = requests.get(
                f"{ODDS_API}/sports/{sport}/odds/",
                params={"apiKey": api_key, "regions": "us,eu", "markets": "h2h", "oddsFormat": "decimal"},
                timeout=10,
            )
            if r.status_code == 200:
                all_odds.extend(r.json())
            elif r.status_code == 422:
                continue  # sport not available
        except Exception as e:
            log(f"[SARB] Odds fetch error for {sport}: {e}")

    log(f"[SARB] Fetched {len(all_odds)} bookmaker events from The Odds API")
    arb_state["odds_cache"] = all_odds
    arb_state["odds_cache_ts"] = datetime.now(timezone.utc).timestamp()
    save_arb_state(arb_state)
    return all_odds

def is_ou_market(question: str) -> bool:
    """True if this is an over/under total market (can't compare to h2h odds)."""
    q = question.lower()
    return any(x in q for x in ["o/u", "over/under", "over under", "total goals", "total points"])

def extract_teams(question: str) -> tuple[str, str]:
    """Extract home/away team names from a Polymarket question."""
    import re
    q = question.lower()
    # "Team A vs. Team B" — strip trailing context like ": O/U 2.5"
    m = re.search(r"(.+?)\s+vs\.?\s+(.+?)(?:\s*[:\-\|]|$|\?)", q)
    if m:
        team_a = re.sub(r"(:\s*.+|o/u.+|over.+|spread.+)$", "", m.group(1)).strip()
        team_b = re.sub(r"(:\s*.+|o/u.+|over.+|spread.+)$", "", m.group(2)).strip()
        return team_a, team_b
    # "Will [Team] win on DATE" or "Will [Team] win?"
    m2 = re.search(r"will\s+(.+?)\s+(win|beat|defeat|score)", q)
    if m2:
        team = re.sub(r"\s+(fc|cf|sc|ac|afc|bc|united|city|on \d{4}.*)$", "", m2.group(1)).strip()
        return team, ""
    return "", ""

def bookmaker_implied_prob(event: dict, team_hint: str) -> float | None:
    """Get consensus implied probability for a team from bookmaker odds."""
    team_hint = team_hint.lower()
    outcomes_data = []

    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for outcome in market.get("outcomes", []):
                name = outcome.get("name", "").lower()
                price = outcome.get("price", 0)
                if price > 1 and any(part in name for part in team_hint.split()):
                    # Convert decimal odds to implied prob
                    impl_prob = 1.0 / price
                    outcomes_data.append(impl_prob)

    if not outcomes_data:
        return None
    return round(sum(outcomes_data) / len(outcomes_data), 4)

def match_to_bookmaker(question: str, all_odds: list) -> tuple[float | None, str]:
    """Match a Polymarket question to a bookmaker event. Returns (implied_prob, matched_team)."""
    team_a, team_b = extract_teams(question)
    if not team_a:
        return None, ""

    q_lower = question.lower()
    best_match = None
    best_score = 0

    for event in all_odds:
        home = (event.get("home_team") or "").lower()
        away = (event.get("away_team") or "").lower()

        # Score the match quality
        score = 0
        if team_a and any(part in home or part in away for part in team_a.split() if len(part) > 3):
            score += 2
        if team_b and any(part in home or part in away for part in team_b.split() if len(part) > 3):
            score += 2
        # Also check if key words from question appear in event name
        if any(part in (home + " " + away) for part in q_lower.split() if len(part) > 4):
            score += 1

        if score > best_score:
            best_score = score
            best_match = event

    if not best_match or best_score < 2:
        return None, ""

    # Determine which team we're betting on
    home = (best_match.get("home_team") or "").lower()
    away = (best_match.get("away_team") or "").lower()
    target_team = home if team_a and any(p in home for p in team_a.split() if len(p) > 3) else away

    prob = bookmaker_implied_prob(best_match, target_team)
    return prob, target_team

def scan_sports_markets() -> list:
    """Find sports markets on Polymarket closing within SPORTS_ARB_MAX_HOURS."""
    now = datetime.now(timezone.utc)
    candidates = []

    for offset in [0, 100, 200]:
        try:
            r = requests.get(GAMMA_API + "/markets", params={
                "active": "true", "closed": "false",
                "order": "volume24hr", "ascending": "false",
                "limit": 100, "offset": offset,
            }, timeout=15)
            batch = r.json()
            if not batch:
                break
            for m in batch:
                if not m.get("active") or m.get("closed") or not m.get("acceptingOrders"):
                    continue
                end = m.get("endDate") or ""
                try:
                    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                    hrs = (end_dt - now).total_seconds() / 3600
                    if not (0.5 < hrs <= SPORTS_ARB_MAX_HOURS):
                        continue
                except Exception:
                    continue

                q = m.get("question", "").lower()
                if not any(kw in q for kw in SPORTS_KEYWORDS):
                    continue

                clob_ids = m.get("clobTokenIds", [])
                if isinstance(clob_ids, str):
                    clob_ids = json.loads(clob_ids)
                if not clob_ids:
                    continue

                yes_token = clob_ids[0]
                mid = market_fetcher.get_market_midpoint(yes_token)
                if mid is None or mid > COPY_MAX_PRICE or mid < COPY_MIN_PRICE:
                    continue

                m["_yes_token"] = yes_token
                m["_yes_mid"]   = mid
                m["_hours_left"] = round(hrs, 1)
                candidates.append(m)
        except Exception as e:
            log(f"[SARB] Market fetch error: {e}")
            break

    return candidates

def run_sports_arb_strategy(balance: float, slots: int) -> int:
    """
    Sports arbitrage: compare Polymarket prices against bookmaker consensus.
    Auto-executes when edge > SPORTS_ARB_MIN_EDGE. No user confirmation.
    Returns number of trades placed.
    """
    log("[SARB] Running sports arbitrage scan...")
    mark_arb_done()

    sports_markets = scan_sports_markets()
    log(f"[SARB] Found {len(sports_markets)} sports market(s) closing in <{SPORTS_ARB_MAX_HOURS}h")

    if not sports_markets:
        return 0

    all_odds = fetch_bookmaker_odds()
    has_external_odds = len(all_odds) > 0

    trades_placed = 0
    arb_size = round(max(SPORTS_ARB_MIN_USDC, min(balance * SPORTS_ARB_SIZE_PCT, SPORTS_ARB_MAX_USDC)), 2)

    for m in sports_markets:
        if trades_placed >= slots:
            break

        question  = m.get("question", "")
        yes_token = m["_yes_token"]
        mid       = m["_yes_mid"]
        hrs       = m["_hours_left"]

        # Get both YES and NO tokens for this market
        clob_ids = m.get("clobTokenIds", [])
        if isinstance(clob_ids, str):
            clob_ids = json.loads(clob_ids)
        no_token = clob_ids[1] if len(clob_ids) > 1 else None

        # Skip if we already hold YES or NO side of this market
        tokens_to_check = [t for t in [yes_token, no_token] if t]
        if already_holds_market(*tokens_to_check):
            log(f"  [SARB] Skip '{question[:50]}' — already holding this market")
            continue

        edge       = 0.0
        trade_side = None
        token_id   = None
        book_prob  = None
        source     = "claude"

        # ── External odds comparison (skip O/U markets — need totals not h2h) ──
        if has_external_odds and not is_ou_market(question):
            book_prob, matched_team = match_to_bookmaker(question, all_odds)
            if book_prob is not None:
                source = f"bookmaker ({matched_team})"
                if book_prob - mid > SPORTS_ARB_MIN_EDGE:
                    edge       = book_prob - mid
                    trade_side = "YES"
                    token_id   = yes_token
                elif (1 - book_prob) - (1 - mid) > SPORTS_ARB_MIN_EDGE:
                    clob_ids = m.get("clobTokenIds", [])
                    if isinstance(clob_ids, str):
                        clob_ids = json.loads(clob_ids)
                    edge       = (1 - book_prob) - (1 - mid)
                    trade_side = "NO"
                    token_id   = clob_ids[1] if len(clob_ids) > 1 else yes_token

        # ── Claude fallback (no external odds or no match found) ───────────────
        if trade_side is None:
            analysis = market_analyzer.analyze_market(m, mid, None)
            if analysis is None or analysis.get("confidence") == "low":
                continue
            claude_p = analysis["probability"]
            source   = "claude"
            if claude_p - mid > SPORTS_ARB_MIN_EDGE:
                edge       = claude_p - mid
                trade_side = "YES"
                token_id   = yes_token
                book_prob  = claude_p
            elif (1 - claude_p) - (1 - mid) > SPORTS_ARB_MIN_EDGE:
                clob_ids = m.get("clobTokenIds", [])
                if isinstance(clob_ids, str):
                    clob_ids = json.loads(clob_ids)
                edge       = (1 - claude_p) - (1 - mid)
                trade_side = "NO"
                token_id   = clob_ids[1] if len(clob_ids) > 1 else yes_token
                book_prob  = 1 - claude_p

        if trade_side is None or token_id is None or edge < SPORTS_ARB_MIN_EDGE:
            continue

        # ── Guard 1: Only execute on real bookmaker odds, not Claude estimates ──
        if SPORTS_ARB_BOOKMAKER_ONLY and source == "claude":
            log(f"  [SARB] Skipping '{question[:50]}' — src=claude (no real bookmaker match found)")
            continue

        # ── Guard 2: Don't bet NO against an extreme favourite (>80% YES) ───────
        if trade_side == "NO" and mid > SPORTS_ARB_MAX_YES_PRICE:
            log(f"  [SARB] Skipping BUY NO '{question[:50]}' — market={mid:.3f} > {SPORTS_ARB_MAX_YES_PRICE} ceiling (too risky)")
            continue

        # ── Kalshi cross-check (required signal) ──────────────────────────────
        # If Kalshi has a matching market, it must agree with our trade direction.
        # Any disagreement > 3% cancels the trade. If Kalshi is unavailable or
        # has no match, we proceed (bookmaker edge is sufficient on its own).
        kalshi_signal = ""
        if KALSHI_ENABLED:
            try:
                kalshi_markets = kalshi_client.get_kalshi_markets()
                match = kalshi_client.find_kalshi_match(question, kalshi_markets)
                if match:
                    k_mid = match["mid"]
                    divergence = k_mid - mid
                    direction = "agrees ✅" if abs(divergence) < 0.03 else ("higher 📈" if divergence > 0 else "lower 📉")
                    kalshi_signal = f"Kalshi: {k_mid:.3f} ({direction}, \u0394{divergence:+.3f})\n"
                    log(f"  [KALSHI] SARB match: '{match['title'][:50]}' mid={k_mid:.3f} divergence={divergence:+.3f}")
                    if trade_side == "YES" and k_mid < mid - 0.03:
                        log(f"  [SARB] Skipping — Kalshi ({k_mid:.3f}) disagrees with BUY YES (market={mid:.3f})")
                        continue
                    if trade_side == "NO" and k_mid > mid + 0.03:
                        log(f"  [SARB] Skipping — Kalshi ({k_mid:.3f}) disagrees with BUY NO (market={mid:.3f})")
                        continue
                else:
                    log(f"  [KALSHI] No match found for '{question[:50]}' — proceeding on bookmaker edge only")
            except Exception as e:
                log(f"  [KALSHI] Lookup failed: {e}")

        log(f"  [SARB] Edge found: BUY {trade_side} '{question[:50]}' "
            f"| market={mid:.3f} book={book_prob:.3f} edge=+{edge:.3f} | src={source}")

        order_id = market_fetcher.place_market_order(
            token_id=token_id,
            size_usdc=arb_size,
            side="BUY",
        )

        if order_id:
            entry  = market_fetcher.get_market_midpoint(token_id) or mid
            sl_pct = market_fetcher.get_stop_loss_pct(entry)

            st = state_module.load_state()
            st = state_module.add_position(
                st, order_id=order_id,
                market_id=m.get("conditionId", ""),
                token_id=token_id,
                question=f"{question} (BUY {trade_side}) [SARB]",
                entry_price=entry,
                size_usdc=arb_size,
                side="BUY",
            )
            state_module.save_state(st)
            register_stop_loss(token_id, entry, sl_pct, f"{question} (BUY {trade_side}) [SARB]")

            msg = (
                f"*Sports Arb Trade*\n"
                f"Market: {question[:70]}\n"
                f"Action: BUY {trade_side} | Amount: ${arb_size:.2f}\n"
                f"Entry: {entry:.3f} | Book: {book_prob:.3f} | Edge: +{edge:.3f}\n"
                f"{kalshi_signal}"
                f"Source: {source} | Closes in: {hrs}h"
            )
            tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, msg)
            log(f"    Order placed: {order_id}")
            trades_placed += 1
            slots -= 1
        else:
            log(f"    [SARB] Order FAILED for '{question[:50]}'")

    return trades_placed


# ── Strategy 3: Opportunity Scanning ──────────────────────────────────────────

def should_run_scan() -> bool:
    """True if >= SCAN_INTERVAL_MIN minutes since last scan."""
    if not SCAN_STATE_FILE.exists():
        return True
    try:
        data = json.loads(SCAN_STATE_FILE.read_text())
        last = data.get("last_scan_ts", 0)
        elapsed_min = (datetime.now(timezone.utc).timestamp() - last) / 60
        return elapsed_min >= SCAN_INTERVAL_MIN
    except Exception:
        return True


def mark_scan_done():
    SCAN_STATE_FILE.write_text(json.dumps({
        "last_scan_ts": datetime.now(timezone.utc).timestamp()
    }))


def kelly_size(balance: float, market_price: float, claude_prob: float) -> float:
    if market_price <= 0 or market_price >= 1 or claude_prob <= market_price:
        return 0.0
    b = (1.0 / market_price) - 1.0
    q = 1.0 - claude_prob
    kelly_frac = (claude_prob * b - q) / b
    half_kelly = kelly_frac / 2.0
    raw = balance * max(0.0, half_kelly)
    sized = max(MIN_TRADE_USDC, min(raw, balance * MAX_TRADE_PCT, MAX_TRADE_USDC))
    return round(sized, 2)


def scan_opportunities() -> list:
    now = datetime.now(timezone.utc)
    all_markets = []

    for offset in [i * 100 for i in range(SCAN_PAGES)]:
        try:
            r = requests.get(GAMMA_API + "/markets", params={
                "active": "true", "closed": "false",
                "order": "volume24hr", "ascending": "false",
                "limit": 100, "offset": offset,
            }, timeout=15)
            batch = r.json()
            if not batch:
                break
            all_markets.extend(batch)
        except Exception as e:
            log(f"[SCAN] Market fetch error at offset {offset}: {e}")
            break

    candidates = []
    for m in all_markets:
        if not m.get("active") or m.get("closed"):
            continue
        if not m.get("acceptingOrders"):
            continue

        end = m.get("endDate") or ""
        try:
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            hours_left = (end_dt - now).total_seconds() / 3600
            if not (1 < hours_left <= 720):
                continue
        except Exception:
            continue

        clob_ids = m.get("clobTokenIds", [])
        if isinstance(clob_ids, str):
            clob_ids = json.loads(clob_ids)
        if not clob_ids:
            continue

        yes_token = clob_ids[0]
        mid = market_fetcher.get_market_midpoint(yes_token)
        if mid is None:
            continue

        if 0.55 <= mid <= 0.95:
            m["_yes_token"] = yes_token
            m["_yes_mid"] = mid
            m["_side"] = "YES"
            m["_hours_left"] = round(hours_left, 1)
            candidates.append(m)
        elif 0.05 <= mid <= 0.45:
            m["_yes_token"] = yes_token
            m["_yes_mid"] = mid
            m["_side"] = "NO"
            m["_hours_left"] = round(hours_left, 1)
            candidates.append(m)

    if not candidates:
        return []

    candidates.sort(key=lambda x: abs(x["_yes_mid"] - 0.5), reverse=True)
    log(f"[SCAN] Candidates: {len(candidates)} — analysing top {min(50, len(candidates))}")

    # Pre-load Kalshi market list once for the whole scan cycle
    kalshi_markets = []
    if KALSHI_ENABLED:
        try:
            kalshi_markets = kalshi_client.get_kalshi_markets()
            log(f"[SCAN] Kalshi oracle loaded: {len(kalshi_markets)} liquid markets")
        except Exception as e:
            log(f"[SCAN] Kalshi unavailable: {e}")

    opportunities = []
    for m in candidates[:50]:
        mid = m["_yes_mid"]
        question = m.get("question", "")

        # ── Kalshi cross-platform price lookup ────────────────────────────────
        kalshi_info = None
        if KALSHI_ENABLED and kalshi_markets:
            try:
                match = kalshi_client.find_kalshi_match(question, kalshi_markets)
                if match:
                    kalshi_info = match
                    log(f"  [KALSHI] Matched '{question[:40]}' → '{match['title'][:40]}' mid={match['mid']:.3f} (score={match['match_score']})")
            except Exception:
                pass

        analysis = market_analyzer.analyze_market(m, mid, None, kalshi_info=kalshi_info)
        if analysis is None:
            continue
        if analysis["confidence"] == "low":
            continue

        claude_p = analysis["probability"]
        side = m["_side"]

        if side == "YES":
            edge = claude_p - mid
            if edge < MIN_EDGE or claude_p < MIN_CLAUDE_PROB:
                continue
            token_id = m["_yes_token"]
            trade_price = mid
            action = "BUY YES"
        else:
            no_market = 1 - mid
            no_claude = 1 - claude_p
            edge = no_claude - no_market
            if edge < MIN_EDGE or no_claude < MIN_CLAUDE_PROB:
                continue
            clob_ids = m.get("clobTokenIds", [])
            if isinstance(clob_ids, str):
                clob_ids = json.loads(clob_ids)
            token_id = clob_ids[1] if len(clob_ids) > 1 else m["_yes_token"]
            trade_price = round(1 - mid, 3)
            action = "BUY NO"
            claude_p = no_claude

        log(f"  [SCAN] Edge: {action} '{question[:45]}' market={mid:.3f} claude={analysis['probability']:.3f} edge=+{edge:.3f}")
        opportunities.append({
            "market": m, "analysis": analysis, "edge": edge,
            "action": action, "trade_price": trade_price,
            "token_id": token_id, "claude_prob": claude_p,
            "kalshi_info": kalshi_info,
        })

    opportunities.sort(key=lambda x: x["edge"], reverse=True)
    return opportunities[:5]


def _load_scan_rejected() -> dict:
    """Return {token_id: rejected_ts} for recently skipped markets."""
    try:
        if SCAN_REJECT_FILE.exists():
            return json.loads(SCAN_REJECT_FILE.read_text())
    except Exception:
        pass
    return {}


def _mark_scan_rejected(token_id: str):
    """Record that the user just skipped this token. Expires after SCAN_REJECT_HOURS."""
    rejected = _load_scan_rejected()
    now = datetime.now(timezone.utc).timestamp()
    # Prune expired entries while we're here
    rejected = {tid: ts for tid, ts in rejected.items()
                if now - ts < SCAN_REJECT_HOURS * 3600}
    rejected[token_id] = now
    SCAN_REJECT_FILE.write_text(json.dumps(rejected, indent=2))


def _is_scan_rejected(token_id: str) -> bool:
    """True if this token was recently skipped by the user."""
    rejected = _load_scan_rejected()
    ts = rejected.get(token_id)
    if ts is None:
        return False
    elapsed_h = (datetime.now(timezone.utc).timestamp() - ts) / 3600
    return elapsed_h < SCAN_REJECT_HOURS


def run_scan_strategy(balance: float, slots: int) -> int:
    """Scan for opportunities and trade the best ones. Returns trades placed."""
    log("[SCAN] Running opportunity scan...")
    opportunities = scan_opportunities()
    mark_scan_done()
    log(f"[SCAN] Found {len(opportunities)} opportunity(ies) with edge >= {MIN_EDGE:.0%}")

    if not opportunities:
        return 0

    trades_placed = 0
    for opp in opportunities:
        if trades_placed >= slots:
            break

        m = opp["market"]
        question = m.get("question", "")
        action = opp["action"]
        trade_price = opp["trade_price"]
        claude_p = opp["claude_prob"]
        edge = opp["edge"]
        token_id = opp["token_id"]

        if already_holds_market(token_id):
            log(f"  [SCAN] Skip '{question[:45]}' — already holding")
            continue

        if _is_scan_rejected(token_id):
            log(f"  [SCAN] Skip '{question[:45]}' — recently rejected (cooldown {SCAN_REJECT_HOURS}h)")
            continue

        size = kelly_size(balance, trade_price, claude_p)
        if size < MIN_TRADE_USDC:
            log(f"  [SCAN] Skip '{question[:45]}' — Kelly size ${size:.2f} too small")
            continue

        kalshi_info = opp.get("kalshi_info")
        log(f"  [SCAN] Opportunity found: {action} on '{question[:55]}'")
        log(f"  [SCAN] Price={trade_price:.3f} | Claude={claude_p:.3f} | Edge=+{edge:.3f} | Size=${size:.2f}")
        if kalshi_info:
            divergence = kalshi_info["mid"] - trade_price
            log(f"  [KALSHI] Signal: {kalshi_info['title'][:50]} | Kalshi={kalshi_info['mid']:.3f} | Divergence={divergence:+.3f}")

        # ── User confirmation via Telegram ────────────────────────────────────
        sl_pct_preview = market_fetcher.get_stop_loss_pct(trade_price)
        kalshi_line = ""
        if kalshi_info:
            divergence = kalshi_info["mid"] - trade_price
            direction = "agrees ✅" if abs(divergence) < 0.05 else ("higher 📈" if divergence > 0 else "lower 📉")
            kalshi_line = f"Kalshi: {kalshi_info['mid']:.3f} ({direction}, Δ{divergence:+.3f}) — {kalshi_info['title'][:50]}\n"
        confirm_msg = (
            f"*Scanned Opportunity — Confirm Trade?*\n\n"
            f"Market: {question[:70]}\n"
            f"Action: *{action}*\n"
            f"Amount: ${size:.2f} (Kelly-sized)\n"
            f"Entry: {trade_price:.3f} | Claude: {claude_p:.3f} | Edge: +{edge:.3f}\n"
            f"{kalshi_line}"
            f"Stop-loss: ~{int(sl_pct_preview*100)}% tier\n"
            f"Closes in: {m['_hours_left']}h\n\n"
            f"Reasoning: {opp['analysis']['reasoning'][:150]}\n\n"
            f"Reply *yes* to execute or *no* to skip. (30 min timeout — no reply = skip)"
        )
        sent_at = int(time.time())
        tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, confirm_msg)
        log(f"  [SCAN] Waiting for confirmation via Telegram (30 min)...")

        reply = tg.poll_for_reply(
            config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
            sent_at=sent_at, wait_seconds=1800
        )

        if not reply or reply.lower().strip() not in ("yes", "y"):
            log(f"  [SCAN] Trade skipped — reply: '{reply}'")
            _mark_scan_rejected(token_id)
            tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
                f"Trade skipped: {question[:60]}\n_Will not suggest again for {SCAN_REJECT_HOURS}h._")
            continue

        log(f"  [SCAN] Confirmed — executing trade.")
        # ─────────────────────────────────────────────────────────────────────

        order_id = market_fetcher.place_market_order(
            token_id=token_id,
            size_usdc=size,
            side="BUY",
        )

        if order_id:
            mid = market_fetcher.get_market_midpoint(token_id) or trade_price
            sl_pct = market_fetcher.get_stop_loss_pct(mid)
            stop_price = round(mid * (1 - sl_pct), 4)

            st = state_module.load_state()
            st = state_module.add_position(
                st, order_id=order_id,
                market_id=m.get("conditionId", ""),
                token_id=token_id,
                question=f"{question} ({action}) [SCAN]",
                entry_price=mid,
                size_usdc=size,
                side="BUY",
            )
            state_module.save_state(st)
            register_stop_loss(token_id, mid, sl_pct, f"{question} ({action}) [SCAN]")

            msg = (
                f"*Scanned Opportunity Trade*\n"
                f"Market: {question[:70]}\n"
                f"Action: {action} | Amount: ${size:.2f} (Kelly-sized)\n"
                f"Entry: {mid:.3f} | Claude: {claude_p:.3f} | Edge: +{edge:.3f}\n"
                f"Stop-loss: {stop_price:.4f} (-{int(sl_pct*100)}%)\n"
                f"Hours left: {m['_hours_left']}h"
            )
            tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, msg)
            log(f"    Order placed: {order_id}")

            balance -= size
            trades_placed += 1
            slots -= 1
        else:
            log(f"    [SCAN] Order FAILED for '{question[:45]}'")

    return trades_placed


# ── Position Review ───────────────────────────────────────────────────────────
# Every 4 hours, re-evaluates each open position against:
#   1. The Odds API  — did bookmaker odds shift significantly? (sports only)
#   2. Kalshi        — does cross-platform price still support our direction?
#   3. Claude        — does the fundamental thesis still hold?
# Alerts via Telegram if thesis has broken. User can reply "exit" to sell.

REVIEW_STATE_FILE    = Path(__file__).parent / "review_state.json"
REVIEW_INTERVAL_MIN  = 60    # re-check every 1 hour
REVIEW_ODDS_SHIFT    = 0.10  # alert if bookmaker odds shift >10% against us
REVIEW_KALSHI_SHIFT  = 0.08  # alert if Kalshi moves >8% against our direction
REVIEW_CLAUDE_FLIP   = 0.12  # alert if Claude probability reverses by >12%
REVIEW_TAKE_PROFIT   = 0.50  # alert if position is up >50% (consider locking in)
REVIEW_HOURS_LEFT    = 6.0   # alert if closing in <6h and we're losing


def should_run_review() -> bool:
    if not REVIEW_STATE_FILE.exists():
        return True
    try:
        data = json.loads(REVIEW_STATE_FILE.read_text())
        elapsed = (datetime.now(timezone.utc).timestamp() - data.get("last_review_ts", 0)) / 60
        return elapsed >= REVIEW_INTERVAL_MIN
    except Exception:
        return True


def mark_review_done():
    REVIEW_STATE_FILE.write_text(json.dumps({
        "last_review_ts": datetime.now(timezone.utc).timestamp()
    }))


def _is_sports_position(question: str) -> bool:
    """True if this position is from the sports arb strategy."""
    return "[SARB]" in question or any(
        kw in question.lower() for kw in ["vs.", " vs ", "nba", "nhl", "mlb", "nfl",
                                           "win on ", "o/u", "over/under"]
    )


def run_position_review():
    """
    Re-evaluate all open positions against Odds API + Kalshi + Claude.
    Alert user on Telegram for any positions whose thesis has broken.
    Offer exit option — if user replies 'exit', sell immediately.
    """
    log("[REVIEW] Starting position review...")
    st = state_module.load_state()
    positions = st.get("positions", {})

    if not positions:
        log("[REVIEW] No open positions to review.")
        mark_review_done()
        return

    # Pre-load bookmaker odds and Kalshi markets once for the whole review
    all_odds = fetch_bookmaker_odds() if True else []
    kalshi_markets = []
    if KALSHI_ENABLED:
        try:
            kalshi_markets = kalshi_client.get_kalshi_markets()
        except Exception:
            pass

    alerts = []

    for order_id, pos in positions.items():
        question   = pos.get("question", "")
        token_id   = pos.get("token_id", "")
        entry_price = float(pos.get("entry_price") or pos.get("price") or 0.5)

        # Skip manual positions — user manages these themselves
        if pos.get("manual") or "[MANUAL]" in question:
            log(f"[REVIEW] Skipping manual position: '{question[:55]}'")
            continue
        side        = pos.get("side", "BUY")
        usdc_spent  = float(pos.get("usdc_spent") or pos.get("size_usdc") or 0)

        # Get current market price
        current_price = market_fetcher.get_market_midpoint(token_id)
        if current_price is None:
            continue   # market may have resolved — sync will clean it up

        # Determine if we're in profit or loss
        pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0
        current_value = usdc_spent * (current_price / entry_price) if entry_price > 0 else 0

        issues   = []
        signals  = []

        # ── Check 1: The Odds API (sports positions only) ─────────────────────
        if _is_sports_position(question) and all_odds and not is_ou_market(question):
            book_prob, matched_team = match_to_bookmaker(question, all_odds)
            if book_prob is not None:
                signals.append(f"Bookmaker: {book_prob:.3f} ({matched_team})")
                # We bought YES — bookmaker now says much lower prob
                if "YES" in question.upper() and book_prob < current_price - REVIEW_ODDS_SHIFT:
                    issues.append(f"⚠️ Bookmaker odds dropped: was ~{entry_price:.2f}, "
                                  f"book now {book_prob:.3f} (shift: {book_prob - entry_price:+.3f})")
                # We bought NO — bookmaker now says YES is more likely
                elif "NO" in question.upper() and (1 - book_prob) < (1 - current_price) - REVIEW_ODDS_SHIFT:
                    issues.append(f"⚠️ Bookmaker odds shifted against NO position: "
                                  f"book YES now {book_prob:.3f} (was ~{1-entry_price:.2f})")

        # ── Check 2: Kalshi cross-platform ────────────────────────────────────
        if kalshi_markets:
            try:
                match = kalshi_client.find_kalshi_match(question, kalshi_markets)
                if match:
                    k_mid = match["mid"]
                    signals.append(f"Kalshi: {k_mid:.3f} (score={match['match_score']})")
                    if "YES" in question.upper() and k_mid < current_price - REVIEW_KALSHI_SHIFT:
                        issues.append(f"⚠️ Kalshi significantly below market: "
                                      f"Kalshi={k_mid:.3f} vs market={current_price:.3f}")
                    elif "NO" in question.upper() and k_mid > current_price + REVIEW_KALSHI_SHIFT:
                        issues.append(f"⚠️ Kalshi significantly above market (bad for NO): "
                                      f"Kalshi={k_mid:.3f} vs market={current_price:.3f}")
            except Exception:
                pass

        # ── Check 3: Claude re-analysis ────────────────────────────────────────
        try:
            market_stub = {"question": question.replace(" [SARB]","").replace(" [COPY]","")
                                                .replace(" [SCAN]","").replace(" [AUTO]","")
                                                .replace("(BUY YES)","").replace("(BUY NO)","").strip(),
                           "_days_to_close": "?", "volume": "?"}
            analysis = market_analyzer.analyze_market(market_stub, current_price, None)
            if analysis and analysis.get("confidence") != "low":
                claude_p = analysis["probability"]
                signals.append(f"Claude: {claude_p:.3f} ({analysis['confidence']} conf)")
                # We're long YES — Claude now thinks it's significantly less likely
                if "YES" in question.upper() and claude_p < entry_price - REVIEW_CLAUDE_FLIP:
                    issues.append(f"⚠️ Claude thesis reversed: entry={entry_price:.2f}, "
                                  f"Claude now={claude_p:.3f} ({analysis['reasoning'][:80]})")
                # We're long NO — Claude now thinks YES is significantly more likely
                elif "NO" in question.upper() and claude_p > (1 - entry_price) + REVIEW_CLAUDE_FLIP:
                    issues.append(f"⚠️ Claude thesis reversed: NO entry={entry_price:.2f}, "
                                  f"Claude YES now={claude_p:.3f} ({analysis['reasoning'][:80]})")
        except Exception:
            pass

        # ── Check 4: Take profit opportunity ──────────────────────────────────
        if pnl_pct >= REVIEW_TAKE_PROFIT:
            issues.append(f"💰 Up {pnl_pct:.0%} — consider taking profit "
                          f"(entry={entry_price:.3f} → now={current_price:.3f})")

        # ── Check 5: Expiring soon while losing ───────────────────────────────
        try:
            # Try to find market close time from gamma API
            mkt_data = requests.get(
                f"{GAMMA_API}/markets",
                params={"clob_token_ids": token_id},
                timeout=8,
            ).json()
            if isinstance(mkt_data, list) and mkt_data:
                end_dt = mkt_data[0].get("endDate") or mkt_data[0].get("end_date_iso")
                if end_dt:
                    close_ts = datetime.fromisoformat(end_dt.replace("Z", "+00:00")).timestamp()
                    hours_left = (close_ts - datetime.now(timezone.utc).timestamp()) / 3600
                    if 0 < hours_left < REVIEW_HOURS_LEFT and pnl_pct < -0.05:
                        issues.append(f"⏰ Closes in {hours_left:.1f}h and down {pnl_pct:.0%} — last chance to exit")
        except Exception:
            pass

        if issues:
            alerts.append({
                "order_id":     order_id,
                "question":     question,
                "token_id":     token_id,
                "entry_price":  entry_price,
                "current_price": current_price,
                "pnl_pct":      pnl_pct,
                "current_value": current_value,
                "issues":       issues,
                "signals":      signals,
            })

    mark_review_done()
    log(f"[REVIEW] Done. {len(alerts)} position(s) flagged out of {len(positions)}.")

    # ── Always send a status summary notification ─────────────────────────────
    try:
        cash = get_usdc_balance()
        positions_value = get_open_positions_value()
        portfolio_value = cash + positions_value
    except Exception:
        cash = positions_value = portfolio_value = 0.0

    # Re-read state after sync (may have changed)
    fresh_st = state_module.load_state()
    open_count = len(fresh_st.get("positions", {}))
    free_slots = max(0, config.MAX_OPEN_POSITIONS - open_count)

    if not alerts:
        summary_msg = (
            f"*\U0001f504 Hourly Position Review*\n\n"
            f"\u2705 All {open_count} positions reviewed \u2014 no issues found.\n\n"
            f"\U0001f4b0 Portfolio: ${portfolio_value:.2f} (cash ${cash:.2f} + positions ${positions_value:.2f})\n"
            f"\U0001f4ca Slots: {open_count}/{config.MAX_OPEN_POSITIONS} "
            f"({free_slots} slot{'s' if free_slots != 1 else ''} free)"
        )
        resp = tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, summary_msg)
        log(f"[REVIEW] Summary notification sent (ok={resp.get('ok')})")
        return

    # Summary header (sent before individual alerts)
    summary_msg = (
        f"*\U0001f504 Hourly Position Review*\n\n"
        f"\u26a0\ufe0f {len(alerts)} of {open_count} position(s) flagged for review.\n\n"
        f"\U0001f4b0 Portfolio: ${portfolio_value:.2f} (cash ${cash:.2f} + positions ${positions_value:.2f})\n"
        f"\U0001f4ca Slots: {open_count}/{config.MAX_OPEN_POSITIONS} "
        f"({free_slots} slot{'s' if free_slots != 1 else ''} free)\n\n"
        f"Details follow..."
    )
    resp = tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, summary_msg)
    log(f"[REVIEW] Summary notification sent (ok={resp.get('ok')})")

    # ── Send Telegram alert for each flagged position ─────────────────────────
    # Auto-placed trades (SARB, COPY, AUTO) exit automatically when thesis breaks.
    # User-confirmed trades (SCAN) ask for confirmation before exiting.
    import math

    def _execute_exit(alert: dict):
        q_raw = alert["question"][:70]
        q_s   = _esc(q_raw)
        size_usdc = math.floor(alert["current_value"] * 100) / 100
        order_id_result = market_fetcher.place_market_order(
            token_id=alert["token_id"],
            size_usdc=size_usdc,
            side="SELL",
        )
        if order_id_result:
            st2 = state_module.load_state()
            st2.get("positions", {}).pop(alert["order_id"], None)
            state_module.save_state(st2)
            tg.send_message(
                config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
                f"\u2705 Auto-exited: {q_s}\nP&L: {alert['pnl_pct']:+.1%} | Order: {order_id_result}"
            )
            log(f"[REVIEW] Auto-exit executed: {order_id_result}")
        else:
            tg.send_message(
                config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
                f"\u274c Auto-exit FAILED for: {q_s}"
            )
            log(f"[REVIEW] Auto-exit FAILED for '{q_raw[:55]}'")

    def _esc(text: str) -> str:
        """Escape Telegram Markdown special chars in free-form text."""
        for ch in ("*", "_", "`", "[", "]", "(", ")"):
            text = text.replace(ch, "\\" + ch)
        return text

    for alert in alerts:
        q         = alert["question"]
        q_safe    = _esc(q[:70])
        pnl_str   = f"+{alert['pnl_pct']:.1%}" if alert["pnl_pct"] >= 0 else f"{alert['pnl_pct']:.1%}"
        val_str   = f"${alert['current_value']:.2f}"
        sigs      = " | ".join(alert["signals"])
        issue_str = "\n".join(alert["issues"])

        # Determine if this was auto-placed or user-confirmed
        is_auto = any(tag in q for tag in ("[SARB]", "[COPY]", "[AUTO]"))
        is_scan = "[SCAN]" in q

        log(f"[REVIEW] Flagged: '{q[:55]}' — {len(alert['issues'])} issue(s) | auto={is_auto}")

        if is_auto and REVIEW_AUTO_EXIT:
            # Auto-placed trades: notify + exit immediately, no confirmation needed
            msg = (
                f"*\U0001f50d Position Review \u2014 Auto Exit*\n\n"
                f"Market: {q_safe}\n"
                f"Entry: {alert['entry_price']:.3f} \u2192 Now: {alert['current_price']:.3f} "
                f"({pnl_str}, ~{val_str})\n"
                f"Signals: {sigs}\n\n"
                f"{issue_str}\n\n"
                f"Auto-placed trade \u2014 exiting automatically."
            )
            tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, msg)
            _execute_exit(alert)

        else:
            # Notify only — warn user, no auto-exit
            msg = (
                f"*\U0001f50d Position Review \u2014 Exit Recommended*\n\n"
                f"Market: {q_safe}\n"
                f"Entry: {alert['entry_price']:.3f} \u2192 Now: {alert['current_price']:.3f} "
                f"({pnl_str}, ~{val_str})\n"
                f"Signals: {sigs}\n\n"
                f"{issue_str}\n\n"
                f"\u26a0\ufe0f Recommendation: consider exiting this position manually."
            )
            tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, msg)
            log(f"[REVIEW] Notified user to consider exit for '{q[:55]}'")

    # ── Final summary after all alerts processed ──────────────────────────────
    try:
        final_cash = get_usdc_balance()
        final_pos_value = get_open_positions_value()
        final_portfolio = final_cash + final_pos_value
    except Exception:
        final_cash = final_pos_value = final_portfolio = 0.0
    final_st    = state_module.load_state()
    final_count = len(final_st.get("positions", {}))
    final_slots = max(0, config.MAX_OPEN_POSITIONS - final_count)

    resp = tg.send_message(
        config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
        f"*\u2705 Review Complete*\n\n"
        f"\U0001f4b0 Portfolio: ${final_portfolio:.2f} (cash ${final_cash:.2f} + positions ${final_pos_value:.2f})\n"
        f"\U0001f4ca Slots: {final_count}/{config.MAX_OPEN_POSITIONS} "
        f"({final_slots} slot{'s' if final_slots != 1 else ''} free)"
    )
    log(f"[REVIEW] Final summary sent (ok={resp.get('ok')})")


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    log("=== Autonomous Trader Run ===")

    # --- Balance check ---
    try:
        balance = get_usdc_balance()
    except Exception as e:
        log(f"Failed to get balance: {e}")
        return
    log(f"Available USDC: ${balance:.2f}")

    if balance < min(MIN_TRADE_USDC, COPY_MIN_USDC):
        log("Balance too low to trade. Exiting.")
        return

    # --- Shared: daily loss guard ---
    if not check_daily_guard(balance):
        return

    # --- Sync positions against live API (frees slots for sold/resolved positions) ---
    sync_positions()

    # --- Shared: position slots ---
    slots = get_available_slots()
    if slots <= 0:
        log("Max open positions reached. Skipping all strategies.")
        log(f"Run complete. Total trades placed: 0")
        return

    total_placed = 0

    # ── Copy Trading: DISABLED — auto-trades on sports events only ───────────
    # run_copy_strategy copies any topic unconditionally; disabled per user preference.

    # ── Sports Arbitrage (every 15 min, auto-execute) ─────────────────────────
    # Only auto-trade: bookmaker consensus + Kalshi cross-check required.
    if slots > 0 and should_run_sports_arb():
        placed = run_sports_arb_strategy(balance, slots)
        total_placed += placed
        slots -= placed
        if placed > 0:
            try:
                balance = get_usdc_balance()
            except Exception:
                pass
    elif slots > 0:
        pass  # not time yet, no log noise

    # ── Opportunity Scanning: DISABLED — sports auto-trades only ─────────────
    # Scan suggested non-sports markets; disabled per user preference.

    # ── Position Review: DISABLED — mirroring sovereign2013 instead ──────────

    log(f"Run complete. Total trades placed: {total_placed}")


if __name__ == "__main__":
    run()
