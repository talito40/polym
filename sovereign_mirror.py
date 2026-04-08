"""
Mirror every trade sovereign2013 makes — both BUY and SELL.
Runs continuously, polls every 120 seconds.

Bet sizing: proportional to sovereign2013's allocation.
  their_alloc_pct = new_position_initial_value / their_total_portfolio_value
  our_bet = our_cash * their_alloc_pct
  capped between MIN_BET ($5) and MAX_BET ($100)
"""
import requests, time, sys, json, math
from pathlib import Path

sys.path.insert(0, '/root/polym')
from dotenv import load_dotenv
load_dotenv('/root/polym/.env')

import telegram_notify as tg
import market_fetcher
import config
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

MIRROR_WALLET  = '0xee613b3fc183ee44f9da9c05f53e2da107e3debf'  # sovereign2013
OUR_PROXY      = '0x3F984E05d151d0F7164d9025C07E176d2b9AeC35'
INTERVAL       = 20    # seconds between polls
MIN_COPY_PRICE = 0.01   # skip positions priced below 1 cent (resolved loser)
MAX_COPY_PRICE = 0.99   # skip positions priced at/above 99 cents (resolved winner — no liquidity)
MIN_BET        = 5.0    # minimum bet regardless of calculation
MAX_BET        = 100.0  # maximum bet regardless of calculation
FIXED_BET      = 0   # if set > 0, overrides proportional sizing (temporary)
STATE_FILE     = Path('/root/polym/sovereign_mirror_state.json')


def log(msg):
    from datetime import datetime
    line = '[%s] %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    with open('/root/polym/sovereign_mirror.log', 'a') as f:
        f.write(line + '\n')


def get_our_cash():
    """Return our available USDC cash balance."""
    try:
        client = market_fetcher.get_clob_client()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=config.SIGNATURE_TYPE)
        b = client.get_balance_allowance(params)
        return float(b.get('balance', 0)) / 1e6
    except Exception as e:
        log('Could not fetch cash balance: %s' % e)
        return 0.0



def calc_bet_size(new_pos_initial_value, sovereign_positions):
    """
    Calculate bet size proportional to sovereign2013's allocation.
    their_alloc_pct = new position's initial value / their total portfolio value
    our_bet = our_cash * their_alloc_pct
    Clamped to [MIN_BET, MAX_BET].
    """
    # sovereign2013's total portfolio = sum of all currentValue
    total_portfolio = sum(
        float(p.get('initialValue', 0))
        for p in sovereign_positions.values()
        if isinstance(p, dict) and p.get('initialValue')
    )
    if total_portfolio <= 0:
        log('Cannot calculate sovereign portfolio value — falling back to MIN_BET')
        return MIN_BET

    alloc_pct = new_pos_initial_value / total_portfolio
    our_cash  = get_our_cash()
    raw_bet   = our_cash * alloc_pct

    bet = max(MIN_BET, min(MAX_BET, raw_bet))
    log('Sizing: sovereign_total=$%.0f new_pos=$%.0f alloc=%.2f%% our_cash=$%.0f raw=$%.2f bet=$%.2f' % (
        total_portfolio, new_pos_initial_value, alloc_pct * 100, our_cash, raw_bet, bet))
    return round(bet, 2)


