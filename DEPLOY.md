# Deploy Amazon Tracker to AWS EC2 (Free Tier)

## What You Get
- **Always-on** app at `http://YOUR_EC2_IP`
- **Free for 12 months** (t2.micro = 750 hrs/month)
- **~200MB RAM** usage (well within 1GB limit)
- **SQLite** database (no separate DB to manage)
- **Auto-restarts** on crash (systemd)
- **Daily scrapes** run automatically at 6 AM IST

---

## Step 1: Launch EC2 Instance

1. Go to [AWS Console → EC2](https://console.aws.amazon.com/ec2)
2. Click **Launch Instance**
3. Configure:
   - **Name**: `amazon-tracker`
   - **AMI**: Ubuntu Server 22.04 LTS (Free tier eligible)
   - **Instance type**: `t2.micro` (Free tier eligible)
   - **Key pair**: Create new → download `.pem` file (save it safe!)
   - **Network settings**: 
     - Allow SSH (port 22) — your IP only
     - Allow HTTP (port 80) — anywhere (0.0.0.0/0)
   - **Storage**: 8 GB gp3 (free tier allows up to 30 GB)
4. Click **Launch Instance**

---

## Step 2: Connect to Your Instance

Wait 1-2 minutes for the instance to start, then:

```bash
# On Windows (PowerShell):
ssh -i "C:\path\to\your-key.pem" ubuntu@YOUR_EC2_PUBLIC_IP

# On Windows (if permission error on .pem):
icacls "your-key.pem" /inheritance:r /grant:r "%username%:R"
```

Find your Public IP: EC2 Dashboard → Instances → click your instance → Public IPv4 address

---

## Step 3: Deploy the App

### Option A: Upload from your PC (Recommended if GitHub auth isn't set up)

**From your local PowerShell** (not SSH):
```powershell
# Compress the project (exclude venv, __pycache__, .git)
cd "C:\Users\LENOVO\Desktop\Claude\Amazon Tracker\.claude\worktrees\stoic-allen-bb3a55"

# Create a zip of essential files
tar -czf tracker.tar.gz --exclude=venv --exclude=__pycache__ --exclude=.git --exclude=tracker.db --exclude=*.pyc app/ templates/ static/ deploy/ requirements.txt Dockerfile alembic.ini alembic/

# Upload to EC2
scp -i "C:\path\to\your-key.pem" tracker.tar.gz ubuntu@YOUR_EC2_IP:~/
```

**Then on EC2 (SSH)**:
```bash
sudo mkdir -p /opt/amazon-tracker
sudo chown ubuntu:ubuntu /opt/amazon-tracker
cd /opt/amazon-tracker
tar -xzf ~/tracker.tar.gz
chmod +x deploy/setup-ec2.sh
./deploy/setup-ec2.sh
```

### Option B: Clone from GitHub

On EC2:
```bash
cd /opt
sudo mkdir amazon-tracker && sudo chown ubuntu:ubuntu amazon-tracker
cd amazon-tracker
git clone https://github.com/shubhammithila/amazon-tracker.git .
chmod +x deploy/setup-ec2.sh
./deploy/setup-ec2.sh
```

---

## Step 4: Access Your App

Open browser: `http://YOUR_EC2_PUBLIC_IP`

- **Password**: `admin123` (change in `/opt/amazon-tracker/.env`)
- The app auto-starts on boot
- Daily scrapes run at 6 AM IST automatically

---

## Managing Your Deployment

### Common Commands (run on EC2 via SSH)

```bash
# Check if app is running
sudo systemctl status tracker

# View live logs
sudo journalctl -u tracker -f

# Restart app
sudo systemctl restart tracker

# Stop app
sudo systemctl stop tracker

# Update code (if using git)
cd /opt/amazon-tracker
git pull
sudo systemctl restart tracker

# Change password
nano /opt/amazon-tracker/.env   # edit APP_PASSWORD
sudo systemctl restart tracker

# Backup database
cp /opt/amazon-tracker/tracker.db ~/tracker-backup-$(date +%Y%m%d).db
```

### Update Code (from local PC, no git)
```powershell
# Zip and upload updated files
cd "C:\Users\LENOVO\Desktop\Claude\Amazon Tracker\.claude\worktrees\stoic-allen-bb3a55"
tar -czf tracker.tar.gz --exclude=venv --exclude=__pycache__ --exclude=.git --exclude=tracker.db app/ templates/ static/ deploy/ requirements.txt

scp -i "C:\path\to\your-key.pem" tracker.tar.gz ubuntu@YOUR_EC2_IP:~/

# Then on EC2:
cd /opt/amazon-tracker
tar -xzf ~/tracker.tar.gz
sudo systemctl restart tracker
```

---

## Security Notes

1. **Change the password** in `.env` immediately after first deploy
2. **Restrict SSH** to your IP only (already done in Security Group)
3. **HTTP port 80** is open to everyone (needed to access the app)
4. For HTTPS later: buy a domain, point DNS to EC2 IP, update Caddyfile with domain name (Caddy auto-provisions SSL)

---

## Cost Breakdown (Free Tier)

| Resource | Free Tier Limit | Our Usage |
|----------|----------------|-----------|
| EC2 t2.micro | 750 hrs/month | ~720 hrs (always on) |
| EBS Storage | 30 GB | 8 GB |
| Data Transfer | 15 GB/month out | <1 GB |
| **Total** | — | **$0/month** |

After 12 months: ~$8.50/month (or switch to Lightsail at $3.50/month)

---

## Troubleshooting

### App not accessible?
1. Check Security Group allows port 80 inbound from 0.0.0.0/0
2. Check app is running: `sudo systemctl status tracker`
3. Check Caddy is running: `sudo systemctl status caddy`
4. Check logs: `sudo journalctl -u tracker --since "5 min ago"`

### App crashes/OOM?
- Unlikely (uses ~200MB, instance has 1GB)
- If it happens: `sudo journalctl -u tracker -n 100`
- Reduce concurrency: edit `.env`, add `SCRAPE_CONCURRENCY=5`

### Database corruption?
- Backup: `cp tracker.db tracker.db.bak`
- Reset: delete `tracker.db`, restart app (creates fresh)

### Can't SSH?
- Check your IP hasn't changed (Security Group SSH rule)
- Check .pem file permissions
- Try: `ssh -v -i key.pem ubuntu@IP` for debug output
