#!/usr/bin/env python3
"""
Nifty Strangle — Real-time WebSocket Monitor for Railway
========================================================
Runs 24/7 on Railway. Stays connected to Angel One WebSocket 
during market hours (9:15-15:30 IST, Mon-Fri).
Provides a healthcheck endpoint for Railway.

Gap risk: 0 seconds (WebSocket is truly real-time).
"""

import os, sys, json, math, time, threading, csv, signal
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
import requests as http_requests

try:
    from flask import Flask, jsonify
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False
    print("⚠️  Flask not installed — healthcheck endpoint disabled")

from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
import pyotp
import yfinance as yf
import pandas as pd
import numpy as np

# ─── CONFIG ────────────────────────────────────────────────────────────────

# Must match bot.py config
STOP_MULT = 2.5
PROFIT_TARGET_PCT = 0.15
LOT_SIZE = 65

# Market hours (IST)
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)

# GH repo for state persistence (state.json lives in git)
GH_REPO = os.environ.get("GH_REPO", "")           # "username/nifty-strangle"
GH_PAT = os.environ.get("GH_PAT", "")              # GitHub PAT with repo scope
GH_BRANCH = os.environ.get("GH_BRANCH", "main")

# Angel One creds
CREDS = {
    "api_key": os.environ.get("ANGEL_API_KEY", ""),
    "client_code": os.environ.get("ANGEL_CLIENT_CODE", ""),
    "pin": os.environ.get("ANGEL_PIN", ""),
    "totp_secret": os.environ.get("ANGEL_TOTP_SECRET", ""),
}

# State — in-memory cache, synced to GitHub
state = {}
state_lock = threading.Lock()

# WebSocket connection
ws = None
ws_connected = False
ws_lock = threading.Lock()

# Running flag
running = True

# Token constants
NIFTY_SPOT_TOKEN = "99926000"

# ─── GITHUB STATE PERSISTENCE ──────────────────────────────────────────────

GH_HEADERS = {
    "Authorization": f"token {GH_PAT}",
    "Accept": "application/vnd.github.v3+json",
} if GH_PAT else {}


def gh_read_state():
    """Read state.json from GitHub repo. Returns dict."""
    if not GH_REPO or not GH_PAT:
        # Fallback: local file (for local testing)
        p = Path("state.json")
        if p.exists():
            with open(p) as f:
                return json.load(f)
        return {"status": "NO_POSITION"}
    
    url = f"https://api.github.com/repos/{GH_REPO}/contents/state.json"
    resp = http_requests.get(url, headers=GH_HEADERS)
    if resp.status_code == 200:
        content = resp.json().get("content", "")
        import base64
        decoded = base64.b64decode(content).decode("utf-8")
        return json.loads(decoded)
    return {"status": "NO_POSITION"}


