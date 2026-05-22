#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy.sh — Pull latest code and redeploy all services with zero-downtime.
#
# Run from the OCI server:
#   cd /opt/ryze/ryze-discord-bot && ./deploy/deploy.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BOT_DIR="/opt/ryze/ryze-discord-bot"
FRONTEND_DIR="/opt/ryze/Ryze-Education-Prototype"

echo ""
echo "=========================================="
echo "  Ryze Education — Deploy"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="
echo ""

# ── Pull latest code ──────────────────────────────────────────────────────────
echo "[1/4] Pulling latest code..."
git -C "$BOT_DIR"      pull origin main
git -C "$FRONTEND_DIR" pull origin main

# ── Rebuild containers ────────────────────────────────────────────────────────
echo "[2/4] Building containers (this may take a few minutes)..."
cd "$BOT_DIR"
docker compose build

# ── Run migrations before bringing up new services ────────────────────────────
echo "[3/4] Running database migrations..."
docker compose run --rm migrate

# ── Restart services ──────────────────────────────────────────────────────────
echo "[4/4] Restarting services..."
docker compose up -d --remove-orphans

echo ""
echo "=========================================="
echo "  Deploy complete!"
echo "=========================================="
echo ""
docker compose ps
echo ""
echo "View logs:  docker compose logs -f [api|bot|web|db]"
echo ""
