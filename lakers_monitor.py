"""
Lakers position monitor.
Checks every 60s. When value >= TARGET_VALUE, sends Telegram alert and waits
for user to reply YES to auto-sell, or anything else to keep holding.
"""
import requests, time, sys, math
sys.path.insert(0, '/root/polym')
from dotenv import load_dotenv
load_dotenv('/root/polym/.env')
import telegram_notify as tg
import market_fetcher
import config

PROXY         = '0x3F984E05d151d0F7164d9025C07E176d2b9AeC35'
ASSET         = '111726757334603814182390046883296764670940579510837431638588399743609803956294'
INITIAL_VALUE = 95.2144
TARGET_PROFIT = 500.0
TARGET_VALUE  = INITIAL_VALUE + TARGET_PROFIT  # ~$595.21
INTERVAL      = 60   # seconds between checks
CONFIRM_WAIT  = 600  # 10 min to reply YES before resuming monitoring

print('Lakers monitor started. Target: $%.2f (profit $%.2f)' % (TARGET_VALUE, TARGET_PROFIT))
tg.send_message(
    config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
    'Lakers monitor started.\nWill alert when position reaches $%.2f (+$500 profit).\nChecking every 60s.' % TARGET_VALUE
)


def fetch_position():
    r = requests.get(
        'https://data-api.polymarket.com/positions?user=%s&sizeThreshold=0.001' % PROXY,
        timeout=10
    )
    positions = [p for p in r.json() if isinstance(p, dict)]
    return next((p for p in positions if p.get('asset') == ASSET), None)


def sell_position(token_id, cur_value, cur_price, pnl):
    size = math.floor(cur_value * 100) / 100
    order_id = market_fetcher.place_market_order(
        token_id=token_id,
        size_usdc=size,
        side='SELL',
    )
    if order_id:
        tg.send_message(
            config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
            '*Lakers sold!*\n\nSold $%.2f worth at price %.3f\nProfit: +$%.2f\nOrder: %s' % (
                cur_value, cur_price, pnl, order_id)
        )
        print('Sold. Order: %s' % order_id)
        return True
    else:
        tg.send_message(
            config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
            'Sell order FAILED. Please sell manually on Polymarket.'
        )
        print('Sell FAILED.')
        return False


while True:
    try:
        pos = fetch_position()

        if pos is None:
            print('Position not found — may be resolved or already sold.')
            tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
                'Lakers monitor: position no longer found (resolved or sold). Stopping.')
            break

        cur_value = float(pos.get('currentValue', 0))
        cur_price = float(pos.get('curPrice', 0))
        pnl       = cur_value - INITIAL_VALUE
        token_id  = pos.get('asset', ASSET)

        print('Lakers: price=%.3f  value=$%.2f  pnl=$%+.2f  (target $%.2f)' % (
            cur_price, cur_value, pnl, TARGET_VALUE))

        if cur_value >= TARGET_VALUE:
            # Send alert and wait for YES reply
            sent_at = int(time.time())
            tg.send_message(
                config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
                '*Lakers - $500 Profit Target Hit!*\n\n'
                'Position value: $%.2f\n'
                'Profit: +$%.2f\n'
                'Lakers token price: %.3f\n\n'
                'Reply *yes* to sell now and lock in profit.\n'
                '(10 min timeout — no reply = keep holding)' % (cur_value, pnl, cur_price)
            )
            print('Alert sent! Waiting for YES reply...')

            reply = tg.poll_for_reply(
                config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
                sent_at=sent_at, wait_seconds=CONFIRM_WAIT
            )

            if reply and reply.lower().strip() in ('yes', 'y'):
                print('User said YES — selling...')
                sold = sell_position(token_id, cur_value, cur_price, pnl)
                if sold:
                    break
                # If sell failed, keep monitoring so user can retry
            else:
                tg.send_message(
                    config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
                    'OK, holding Lakers position. Will alert again if target is hit again.'
                )
                print('User did not confirm — holding. Resuming monitoring.')

        time.sleep(INTERVAL)

    except Exception as e:
        print('Error: %s' % e)
        time.sleep(30)