def gh_write_state(state_data):
    """Write state.json to GitHub repo. Creates commit."""
    if not GH_REPO or not GH_PAT:
        # Fallback: local file
        with open("state.json", "w") as f:
            json.dump(state_data, f, indent=2, default=str)
        return True
    
    import base64
    content = base64.b64encode(json.dumps(state_data, indent=2, default=str).encode()).decode()
    
    # Get current file SHA (required for update)
    url = f"https://api.github.com/repos/{GH_REPO}/contents/state.json"
    get_resp = http_requests.get(url, headers=GH_HEADERS)
    sha = get_resp.json().get("sha", "") if get_resp.status_code == 200 else ""
    
    payload = {
        "message": f"bot: ws-monitor update {datetime.now().strftime('%H:%M:%S')}",
        "content": content,
        "branch": GH_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    
    put_resp = http_requests.put(url, headers=GH_HEADERS, json=payload)
    return put_resp.status_code in (200, 201)


def gh_append_trade_log(trade_data):
    """Append trade to trade_log.csv in GitHub."""
    if not GH_REPO or not GH_PAT:
        return
    
    # Read current file
    url = f"https://api.github.com/repos/{GH_REPO}/contents/trade_log.csv"
    get_resp = http_requests.get(url, headers=GH_HEADERS)
    sha = ""
    current_content = ""
    if get_resp.status_code == 200:
        import base64
        sha = get_resp.json().get("sha", "")
        current_content = base64.b64decode(get_resp.json()["content"]).decode()
    
    # Append new row
    new_row = f"{trade_data['entry_date']},{trade_data['expiry']},{trade_data['entry_spot']},{trade_data['put_strike']},{trade_data['call_strike']},{trade_data['put_credit']},{trade_data['call_credit']},{trade_data['total_credit']},{trade_data['stop_loss']},{trade_data['exit_date']},{trade_data['exit_spot']},{trade_data['exit_reason']},{trade_data['exit_premium']},{trade_data['pnl']}\n"
    
    if not current_content:
        current_content = "entry_date,expiry,entry_spot,put_strike,call_strike,put_credit,call_credit,total_credit,stop_loss,exit_date,exit_spot,exit_reason,exit_premium,pnl\n"
    
    new_content = current_content + new_row
    import base64
    encoded = base64.b64encode(new_content.encode()).decode()
    
    payload = {
        "message": f"bot: trade logged {datetime.now().strftime('%Y-%m-%d')}",
        "content": encoded,
        "branch": GH_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    
    http_requests.put(url, headers=GH_HEADERS, json=payload)


# ─── ANGEL ONE ─────────────────────────────────────────────────────────────

def angel_login():
    """Login to Angel One. Returns (obj, feed_token, jwt_token)."""
    obj = SmartConnect(api_key=CREDS["api_key"])
    resp = obj.generateSession(
        CREDS["client_code"],
        CREDS["pin"],
        pyotp.TOTP(CREDS["totp_secret"]).now()
    )
    if not resp.get("status"):
        raise Exception(f"Login failed: {resp}")
    
    data = resp.get("data", {})
    feed_token = data.get("feedToken", "")
    jwt_token = data.get("jwtToken", "")
    return obj, feed_token, jwt_token


def get_spot_and_premiums(obj):
    """Fetch current spot + option premiums via REST (fallback when WS not connected)."""
    spot = 0
    
    # Spot
    qr = obj.getMarketData("LTP", {"NSE": [NIFTY_SPOT_TOKEN]})
    if qr and qr.get("data"):
        items = qr["data"].get("fetched", [])
        for item in items:
            if isinstance(item, dict) and str(item.get("symbolToken","")) == NIFTY_SPOT_TOKEN:
                spot = float(item.get("ltp", 0))
    
    # Option premiums
    with state_lock:
        put_token = state.get("put_token", "")
        call_token = state.get("call_token", "")
    
    put_ltp = 0
    call_ltp = 0
    
    if put_token:
        tokens = [t for t in [put_token, call_token] if t]
        if tokens:
            qr2 = obj.getMarketData("LTP", {"NFO": tokens})
            if qr2 and qr2.get("data"):
                items = qr2["data"].get("fetched", [])
                for item in items:
                    if isinstance(item, dict):
                        tok = str(item.get("symbolToken",""))
                        if tok == put_token:
                            put_ltp = float(item.get("ltp", 0))
                        if tok == call_token:
                            call_ltp = float(item.get("ltp", 0))
    
    return spot, put_ltp, call_ltp


# ─── WEBSOCKET ─────────────────────────────────────────────────────────────

def on_ws_connect(wsapp):
    """Called when WebSocket connects (wsapp is raw WebSocketApp)."""
    global ws_connected, ws
    with ws_lock:
        ws_connected = True
    print("✅ WebSocket connected — real-time feed active", flush=True)
    
    # Subscribe to tokens (LTP mode = 1) — use global ws which is SmartWebSocketV2
    tokens = []
    tokens.append({"exchangeType": 1, "tokens": [NIFTY_SPOT_TOKEN]})  # NSE Nifty
    with state_lock:
        if state.get("put_token"):
            tokens.append({"exchangeType": 2, "tokens": [state["put_token"], state["call_token"]]})  # NFO options
    
    if tokens and ws is not None:
        try:
            ws.subscribe("CORR1", 1, tokens)  # correlation_id, LTP mode, tokens
            print(f"📡 Subscribed to {len(tokens)} token groups", flush=True)
        except Exception as e:
            print(f"⚠️  Subscribe error: {e}", flush=True)


def on_ws_close(ws_instance):
    """Called when WebSocket disconnects."""
    global ws_connected
    with ws_lock:
        ws_connected = False
    print("🔌 WebSocket disconnected", flush=True)


def on_ws_error(ws_instance, error):
    """Called on WebSocket error."""
    print(f"⚠️  WebSocket error: {error}", flush=True)


def on_ws_tick(ws_instance, tick):
    """
    Called on EVERY tick from Angel One V2 WebSocket.
    tick keys:
        token, exchange_type, subscription_mode, last_traded_price (paise)
        last_traded_quantity, average_traded_price, volume_trade_for_the_day
        ...
    LTP needs /100 to get rupees.
    """
    global state
    
    token = str(tick.get("token", ""))
    # V2 LTP is in paise (divide by 100)
    ltp = float(tick.get("last_traded_price", 0)) / 100.0
    exchange = tick.get("exchange_type", 0)
    
    with state_lock:
        if state.get("status") != "IN_POSITION":
            return  # No position to monitor
        
        # Update cached prices
        if token == NIFTY_SPOT_TOKEN:
            state["last_spot"] = ltp
        elif token == state.get("put_token"):
            state["put_ltp"] = ltp
        elif token == state.get("call_token"):
            state["call_ltp"] = ltp
        
        # Only check after we have both spot and both premiums
        if not all([state.get("last_spot"), state.get("put_ltp") is not None, state.get("call_ltp") is not None]):
            return
        
        spot = state["last_spot"]
        put_ltp = state.get("put_ltp", 0)
        call_ltp = state.get("call_ltp", 0)
        premium = put_ltp + call_ltp
        total_credit = state.get("total_credit", 0)
        stop_level = state.get("stop_level", 0)
        target_level = state.get("target_level", 0)
        put_strike = state.get("put_strike", 0)
        call_strike = state.get("call_strike", 0)
    
    # ─── DECISIONS (real-time, 0ms after tick arrives) ───
    reason = None
    
    # 1. Breach: spot at or outside strike
    if spot >= call_strike:
        reason = "STRIKE_BREACH_CALL"
    elif spot <= put_strike:
        reason = "STRIKE_BREACH_PUT"
    
    # 2. Stop loss: premium ≥ 2.5× credit
    if reason is None and premium >= stop_level:
        reason = "STOP_LOSS"
    
    # 3. Profit target: premium ≤ 15% credit (only outside expiry day)
    if reason is None and premium <= target_level:
        is_expiry = datetime.now().weekday() == 1  # Tuesday
        if not is_expiry:
            reason = "PROFIT_TARGET"
    
    if reason:
        print(f"\n🚨 {reason} triggered!", flush=True)
        print(f"  Spot: {spot} | Premium: {premium:.2f} | Credit: {total_credit:.2f}", flush=True)
        
        # Close position via Angel One REST API
        try:
            obj, _, _ = angel_login()
            from bot import place_order  # Reuse bot.py's order function
            
            with state_lock:
                put_sym = state["put_symbol"]
                put_tok = state["put_token"]
                call_sym = state["call_symbol"]
                call_tok = state["call_token"]
            
            place_order(obj, put_sym, put_tok, LOT_SIZE, "BUY")
            place_order(obj, call_sym, call_tok, LOT_SIZE, "BUY")
            
            obj.terminateSession(CREDS["client_code"])
            print("✅ Position closed via API", flush=True)
        except Exception as e:
            print(f"❌ Close order failed: {e}", flush=True)
            return
        
        # Calculate P&L
        with state_lock:
            pnl_per_share = total_credit - premium
            pnl_total = pnl_per_share * LOT_SIZE
        
        # Log trade to GitHub
        trade_data = {
            "entry_date": state.get("entry_time", ""),
            "expiry": state.get("expiry", ""),
            "entry_spot": state.get("entry_spot", 0),
            "put_strike": state.get("put_strike", 0),
            "call_strike": state.get("call_strike", 0),
            "put_credit": state.get("put_credit", 0),
            "call_credit": state.get("call_credit", 0),
            "total_credit": total_credit,
            "stop_loss": stop_level,
            "exit_date": datetime.now().isoformat(),
            "exit_spot": spot,
            "exit_reason": reason,
            "exit_premium": premium,
            "pnl": pnl_total,
        }
        gh_append_trade_log(trade_data)
        
        # Reset state
        with state_lock:
            state["status"] = "NO_POSITION"
            state["last_trade"] = trade_data
        gh_write_state(state)


def start_websocket():
    """Start the WebSocket connection in a background thread."""
    global ws
    
    while running:
        try:
            # Login for tokens
            obj, feed_token, jwt_token = angel_login()
            
            # Create V2 WebSocket
            # Strip "Bearer " prefix if present
            auth_token = jwt_token.replace("Bearer ", "")
            ws = SmartWebSocketV2(
                auth_token=auth_token,
                api_key=CREDS["api_key"],
                client_code=CREDS["client_code"],
                feed_token=feed_token,
            )
            
            ws.on_open = on_ws_connect
            ws.on_close = on_ws_close
            ws.on_error = on_ws_error
            ws.on_data = on_ws_tick
            
            print("🔌 Connecting WebSocket V2...", flush=True)
            ws.connect()  # This blocks until disconnect
            
        except Exception as e:
            print(f"⚠️  WebSocket error (reconnecting in 5s): {e}", flush=True)
        
        time.sleep(5)  # Reconnect delay
    
    print("🛑 WebSocket thread stopped", flush=True)


def stop_websocket():
    """Gracefully stop the WebSocket."""
    global ws, running
    running = False
    if ws:
        try:
            ws.close_connection()
        except:
            pass


# ─── MARKET HOURS ──────────────────────────────────────────────────────────

def is_market_hours():
    """Check if current time is within market hours (Mon-Fri, 9:15-15:30 IST)."""
    now = datetime.now()
    
    # Weekend check
    if now.weekday() >= 5:
        return False
    
    # Minute-of-day check
    minute_of_day = now.hour * 60 + now.minute
    open_md = MARKET_OPEN[0] * 60 + MARKET_OPEN[1]  # 9:15 = 555
    close_md = MARKET_CLOSE[0] * 60 + MARKET_CLOSE[1]  # 15:30 = 930
    
    return open_md <= minute_of_day <= close_md


# ─── HEALTHCHECK ENDPOINT (Flask) ──────────────────────────────────────────

app = Flask(__name__) if FLASK_AVAILABLE else None

if app:
    @app.route("/health")
    def health():
        """Railway healthcheck. Returns 200 if alive."""
        with state_lock:
            s = state.get("status", "UNKNOWN")
            ws_status = "connected" if ws_connected else "disconnected"
        return jsonify({
            "status": "alive",
            "position": s,
            "websocket": ws_status,
            "time": datetime.now().isoformat(),
        })
    
    @app.route("/")
    def index():
        return jsonify({
            "service": "nifty-strangle-websocket-monitor",
            "version": "1.0",
            "position": state.get("status", "NO_POSITION"),
            "ws": "connected" if ws_connected else "disconnected",
        })
    
    @app.route("/state")
    def get_state():
        """Return current state (read-only)."""
        with state_lock:
            return jsonify(state)


# ─── MAIN LOOP ─────────────────────────────────────────────────────────────

def sync_state_from_github():
    """Load latest state from GitHub repo."""
    global state
    gh_state = gh_read_state()
    with state_lock:
        # Preserve runtime fields
        state.update(gh_state)
        print(f"📖 State loaded: {state.get('status', 'UNKNOWN')}", flush=True)


def main():
    print(f"{'='*60}", flush=True)
    print(f"  NIFTY STRANGLE — WebSocket Monitor", flush=True)
    print(f"  Started: {datetime.now().isoformat()}", flush=True)
    print(f"{'='*60}", flush=True)
    
    # Load initial state from GitHub
    sync_state_from_github()
    
    # Start WebSocket thread (connects immediately, handles reconnection)
    ws_thread = threading.Thread(target=start_websocket, daemon=True)
    ws_thread.start()
    time.sleep(3)  # Give it time to connect
    
    # State sync thread: pull latest state from GitHub every 5 min
    # This catches entries made by GH Actions
    def state_sync_loop():
        while running:
            time.sleep(300)  # 5 min
            sync_state_from_github()
    
    sync_thread = threading.Thread(target=state_sync_loop, daemon=True)
    sync_thread.start()
    
    # Periodic REST fallback (if WS is down, still get prices)
    def rest_fallback_loop():
        while running:
            time.sleep(60)  # Every 1 min
            with state_lock:
                if state.get("status") != "IN_POSITION":
                    continue
                if ws_connected:
                    continue  # WS is active, skip REST
            
            try:
                obj, _, _ = angel_login()
                spot, put_ltp, call_ltp = get_spot_and_premiums(obj)
                obj.terminateSession(CREDS["client_code"])
                
                with state_lock:
                    if spot:
                        state["last_spot"] = spot
                    if put_ltp:
                        state["put_ltp"] = put_ltp
                    if call_ltp:
                        state["call_ltp"] = call_ltp
            except Exception as e:
                print(f"⚠️  REST fallback error: {e}", flush=True)
    
    rest_thread = threading.Thread(target=rest_fallback_loop, daemon=True)
    rest_thread.start()
    
    # Flask healthcheck server — use configurable port, default 8080 on Railway
    port = int(os.environ.get("PORT", 8080))
    if app:
        # Try port, fallback to random if busy
        try:
            print(f"🌐 Healthcheck server on port {port}", flush=True)
            app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
        except OSError:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("0.0.0.0", 0))
            port = sock.getsockname()[1]
            sock.close()
            print(f"🌐 Healthcheck server on port {port} (fallback, 8080 busy)", flush=True)
            app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    else:
        # No Flask — just keep main thread alive
        print("⚠️  Flask not installed, running headless", flush=True)
        try:
            while running:
                time.sleep(10)
        except KeyboardInterrupt:
            pass
    
    stop_websocket()


if __name__ == "__main__":
    # Graceful shutdown
    signal.signal(signal.SIGTERM, lambda *_: stop_websocket())
    signal.signal(signal.SIGINT, lambda *_: stop_websocket())
    main()
