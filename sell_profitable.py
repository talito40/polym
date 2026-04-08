import requests, sys, time, math
sys.path.insert(0, '/root/polym')
from dotenv import load_dotenv
load_dotenv('/root/polym/.env')
import market_fetcher

OUR_PROXY = '0x3F984E05d151d0F7164d9025C07E176d2b9AeC35'
MAX_COPY_PRICE = 0.99

r = requests.get('https://data-api.polymarket.com/positions?user=%s&sizeThreshold=0.001&limit=500' % OUR_PROXY, timeout=15)
positions = [p for p in r.json() if isinstance(p, dict)]

profitable = []
for p in positions:
    cur   = float(p.get('curPrice', 0))
    avg   = float(p.get('avgPrice', 0))
    val   = float(p.get('currentValue', 0))
    init  = float(p.get('initialValue', 0))
    if cur > avg and val > init and val > 0.50 and cur < MAX_COPY_PRICE:
        profitable.append(p)

print('Total positions: %d' % len(positions))
print('Profitable (curPrice > avgPrice): %d' % len(profitable))
print()

sold = failed = 0
for p in sorted(profitable, key=lambda x: float(x.get('currentValue',0)), reverse=True):
    asset   = p.get('asset','')
    title   = p.get('title','')[:55]
    outcome = p.get('outcome','')
    cur     = float(p.get('curPrice',0))
    avg     = float(p.get('avgPrice',0))
    val     = float(p.get('currentValue',0))
    init    = float(p.get('initialValue',0))
    profit  = val - init
    size    = math.floor(val * 100) / 100

    print('SELL $%.2f (profit +$%.2f | %.0f%%) @ %.3f: %s [%s]' % (
        size, profit, (profit/init*100) if init > 0 else 0, cur, title, outcome))

    order_id = market_fetcher.place_market_order(token_id=asset, size_usdc=size, side='SELL')
    if order_id:
        print('  -> OK: %s' % order_id)
        sold += 1
    else:
        print('  -> FAILED')
        failed += 1
    time.sleep(0.3)

print()
print('Done. Sold: %d | Failed: %d' % (sold, failed))
