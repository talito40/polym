"""
Every 5 hours: report convergence between our portfolio and sovereign2013's.
Auto-disables cron job when convergence reaches 95%.
"""
import requests, sys, subprocess
sys.path.insert(0, '/root/polym')
from dotenv import load_dotenv
load_dotenv('/root/polym/.env')
import telegram_notify as tg
import market_fetcher, config
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

MIRROR_WALLET = '0xee613b3fc183ee44f9da9c05f53e2da107e3debf'
OUR_PROXY     = '0x3F984E05d151d0F7164d9025C07E176d2b9AeC35'
CONVERGENCE_THRESHOLD = 0.95

def fetch_assets(wallet, threshold=0.001):
    r = requests.get(
        'https://data-api.polymarket.com/positions?user=%s&sizeThreshold=%s&limit=500' % (wallet, threshold),
        timeout=15
    )
    data = [p for p in r.json() if isinstance(p, dict)]
    return {p['asset']: p for p in data if p.get('asset')}

def get_cash():
    try:
        client = market_fetcher.get_clob_client()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=config.SIGNATURE_TYPE)
        b = client.get_balance_allowance(params)
        return float(b.get('balance', 0)) / 1e6
    except:
        return 0.0

def remove_cron():
    """Remove the sync_report cron job."""
    result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    lines = [l for l in result.stdout.splitlines() if 'sync_report' not in l]
    new_cron = chr(10).join(lines) + chr(10)
    subprocess.run(['crontab', '-'], input=new_cron, text=True)
    print('Cron job removed -- 95% convergence reached.')

# Fetch positions
sovereign = fetch_assets(MIRROR_WALLET)
ours = fetch_assets(OUR_PROXY)

sovereign_assets = set(sovereign.keys())
our_assets = set(ours.keys())

in_sync     = sovereign_assets & our_assets
theirs_only = sovereign_assets - our_assets
ours_only   = our_assets - sovereign_assets

total_sovereign = len(sovereign_assets)
convergence = len(in_sync) / total_sovereign if total_sovereign > 0 else 0.0

# Portfolio value
cash = get_cash()
positions_value = sum(float(p.get('currentValue', 0)) for p in ours.values())
total = cash + positions_value

msg = (
    '*Mirror Sync Update*\n\n'
    'Sovereign2013: %d positions\n'
    'Ours: %d positions\n\n'
    'In sync: %d (we both hold)\n'
    'Theirs only: %d (waiting for re-entry)\n'
    'Ours only: %d (they exited, we still hold)\n\n'
    'Convergence: %.0f%% (%d/%d)\n\n'
    'Portfolio: $%.0f cash + $%.0f positions = *$%.0f total*'
) % (
    total_sovereign, len(our_assets),
    len(in_sync),
    len(theirs_only),
    len(ours_only),
    convergence * 100, len(in_sync), total_sovereign,
    cash, positions_value, total
)

tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, msg)
print(msg)

if convergence >= CONVERGENCE_THRESHOLD:
    tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
        '95%% convergence reached -- disabling sync report.')
    remove_cron()
