#!/bin/bash
set -e

echo "=== Polymarket Bot Setup ==="

# Install dependencies
echo "[1/4] Installing Python dependencies..."
pip install -r requirements.txt

# Create .env if it doesn't exist
echo "[2/4] Setting up .env..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo "  Created .env from .env.example — fill in your credentials"
else
    echo "  .env already exists — skipping"
fi

# Create logs directory
echo "[3/4] Creating logs directory..."
mkdir -p logs

# Set up cron job (runs every 6 hours)
echo "[4/4] Setting up cron job..."
CRON_JOB="0 */6 * * * cd $(pwd) && python main.py --run >> logs/bot.log 2>&1"
EXISTING=$(crontab -l 2>/dev/null || true)

if echo "$EXISTING" | grep -qF "polym"; then
    echo "  Cron job already configured — skipping"
else
    (echo "$EXISTING"; echo "$CRON_JOB") | crontab -
    echo "  Cron job added: runs every 6 hours"
fi

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env and fill in your POLYMARKET_PRIVATE_KEY and ANTHROPIC_API_KEY"
echo "  2. Run: python main.py --analyze   (test analysis without trading)"
echo "  3. Run: python main.py --run       (DRY_RUN=True by default)"
echo "  4. Set DRY_RUN=False in .env when ready to go live"
echo ""
