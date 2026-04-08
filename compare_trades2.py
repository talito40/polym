import re, requests, json
from pathlib import Path

SOVEREIGN = '0xee613b3fc183ee44f9da9c05f53e2da107e3debf'
START_TS = 1775613539  # when mirror started: 2026-04-08 01:58:59

# Fetch all sovereign2013 BUY activity since mirror start, with pagination
all_buys = []
offset = 0
limit = 500

print(f'Fetching sovereign2013 BUY activity since mirror start...')
while True:
    r = requests.get(
        f'https://data-api.polymarket.com/activity?user={SOVEREIGN}&limit={limit}&offset={offset}',
        timeout=15
    )
    data = r.json()
    if not data:
        break

    # Filter to BUY trades after our start time
    for rec in data:
        ts = int(rec.get('timestamp', 0))
        side = rec.get('side', '') or rec.get('type', '')
        if ts >= START_TS and str(side).upper() == 'BUY':
            all_buys.append(rec)

    # If we got fewer than limit, we've hit the end; also stop if oldest record is before our start
    oldest_ts = min(int(r.get('timestamp', 0)) for r in data)
    print(f'  Batch offset={offset}: {len(data)} records, oldest_ts={oldest_ts}, buys_so_far={len(all_buys)}')

    if len(data) < limit or oldest_ts < START_TS:
        break
    offset += limit

print(f'\nTotal sovereign2013 BUY transactions since mirror start: {len(all_buys)}')

# Count unique assets (new positions opened)
unique_assets = set(b.get('asset', '') for b in all_buys)
print(f'Unique assets bought: {len(unique_assets)}')

# Load sovereign mirror state to see what was in initial snapshot
import sys
sys.path.insert(0, '/root/polym')
state_file = Path('/root/polym/sovereign_mirror_state.json')
if state_file.exists():
    state = json.loads(state_file.read_text())
    known = set(state.get('sovereign_positions', {}).keys())
    print(f'Assets in initial snapshot (ignored by mirror): {len(known)}')
    new_assets = unique_assets - known
    print(f'Assets bought AFTER mirror start (not in snapshot): {len(new_assets)}')
else:
    print('State file not found')

# Compare with our log
mirror_log = Path('/root/polym/sovereign_mirror.log').read_text()
our_buys = re.findall(r'BUY executed:', mirror_log)
our_new_detections = re.findall(r'NEW position:', mirror_log)
print(f'\nOur mirror log:')
print(f'  NEW positions detected: {len(our_new_detections)}')
print(f'  BUY executed: {len(our_buys)}')
