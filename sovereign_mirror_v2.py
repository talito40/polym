"""
Order-level mirror of sovereign2013.
Watches the activity API (individual transactions) instead of the positions API.
Copies every BUY and SELL sovereign places, proportionally sized to our cash.

State: tracks processed tx hashes so we never double-copy a trade.
Startup: snapshots all current tx hashes so we don't copy historical trades.
"""
import requests, time, sys, json, math
from pathlib import Path
from datetime import datetime

sys.path.insert(0, '/root/polym')
from dotenv import load_dotenv
load_dotenv('/root/polym/.env')

import telegram_notify as tg
import market_fetcher
import config
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

MIRROR_WALLET  = '0xee613b3fc183ee44f9da9c05f53e2da107e3debf'
OUR_PROXY      = '0x3F984E05d151d0F7164d9025C07E176d2b9AeC35'
INTERVAL       = 20
MIN_COPY_PRICE = 0.01
MAX_COPY_PRICE = 0.99
MIN_BET        = 2.5
CASH_FLOOR     = 100.0  # pause all BUYs when cash drops below this, resume automatically
MAX_BET        = 100.0
STATE_FILE     = Path('/root/polym/sovereign_mirror_v2_state.json')
LOG_PATH       = '/root/polym/sovereign_mirror.log'


def log(msg):
    line = '[%s] %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    with open(LOG_PATH, 'a') as f:
        f.write(line + '\n')


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except:
            pass
    return {'startup_tx_hashes': [], 'processed_tx_hashes': [], 'our_positions': {}}


def save_state(state):
    # Keep processed list bounded to last 50000 hashes to prevent unbounded growth
    if len(state['processed_tx_hashes']) > 50000:
        state['processed_tx_hashes'] = state['processed_tx_hashes'][-50000:]
    STATE_FILE.write_text(json.dumps(state))


def fetch_activity(wallet, limit=200):
    """Fetch recent activity records for a wallet. Returns list sorted oldest-first."""
    try:
        r = requests.get(
            'https://data-api.polymarket.com/activity?user=%s&limit=%d' % (wallet, limit),
            timeout=15
        )
        data = r.json()
        if not isinstance(data, list):
            return []
        # API returns newest-first; reverse to process oldest-first
        return list(reversed(data))
    except Exception as e:
        log('fetch_activity error: %s' % e)
        return []


def get_our_cash():
    try:
        client = market_fetcher.get_clob_client()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=config.SIGNATURE_TYPE)
        b = client.get_balance_allowance(params)
        return float(b.get('balance', 0)) / 1e6
    except Exception as e:
        log('Cash fetch error: %s' % e)
        return 0.0


def get_sovereign_portfolio_value():
    """Sum of currentValue across all sovereign positions â€” used for proportional sizing."""
    try:
        r = requests.get(
            'https://data-api.polymarket.com/positions?user=%s&sizeThreshold=0.001&limit=500' % MIRROR_WALLET,
            timeout=15
        )
        positions = [p for p in r.json() if isinstance(p, dict)]
        return sum(float(p.get('currentValue', 0)) for p in positions)
    except:
        return 0.0


def get_our_position_value(asset):
    """Return current value of a specific asset in our portfolio, or 0."""
    try:
        r = requests.get(
            'https://data-api.polymarket.com/positions?user=%s&sizeThreshold=0.001&limit=500' % OUR_PROXY,
            timeout=15
        )
        for p in r.json():
            if isinstance(p, dict) and p.get('asset') == asset:
                return float(p.get('currentValue', 0)), float(p.get('curPrice', 0))
        return 0.0, 0.0
    except:
        return 0.0, 0.0


def calc_bet(usdc_size, sovereign_total, our_cash):
    if sovereign_total <= 0 or our_cash <= 0:
        return MIN_BET
    pct = usdc_size / sovereign_total
    bet = our_cash * pct
    return max(MIN_BET, min(MAX_BET, bet))


def place_buy(asset, title, outcome, bet):
    try:
        order_id = market_fetcher.place_market_order(token_id=asset, size_usdc=bet, side='BUY')
        if order_id:
            log('BUY executed: %s [%s] $%.2f | order=%s' % (title[:55], outcome, bet, order_id))
            return order_id
        else:
            log('BUY FAILED: %s [%s] $%.2f' % (title[:55], outcome, bet))
            return None
    except Exception as e:
        log('BUY exception: %s | %s [%s]' % (e, title[:55], outcome))
        return None


