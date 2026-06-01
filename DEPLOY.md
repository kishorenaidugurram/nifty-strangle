# Nifty Weekly Strangle — Autonomous Trading Bot

## Deployment Options

### Option A: WSL (Your Machine) — $0 ✅ RECOMMENDED

Run the WebSocket monitor directly on your WSL. Your machine is already on during market hours.

```bash
# Start the monitor
cd /mnt/c/Users/Admin/Documents/Claude/Projects/nifty\ strangle
./monitor.sh start

# Check status
./monitor.sh status

# View logs
./monitor.sh logs

# Stop
./monitor.sh stop
```

Auto-start on WSL boot:
```bash
echo '~/nifty/strangle/monitor.sh start' >> ~/.bashrc
```

**Pros:** $0, unlimited uptime, true WebSocket, zero deployment
**Cons:** Only runs when WSL is on (but that's already 100% during market hours)

---

### Option B: Fly.io — $0/mo (credit card needed for verification)

Best free cloud option. 3 VMs, 256 MB RAM each, **never spins down** — persistent WebSocket works perfectly.

```bash
# Install flyctl
curl -L https://fly.io/install.sh | sh

# Login (needs credit card for ID verification — $0 charged)
fly auth login

# Deploy from project directory
cd /mnt/c/Users/Admin/Documents/Claude/Projects/nifty\ strangle
fly launch --no-deploy --name nifty-strangle

# Set secrets
fly secrets set ANGEL_API_KEY=2siOJ0EZ
fly secrets set ANGEL_CLIENT_CODE=G188451
fly secrets set ANGEL_PIN=1980
fly secrets set ANGEL_TOTP_SECRET=LIONHZIIQLSN7MZEDLRSPE5HE4

# Deploy
fly deploy
```

**Pros:** $0, 24/7 uptime, WebSocket works, auto-restarts on crash, 3 VMs
**Cons:** Needs credit card once for verification (no charge)

---

### Option C: Render — $0/mo (no credit card)

```bash
# 1. Push code to GitHub (done)
# 2. Go to https://render.com → Sign up with GitHub
# 3. New Web Service → Connect nifty-strangle repo
# 4. Build command: (leave default)
# 5. Start command: python railway_monitor.py
# 6. Add 6 env vars (same as Railway)
# 7. Deploy
```

**Pros:** $0, no credit card
**Cons:** Spins down after 15 min idle — but WebSocket keeps it alive during market hours

---

### Option D: GitHub Actions (already deployed) — $0

Hard-stop already runs every 1 minute as a safety net. Kept as backup regardless of which option you choose above.

| File | Runs where | Purpose |
|------|-----------|---------|
| `bot.py` | GH Actions + local | Entry, close, status, preview |
| `railway_monitor.py` | Railway | WebSocket monitor, real-time risk, healthcheck |
| `strangle_calculator.py` | Local | Interactive calc for manual use |
| `state.json` | GitHub | Trade persistence (synced by both) |
| `trade_log.csv` | GitHub | Trade history |
| `Dockerfile` | Railway build | Container setup |
| `railway.json` | Railway build | Healthcheck config |
| `requirements.txt` | Both | Python dependencies |
