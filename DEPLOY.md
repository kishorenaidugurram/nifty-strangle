# Nifty Weekly Strangle — Autonomous Trading Bot

## Deployment Architecture

```
                           RAILWAY (24/7 persistent)
┌──────────────────────────────────────────────────────────────┐
│  railway_monitor.py                                          │
│  ┌────────────────────────────────────┐                      │
│  │ WebSocket → Angel One (real-time)  │  <── 0ms latency    │
│  │    ↓ tick received                 │                      │
│  │ Check stop/target/breach           │                      │
│  │    ↓ triggered                     │                      │
│  │ Close via Angel One REST API       │  <── instant close   │
│  │    ↓                              │                      │
│  │ Write state.json → GitHub API      │  <── sync to repo    │
│  └────────────────────────────────────┘                      │
│  Flask healthcheck at /health                                │
└──────────────────────────────────────────────────────────────┘
         ▲                              ▲
         │ syncs state every 5min       │ writes state on close
         │                              │
┌──────────────────────┐   ┌──────────────────────────┐
│  GH ACTIONS          │   │  GH ACTIONS              │
│  entry.yml (Tue 3:20)│   │  nightly.yml (8 PM)      │
│  Opens position      │   │  Status report           │
└──────────────────────┘   └──────────────────────────┘
```

## Railway Deployment

### 1. Create Railway Account
Go to https://railway.app — sign up with GitHub.

### 2. Deploy from GitHub

```bash
# In Railway dashboard:
# 1. "New Project" → "Deploy from GitHub repo"
# 2. Select your nifty-strangle repo
# 3. Railway auto-detects Dockerfile and deploys
```

### 3. Set Environment Variables in Railway Dashboard

| Variable | Value | Purpose |
|----------|-------|---------|
| `ANGEL_API_KEY` | Your API key | Angel One login |
| `ANGEL_CLIENT_CODE` | Your client code | Angel One login |
| `ANGEL_PIN` | Your PIN | Angel One login |
| `ANGEL_TOTP_SECRET` | Your TOTP secret | Angel One 2FA |
| `GH_PAT` | GitHub PAT with repo scope | Write state.json + trade_log.csv |
| `GH_REPO` | `username/nifty-strangle` | GitHub repo path |
| `GH_BRANCH` | `main` | Branch to write to |

### 4. Verify Deployment

Railway will:
- Build the Docker image
- Deploy, exposing port 8080
- Healthcheck via `/health` every 10s
- Auto-restart on crash (up to 10 retries)

Check logs in Railway dashboard. You should see:
```
✅ WebSocket connected — real-time feed active
📡 Subscribed to X token groups
```

### 5. Scaling & Cost

| Plan | RAM | Cost | Uptime |
|------|:---:|:----:|:------:|
| Free | 512 MB | $0 | 500 hrs/mo (~20 days) |
| Developer | 1 GB | $5/mo | Unlimited |

**Recommended:** Developer plan ($5/mo). The 500 free hours won't cover a full month (744 hrs).

### Actual Monthly Cost Breakdown

| Service | Cost |
|---------|:----:|
| Railway (Developer) | $5/mo |
| Angel One API | Free |
| GitHub | Free |
| **Total** | **$5/mo (~₹420)** |

---

## How Real-Time Is It?

### Railway + WebSocket (this setup)
```
Flash crash starts:            10:00:00.000
Angel One tick received:       10:00:00.015  (15ms)
Bot checks stop/breach:        10:00:00.016  (1ms)
Close order sent:              10:00:00.050  (50ms to API)
                             ──────────────────
  Total latency:               ~50ms — near-instant ✅
```

### GH Actions only (5-min cron)
```
Flash crash starts:            10:00:00
Next GH cron run:              10:04:30  (4.5 min later)
Close order sent:              10:04:32
                             ──────────────────
  Total latency:               ~4.5 min — 5400× worse ❌
```

---

## Files

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
