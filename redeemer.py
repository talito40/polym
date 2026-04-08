"""
Auto-redeemer — scans for resolved Polymarket positions and redeems
winning tokens back to USDC on-chain via the Gnosis Safe proxy wallet.

Runs every 15 minutes via cron.

Flow:
  1. Fetch all positions with redeemable=true from Polymarket data API
  2. Skip dust (currentValue < MIN_REDEEM_VALUE) — not worth gas
  3. Skip conditions that failed recently (tracked in redeem_skip.json)
  4. For each real winner, call redeemPositions on CTF via the Safe proxy
  5. Notify via Telegram only for meaningful redemptions
"""

import sys, json, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from web3 import Web3
from eth_account import Account

sys.path.insert(0, str(Path(__file__).parent))
import config
import telegram_notify as tg

LOG_FILE       = Path(__file__).parent / "redeemer.log"
SKIP_FILE      = Path(__file__).parent / "redeem_skip.json"

# Minimum USDC value worth redeeming — skips dust positions
MIN_REDEEM_VALUE = 0.10
# How long to skip a failed condition before retrying (days)
SKIP_RETRY_DAYS  = 7

# ── Polygon / contract constants ──────────────────────────────────────────────
POLYGON_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
]
CTF_ADDRESS   = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
USDC_ADDRESS  = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
ZERO_BYTES32  = b"\x00" * 32
ZERO_ADDRESS  = "0x0000000000000000000000000000000000000000"
DATA_API      = "https://data-api.polymarket.com"

CTF_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken",    "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId",        "type": "bytes32"},
            {"name": "indexSets",          "type": "uint256[]"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    }
]

