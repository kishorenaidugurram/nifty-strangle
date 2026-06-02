#!/data/data/com.termux/files/usr/bin/bash
"""
Nifty Strangle — Termux Auto-Setup Script
==========================================
Run this ONCE on your Android phone via Termux.
It sets up everything: deps, credentials, auto-start, watchdog, notifications.

Usage:
  pkg install curl -y
  curl -s https://raw.githubusercontent.com/kishorenaidugurram/nifty-strangle/main/termux_setup.sh | bash

Or copy this file to your phone and run:
  bash termux_setup.sh
"""

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}══════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  NIFTY STRANGLE — Termux 24/7 Auto Setup${NC}"
echo -e "${BLUE}══════════════════════════════════════════════════${NC}"
echo ""

# ─── Step 1: Update packages ───
echo -e "${YELLOW}[1/7] Updating Termux packages...${NC}"
pkg update -y && pkg upgrade -y
echo -e "${GREEN}  ✅ Packages updated${NC}"

# ─── Step 2: Install dependencies ───
echo -e "${YELLOW}[2/7] Installing Python + tools...${NC}"
pkg install -y python git openssl cronie termux-services termux-api 2>&1 | tail -1
pip install --upgrade pip 2>&1 | tail -1
pip install yfinance pandas numpy requests pyotp flask gunicorn 2>&1 | tail -1
echo -e "${GREEN}  ✅ Python + deps installed${NC}"

# ─── Step 3: Clone repo ───
echo -e "${YELLOW}[3/7] Cloning nifty-strangle repo...${NC}"
if [ -d ~/nifty-strangle ]; then
    echo -e "  📁 Repo already exists, pulling updates..."
    cd ~/nifty-strangle && git pull
else
    git clone https://github.com/kishorenaidugurram/nifty-strangle.git ~/nifty-strangle
fi
cd ~/nifty-strangle
echo -e "${GREEN}  ✅ Repo ready at ~/nifty-strangle${NC}"

# ─── Step 4: Set credentials ───
echo -e "${YELLOW}[4/7] Setting Angel One credentials...${NC}"
cat >> ~/.bashrc << 'CREDS'
export ANGEL_API_KEY='2siOJ0EZ'
export ANGEL_CLIENT_CODE='G188451'
export ANGEL_PIN='1980'
export ANGEL_TOTP_SECRET='LIONHZIIQLSN7MZEDLRSPE5HE4'
CREDS
source ~/.bashrc
echo -e "${GREEN}  ✅ Credentials saved to ~/.bashrc${NC}"

# ─── Step 5: Create auto-start on boot ───
echo -e "${YELLOW}[5/7] Setting up auto-start on phone boot...${NC}"
mkdir -p ~/.termux/boot/

cat > ~/.termux/boot/start-strangle.sh << 'BOOTSCRIPT'
#!/data/data/com.termux/files/usr/bin/bash
# Nifty Strangle — Auto-start on phone boot
# This runs the WebSocket monitor and keeps the CPU awake

# Wait for network
sleep 15

# Load credentials
source ~/.bashrc

# Kill any existing instance
pkill -f railway_monitor.py 2>/dev/null || true

# Acquire wake lock (prevents CPU sleep)
termux-wake-lock
echo "[$(date)] Wake lock acquired" >> ~/nifty-strangle/monitor.log

# Start the monitor
cd ~/nifty-strangle
python railway_monitor.py >> ~/nifty-strangle/monitor.log 2>&1 &

echo "[$(date)] Monitor started with PID $!" >> ~/nifty-strangle/monitor.log

# Notify
termux-notification -t "📈 Strangle Bot" -c "Monitor started on boot (PID: $!)" --priority high
BOOTSCRIPT

chmod +x ~/.termux/boot/start-strangle.sh
echo -e "${GREEN}  ✅ Boot script created${NC}"

# ─── Step 6: Create watchdog cron (checks every 5 min) ───
echo -e "${YELLOW}[6/7] Setting up watchdog cron (checks every 5 min)...${NC}"

# Enable cron service
sv-enable crond 2>/dev/null || true

# Add cron job
(crontab -l 2>/dev/null; echo "*/5 * * * * /data/data/com.termux/files/home/.termux/boot/start-strangle.sh") | crontab -
echo -e "${GREEN}  ✅ Watchdog cron installed (every 5 min)${NC}"

# ─── Step 7: Create management shortcuts ───
echo -e "${YELLOW}[7/7] Creating management commands...${NC}"

# Start command
cat > ~/nifty-strangle/start.sh << 'START'
#!/data/data/com.termux/files/usr/bin/bash
source ~/.bashrc
termux-wake-lock
cd ~/nifty-strangle
pkill -f railway_monitor.py 2>/dev/null || true
nohup python railway_monitor.py > monitor.log 2>&1 &
echo "✅ Monitor started (PID: $!)"
termux-notification -t "📈 Strangle Bot" -c "Monitor started" --priority high
START
chmod +x ~/nifty-strangle/start.sh

# Stop command
cat > ~/nifty-strangle/stop.sh << 'STOP'
#!/data/data/com.termux/files/usr/bin/bash
pkill -f railway_monitor.py 2>/dev/null && echo "✅ Monitor stopped" || echo "❌ Monitor not running"
termux-wake-unlock 2>/dev/null || true
STOP
chmod +x ~/nifty-strangle/stop.sh

# Status command
cat > ~/nifty-strangle/status.sh << 'STATUS'
#!/data/data/com.termux/files/usr/bin/bash
cd ~/nifty-strangle
if pgrep -f railway_monitor.py > /dev/null; then
    PID=$(pgrep -f railway_monitor.py)
    UPTIME=$(ps -o etime= -p $PID | xargs)
    echo "✅ Monitor RUNNING (PID: $PID, uptime: $UPTIME)"
else
    echo "❌ Monitor NOT running"
fi
echo ""
python bot.py status 2>/dev/null || echo "(bot.py not available)"
echo ""
echo "📋 Last 5 log lines:"
tail -5 monitor.log 2>/dev/null || echo "(no logs)"
STATUS
chmod +x ~/nifty-strangle/status.sh

# Alias for convenience
echo "alias strangle='cd ~/nifty-strangle && ./status.sh'" >> ~/.bashrc
echo "alias strangle-start='~/nifty-strangle/start.sh'" >> ~/.bashrc
echo "alias strangle-stop='~/nifty-strangle/stop.sh'" >> ~/.bashrc
source ~/.bashrc

echo -e "${GREEN}  ✅ Management scripts created${NC}"
echo -e ""
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✅ SETUP COMPLETE${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${BLUE}Commands:${NC}"
echo -e "    strangle        → Check status"
echo -e "    strangle-start  → Start the monitor"
echo -e "    strangle-stop   → Stop the monitor"
echo ""
echo -e "  ${BLUE}Or manually:${NC}"
echo -e "    cd ~/nifty-strangle && ./start.sh"
echo ""
echo -e "  ${YELLOW}⚠️  IMPORTANT — Phone Settings:${NC}"
echo -e "  1. Settings → Apps → Termux → Battery → Unrestricted"
echo -e "  2. Settings → Apps → Termux → Allow background activity"
echo -e "  3. Install Termux:Boot from F-Droid (auto-start on boot)"
echo -e "  4. Install Termux:WakeLock from F-Droid (keep CPU alive)"
echo -e "  5. Keep phone plugged in overnight"
echo ""
echo -e "  ${GREEN}→ Run 'strangle-start' now to start monitoring${NC}"
