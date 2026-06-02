#!/usr/bin/env python3
"""
Nifty Weekly Strangle — Autonomous Trading Bot
===============================================
Complete lifecycle: entry, continuous risk monitoring, hard stop, profit booking.

Deployed via GitHub Actions with 3 workflows:
  1. entry.yml      — Tue @ 3:25 PM IST → open strangle
  2. monitor.yml    — Every 30 min, Mon-Fri 9:15-15:30 IST → risk check + manage
  3. nightly.yml    — 8 PM IST daily → log status, send report

Architecture:
  state.json        — Trade state persistence
  trade_log.csv     — Full trade history
  bot.py            — Core engine (this file)
"""

import os, sys, json, math, time, csv
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

import yfinance as yf
import pandas as pd
import numpy as np
from SmartApi import SmartConnect
import pyotp
import requests

# ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ───
# CONFIG
# ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ───

CONFIG = {
    "std_dev": 2.0,          # Starting anchor (strikes searched ±0.5σ around this)
    "stop_mult": 2.5,        # Close when premium reaches 2.5× credit
    "profit_target_pct": 0.15,  # Book when 15% credit remains (85% profit)
    "lot_size": 65,          # Nifty weekly lot
    "strike_rounding": 50,   # Nifty strikes every 50 pts
    "entry_hour": 15,        # 3 PM
    "entry_minute": 25,      # 25 minutes
    "entry_weekday": 1,      # Tuesday
    "expiry_weekday": 1,     # Tuesday
    "max_dte": 10,           # Max days to expiry for entry
    "min_dte": 5,            # Min days to expiry for entry
    "viy_threshold": 25,     # Skip if VIX > 25
    "margin_pct": 0.15,      # Margin estimate % of notional
    "monitor_interval_mins": 30,
    "market_open": (9, 15),  # IST
    "market_close": (15, 30), # IST
    "state_file": "state.json",
    "trade_log": "trade_log.csv",
    "angel_env": "/mnt/c/Users/Admin/Documents/Claude/Projects/NSE_PCS_CCS_TO_BE_DEPLOYED/.env",
    # Premium-targeted strike selection (replaces fixed 2σ)
    "premium_target_min": 8,    # Minimum ₹ per leg (avoid too-thin options)
    "premium_target_max": 25,   # Maximum ₹ per leg (avoid overpriced)
    "premium_balance_pct": 30,  # Max % difference between leg premiums
    "vol_smile_search": 0.5,    # ±σ range to search around anchor
}

# Weekly expiry mapping: NSE weekly options expire on Tuesday
# Angel One token for Nifty index
NIFTY_SPOT_TOKEN = "99926000"

BOT_DIR = Path(__file__).parent.resolve()

# ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ───
# CREDENTIALS
# ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ───

def load_creds():
    """Load Angel One + DeepSeek creds from env."""
    # GH Actions sets these as secrets
    env_vars = ["ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", 
                "ANGEL_TOTP_SECRET", "DEEPSEEK_API_KEY"]
    
    creds = {}
    for var in env_vars:
        creds[var] = os.environ.get(var, "")
    
    # Fallback: load from .env file (local dev)
    if not all(creds.values()):
        env_path = Path(os.environ.get("ANGEL_ENV_FILE", CONFIG["angel_env"]))
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        k = k.strip(); v = v.strip()
                        if k in creds and not creds[k]:
                            creds[k] = v
    
    return creds


def angel_connect(creds):
    """Return authenticated SmartConnect object."""
    obj = SmartConnect(api_key=creds["ANGEL_API_KEY"])
    resp = obj.generateSession(
        creds["ANGEL_CLIENT_CODE"], 
        creds["ANGEL_PIN"], 
        pyotp.TOTP(creds["ANGEL_TOTP_SECRET"]).now()
    )
    if not resp.get("status"):
        raise Exception(f"Angel One login failed: {resp}")
    return obj


# ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ───
# STATE MANAGEMENT
# ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ───

def read_state():
    """Read persistent trade state from state.json."""
    state_path = BOT_DIR / CONFIG["state_file"]
    if state_path.exists():
        with open(state_path) as f:
            return json.load(f)
    return {"status": "NO_POSITION", "trades": [], "last_run": None}


def write_state(state):
    """Persist trade state to state.json."""
    state_path = BOT_DIR / CONFIG["state_file"]
    state["last_run"] = datetime.now().isoformat()
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2, default=str)
    return state


def log_trade(trade_data):
    """Append trade to CSV log."""
    log_path = BOT_DIR / CONFIG["trade_log"]
    fieldnames = [
        "entry_date", "expiry", "entry_spot", "put_strike", "call_strike",
        "put_credit", "call_credit", "total_credit", "stop_loss",
        "exit_date", "exit_spot", "exit_reason", "exit_premium", "pnl"
    ]
    is_new = not log_path.exists()
    with open(log_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if is_new:
            writer.writeheader()
        writer.writerow(trade_data)


# ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ───
# MARKET DATA
# ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ───

def get_nifty_spot(obj):
    """Fetch Nifty spot from Angel One LTP."""
    qr = obj.getMarketData("LTP", {"NSE": [NIFTY_SPOT_TOKEN]})
    if qr and qr.get("data"):
        items = qr["data"].get("fetched", qr["data"] if isinstance(qr["data"], list) else [])
        for item in items if isinstance(items, list) else [items]:
            if isinstance(item, dict) and str(item.get("symbolToken","")) == NIFTY_SPOT_TOKEN:
                return float(item.get("ltp", 0))
    # Fallback: yfinance
    d = yf.download('^NSEI', period='2d', progress=False, auto_adjust=True)
    return float(d['Close'].values.flatten()[-1])


def get_volatility():
    """Estimate Nifty daily volatility. Returns blended vol."""
    d = yf.download('^NSEI', period='180d', interval='1d', progress=False, auto_adjust=True)
    s = pd.Series(d['Close'].values.flatten())
    log_ret = np.log(s / s.shift(1))
    full_vol = float(log_ret.std())
    recent = log_ret.tail(20)
    recent_vol = float(recent.std()) if len(recent) > 5 else full_vol
    return 0.6 * full_vol + 0.4 * recent_vol


def get_option_ltp(obj, token):
    """Fetch single option LTP from Angel One."""
    qr = obj.getMarketData("LTP", {"NFO": [token]})
    if qr and qr.get("data"):
        items = qr["data"].get("fetched", qr["data"] if isinstance(qr["data"], list) else [])
        for item in items if isinstance(items, list) else [items]:
            if isinstance(item, dict) and str(item.get("symbolToken","")) == token:
                return float(item.get("ltp", 0))
    return 0


def get_india_vix():
    """Fetch India VIX. Returns None if unavailable."""
    try:
        d = yf.download('^INDIAVIX', period='5d', progress=False)
        if not d.empty:
            return float(d['Close'].values.flatten()[-1])
    except:
        pass
    try:
        d = yf.download('INDIAVIX.NS', period='5d', progress=False)
        if not d.empty:
            return float(d['Close'].values.flatten()[-1])
    except:
        pass
    return None


def load_master(obj):
    """Load and cache Angel One instrument master."""
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    master = pd.DataFrame(requests.get(url, timeout=30).json())
    nfo = master[master["exch_seg"] == "NFO"].copy()
    nfo["stk"] = pd.to_numeric(nfo["strike"], errors="coerce") / 100.0
    nfo["exp_dt"] = pd.to_datetime(nfo["expiry"], format="%d%b%Y", errors="coerce")
    nfo["dte"] = (nfo["exp_dt"] - pd.Timestamp.now()).dt.days
    nfo["otype"] = nfo["symbol"].str.extract(r"(CE|PE)$", expand=False)
    return nfo


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ───
# STRIKE CALCULATION
# ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ───

def find_next_expiry(nfo):
    """Find next Tuesday's weekly expiry with 5-10 DTE."""
    all_exp = sorted(nfo[(nfo["name"]=="NIFTY") & (nfo["instrumenttype"]=="OPTIDX")]["exp_dt"].dropna().unique())
    best_exp, best_dte = None, 99
    for exp in all_exp:
        dte = (exp - pd.Timestamp.now()).days
        if CONFIG["min_dte"] <= dte <= CONFIG["max_dte"] and exp.weekday() == CONFIG["expiry_weekday"]:
            if dte < best_dte:
                best_dte = dte; best_exp = exp
    if best_exp is None:
        for exp in all_exp:
            dte = (exp - pd.Timestamp.now()).days
            if 3 <= dte <= 14 and dte < best_dte:
                best_dte = dte; best_exp = exp
    return best_exp, best_dte


def calc_strikes(spot, vol, dte, obj, nfo, best_exp):
    """
    Premium-targeted strike selection: scan strikes around 2σ anchor,
    pick the pair where each leg yields ₹8-25 and premiums are balanced.
    
    Returns (put_strike, call_strike, sd, put_ltp, call_ltp, total_credit).
    Falls back to standard 2σ if premium check fails.
    """
    sd = spot * vol * math.sqrt(dte)
    
    # Anchor: standard 2σ strikes (rounded to 50)
    anchor_put = round((spot - sd * CONFIG["std_dev"]) / CONFIG["strike_rounding"]) * CONFIG["strike_rounding"]
    anchor_call = round((spot + sd * CONFIG["std_dev"]) / CONFIG["strike_rounding"]) * CONFIG["strike_rounding"]
    
    # Search range: ±σ range around anchor
    search_sd = spot * vol * math.sqrt(dte) * CONFIG["vol_smile_search"]
    search_pts = int(round(search_sd / 50) * 50)  # round to nearest 50
    
    low_put = round((anchor_put - search_pts) / 50) * 50
    high_call = round((anchor_call + search_pts) / 50) * 50
    
    # Scan the chain for live premiums
    chain = nfo[(nfo["name"]=="NIFTY") & (nfo["instrumenttype"]=="OPTIDX") & (nfo["exp_dt"]==best_exp)]
    
    # Batch-fetch all option LTPs in range (single API call per side)
    put_strikes = []
    call_strikes = []
    put_tokens = {}
    call_tokens = {}
    
    for _, row in chain.iterrows():
        stk = int(row["stk"])
        otype = row["symbol"][-2:]
        tok = str(int(row["token"]))
        if otype == "PE" and low_put <= stk <= spot:
            put_strikes.append(stk)
            put_tokens[stk] = tok
        elif otype == "CE" and spot <= stk <= high_call:
            call_strikes.append(stk)
            call_tokens[stk] = tok
    
    put_strikes.sort(reverse=True)   # Highest strike first (closest to spot)
    call_strikes.sort()              # Lowest strike first (closest to spot)
    
    # Fetch LTPs in batches
    put_ltps = {}
    call_ltps = {}
    
    for stk in put_strikes:
        put_ltps[stk] = get_option_ltp(obj, put_tokens[stk])
    for stk in call_strikes:
        call_ltps[stk] = get_option_ltp(obj, call_tokens[stk])
    
    # Score each pair: balance = premium diff, ideal = total 16-50, legs = 8-25
    best_score = -1
    best_pair = None
    
    for ps in put_strikes:
        if ps not in put_ltps or put_ltps[ps] == 0:
            continue
        pv = put_ltps[ps]
        if pv < CONFIG["premium_target_min"] or pv > CONFIG["premium_target_max"]:
            continue
        
        for cs in call_strikes:
            if cs not in call_ltps or call_ltps[cs] == 0:
                continue
            cv = call_ltps[cs]
            if cv < CONFIG["premium_target_min"] or cv > CONFIG["premium_target_max"]:
                continue
            
            total = pv + cv
            balance = min(pv, cv) / max(pv, cv) * 100  # higher = more balanced
            
            # Prefer balanced pairs within premium range
            if balance >= (100 - CONFIG["premium_balance_pct"]):
                # Preference: higher total credit is better, but balance matters more
                score = total * (balance / 100)
                if score > best_score:
                    best_score = score
                    best_pair = (ps, cs, pv, cv, total)
    
    if best_pair:
        ps, cs, pv, cv, total = best_pair
        return ps, cs, sd, pv, cv, total
    
    # Fallback: standard 2σ with live premiums
    put_stk = anchor_put
    call_stk = anchor_call
    put_ltp = get_option_ltp(obj, put_tokens.get(put_stk, ""))
    call_ltp = get_option_ltp(obj, call_tokens.get(call_stk, ""))
    return put_stk, call_stk, sd, put_ltp, call_ltp, put_ltp + call_ltp


def find_strikes_in_chain(chain, put_target, call_target):
    """Find closest available strikes in the option chain."""
    chain["otype"] = chain["symbol"].str.extract(r"(CE|PE)$", expand=False)
    puts = chain[chain["otype"]=="PE"]
    calls = chain[chain["otype"]=="CE"]
    
    result = {"put_strike": None, "call_strike": None, "put_token": None, "call_token": None}
    
    if not puts.empty:
        idx = (puts["stk"] - put_target).abs().idxmin()
        result["put_strike"] = int(puts.loc[idx, "stk"])
        result["put_token"] = str(int(puts.loc[idx, "token"]))
    
    if not calls.empty:
        idx = (calls["stk"] - call_target).abs().idxmin()
        result["call_strike"] = int(calls.loc[idx, "stk"])
        result["call_token"] = str(int(calls.loc[idx, "token"]))
    
    return result


# ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ───
# ORDER EXECUTION
# ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ───

def place_order(obj, symbol, token, qty, side, order_type="MARKET", price=0):
    """
    Place an order on Angel One.
    side: "SELL" or "BUY"
    order_type: "MARKET", "LIMIT"
    """
    payload = {
        "variety": "NORMAL",
        "tradingsymbol": symbol,
        "symboltoken": token,
        "transactiontype": side,
        "exchange": "NFO",
        "ordertype": order_type,
        "producttype": "CARRYFORWARD",
        "duration": "DAY",
        "price": price if order_type == "LIMIT" else 0,
        "triggerprice": 0,
        "quantity": qty,
    }
    resp = obj.placeOrder(payload)
    return resp


def cancel_order(obj, order_id):
    """Cancel an order by ID."""
    return obj.cancelOrder(order_id, "NFO")


def get_position(obj, symbol_token):
    """Check if we have an active position for this token."""
    pos = obj.position()
    if not pos.get("status"):
        return None
    for p in pos.get("data", []):
        if str(p.get("symboltoken","")) == symbol_token and int(p.get("netqty",0)) != 0:
            return p
    return None


# ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ───
# RISK METRICS
# ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ───

def compute_risk(spot, put_strike, call_strike, put_ltp, call_ltp, vol, dte):
    """Compute full risk metrics for the strangle."""
    total_credit = put_ltp + call_ltp
    
    # Fat-tail adjusted probability
    z_eff = CONFIG["std_dev"] * 0.7
    prob_in = norm_cdf(z_eff) - norm_cdf(-z_eff)
    prob_out = 1 - prob_in
    
    # Stop and target levels
    stop_level = total_credit * CONFIG["stop_mult"]
    target_level = total_credit * CONFIG["profit_target_pct"]
    avg_win = total_credit - target_level
    avg_loss = stop_level
    
    # Expectancy
    ev_per_share = prob_in * avg_win - prob_out * avg_loss
    ev_per_lot = ev_per_share * CONFIG["lot_size"]
    
    # Breakevens
    put_be = put_strike - put_ltp
    call_be = call_strike + call_ltp
    
    # Distance to strikes in σ
    sd = spot * vol * math.sqrt(max(dte, 1))
    dist_put_sigma = (spot - put_strike) / sd if sd > 0 else 99
    dist_call_sigma = (call_strike - spot) / sd if sd > 0 else 99
    
    return {
        "total_credit": total_credit,
        "put_ltp": put_ltp,
        "call_ltp": call_ltp,
        "stop_level": stop_level,
        "target_level": target_level,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "ev_per_share": ev_per_share,
        "ev_per_lot": ev_per_lot,
        "prob_in": prob_in,
        "prob_out": prob_out,
        "put_be": put_be,
        "call_be": call_be,
        "dist_put_sigma": dist_put_sigma,
        "dist_call_sigma": dist_call_sigma,
        "current_premium": put_ltp + call_ltp,
        "stop_triggered": (put_ltp + call_ltp) >= stop_level,
        "profit_target_hit": (put_ltp + call_ltp) <= target_level,
        "breach_detected": spot <= put_strike or spot >= call_strike,
    }


# ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ───
# CORE BOT ACTIONS
# ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ───

def run_entry_check():
    """
    Entry workflow (triggered by GH Actions cron Tue @ 3:25 PM IST):
    1. Check VIX — skip if > 25
    2. Calculate 2σ strikes
    3. Fetch live option premiums
    4. Place sell orders for put + call (market)
    5. Save state
    """
    state = read_state()
    
    if state["status"] != "NO_POSITION":
        return {"action": "SKIP", "reason": f"Already in position ({state['status']})"}
    
    # VIX check
    vix = get_india_vix()
    if vix is not None and vix > CONFIG["vix_threshold"]:
        return {"action": "SKIP", "reason": f"VIX {vix:.1f} > {CONFIG['vix_threshold']}, skipping"}
    
    # Connect
    creds = load_creds()
    obj = angel_connect(creds)
    nfo = load_master(obj)
    spot = get_nifty_spot(obj)
    
    # Volatility
    vol = get_volatility()
    
    # Expiry
    best_exp, dte = find_next_expiry(nfo)
    if best_exp is None:
        return {"action": "ERROR", "reason": "No valid expiry found"}
    
    # Strikes — premium-targeted (returns live LTPs too)
    expiry_str = best_exp.strftime("%d%b%Y").upper()
    chain = nfo[(nfo["name"]=="NIFTY") & (nfo["instrumenttype"]=="OPTIDX") & (nfo["exp_dt"]==best_exp)]
    put_stk, call_stk, sd, put_ltp, call_ltp, total_credit = calc_strikes(spot, vol, dte, obj, nfo, best_exp)
    
    # Find tokens for the selected strikes
    strikes = find_strikes_in_chain(chain, put_stk, call_stk)
    
    if not strikes["put_token"] or not strikes["call_token"]:
        return {"action": "ERROR", "reason": "Required strikes not found in chain"}
    
    # Build option symbols
    put_symbol = f"NIFTY{expiry_str}{strikes['put_strike']}PE"
    call_symbol = f"NIFTY{expiry_str}{strikes['call_strike']}CE"
    
    # Risk check (use live premiums from calc_strikes)
    risk = compute_risk(spot, strikes["put_strike"], strikes["call_strike"], 
                        put_ltp, call_ltp, vol, dte)
    
    if risk["ev_per_share"] <= 0:
        return {"action": "SKIP", "reason": f"Negative expectancy: Rs {risk['ev_per_share']:.2f}/share"}
    
    # Place orders
    put_order = place_order(obj, put_symbol, strikes["put_token"], 
                            CONFIG["lot_size"], "SELL")
    call_order = place_order(obj, call_symbol, strikes["call_token"], 
                             CONFIG["lot_size"], "SELL")
    
    now = datetime.now()
    
    # Save state
    new_state = {
        "status": "IN_POSITION",
        "entry_time": now.isoformat(),
        "entry_spot": spot,
        "expiry": expiry_str,
        "expiry_dt": best_exp.isoformat(),
        "dte": dte,
        "put_strike": strikes["put_strike"],
        "call_strike": strikes["call_strike"],
        "put_token": strikes["put_token"],
        "call_token": strikes["call_token"],
        "put_symbol": put_symbol,
        "call_symbol": call_symbol,
        "put_credit": put_ltp,
        "call_credit": call_ltp,
        "total_credit": total_credit,
        "stop_level": risk["stop_level"],
        "target_level": risk["target_level"],
        "avg_win": risk["avg_win"],
        "avg_loss": risk["avg_loss"],
        "ev_per_lot": risk["ev_per_lot"],
        "put_order_id": put_order.get("data", {}).get("orderid", "") if put_order else "",
        "call_order_id": call_order.get("data", {}).get("orderid", "") if call_order else "",
        "exit_time": None,
        "exit_reason": None,
        "exit_spot": None,
        "exit_premium": None,
        "pnl": None,
        "last_premium": total_credit,
        "last_spot": spot,
        "last_check": now.isoformat(),
    }
    
    write_state(new_state)
    obj.terminateSession(creds["ANGEL_CLIENT_CODE"])
    
    return {
        "action": "ENTERED",
        "put": f"{strikes['put_strike']}PE @ Rs {put_ltp:.2f}",
        "call": f"{strikes['call_strike']}CE @ Rs {call_ltp:.2f}",
        "total_credit": total_credit,
        "ev_per_lot": risk["ev_per_lot"],
        "expiry": expiry_str,
    }


def run_monitor():
    """
    Monitor workflow (triggered every 30 min during market hours):
    1. Read state
    2. If NO_POSITION → nothing to do
    3. Fetch current option premiums + spot
    4. Check stop loss (premium ≥ 2.5× credit) → close
    5. Check profit target (premium ≤ 0.15× credit) → close
    6. Check strike breach (spot at/outside either strike) → close
    7. Check expiry day → handle
    8. Update state
    """
    state = read_state()
    
    if state["status"] not in ["IN_POSITION"]:
        return {"action": "IDLE", "reason": f"State: {state['status']}"}
    
    creds = load_creds()
    obj = angel_connect(creds)
    spot = get_nifty_spot(obj)
    
    # Get current premiums
    put_ltp = get_option_ltp(obj, state["put_token"])
    call_ltp = get_option_ltp(obj, state["call_token"])
    current_premium = put_ltp + call_ltp
    
    stop_level = state["stop_level"]
    target_level = state["target_level"]
    total_credit = state["total_credit"]
    
    now = datetime.now()
    expiry_dt = pd.Timestamp(state["expiry_dt"])
    dte = (expiry_dt - pd.Timestamp.now()).days
    is_expiry_day = dte <= 0 and now.weekday() == CONFIG["expiry_weekday"]
    market_closing = now.hour == 15 and now.minute >= 20
    
    # ─── DECISION TREE ───
    reason = None
    close_orders = None
    
    # 1. BREACH: spot at or outside strike
    if spot >= state["call_strike"]:
        reason = "STRIKE_BREACH_CALL"
    elif spot <= state["put_strike"]:
        reason = "STRIKE_BREACH_PUT"
    
    # 2. STOP LOSS: premium ≥ 2.5× credit
    if reason is None and current_premium >= stop_level:
        reason = "STOP_LOSS"
    
    # 3. PROFIT TARGET: premium ≤ 0.15× credit (non-expiry)
    if reason is None and current_premium <= target_level and not is_expiry_day:
        reason = "PROFIT_TARGET"
    
    # 4. EXPIRY DAY: close before 3:30 PM
    if reason is None and is_expiry_day and market_closing:
        reason = "EXPIRY_CLOSE"
    elif reason is None and is_expiry_day and dte <= 0:
        # If its expiry day but early, don't close yet — theta works for us
        pass
    
    # Execute close if triggered
    if reason:
        put_qty = CONFIG["lot_size"]
        call_qty = CONFIG["lot_size"]
        
        put_close = place_order(obj, state["put_symbol"], state["put_token"],
                                put_qty, "BUY")  # Buy to close
        call_close = place_order(obj, state["call_symbol"], state["call_token"],
                                 call_qty, "BUY")
        
        # Calculate P&L
        exit_premium = put_ltp + call_ltp
        pnl_per_share = total_credit - exit_premium
        pnl_total = pnl_per_share * CONFIG["lot_size"]
        
        # Log trade
        trade_data = {
            "entry_date": state["entry_time"],
            "expiry": state["expiry"],
            "entry_spot": state["entry_spot"],
            "put_strike": state["put_strike"],
            "call_strike": state["call_strike"],
            "put_credit": state["put_credit"],
            "call_credit": state["call_credit"],
            "total_credit": total_credit,
            "stop_loss": stop_level,
            "exit_date": now.isoformat(),
            "exit_spot": spot,
            "exit_reason": reason,
            "exit_premium": exit_premium,
            "pnl": pnl_total,
        }
        log_trade(trade_data)
        
        # Reset state
        new_state = {
            "status": "NO_POSITION",
            "last_trade": trade_data,
            "trades": state.get("trades", []) + [trade_data],
        }
        write_state(new_state)
        
        result = {
            "action": "CLOSED",
            "reason": reason,
            "pnl": pnl_total,
            "pnl_per_share": pnl_per_share,
            "exit_premium": exit_premium,
        }
    else:
        # Update state with latest premiums
        state["last_premium"] = current_premium
        state["last_spot"] = spot
        state["last_check"] = now.isoformat()
        state["dte"] = dte
        write_state(state)
        
        dist_call = (state["call_strike"] - spot) / spot * 100
        dist_put = (spot - state["put_strike"]) / spot * 100
        
        result = {
            "action": "HOLDING",
            "current_premium": current_premium,
            "vs_stop": f"{current_premium/stop_level*100:.0f}% of stop",
            "vs_target": f"{current_premium/target_level*100:.0f}% of target",
            "dist_call": f"{dist_call:.1f}% to call strike",
            "dist_put": f"{dist_put:.1f}% to put strike",
            "spot": spot,
            "dte": dte,
        }
    
    obj.terminateSession(creds["ANGEL_CLIENT_CODE"])
    return result


def run_force_close(reason="MANUAL"):
    """
    Emergency close. Triggered manually or by violent move detection.
    Closes both legs at market immediately.
    """
    state = read_state()
    
    if state["status"] != "IN_POSITION":
        return {"action": "SKIP", "reason": "No position to close"}
    
    creds = load_creds()
    obj = angel_connect(creds)
    spot = get_nifty_spot(obj)
    
    put_ltp = get_option_ltp(obj, state["put_token"])
    call_ltp = get_option_ltp(obj, state["call_token"])
    exit_premium = put_ltp + call_ltp
    total_credit = state["total_credit"]
    
    pnl_per_share = total_credit - exit_premium
    pnl_total = pnl_per_share * CONFIG["lot_size"]
    
    # Close both legs
    place_order(obj, state["put_symbol"], state["put_token"],
                CONFIG["lot_size"], "BUY")
    place_order(obj, state["call_symbol"], state["call_token"],
                CONFIG["lot_size"], "BUY")
    
    trade_data = {
        "entry_date": state["entry_time"],
        "expiry": state["expiry"],
        "entry_spot": state["entry_spot"],
        "put_strike": state["put_strike"],
        "call_strike": state["call_strike"],
        "put_credit": state["put_credit"],
        "call_credit": state["call_credit"],
        "total_credit": total_credit,
        "stop_loss": state["stop_level"],
        "exit_date": datetime.now().isoformat(),
        "exit_spot": spot,
        "exit_reason": reason,
        "exit_premium": exit_premium,
        "pnl": pnl_total,
    }
    log_trade(trade_data)
    
    new_state = {"status": "NO_POSITION", "last_trade": trade_data,
                 "trades": state.get("trades", []) + [trade_data]}
    write_state(new_state)
    
    obj.terminateSession(creds["ANGEL_CLIENT_CODE"])
    
    return {
        "action": "FORCE_CLOSED",
        "reason": reason,
        "pnl": pnl_total,
        "exit_premium": exit_premium,
    }


def run_status():
    """Return current status for reporting."""
    state = read_state()
    result = {
        "status": state["status"],
        "time": datetime.now().isoformat(),
    }
    
    if state["status"] == "IN_POSITION":
        result.update({
            "expiry": state["expiry"],
            "put_strike": state["put_strike"],
            "call_strike": state["call_strike"],
            "total_credit": state["total_credit"],
            "stop_level": state["stop_level"],
            "entry_spot": state["entry_spot"],
            "last_spot": state.get("last_spot"),
            "last_premium": state.get("last_premium"),
        })
    
    # Trading history
    trades = state.get("trades", [])
    if state.get("last_trade"):
        trades = trades + [state["last_trade"]]
    
    result["trade_count"] = len(trades)
    if trades:
        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        losses = sum(1 for t in trades if t.get("pnl", 0) <= 0)
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        result["wins"] = wins
        result["losses"] = losses
        result["total_pnl"] = total_pnl
        result["win_rate"] = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
    
    return result


# ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ───
# CLI ENTRY POINT
# ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ─── ───

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    
    if mode == "entry":
        result = run_entry_check()
    elif mode == "monitor":
        result = run_monitor()
    elif mode == "close":
        reason = sys.argv[2] if len(sys.argv) > 2 else "MANUAL"
        result = run_force_close(reason)
    elif mode == "status":
        result = run_status()
    elif mode == "preview":
        # Dry run: show what entry would look like without placing orders
        creds = load_creds()
        obj = angel_connect(creds)
        nfo = load_master(obj)
        spot = get_nifty_spot(obj)
        vol = get_volatility()
        best_exp, dte = find_next_expiry(nfo)
        put_stk, call_stk, sd, put_ltp, call_ltp, total_credit = calc_strikes(spot, vol, dte, obj, nfo, best_exp)
        
        if best_exp:
            chain = nfo[(nfo["name"]=="NIFTY") & (nfo["instrumenttype"]=="OPTIDX") & (nfo["exp_dt"]==best_exp)]
            strikes = find_strikes_in_chain(chain, put_stk, call_stk)
        else:
            strikes = {"put_strike": put_stk, "call_strike": call_stk, "put_token": None, "call_token": None}
        
        risk = compute_risk(spot, strikes["put_strike"], strikes["call_strike"], put_ltp, call_ltp, vol, dte)
        
        result = {
            "action": "PREVIEW",
            "spot": spot,
            "daily_vol_pct": vol * 100,
            "expiry": best_exp.strftime("%d%b%Y").upper() if best_exp else "N/A",
            "dte": dte,
            "put_strike": strikes["put_strike"],
            "call_strike": strikes["call_strike"],
            "put_ltp": put_ltp,
            "call_ltp": call_ltp,
            **risk,
        }
        obj.terminateSession(creds["ANGEL_CLIENT_CODE"])
    else:
        result = {"error": f"Unknown mode: {mode}. Use: entry, monitor, close, status, preview"}
    
    print(json.dumps(result, indent=2, default=str))
