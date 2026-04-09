# Polymarket Copy-Trading Bot

Mirrors trades from a top Polymarket trader (sovereign2013) into the user's wallet, sized proportionally.

## Server
- Host: `134.209.159.134` (root)
- Connect via `plink -ssh -pw "<pwd>" -batch root@134.209.159.134 "<cmd>"` (sshpass not available, OpenSSH password auth fails — use plink from PuTTY)
- Code lives in `/root/polym/`, venv at `/root/polym/venv/`

## Wallets
- **Our proxy**: `0x3F984E05d151d0F7164d9025C07E176d2b9AeC35`
- **sovereign2013 (mirror target)**: `0xee613b3fc183ee44f9da9c05f53e2da107e3debf`

## Key Scripts
| File | Purpose |
|---|---|
| `sovereign_mirror_v2.py` | Main order-level mirror. Polls sovereign's activity API every 20s, copies new BUYs proportionally. Runs as long-lived process. |
| `redeemer.py` | Claims resolved winning positions on-chain via Safe proxy. Cron every 1 min. |
| `hourly_report.py` | Telegram notification with BUY/SELL counts, portfolio %, sovereign comparison. Cron hourly. |
| `autonomous_trader.py` | Separate AI-driven trader. Cron every 2 min. |
| `sync_report.py` | Convergence tracker (auto-disables at 95%). |

## Key Constants (sovereign_mirror_v2.py)
- `INTERVAL = 20` (poll seconds)
- `MIN_BET = 2.5`, `MAX_BET = 100.0`
- `CASH_FLOOR = 100.0` — pause BUYs when cash < $100
- State: `sovereign_mirror_v2_state.json` (tracks `processed_tx_hashes`, `startup_tx_hashes`)
- Log: `sovereign_mirror.log`

## Sizing Formula
```
our_total = our_cash + our_positions_value
sovereign_total = /value API for sovereign
ratio = our_total / sovereign_total
our_bet = sovereign_trade_usdc × ratio  (clamped MIN_BET/MAX_BET/our_cash)
```

## Polymarket Data API (no auth needed)
- `GET /value?user=WALLET` — total portfolio value
- `GET /positions?user=WALLET&limit=500` — open positions (always use limit=500, default truncates)
- `GET /activity?user=WALLET&limit=500` — historical trades (BUY/SELL/REDEEM/MERGE)
- `GET /trades?limit=500` — global recent trades
- Polymarket leaderboard: `https://polymarket.com/leaderboard` (HTML, parse the dehydratedState JSON for proxyWallet fields)

## Sovereign Cash Estimation
Cash balance is **not directly queryable**. Best proxy:
```
sovereign_cash ≈ /value_total − sum(positions[].currentValue)
```
On-chain USDC is usually $0 because cash cycles fast (sells → immediate redeploy).

## Decisions Made (April 2026)
- **Order-level mirroring** chosen over position-level (better trade coverage)
- **SELLs disabled** — exits not copied. Hold positions to resolution. Reason: sovereign's exits are capital recycling, not loss-cutting; copying them caused us to exit FAA right before sovereign re-entered for a $12k win
- **Fresh start** done after positions diverged badly from sovereign — sold everything, reset state, restarted clean
- **Redeemer** runs every 1 min (was 3). Skips dust positions (`MIN_REDEEM_VALUE = 0.0` — accept any but losing positions revert on-chain anyway)
- **No additional wallets copied** for now (researched RN1 and k-maniac, declined)
- **No trade-size filter** for now (researched: sovereign's >$1k trades have -66% ROI but user wants more live data first)

## Known Issues
- Cash floor activates frequently with small portfolio (~$1.8k) vs sovereign ($65k+). Many BUYs skipped.
- Losing positions can't be cleared from UI via redeemPositions (contract reverts on $0)
- Activity API limit=500 may be hit during burst periods
