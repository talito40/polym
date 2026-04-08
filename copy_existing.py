"""
One-time script: copy sovereign2013's current positions at $5 each,
only if current price <= their avg entry price (same or better deal).
"""
import requests, time, sys, math
sys.path.insert(0, '/root/polym')
from dotenv import load_dotenv
load_dotenv('/root/polym/.env')
import telegram_notify as tg
import market_fetcher
import config

MIRROR_WALLET = '0xee613b3fc183ee44f9da9c05f53e2da107e3debf'
OUR_PROXY     = '0x3F984E05d151d0F7164d9025C07E176d2b9AeC35'
TRADE_SIZE    = 5.0

def log(msg):
    from datetime import datetime
    line = '[%s] %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    print(line, flush=True)
    with open('/root/polym/copy_existing.log', 'a') as f:
        f.write(line + '\n')

log('=== copy_existing started ===')

# Fetch sovereign2013 positions
r = requests.get('https://data-api.polymarket.com/positions?user=%s&sizeThreshold=0.01' % MIRROR_WALLET, timeout=10)
positions = [p for p in r.json() if isinstance(p, dict)]
log('sovereign2013 positions: %d' % len(positions))

# Fetch our current positions to avoid duplicates
r2 = requests.get('https://data-api.polymarket.com/positions?user=%s&sizeThreshold=0.001' % OUR_PROXY, timeout=10)
our_assets = {p['asset'] for p in r2.json() if isinstance(p, dict)}
log('Our current holdings: %d tokens' % len(our_assets))

bought = []
skipped_hold  = []
skipped_price = []
skipped_fail  = []

for p in positions:
    asset   = p.get('asset', '')
    title   = p.get('title', 'Unknown')[:55]
    outcome = p.get('outcome', 'YES')
    cur     = float(p.get('curPrice', 0))
    avg     = float(p.get('avgPrice', 0))

    # Skip if we already hold this token
    if asset in our_assets:
        log('SKIP (already hold): %s [%s]' % (title, outcome))
        skipped_hold.append(title)
        continue

    # Skip if price has moved against us (more expensive than sovereign's entry)
    if cur > avg:
        log('SKIP (price worse %.3f > avg %.3f): %s [%s]' % (cur, avg, title, outcome))
        skipped_price.append('%s [%s] cur=%.3f avg=%.3f' % (title, outcome, cur, avg))
        continue

    # Place buy
    log('BUY $%.0f: %s [%s] cur=%.3f avg=%.3f' % (TRADE_SIZE, title, outcome, cur, avg))
    order_id = market_fetcher.place_market_order(
        token_id=asset,
        size_usdc=TRADE_SIZE,
        side='BUY',
    )

    if order_id:
        log('  -> Order: %s' % order_id)
        bought.append('%s [%s] @%.3f' % (title, outcome, cur))
        our_assets.add(asset)  # prevent duplicate in same run
    else:
        log('  -> FAILED')
        skipped_fail.append('%s [%s]' % (title, outcome))

    time.sleep(0.5)  # small delay between orders

# Summary
log('=== Done. Bought: %d | Skipped (holding): %d | Skipped (price): %d | Failed: %d ===' % (
    len(bought), len(skipped_hold), len(skipped_price), len(skipped_fail)))

summary = (
    '*sovereign2013 Position Copy Complete*\n\n'
    'Bought: %d positions at $%.0f each (total $%.0f)\n'
    'Skipped - already holding: %d\n'
    'Skipped - price worse than entry: %d\n'
    'Failed orders: %d\n\n'
    % (len(bought), TRADE_SIZE, len(bought) * TRADE_SIZE,
       len(skipped_hold), len(skipped_price), len(skipped_fail))
)
if bought:
    summary += 'Bought:\n' + '\n'.join('  - ' + b for b in bought[:20])
    if len(bought) > 20:
        summary += '\n  ...and %d more' % (len(bought) - 20)
if skipped_fail:
    summary += '\n\nFailed:\n' + '\n'.join('  - ' + f for f in skipped_fail)

tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, summary)
log('Summary sent via Telegram.')