SAFE_ABI = [
    {
        "name": "nonce",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "getTransactionHash",
        "type": "function",
        "inputs": [
            {"name": "to",             "type": "address"},
            {"name": "value",          "type": "uint256"},
            {"name": "data",           "type": "bytes"},
            {"name": "operation",      "type": "uint8"},
            {"name": "safeTxGas",      "type": "uint256"},
            {"name": "baseGas",        "type": "uint256"},
            {"name": "gasPrice",       "type": "uint256"},
            {"name": "gasToken",       "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "_nonce",         "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "view",
    },
    {
        "name": "execTransaction",
        "type": "function",
        "inputs": [
            {"name": "to",             "type": "address"},
            {"name": "value",          "type": "uint256"},
            {"name": "data",           "type": "bytes"},
            {"name": "operation",      "type": "uint8"},
            {"name": "safeTxGas",      "type": "uint256"},
            {"name": "baseGas",        "type": "uint256"},
            {"name": "gasPrice",       "type": "uint256"},
            {"name": "gasToken",       "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "signatures",     "type": "bytes"},
        ],
        "outputs": [{"name": "success", "type": "bool"}],
        "stateMutability": "payable",
    },
]


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ── Skip list ─────────────────────────────────────────────────────────────────

def load_skip_list() -> dict:
    if SKIP_FILE.exists():
        try:
            return json.loads(SKIP_FILE.read_text())
        except Exception:
            pass
    return {}


def save_skip_list(skip: dict):
    SKIP_FILE.write_text(json.dumps(skip, indent=2))


def is_skipped(condition_id: str, skip: dict) -> bool:
    entry = skip.get(condition_id)
    if not entry:
        return False
    skip_until = datetime.fromisoformat(entry["skip_until"])
    if datetime.now(timezone.utc) < skip_until:
        return True
    del skip[condition_id]
    return False


def mark_skip(condition_id: str, title: str, reason: str, skip: dict):
    skip_until = (datetime.now(timezone.utc) + timedelta(days=SKIP_RETRY_DAYS)).isoformat()
    skip[condition_id] = {"title": title[:60], "reason": reason, "skip_until": skip_until}
    log(f"  Marked skip for {SKIP_RETRY_DAYS}d: '{title[:45]}' ({reason})")


# ── Web3 setup ────────────────────────────────────────────────────────────────

def get_w3() -> Web3:
    for rpc in POLYGON_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
            block = w3.eth.block_number
            log(f"Connected to Polygon via {rpc} (block {block})")
            return w3
        except Exception as e:
            log(f"RPC {rpc} failed: {e}")
    raise RuntimeError("Could not connect to any Polygon RPC")


# ── Position fetching ─────────────────────────────────────────────────────────

def fetch_redeemable_positions(proxy_wallet: str) -> list:
    import requests as req
    try:
        r = req.get(
            f"{DATA_API}/positions",
            params={"user": proxy_wallet, "sizeThreshold": "0"},
            timeout=10,
        )
        data = r.json()
        positions = data if isinstance(data, list) else []
        redeemable = [p for p in positions if p.get("redeemable")]
        seen = {}
        for p in redeemable:
            cid = p.get("conditionId", "")
            if cid and cid not in seen:
                seen[cid] = p
        return list(seen.values())
    except Exception as e:
        log(f"Error fetching positions: {e}")
        return []


# ── Redemption ────────────────────────────────────────────────────────────────

def redeem_position(w3: Web3, account, proxy_wallet: str, condition_id_hex: str) -> bool:
    ctf  = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
    safe = w3.eth.contract(
        address=Web3.to_checksum_address(proxy_wallet), abi=SAFE_ABI
    )

    condition_id_bytes = bytes.fromhex(condition_id_hex.lstrip("0x"))
    call_data = ctf.encode_abi(
        "redeemPositions",
        [USDC_ADDRESS, ZERO_BYTES32, condition_id_bytes, [1, 2]],
    )

    try:
        nonce = safe.functions.nonce().call()
    except Exception as e:
        log(f"  Failed to get Safe nonce: {e}")
        return False

    try:
        safe_tx_hash = safe.functions.getTransactionHash(
            CTF_ADDRESS, 0, call_data, 0, 0, 0, 0,
            ZERO_ADDRESS, ZERO_ADDRESS, nonce,
        ).call()
    except Exception as e:
        log(f"  Failed to get Safe tx hash: {e}")
        return False

    signed = account.unsafe_sign_hash(safe_tx_hash)
    sig = signed.signature

    eoa_balance_matic = w3.from_wei(w3.eth.get_balance(account.address), "ether")
    if eoa_balance_matic < 0.001:
        log(f"  EOA has only {eoa_balance_matic:.6f} MATIC — not enough for gas")
        return False

    try:
        gas_price = w3.eth.gas_price
        tx_nonce  = w3.eth.get_transaction_count(account.address, "pending")
        tx = safe.functions.execTransaction(
            CTF_ADDRESS, 0, call_data, 0, 0, 0, 0,
            ZERO_ADDRESS, ZERO_ADDRESS, sig,
        ).build_transaction({
            "from":     account.address,
            "nonce":    tx_nonce,
            "gas":      300_000,
            "gasPrice": int(gas_price * 1.2),
            "chainId":  137,
        })
        signed_tx = account.sign_transaction(tx)
        tx_hash   = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        log(f"  Tx sent: {tx_hash.hex()}")

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt.status == 1:
            log(f"  Confirmed in block {receipt.blockNumber}")
            return True
        else:
            log(f"  Tx reverted: {tx_hash.hex()}")
            return False
    except Exception as e:
        log(f"  execTransaction failed: {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    log("=== Redeemer Run ===")

    proxy_wallet = config.POLYMARKET_PROXY_WALLET
    private_key  = config.POLYMARKET_PRIVATE_KEY
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    account = Account.from_key(private_key)

    all_redeemable = fetch_redeemable_positions(proxy_wallet)
    if not all_redeemable:
        log("No redeemable positions found.")
        return

    log(f"Found {len(all_redeemable)} redeemable position(s) — filtering...")

    skip = load_skip_list()
    to_redeem = []

    for p in all_redeemable:
        cid       = p.get("conditionId", "")
        title     = p.get("title", "")
        cur_value = float(p.get("currentValue") or 0)

        # Skip dust — not worth gas, silence permanently
        if cur_value < MIN_REDEEM_VALUE:
            if not is_skipped(cid, skip):
                mark_skip(cid, title, f"dust (${cur_value:.4f})", skip)
            continue

        # Skip recently failed conditions
        if is_skipped(cid, skip):
            log(f"  Skipping '{title[:50]}' (in skip list)")
            continue

        log(f"  Queued: '{title[:55]}' | value=${cur_value:.2f}")
        to_redeem.append(p)

    save_skip_list(skip)

    if not to_redeem:
        log("No positions above $0.10 threshold. Nothing to redeem.")
        return

    w3 = get_w3()
    eoa_matic = w3.from_wei(w3.eth.get_balance(account.address), "ether")
    log(f"EOA MATIC: {eoa_matic:.6f}")

    if eoa_matic < 0.001:
        msg = (
            "*Redeemer: Low MATIC*\n"
            f"EOA `{account.address}` needs MATIC for gas.\n"
            f"Balance: {eoa_matic:.6f} — send at least 0.01 MATIC on Polygon."
        )
        tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, msg)
        return

    redeemed = []
    failed   = []

    for pos in to_redeem:
        cid       = pos.get("conditionId", "")
        title     = pos.get("title", "")[:55]
        cur_value = float(pos.get("currentValue") or 0)

        log(f"Redeeming: {title} (${cur_value:.2f})")
        success = redeem_position(w3, account, proxy_wallet, cid)

        if success:
            redeemed.append((title, cur_value))
        else:
            failed.append(title)
            mark_skip(cid, title, "tx failed", skip)

        time.sleep(3)

    save_skip_list(skip)
    log(f"Done. Redeemed: {len(redeemed)} | Failed: {len(failed)}")

    # Only send Telegram notification for real money redemptions
    if redeemed and config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID:
        total = sum(v for _, v in redeemed)
        lines = [f"*Positions Redeemed* (+${total:.2f} USDC)"]
        for t, v in redeemed:
            lines.append(f"+ {t} (${v:.2f})")
        if failed:
            lines.append("\nFailed (will retry in 7 days):")
            for t in failed:
                lines.append(f"- {t}")
        tg.send_message(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, "\n".join(lines))


if __name__ == "__main__":
    run()
