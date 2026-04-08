import re, requests, json
from pathlib import Path
from datetime import datetime

# 1. Count our mirror BUYs from log and find start time
mirror_log = Path('/root/polym/sovereign_mirror.log').read_text()

buy_lines = re.findall(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] BUY executed:', mirror_log)
print(f'Our mirror BUYs logged: {len(buy_lines)}')
if buy_lines:
    print(f'  First buy: {buy_lines[0]}')
    print(f'  Last buy:  {buy_lines[-1]}')

# Also count NEW position detections (before buy attempt)
new_detections = re.findall(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] NEW position:', mirror_log)
print(f'NEW positions detected: {len(new_detections)}')

# Count skipped buys
skip_resolved_loser = len(re.findall(r'Skip BUY \u2014 resolved loser', mirror_log))
skip_resolved_winner = len(re.findall(r'Skip BUY \u2014 resolved winner', mirror_log))
skip_already_hold = len(re.findall(r'Skip BUY \u2014 already holding', mirror_log))
buy_failed = len(re.findall(r'BUY FAILED:', mirror_log))
print(f'Skipped (resolved loser): {skip_resolved_loser}')
print(f'Skipped (resolved winner): {skip_resolved_winner}')
print(f'Skipped (already holding): {skip_already_hold}')
print(f'BUY FAILED: {buy_failed}')
print(f'Expected = {len(buy_lines)} executed + {skip_resolved_loser} loser + {skip_resolved_winner} winner + {skip_already_hold} holding + {buy_failed} failed = {len(buy_lines)+skip_resolved_loser+skip_resolved_winner+skip_already_hold+buy_failed}')

# 2. Fetch sovereign2013 trade history from Polymarket
start_match = re.search(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', mirror_log)
if start_match:
    start_str = start_match.group(1)
    start_dt = datetime.strptime(start_str, '%Y-%m-%d %H:%M:%S')
    start_ts = int(start_dt.timestamp())
    print(f'\nMirror started: {start_str} (unix: {start_ts})')

SOVEREIGN = '0xee613b3fc183ee44f9da9c05f53e2da107e3debf'
print(f'\nFetching sovereign2013 activity...')

try:
    r = requests.get(f'https://data-api.polymarket.com/activity?user={SOVEREIGN}&limit=500', timeout=15)
    data = r.json()
    print(f'Activity API returned {len(data)} records')
    if data and isinstance(data[0], dict):
        print(f'Fields: {list(data[0].keys())}')
        print(f'Sample: {json.dumps(data[0], indent=2)[:300]}')
except Exception as e:
    print(f'Activity API error: {e}')

try:
    r2 = requests.get(f'https://data-api.polymarket.com/trades?user={SOVEREIGN}&limit=500', timeout=15)
    data2 = r2.json()
    print(f'\nTrades API returned {len(data2)} records')
    if data2 and isinstance(data2[0], dict):
        print(f'Fields: {list(data2[0].keys())}')
        print(f'Sample: {json.dumps(data2[0], indent=2)[:300]}')
except Exception as e:
    print(f'Trades API error: {e}')
