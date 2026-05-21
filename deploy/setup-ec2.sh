#!/bin/bash
set -e

echo "=============================================="
echo "  Amazon Tracker v2 — EC2 Deployment Script"
echo "  Ubuntu 22.04 LTS | t2.micro | SQLite"
echo "=============================================="
echo ""

# ─── System Packages ───────────────────────────────────────
echo ">>> Installing system packages..."
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3.11-venv python3-pip git curl

# ─── App Directory ─────────────────────────────────────────
echo ">>> Setting up app directory..."
sudo mkdir -p /opt/amazon-tracker
sudo chown ubuntu:ubuntu /opt/amazon-tracker

# ─── Clone Repo ────────────────────────────────────────────
echo ">>> Cloning repository..."
cd /opt/amazon-tracker

if [ -d ".git" ]; then
    echo "    Repository already exists, pulling latest..."
    git pull
else
    echo "    Enter your GitHub repo URL (or press Enter to skip and upload manually):"
    read -r REPO_URL
    if [ -n "$REPO_URL" ]; then
        git clone "$REPO_URL" .
    else
        echo "    Skipping clone. Upload files manually with:"
        echo "    scp -i your-key.pem -r ./* ubuntu@YOUR_EC2_IP:/opt/amazon-tracker/"
    fi
fi

# ─── Python Virtual Environment ───────────────────────────
echo ">>> Setting up Python environment..."
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# ─── Environment File ─────────────────────────────────────
if [ ! -f .env ]; then
    echo ">>> Creating .env file..."
    # Generate a random secret key
    SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    cat > .env <<ENVEOF
# Amazon Tracker v2 Configuration
DATABASE_URL=sqlite+aiosqlite:///./tracker.db
APP_PASSWORD=admin123
SECRET_KEY=${SECRET}
SCHEDULER_ENABLED=true
DAILY_SCRAPE_HOUR=6
DAILY_SCRAPE_MINUTE=0
ENVEOF
    echo "    .env created with default settings"
    echo "    >>> IMPORTANT: Change APP_PASSWORD in /opt/amazon-tracker/.env"
else
    echo "    .env already exists, skipping..."
fi

# ─── Create data directory for SQLite ─────────────────────
mkdir -p /opt/amazon-tracker/data

# ─── Install Caddy (reverse proxy) ────────────────────────
echo ">>> Installing Caddy web server..."
if ! command -v caddy &> /dev/null; then
    sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
    sudo apt update
    sudo apt install -y caddy
else
    echo "    Caddy already installed"
fi

# ─── Configure Caddy (IP-only, port 80) ──────────────────
echo ">>> Configuring Caddy for IP access..."
sudo cp deploy/caddy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl restart caddy
sudo systemctl enable caddy

# ─── Setup Systemd Service ────────────────────────────────
echo ">>> Setting up systemd service..."
sudo cp deploy/systemd/tracker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tracker
sudo systemctl start tracker

# ─── Open Firewall (if ufw is active) ────────────────────
if command -v ufw &> /dev/null && sudo ufw status | grep -q "active"; then
    echo ">>> Configuring firewall..."
    sudo ufw allow 80/tcp
    sudo ufw allow 22/tcp
    sudo ufw reload
fi

# ─── Verify ──────────────────────────────────────────────
echo ""
echo ">>> Waiting for app to start..."
sleep 3

if curl -s http://localhost:8000 > /dev/null 2>&1; then
    echo ""
    echo "=============================================="
    echo "  DEPLOYMENT SUCCESSFUL!"
    echo "=============================================="
    echo ""
    # Get public IP
    PUBLIC_IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "YOUR_EC2_IP")
    echo "  Access your app at: http://${PUBLIC_IP}"
    echo "  Password: admin123 (change in .env!)"
    echo ""
    echo "  Useful commands:"
    echo "    sudo systemctl status tracker   # Check status"
    echo "    sudo journalctl -u tracker -f   # View logs"
    echo "    sudo systemctl restart tracker  # Restart app"
    echo "    cd /opt/amazon-tracker && git pull && sudo systemctl restart tracker  # Update"
    echo ""
else
    echo ""
    echo "  App may still be starting. Check with:"
    echo "    sudo systemctl status tracker"
    echo "    sudo journalctl -u tracker -f"
fi
