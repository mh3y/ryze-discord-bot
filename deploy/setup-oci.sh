#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup-oci.sh — First-time setup for a fresh OCI Ampere A1 Ubuntu 22.04 VM
#
# Run once after SSH-ing in:
#   chmod +x setup-oci.sh && ./setup-oci.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

echo ""
echo "=========================================="
echo "  Ryze Education — OCI Server Setup"
echo "=========================================="
echo ""

# ── 1. System update ──────────────────────────────────────────────────────────
echo "[1/6] Updating system packages..."
sudo apt-get update -y -q
sudo apt-get upgrade -y -q
sudo apt-get install -y -q \
    ca-certificates curl gnupg git \
    iptables-persistent netfilter-persistent \
    unzip htop

# ── 2. Install Docker ─────────────────────────────────────────────────────────
echo "[2/6] Installing Docker..."
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update -y -q
sudo apt-get install -y -q \
    docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin

sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker "$USER"
echo "Docker $(docker --version) installed."

# ── 3. Open firewall ports (OCI Ubuntu uses iptables) ────────────────────────
echo "[3/6] Configuring firewall..."
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80  -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
echo "Ports 80 and 443 open."
echo ""
echo "  !! Also open these ports in OCI Console:"
echo "     Networking > Virtual Cloud Networks > your-VCN"
echo "     > Security Lists > Default Security List"
echo "     Add Ingress Rules for TCP ports 80 and 443 from 0.0.0.0/0"
echo ""

# ── 4. Create app directory ───────────────────────────────────────────────────
echo "[4/6] Creating /opt/ryze directory..."
sudo mkdir -p /opt/ryze
sudo chown "$USER:$USER" /opt/ryze

# ── 5. Clone repositories ─────────────────────────────────────────────────────
echo "[5/6] Cloning repositories..."
echo ""
echo "  Enter your GitHub repo URLs when prompted."
echo "  (Find them at: GitHub > your repo > Code > HTTPS)"
echo ""

read -rp "  Bot repo URL (e.g. https://github.com/yourorg/ryze-discord-bot): " BOT_REPO
read -rp "  Frontend repo URL (e.g. https://github.com/yourorg/Ryze-Education-Prototype): " FRONTEND_REPO

cd /opt/ryze
git clone "$BOT_REPO"       ryze-discord-bot
git clone "$FRONTEND_REPO"  Ryze-Education-Prototype
echo "Repositories cloned."

# ── 6. Create production .env ─────────────────────────────────────────────────
echo "[6/6] Creating .env from template..."
cp /opt/ryze/ryze-discord-bot/.env.production.example \
   /opt/ryze/ryze-discord-bot/.env

echo ""
echo "=========================================="
echo "  Setup complete!"
echo "=========================================="
echo ""
echo "NEXT STEPS:"
echo ""
echo "  1. Edit your production .env:"
echo "       nano /opt/ryze/ryze-discord-bot/.env"
echo ""
echo "     Fill in:"
echo "       DB_PASSWORD       — strong random password"
echo "       DISCORD_TOKEN     — your bot token"
echo "       DASHBOARD_API_KEY — run: python3 -c \"import secrets; print(secrets.token_hex(32))\""
echo "       JWT_SECRET_KEY    — run: python3 -c \"import secrets; print(secrets.token_hex(32))\""
echo "       GOOGLE_*          — your Google Calendar credentials"
echo ""
echo "  2. Start everything:"
echo "       cd /opt/ryze/ryze-discord-bot"
echo "       docker compose up -d"
echo ""
echo "  3. Check logs:"
echo "       docker compose logs -f"
echo ""
echo "  4. Point your domain DNS A record to this server's IP:"
echo "       $(curl -s ifconfig.me 2>/dev/null || echo '<your-server-ip>')"
echo ""
echo "  NOTE: Log out and back in (or run 'newgrp docker') before using docker."
echo ""
