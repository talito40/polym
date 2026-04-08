"""Hourly summary: BUYs/SELLs, portfolio % change, sovereign deposits."""
import re, sys, requests, json
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, '/root/polym')
from dotenv import load_dotenv
load_dotenv('/root/polym/.env')
import telegram_notify as tg
import market_fetcher, config
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

OUR_PROXY     = '0x3F984E05d151d0F7164d9025C07E176d2b9AeC35'
MIRROR_WALLET = '0xee613b3fc183ee44f9da9c05f53e2da107e3debf'
LOG_FILE      = Path('/root/polym/sovereign_mirror.log')
STATE_FILE    = Path('/root/polym/hourly_report_state.json')

def get_cash():
    try:
        client = market_fetcher.get_clob_client()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=config.SIGNATURE_TYPE)
        b = client.get_balance_allowance(params)
        return float(b.get('balance', 0)) / 1e6
    except:
        return 0.0

def get_portfolio(wallet):
    try:
        r = requests.get(
            'https://data-api.polymarket.com/positions?user=%s&sizeThreshold=0.001&limit=500' % wallet,
            timeout=15
        )
        positions = [p for p in r.json() if isinstance(p, dict)]
        value = sum(float(p.get('currentValue', 0)) for p in positions)
        return value, len(positions)
    except:
        return 0.0, 0

def pct_str(prev, curr):
    if prev is None or prev == 0:
        return 'n/a ($%.0f)' % curr
    pct = (curr - prev) / prev * 100
    arrow = '\u25b2' if pct >= 0 else '\u25bc'
    sign = '+' if pct >= 0 else ''
    return '%s%s%.1f%% ($%.0f)' % (arrow, sign, pct, curr)

def get_sovereign_activity(since_ts):
    """Count sovereign's BUYs and SELLs in the last hour from activity API."""
    try:
        r = requests.get(
            'https://data-api.polymarket.com/activity?user=%s&limit=500' % MIRROR_WALLET,
            timeout=15
        )
        data = r.json()
        sov_buys = sov_sells = 0
        sov_deposits = []
        for rec in data:
            ts = int(rec.get('timestamp', 0))
            if ts < since_ts:
                continue
            side = str(rec.get('side', '')).upper()
            rec_type = str(rec.get('type', '')).upper()
            if side == 'BUY':
                sov_buys += 1
            elif side == 'SELL':
                sov_sells += 1
            if rec_type == 'DEPOSIT' or side == 'DEPOSIT':
                amt = float(rec.get('usdcSize', 0) or rec.get('size', 0))
                sov_deposits.append(amt)
        return sov_buys, sov_sells, sov_deposits
    except:
        return 0, 0, []

# Load previous state
state = {}
if STATE_FILE.exists():
    try:
        state = json.loads(STATE_FILE.read_text())
    except:
        state = {}

prev_our_total     = state.get('our_total')
prev_sovereign_val = state.get('sovereign_val')

# Count trades in last hour
now = datetime.utcnow()
one_hour_ago = now - timedelta(hours=1)
one_hour_ago_ts = int(one_hour_ago.timestamp())

buys = sells = skipped = failed = 0
buy_total = sell_total = 0.0

log_text = LOG_FILE.read_text() if LOG_FILE.exists() else ''
for line in log_text.splitlines():
    m = re.match(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', line)
    if not m:
        continue
    ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
    if ts < one_hour_ago:
        continue
    if 'BUY executed:' in line:
        buys += 1
        amt = re.search(r'\$(\d+\.\d+)', line)
        if amt: buy_total += float(amt.group(1))
    elif 'SELL executed:' in line:
        sells += 1
        amt = re.search(r'\$(\d+\.\d+)', line)
        if amt: sell_total += float(amt.group(1))
    elif 'Skip BUY' in line or 'Skip SELL' in line:
        skipped += 1
    elif 'BUY FAILED' in line or 'SELL FAILED' in line:
        failed += 1

# Get current values
cash = get_cash()
our_pos_val, num_positions = get_portfolio(OUR_PROXY)
our_total = cash + our_pos_val
sovereign_val, sov_num_positions = get_portfolio(MIRROR_WALLET)

# Sovereign activity
sov_buys, sov_sells, deposits = get_sovereign_activity(one_hour_ago_ts)
deposit_line = 'Sovereign deposits: $%.0f' % sum(deposits) if deposits else 'Sovereign deposits: $0'

# Save state
STATE_FILE.write_text(json.dumps({
    'our_total': our_total,
    'sovereign_val': sovereign_val,
    'timestamp': now.strftime('%Y-%m-%d %H:%M:%S')
}))

# Compute match rates
buy_match  = int(min(buys, sov_buys)  / sov_buys  * 100) if sov_buys  > 0 else 100
sell_match = int(min(sells, sov_sells) / sov_sells * 100) if sov_sells > 0 else 100

msg = (
    '*Mirror Hourly Summary*\n\n'
    'BUYs:  ours %d ($%.2f) | sovereign %d\n'
    'SELLs: ours %d ($%.2f) | sovereign %d\n'
    'Skipped: %d | Failed: %d\n'
    'Trade match rate:\n'
    '  BUYs:  sovereign %d | ours %d \u2192 %d%%\n'
    '  SELLs: sovereign %d | ours %d \u2192 %d%%\n\n'
    '*Portfolio change (last hour):*\n'
    'Sovereign: %s\n'
    'Ours:      %s\n'
    '%s\n\n'
    'Open positions: ours %d | sovereign %d\n'
    'Portfolio: $%.0f cash + $%.0f positions = *$%.0f total*'
) % (
    buys, buy_total, sov_buys,
    sells, sell_total, sov_sells,
    skipped, failed,
    sov_buys, buys, buy_match,
    sov_sells, sells, sell_match,
    pct_str(prev_sovereign_val, sovereign_val),
    pct_str(prev_our_total, our_total),
    deposit_line,
    num_positions, sov_num_positions,
    cash, our_pos_val, our_total
)

tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, msg)
print(msg)

# Log divergence data for analysis
import os
div_log = Path('/root/polym/divergence.log')
write_header = not div_log.exists()
with open(div_log, 'a') as f:
    if write_header:
        f.write('timestamp,sov_buys,our_buys,buy_match_pct,sov_sells,our_sells,sell_match_pct,sov_pct_change,our_pct_change\n')
    # Extract numeric pct changes from strings like '\u25b2+2.3% ($109582)'
    def extract_pct(s):
        import re
        m = re.search(r'([+-]?\d+\.\d+)%', s)
        return m.group(1) if m else '0'
    sov_pct = extract_pct(pct_str(prev_sovereign_val, sovereign_val))
    our_pct = extract_pct(pct_str(prev_our_total, our_total))
    f.write('%s,%d,%d,%d,%d,%d,%d,%s,%s\n' % (
        now.strftime('%Y-%m-%d %H:%M'), sov_buys, buys, buy_match,
        sov_sells, sells, sell_match, sov_pct, our_pct
    ))
