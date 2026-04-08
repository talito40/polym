"""
One-time cleanup: sell all positions where curPrice <= 0.001 (essentially worthless).
"""
import requests, time, sys, math
sys.path.insert(0, '/root/polym')
from dotenv import load_dotenv
load_dotenv('/root/polym/.env')
import telegram_notify as tg
import market_fetcher
import config

OUR_PROXY   = '0x3F984E05d151d0F7164d9025C07E176d2b9AeC35'
DEAD_PRICE  = 0.001  # at or below this = dead

def log(msg):
    from datetime import datetime
    line = '[%s] %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    print(line, flush=True)

log('=== cleanup_dead started ===')

r = requests.get('https://data-api.polymarket.com/positions?user=%s&sizeThreshold=0.001' % OUR_PROXY, timeout=10)
positions = [p for p in r.json() if isinstance(p, dict)]
log('Total positions: %d' % len(positions))

dead = [p for p in positions if float(p.get('curPrice', 0)) <= DEAD_PRICE]
log('Dead positions (price <= %.3f): %d' % (DEAD_PRICE, len(dead)))

sold = []
failed = []
skipped = []

for p in dead:
    asset   = p.get('asset', '')
    title   = p.get('title', 'Unknown')[:55]
    outcome = p.get('outcome', '')
    cur     = float(p.get('curPrice', 0))
    val     = float(p.get('currentValue', 0))

    # If value is truly zero (price=0.000), nothing to sell
    if val < 0.01:
        log('SKIP (zero value): %s [%s]' % (title, outcome))
        skipped.append(title)
        continue

    size = math.floor(val * 100) / 100
    log('SELL $%.4f: %s [%s] price=%.4f' % (size, title, outcome, cur))

    order_id = market_fetcher.place_market_order(
        token_id=asset,
        size_usdc=size,
        side='SELL',
    )
    if order_id:
        log('  -> Sold: %s' % order_id)
        sold.append('%s [%s] $%.4f' % (title, outcome, val))
    else:
        log('  -> FAILED (market may be closed/illiquid)')
        failed.append('%s [%s]' % (title, outcome))

    time.sleep(0.5)

log('=== Done. Sold: %d | Zero-value skipped: %d | Failed: %d ===' % (len(sold), len(skipped), len(failed)))

msg = (
    '*Dead Position Cleanup*\n\n'
    'Sold: %d positions\n'
    'Zero-value (not sellable): %d\n'
    'Failed: %d\n' % (len(sold), len(skipped), len(failed))
)
if failed:
    msg += '\nFailed (sell manually):\n' + '\n'.join('  - ' + f for f in failed)

tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, msg)
log('Done.')
