import os
from dotenv import load_dotenv

load_dotenv()

# --- Polymarket credentials ---
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_SECRET = os.getenv("POLYMARKET_SECRET", "")
POLYMARKET_PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE", "")

# --- CLOB settings ---
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

# --- Anthropic ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# --- Safety ---
DRY_RUN = os.getenv("DRY_RUN", "True").lower() not in ("false", "0", "no")

# --- Risk management ---
MAX_ORDER_SIZE_USDC = float(os.getenv("MAX_ORDER_SIZE_USDC", "10"))
MIN_EDGE_PCT = float(os.getenv("MIN_EDGE_PCT", "0.05"))   # 5% edge required
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.40"))  # exit if down 40%
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.80"))  # exit if up 80%
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "5"))

# --- Market filters ---
MIN_VOLUME_USDC = float(os.getenv("MIN_VOLUME_USDC", "10000"))
MIN_DAYS_TO_CLOSE = float(os.getenv("MIN_DAYS_TO_CLOSE", "1"))
MAX_DAYS_TO_CLOSE = float(os.getenv("MAX_DAYS_TO_CLOSE", "30"))

# --- State file ---
STATE_FILE = os.getenv("STATE_FILE", "state.json")