def place_sell(asset, title, outcome, value, cur_price):
    if cur_price >= MAX_COPY_PRICE:
        log('Skip SELL â€” resolved winner, redeemer will claim: %s [%s]' % (title[:55], outcome))
        return None
    if value < 0.50:
        log('Skip SELL â€” value too small ($%.2f): %s [%s]' % (value, title[:55], outcome))
        return None
    size = math.floor(value * 100) / 100
    try:
        order_id = market_fetcher.place_market_order(token_id=asset, size_usdc=size, side='SELL')
        if order_id:
            log('SELL executed: %s [%s] $%.2f | order=%s' % (title[:55], outcome, size, order_id))
            return order_id
        else:
            log('SELL FAILED: %s [%s] $%.2f' % (title[:55], outcome, size))
            return None
    except Exception as e:
        log('SELL exception: %s | %s [%s]' % (e, title[:55], outcome))
        return None


# â”€â”€ Startup â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

state = load_state()

# On first run, snapshot all current tx hashes so we don't copy historical trades
if not state['startup_tx_hashes']:
    log('sovereign_mirror_v2 first start â€” snapshotting historical tx hashes...')
    history = fetch_activity(MIRROR_WALLET, limit=500)
    startup_hashes = set(r.get('transactionHash', '') for r in history if r.get('transactionHash'))
    state['startup_tx_hashes'] = list(startup_hashes)
    save_state(state)
    log('Snapshot: %d historical tx hashes recorded. Will only copy NEW trades from now on.' % len(startup_hashes))
else:
    log('sovereign_mirror_v2 started. Order-level mirroring | min=$%.0f max=$%.0f | interval=%ds' % (
        MIN_BET, MAX_BET, INTERVAL))

startup_hashes = set(state['startup_tx_hashes'])
processed_hashes = set(state['processed_tx_hashes'])

# â”€â”€ Main loop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

while True:
    try:
        activity = fetch_activity(MIRROR_WALLET, limit=200)

        new_records = [
            r for r in activity
            if r.get('transactionHash')
            and r['transactionHash'] not in startup_hashes
            and r['transactionHash'] not in processed_hashes
        ]

        if new_records:
            our_cash = get_our_cash()
            cash_paused = our_cash < CASH_FLOOR
            if cash_paused:
                log('Cash floor active ($%.2f < $%.2f) â€” skipping BUYs this cycle' % (our_cash, CASH_FLOOR))
            sov_total = get_sovereign_portfolio_value()

            for rec in new_records:
                tx   = rec['transactionHash']
                side = str(rec.get('side', '')).upper()
                asset = rec.get('asset', '')
                title = rec.get('title', 'Unknown')
                outcome = rec.get('outcome', 'YES')
                usdc_size = float(rec.get('usdcSize', 0) or 0)
                cur_price = float(rec.get('price', 0) or 0)

                if not asset or not side:
                    processed_hashes.add(tx)
                    continue

                if side == 'BUY':
                    if cash_paused:
                        log('Skip BUY â€” cash floor active: %s [%s]' % (title[:50], outcome))
                    elif cur_price < MIN_COPY_PRICE:
                        log('Skip BUY â€” resolved loser (%.4f): %s' % (cur_price, title[:50]))
                    elif cur_price >= MAX_COPY_PRICE:
                        log('Skip BUY â€” resolved winner (%.4f): %s' % (cur_price, title[:50]))
                    elif usdc_size <= 0:
                        log('Skip BUY â€” zero size: %s' % title[:50])
                    else:
                        bet = calc_bet(usdc_size, sov_total, our_cash)
                        order_id = place_buy(asset, title, outcome, bet)
                        if order_id:
                            our_cash = max(0, our_cash - bet)  # update local estimate
                            state.setdefault('our_positions', {})[asset] = {
                                'title': title, 'outcome': outcome, 'size_usdc': bet
                            }

                elif side == 'SELL':
                    pos_value, pos_price = get_our_position_value(asset)
                    if pos_value > 0:
                        place_sell(asset, title, outcome, pos_value, pos_price)
                    else:
                        log('Skip SELL â€” not holding: %s [%s]' % (title[:50], outcome))

                processed_hashes.add(tx)

            state['processed_tx_hashes'] = list(processed_hashes)
            save_state(state)

    except Exception as e:
        log('Main loop error: %s' % e)

    time.sleep(INTERVAL)
