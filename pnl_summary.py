import re, json, requests
from pathlib import Path

OUR_PROXY = '0x3F984E05d151d0F7164d9025C07E176d2b9AeC35'

# Parse mirror log
mirror_log = Path('/root/polym/sovereign_mirror.log').read_text()
copy_log = Path('/root/polym/copy_existing.log').read_text()

# BUY executed lines from mirror log
buy_lines = re.findall(r'BUY executed:.*?\$(\d+\.\d+)', mirror_log)
total_mirror_buy = sum(float(x) for x in buy_lines)
num_mirror_buys = len(buy_lines)

# SELL executed lines from mirror log
sell_lines = re.findall(r'SELL executed:.*?\$(\d+\.\d+)', mirror_log)
total_mirror_sell = sum(float(x) for x in sell_lines)
num_mirror_sells = len(sell_lines)

# copy_existing buys: "BUY $5: ..."
copy_buy_lines = re.findall(r'BUY \$(\d+\.\d+):', copy_log)
total_copy_buy = sum(float(x) for x in copy_buy_lines)
num_copy_buys = len(copy_buy_lines)

print(f'Mirror BUYs: {num_mirror_buys} trades, total invested: ${total_mirror_buy:.2f}')
print(f'Mirror SELLs: {num_mirror_sells} trades, total received: ${total_mirror_sell:.2f}')
print(f'copy_existing BUYs: {num_copy_buys} trades, total invested: ${total_copy_buy:.2f}')

total_invested = total_mirror_buy + total_copy_buy
print(f'\nTotal invested (mirror + copy_existing): ${total_invested:.2f}')
print(f'Total realized from sells: ${total_mirror_sell:.2f}')

# Fetch current positions from Polymarket
r = requests.get(f'https://data-api.polymarket.com/positions?user={OUR_PROXY}&sizeThreshold=0.001', timeout=15)
positions = [p for p in r.json() if isinstance(p, dict)]
total_current_value = sum(float(p.get('currentValue', 0)) for p in positions)
num_positions = len(positions)
print(f'\nCurrent open positions: {num_positions}, total current value: ${total_current_value:.2f}')

# Net P&L = current value + realized sells - total invested
net_pnl = total_current_value + total_mirror_sell - total_invested
print(f'\nNet P&L = ${total_current_value:.2f} (current) + ${total_mirror_sell:.2f} (realized) - ${total_invested:.2f} (invested) = ${net_pnl:.2f}')

# Also show cash balance
import sys
sys.path.insert(0, '/root/polym')
from dotenv import load_dotenv
load_dotenv('/root/polym/.env')
import market_fetcher, config
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
try:
    client = market_fetcher.get_clob_client()
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=config.SIGNATURE_TYPE)
    b = client.get_balance_allowance(params)
    cash = float(b.get('balance', 0)) / 1e6
    print(f'Available cash: ${cash:.2f}')
except Exception as e:
    print(f'Cash fetch error: {e}')
