"""Sell all current positions in our wallet."""
import requests, time, sys, math
sys.path.insert(0, '/root/polym')
from dotenv import load_dotenv
load_dotenv('/root/polym/.env')
import market_fetcher, config
import telegram_notify as tg

OUR_PROXY = '0x3F984E05d151d0F7164d9025C07E176d2b9AeC35'
MAX_COPY_PRICE = 0.99

def log(msg):
    from datetime import datetime
    print('[%s] %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg), flush=True)

log('=== sell_all started ===')

r = requests.get('https://data-api.polymarket.com/positions?user=%s&sizeThreshold=0.001' % OUR_PROXY, timeout=15)
positions = [p for p in r.json() if isinstance(p, dict)]
log('Total positions: %d' % len(positions))

sold = []
skipped_winner = []
skipped_zero = []
failed = []

for p in positions:
    asset   = p.get('asset', '')
    title   = p.get('title', 'Unknown')[:55]
    outcome = p.get('outcome', '')
    cur     = float(p.get('curPrice', 0))
    val     = float(p.get('currentValue', 0))

    if val < 0.01:
        log('SKIP (zero value): %s [%s]' % (title, outcome))
        skipped_zero.append(title)
        continue

    if cur >= MAX_COPY_PRICE:
        log('SKIP (resolved winner, redeemer will claim): %s [%s]' % (title, outcome))
        skipped_winner.append('%s [%s]' % (title, outcome))
        continue

    size = math.floor(val * 100) / 100
    log('SELL $%.2f @ %.3f: %s [%s]' % (size, cur, title, outcome))

    order_id = market_fetcher.place_market_order(token_id=asset, size_usdc=size, side='SELL')
    if order_id:
        log('  -> OK: %s' % order_id)
        sold.append('%s [%s] $%.2f' % (title, outcome, val))
    else:
        log('  -> FAILED (illiquid/closed)')
        failed.append('%s [%s] $%.2f' % (title, outcome, val))

    time.sleep(0.3)

log('=== Done. Sold: %d | Winners(redeemer): %d | Zero: %d | Failed: %d ===' % (
    len(sold), len(skipped_winner), len(skipped_zero), len(failed)))

msg = (
    '*Sell All — Fresh Mirror Start*\n\n'
    'Sold: %d positions\n'
    'Resolved winners (redeemer will claim): %d\n'
    'Zero value (unrecoverable): %d\n'
    'Failed (illiquid): %d' % (len(sold), len(skipped_winner), len(skipped_zero), len(failed))
)
if failed:
    msg += '\n\nFailed:\n' + '\n'.join('- ' + f for f in failed[:10])
tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, msg)
log('Done.')