def fetch_positions(wallet):
    r = requests.get(
        'https://data-api.polymarket.com/positions?user=%s&sizeThreshold=0.01&limit=500' % wallet,
        timeout=10
    )
    data = r.json()
    positions = [p for p in data if isinstance(p, dict)]
    return {p['asset']: p for p in positions if p.get('asset')}


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {'sovereign_positions': {}, 'our_positions': {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def place_buy(asset, title, outcome, size_usdc):
    order_id = market_fetcher.place_market_order(
        token_id=asset,
        size_usdc=size_usdc,
        side='BUY',
    )
    if order_id:
        log('BUY executed: %s [%s] $%.2f | order=%s' % (title[:50], outcome, size_usdc, order_id))
        return order_id
    else:
        log('BUY FAILED: %s [%s]' % (title[:50], outcome))
        return None


def place_sell(asset, title, outcome, cur_value):
    size_usdc = math.floor(cur_value * 100) / 100
    if size_usdc < 0.01:
        log('Skip sell — value too small: $%.4f for %s' % (size_usdc, title[:40]))
        return None
    # Check current price — if resolved winner (>=0.99), can't sell, redeemer will handle it
    cur_price = float(market_fetcher.get_market_midpoint(asset) or 0)
    if cur_price >= MAX_COPY_PRICE:
        log('Skip sell — resolved winner (price=%.4f), redeemer will claim: %s' % (cur_price, title[:40]))
        return None
    order_id = market_fetcher.place_market_order(
        token_id=asset,
        size_usdc=size_usdc,
        side='SELL',
    )
    if order_id:
        log('SELL executed: %s [%s] $%.2f | order=%s' % (title[:50], outcome, size_usdc, order_id))
        return order_id
    else:
        log('SELL FAILED: %s [%s]' % (title[:50], outcome))
        return None


# ── Startup ────────────────────────────────────────────────────────────────────
log('sovereign_mirror started. Proportional sizing | min=$%.0f max=$%.0f | interval=%ds' % (
    MIN_BET, MAX_BET, INTERVAL))

state = load_state()
try:
    current = fetch_positions(MIRROR_WALLET)
    # Store full position data (needed for portfolio value calculation)
    state['sovereign_positions'] = {k: v for k, v in current.items()}
    save_state(state)
    total = sum(float(p.get('currentValue', 0)) for p in current.values())
    log('Snapshot: %d positions, total portfolio ~$%.0f' % (len(current), total))
except Exception as e:
    log('Startup snapshot failed: %s' % e)
    current = {}

tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
    'sovereign2013 mirror running.\n'
    'Sizing: proportional to their allocation (min $%.0f / max $%.0f)\n'
    '%d existing positions snapshotted (ignored).' % (MIN_BET, MAX_BET, len(state['sovereign_positions'])))


# ── Main loop ──────────────────────────────────────────────────────────────────
while True:
    time.sleep(INTERVAL)
    try:
        new_sovereign = fetch_positions(MIRROR_WALLET)
        our_positions = fetch_positions(OUR_PROXY)
        state = load_state()
        known = state.get('sovereign_positions', {})

        # ── Detect new positions (sovereign2013 bought something new) ──────────
        for asset, pos in new_sovereign.items():
            if asset not in known:
                title     = pos.get('title', 'Unknown')
                outcome   = pos.get('outcome', 'YES')
                cur_price = float(pos.get('curPrice', 0))
                init_val  = float(pos.get('initialValue', 0))

                log('NEW position: %s [%s] price=%.3f initial=$%.2f' % (
                    title[:50], outcome, cur_price, init_val))

                if cur_price < MIN_COPY_PRICE:
                    log('Skip BUY — resolved loser (price=%.4f): %s' % (cur_price, title[:40]))
                    known[asset] = pos
                    continue

                if cur_price >= MAX_COPY_PRICE:
                    log('Skip BUY — resolved winner, no liquidity (price=%.4f): %s' % (cur_price, title[:40]))
                    known[asset] = pos
                    continue

                if asset in our_positions:
                    log('Skip BUY — already holding: %s' % title[:40])
                else:
                    bet = FIXED_BET if FIXED_BET > 0 else calc_bet_size(init_val, new_sovereign)
                    order_id = place_buy(asset, title, outcome, bet)
                    if order_id:
                        state.setdefault('our_positions', {})[asset] = {
                            'asset': asset, 'title': title, 'outcome': outcome,
                            'order_id': order_id, 'size_usdc': bet
                        }

                known[asset] = pos  # store full data for future portfolio calculations

        # ── Detect closed positions (sovereign2013 exited) ────────────────────
        for asset, pos in list(known.items()):
            if asset not in new_sovereign:
                title   = pos.get('title', 'Unknown') if isinstance(pos, dict) else 'Unknown'
                outcome = pos.get('outcome', '') if isinstance(pos, dict) else ''
                log('CLOSED: %s [%s] — mirroring sell' % (title[:50], outcome))

                our_pos = our_positions.get(asset)
                if our_pos:
                    cur_value = float(our_pos.get('currentValue', 0))
                    place_sell(asset, title, outcome, cur_value)
                    state.get('our_positions', {}).pop(asset, None)
                else:
                    log('No matching position in our wallet for: %s' % title[:40])

                del known[asset]

        state['sovereign_positions'] = known
        save_state(state)

    except Exception as e:
        log('Loop error: %s' % e)
