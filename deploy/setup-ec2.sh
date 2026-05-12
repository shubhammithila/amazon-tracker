#!/bin/bash
set -e

echo "=== Amazon Tracker v2 — EC2 Setup ==="
echo "Run this on a fresh Ubuntu 22.04 LTS EC2 instance (t2.micro)"
echo ""

# System packages
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3.11-venv python3-pip git

# Create app directory
sudo mkdir -p /opt/amazon-tracker
sudo chown ubuntu:ubuntu /opt/amazon-tracker
cd /opt/amazon-tracker

# Clone or copy your code here
echo ">>> Copy your project files to /opt/amazon-tracker/"
echo ">>> e.g.: git clone <your-repo> . OR scp -r local-files ec2:~/amazon-tracker/"
echo ""

# Python virtual environment
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Create .env file
if [ ! -f .env ]; then
cat > .env <<'ENVEOF'
DATABASE_URL=postgresql+asyncpg://tracker_user:YOUR_RDS_PASSWORD@YOUR_RDS_ENDPOINT:5432/tracker
APP_PASSWORD=your-secure-password-here
SECRET_KEY=generate-with-python-c-import-secrets-secrets.token_hex-32
SCHEDULER_ENABLED=true
DAILY_SCRAPE_HOUR=6
DAILY_SCRAPE_MINUTE=0
ENVEOF
echo ">>> Edit .env with your actual RDS credentials and password"
fi

# Install Caddy (reverse proxy with auto-HTTPS)
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy

# Copy Caddy config
sudo cp deploy/caddy/Caddyfile /etc/caddy/Caddyfile
echo ">>> Edit /etc/caddy/Caddyfile with your domain name"
sudo systemctl reload caddy

# Setup systemd service
sudo cp deploy/systemd/tracker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tracker
sudo systemctl start tracker

echo ""
echo "=== Setup Complete ==="
echo "1. Edit /opt/amazon-tracker/.env with your RDS credentials"
echo "2. Edit /etc/caddy/Caddyfile with your domain"
echo "3. Run: sudo systemctl restart tracker"
echo "4. Run: sudo systemctl restart caddy"
echo "5. Access at https://your-domain.com"
