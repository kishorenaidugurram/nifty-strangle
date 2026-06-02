#!/usr/bin/env python3
"""
Monitor ALL open positions via Angel One REST API.
Auto-discovers spreads, alerts on breach/stop/profit via Telegram.
No hardcoded positions — works dynamically.

Exit rules per position type:
  - Strangle:  strike breach > premium stop > profit target > expiry
  - Credit spread (PCS/CCS): strike breach > premium stop > profit target
  - Naked: strike ITM = immediate alert

Telegram: sends formatted alert when any threshold is hit.
GH Actions: runs every 15 min during market hours.
"""
import os, sys, json, math, pyotp
from datetime import datetime
from SmartApi import SmartConnect
import yfinance as yf
import requests
from collections import defaultdict
import numpy as np

# ─── CONFIG ───
CREDS = {
    "api_key": os.environ.get("ANGEL_API_KEY", "2siOJ0EZ"),
    "client_code": os.environ.get("ANGEL_CLIENT_CODE", "G188451"),
    "pin": os.environ.get("ANGEL_PIN", "1980"),
    "totp": os.environ.get("ANGEL_TOTP_SECRET", "LIONHZIIQLSN7MZEDLRSPE5HE4"),
    "static_ip": os.environ.get("ANGEL_STATIC_IP", "49.43.230.169"),
}

STOP_MULT = 2.5
PROFIT_TARGET_PCT = 0.15
DEFAULT_DAILY_VOL = 0.0092

# Cache for support/resistance levels
_sr_cache = {}

def send_audio_alert(messages):
    """Generate TTS and send as voice message to Telegram for critical alerts."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        # Pick the first critical/warning message
        for level, msg in messages:
            if level in ("CRITICAL", "WARNING"):
                lines = msg.split("\n")
                ticker_line = [l for l in lines if "*" in l]
                ticker = ticker_line[0].replace("*","").strip() if ticker_line else "portfolio"
                
                tts_text = f"{level} alert for {ticker}. {msg[:100]}"
                
                # Use gTTS (works in GH Actions, no audio hardware needed)
                from gtts import gTTS
                tts = gTTS(text=tts_text[:300], lang="en", slow=False)
                temp_path = "/tmp/portfolio_alert.mp3"
                tts.save(temp_path)
                
                import requests as rq
                with open(temp_path, 'rb') as f:
                    rq.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVoice",
                           data={"chat_id": TELEGRAM_CHAT_ID}, files={"voice": f}, timeout=15)
                break
    except ImportError:
        print("  ⚠ gTTS not installed — skipping audio alert")
    except Exception as e:
        print(f"  ⚠ Audio alert error: {e}")

def get_volume_by_price(name):
    """
    Volume by Price (VbP): bin price into 1% buckets and sum volume.
    Returns sorted list of (price_level, volume) for volume peaks.
    A peak is where volume in a bin is 1.5x higher than both neighbors.
    """
    d = yf.download(f"{name}.NS", period="3mo", progress=False, auto_adjust=True)
    if d.empty or len(d) < 20:
        return [], []
    
    highs = d['High'].values.flatten()
    lows = d['Low'].values.flatten()
    volumes = d['Volume'].values.flatten()
    close = float(d['Close'].values.flatten()[-1])
    
    price_min = float(np.min(lows))
    price_max = float(np.max(highs))
    bin_size = max(price_max * 0.01, 1.0)  # 1% bins
    bins = np.arange(price_min, price_max + bin_size, bin_size)
    vol_by_price = np.zeros(len(bins) - 1, dtype=float)
    
    for i in range(len(highs)):
        bar_low = float(lows[i])
        bar_high = float(highs[i])
        bar_vol = float(volumes[i])
        if bar_high <= bar_low or bar_vol <= 0:
            continue
        start_bin = max(0, int((bar_low - price_min) / bin_size))
        end_bin = min(len(bins) - 2, int((bar_high - price_min) / bin_size))
        if end_bin < start_bin:
            continue
        bars_covered = end_bin - start_bin + 1
        vol_per_bar = bar_vol / bars_covered
        vol_by_price[start_bin:end_bin + 1] += vol_per_bar
    
    # Find volume peaks (1.5x neighbors)
    vol_peaks_below = []
    vol_peaks_above = []
    for i in range(1, len(vol_by_price) - 1):
        if vol_by_price[i] > vol_by_price[i-1] * 1.5 and \
           vol_by_price[i] > vol_by_price[i+1] * 1.5:
            price_level = round((bins[i] + bins[i+1]) / 2, 1)
            if price_level < close:
                vol_peaks_below.append((price_level, vol_by_price[i]))
            elif price_level > close:
                vol_peaks_above.append((price_level, vol_by_price[i]))
    
    vol_peaks_below.sort(key=lambda x: -x[1])
    vol_peaks_above.sort(key=lambda x: -x[1])
    return vol_peaks_below, vol_peaks_above

def get_support_resistance(name, direction="PCS"):
    """
    Real S/R from Volume by Price peaks.
    Support = highest-volume price bin BELOW current price
    Resistance = highest-volume price bin ABOVE current price
    """
    if name in _sr_cache:
        return _sr_cache[name].get(direction)
    
    try:
        below, above = get_volume_by_price(name)
        best_support = below[0][0] if below else None
        best_resistance = above[0][0] if above else None
        
        cache = {"PCS": best_support, "CCS": best_resistance}
        _sr_cache[name] = cache
        return cache.get(direction)
    except Exception as e:
        print(f"  ⚠ VbP error for {name}: {e}")
        return None

# Telegram — set these as GH Actions secrets
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

def send_telegram(msg, alert_level="INFO"):
    """Send alert to Telegram. Falls back to print if no token configured."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"  [Telegram not configured — skipping send]")
        return False
    try:
        emoji = {"CRITICAL": "🚨", "WARNING": "⚠️", "INFO": "ℹ️", "SUCCESS": "✅"}
        prefix = emoji.get(alert_level, "ℹ️")
        text = f"{prefix} *Portfolio Monitor*\n{msg}"
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"  ⚠ Telegram send error: {e}")
        return False

def get_nifty_spot():
    d = yf.download("^NSEI", period="2d", progress=False, auto_adjust=True)
    return float(d["Close"].values.flatten()[-1]) if not d.empty else 0

def get_stock_spot(ticker):
    d = yf.download(f"{ticker}.NS", period="2d", progress=False, auto_adjust=True)
    return float(d["Close"].values.flatten()[-1]) if not d.empty else 0

def main():
    now = datetime.now()
    print(f"\n{'='*60}")
    print(f"  PORTFOLIO MONITOR — {now.strftime('%a %b %d, %H:%M:%S')}")
    print(f"{'='*60}")
    
    # Login
    try:
        obj = SmartConnect(api_key=CREDS["api_key"], clientPublicIP=CREDS["static_ip"])
        resp = obj.generateSession(
            CREDS["client_code"], CREDS["pin"],
            pyotp.TOTP(CREDS["totp"]).now()
        )
        if not resp.get("status"):
            print("❌ Login failed")
            return
    except Exception as e:
        print(f"❌ Login error: {e}")
        return
    
    # Fetch positions
    pos = obj.position()
    all_positions = pos.get("data", [])
    
    if not all_positions:
        print("  No open positions found")
        obj.terminateSession(CREDS["client_code"])
        return
    
    # Group by (name, expiry) to form spreads
    groups = defaultdict(list)
    for p in all_positions:
        nq = int(p.get("netqty", 0))
        if nq == 0:
            continue
        key = (p["symbolname"], p["expirydate"])
        groups[key].append(p)
    
    print(f"  Found {len(groups)} position groups")
    print()
    
    alerts = []
    telegram_alerts = []
    total_upl = 0
    
    for (name, expiry), legs in sorted(groups.items()):
        shorts = [p for p in legs if int(p.get("netqty", 0)) < 0]
        longs = [p for p in legs if int(p.get("netqty", 0)) > 0]
        
        is_strangle = len(shorts) == 2 and len(longs) == 0 and name == "NIFTY"
        is_spread = len(shorts) == 1 and len(longs) == 1
        is_naked = len(shorts) == 1 and len(longs) == 0
        
        expiry_dt = datetime.strptime(expiry, "%d%b%Y") if len(expiry) == 9 else now
        dte = max(0, (expiry_dt - now).days)
        is_expiry_day = dte <= 0
        
        # Spot
        if name == "NIFTY":
            spot = get_nifty_spot()
        elif name == "BANKNIFTY":
            d = yf.download("^NSEBANK", period="2d", progress=False, auto_adjust=True)
            spot = float(d["Close"].values.flatten()[-1]) if not d.empty else 0
        else:
            spot = get_stock_spot(name)
        
        print(f"  ─{'─'*58}")
        
        position_summary = f"*{name}* | Exp: {expiry[:5]} | DTE: {dte}"
        
        # ─── EXIT RULES — APPLIED CONSISTENTLY ───
        # Priority: Breach > Premium Stop > Profit Target > Expiry
        
        breach_triggered = False
        stop_triggered = False
        profit_triggered = False
        
        if is_strangle and len(shorts) == 2:
            put = [s for s in shorts if s["optiontype"] == "PE"]
            call = [s for s in shorts if s["optiontype"] == "CE"]
            if put and call:
                p = put[0]; c = call[0]
                put_stk = float(p["strikeprice"])
                call_stk = float(c["strikeprice"])
                put_ltp = float(p.get("ltp", 0))
                call_ltp = float(c.get("ltp", 0))
                sell_p = float(p.get("totalsellavgprice", 0) or 0)
                sell_c = float(c.get("totalsellavgprice", 0) or 0)
                lot = int(p.get("lotsize", 65))
                qty = abs(int(p.get("netqty", 0)))
                total_credit = sell_p + sell_c
                current_premium = put_ltp + call_ltp
                pnl = float(p.get("pnl", 0)) + float(c.get("pnl", 0))
                
                print(f"  NIFTY STRANGLE {put_stk:.0f}PE/{call_stk:.0f}CE ×{qty//lot} lot")
                print(f"  Credit: ₹{total_credit:.2f} | Current: ₹{current_premium:.2f} | PnL: ₹{pnl:+,.0f}")
                print(f"  Spot: ₹{spot:,.0f} | Range: {put_stk:.0f}-{call_stk:.0f} | DTE: {dte}")
                
                # EXIT 1: Strike breach
                if spot <= put_stk:
                    breach_triggered = True
                    msg = f"🚨 STRIKE BREACH — {name} {put_stk:.0f}PE breached! Spot ₹{spot:.0f} ≤ ₹{put_stk:.0f}. Close immediately."
                    alerts.append(f"🔴 {msg}")
                    telegram_alerts.append(("CRITICAL", f"{position_summary}\n🔴 {msg}"))
                
                elif spot >= call_stk:
                    breach_triggered = True
                    msg = f"🚨 STRIKE BREACH — {name} {call_stk:.0f}CE breached! Spot ₹{spot:.0f} ≥ ₹{call_stk:.0f}. Close immediately."
                    alerts.append(f"🔴 {msg}")
                    telegram_alerts.append(("CRITICAL", f"{position_summary}\n🔴 {msg}"))
                
                # EXIT 2: Premium stop
                if not breach_triggered:
                    stop_level = total_credit * STOP_MULT
                    if current_premium >= stop_level:
                        stop_triggered = True
                        msg = f"⚠️ PREMIUM STOP — {name} strangle: current ₹{current_premium:.2f} ≥ stop ₹{stop_level:.2f} (2.5× credit). Close."
                        alerts.append(f"⚠️ {msg}")
                        telegram_alerts.append(("WARNING", f"{position_summary}\n⚠️ {msg}"))
                
                # EXIT 3: Profit target (not on expiry day)
                if not breach_triggered and not stop_triggered and not is_expiry_day:
                    target = total_credit * PROFIT_TARGET_PCT
                    if current_premium <= target:
                        profit_triggered = True
                        msg = f"✅ PROFIT TARGET — {name} strangle: current ₹{current_premium:.2f} ≤ target ₹{target:.2f} (15% credit). Book profit."
                        alerts.append(f"✅ {msg}")
                        telegram_alerts.append(("SUCCESS", f"{position_summary}\n✅ {msg}"))
                
                # Print status
                print(f"  Stop: ₹{total_credit*STOP_MULT:.2f} | Target: ₹{total_credit*PROFIT_TARGET_PCT:.2f}")
                status = "🔴 BREACH" if breach_triggered else ("⚠️ STOP" if stop_triggered else ("✅ PROFIT" if profit_triggered else "✅ SAFE"))
                print(f"  Status: {status}")
        
        elif is_spread:
            spread = shorts[0]
            hedge = longs[0]
            short_stk = float(spread["strikeprice"])
            long_stk = float(hedge["strikeprice"])
            opt = spread["optiontype"]
            direction = "PCS" if opt == "PE" else "CCS"
            
            sell_ltp = float(spread.get("ltp", 0))
            buy_ltp = float(hedge.get("ltp", 0))
            sell_price = float(spread.get("totalsellavgprice", 0) or 0)
            buy_price = float(hedge.get("totalbuyavgprice", 0) or 0)
            
            if direction == "PCS":
                credit = sell_price - buy_price
                current_cost = sell_ltp - buy_ltp
                buffer = (spot - short_stk) / spot * 100 if spot > 0 else 0
            else:
                credit = buy_price - sell_price
                current_cost = buy_ltp - sell_ltp
                buffer = (short_stk - spot) / spot * 100 if spot > 0 else 0
            
            pnl = float(spread.get("pnl", 0)) + float(hedge.get("pnl", 0))
            lot = int(spread.get("lotsize", 1))
            qty = abs(int(spread.get("netqty", 0)))
            width = abs(short_stk - long_stk)
            max_risk = width - credit
            cr_pct = credit / max_risk * 100 if max_risk > 0 else 0
            
            print(f"  {name} {direction} {short_stk:.0f}/{long_stk:.0f} ×{qty//lot} lot")
            print(f"  Credit: ₹{credit:.2f} | Current: ₹{current_cost:.2f} | PnL: ₹{pnl:+,.0f}")
            print(f"  Spot: ₹{spot:.2f} | Buffer: {buffer:.1f}% | C/R: {cr_pct:.0f}% | DTE: {dte}")
            
            stop_level = credit * STOP_MULT
            target_level = credit * PROFIT_TARGET_PCT
            print(f"  Stop: ₹{stop_level:.2f} (2.5×) | Target: ₹{target_level:.2f} (15%)")
            
            # EXIT 0: Support/Resistance breach (PATTERN LEVEL) — early warning
            # Uses 20-day SMA as dynamic support/resistance level
            if direction == "PCS":
                support_level = get_support_resistance(name, direction="PCS")
                if support_level is None:
                    support_level = round((spot + short_stk) / 2, 1)  # fallback
                buffer_to_support = (spot - support_level) / spot * 100 if spot > 0 else 0
                pct_of_buffer_consumed = (1 - buffer_to_support / buffer) * 100 if buffer > 0 else 0
                
                print(f"  Support (volume node): ₹{support_level:.0f} (spot {buffer_to_support:.1f}% above — {pct_of_buffer_consumed:.0f}% of buffer consumed)")
                
                if buffer_to_support < 2.0 and not breach_triggered and not stop_triggered:
                    msg = f"🔸 EARLY WARNING — {name} PCS approaching volume node (₹{support_level:.0f}). Spot ₹{spot:.2f}, only {buffer_to_support:.1f}% above."
                    alerts.append(f"🔸 {msg}")
                    telegram_alerts.append(("WARNING", f"{position_summary}\n🔸 {msg}"))
                    print(f"  ⚠️ Layer 1 — Pattern breach WARNING")
                elif buffer_to_support <= 0 and not breach_triggered:
                    msg = f"🔴 PATTERN BREACH — {name} PCS: Spot ₹{spot:.2f} broke volume support ₹{support_level:.0f}. Pattern invalidated."
                    alerts.append(f"🔴 {msg}")
                    telegram_alerts.append(("CRITICAL", f"{position_summary}\n🔴 {msg}"))
                    print(f"  🔴 Layer 1 — Pattern BREACHED")
                else:
                    print(f"  ✅ Layer 1 — Pattern intact ({buffer_to_support:.1f}% to volume node)")
            else:
                # For CCS, resistance is the same SMA20
                resistance_level = get_support_resistance(name, direction="CCS")
                if resistance_level is None:
                    resistance_level = round((spot + short_stk) / 2, 1)  # fallback
                buffer_to_resistance = (resistance_level - spot) / spot * 100 if spot > 0 else 0
                pct_of_buffer_consumed = (1 - buffer_to_resistance / buffer) * 100 if buffer > 0 else 0
                
                print(f"  Resistance (volume node): ₹{resistance_level:.0f} (spot {buffer_to_resistance:.1f}% below — {pct_of_buffer_consumed:.0f}% of buffer consumed)")
                
                if buffer_to_resistance < 2.0 and not breach_triggered and not stop_triggered:
                    msg = f"🔸 EARLY WARNING — {name} CCS approaching volume node (₹{resistance_level:.0f}). Spot ₹{spot:.2f}, only {buffer_to_resistance:.1f}% below."
                    alerts.append(f"🔸 {msg}")
                    telegram_alerts.append(("WARNING", f"{position_summary}\n🔸 {msg}"))
                    print(f"  ⚠️ Layer 1 — Pattern breach WARNING")
                elif buffer_to_resistance <= 0 and not breach_triggered:
                    msg = f"🔴 PATTERN BREACH — {name} CCS: Spot ₹{spot:.2f} broke volume resistance ₹{resistance_level:.0f}. Pattern invalidated."
                    alerts.append(f"🔴 {msg}")
                    telegram_alerts.append(("CRITICAL", f"{position_summary}\n🔴 {msg}"))
                    print(f"  🔴 Layer 1 — Pattern BREACHED")
                else:
                    print(f"  ✅ Layer 1 — Pattern intact ({buffer_to_resistance:.1f}% to volume node)")
            
            # EXIT 1: Strike breach
            if direction == "PCS" and spot <= short_stk:
                breach_triggered = True
                msg = f"🚨 STRIKE BREACH — {name} PCS {short_stk:.0f}PE breached! Spot ₹{spot:.2f} ≤ ₹{short_stk:.0f}. Close spread."
                alerts.append(f"🔴 {msg}")
                telegram_alerts.append(("CRITICAL", f"{position_summary}\n🔴 {msg}"))
            
            elif direction == "CCS" and spot >= short_stk:
                breach_triggered = True
                msg = f"🚨 STRIKE BREACH — {name} CCS {short_stk:.0f}CE breached! Spot ₹{spot:.2f} ≥ ₹{short_stk:.0f}. Close spread."
                alerts.append(f"🔴 {msg}")
                telegram_alerts.append(("CRITICAL", f"{position_summary}\n🔴 {msg}"))
            
            # EXIT 2: Premium stop
            if not breach_triggered and current_cost >= stop_level:
                stop_triggered = True
                msg = f"⚠️ PREMIUM STOP — {name} {direction}: current ₹{current_cost:.2f} ≥ stop ₹{stop_level:.2f} (2.5× credit). Consider closing."
                alerts.append(f"⚠️ {msg}")
                telegram_alerts.append(("WARNING", f"{position_summary}\n⚠️ {msg}"))
            
            # EXIT 3: Profit target
            if not breach_triggered and not stop_triggered and not is_expiry_day and current_cost <= target_level:
                profit_triggered = True
                msg = f"✅ PROFIT TARGET — {name} {direction}: current ₹{current_cost:.2f} ≤ target ₹{target_level:.2f} (15% credit). Book profit."
                alerts.append(f"✅ {msg}")
                telegram_alerts.append(("SUCCESS", f"{position_summary}\n✅ {msg}"))
            
            status = "🔴 BREACH" if breach_triggered else ("⚠️ STOP" if stop_triggered else ("✅ PROFIT" if profit_triggered else "✅ SAFE"))
            print(f"  Status: {status}")
        
        elif is_naked:
            s = shorts[0]
            stk = float(s["strikeprice"])
            opt = s["optiontype"]
            ltp = float(s.get("ltp", 0))
            sell_p = float(s.get("totalsellavgprice", 0) or 0)
            pnl_val = float(s.get("pnl", 0))
            lot = int(s.get("lotsize", 1))
            qty = abs(int(s.get("netqty", 0)))
            
            print(f"  {name} {stk:.0f}{opt} ×{qty//lot} lot (NAKED)")
            print(f"  Sell @ ₹{sell_p:.2f} | LTP: ₹{ltp:.2f} | PnL: ₹{pnl_val:+,.0f}")
            print(f"  Spot: ₹{spot:.2f} | DTE: {dte}")
            
            # Naked options: ITM = immediate alert
            if opt == "PE" and spot <= stk:
                breach_triggered = True
                msg = f"🚨 NAKED PUT ITM — {name} {stk:.0f}PE. Spot ₹{spot:.2f} below ₹{stk:.0f}. Close immediately — unlimited downside!"
                alerts.append(f"🔴 {msg}")
                telegram_alerts.append(("CRITICAL", f"{position_summary}\n🔴 {msg}"))
            
            elif opt == "CE" and spot >= stk:
                breach_triggered = True
                msg = f"🚨 NAKED CALL ITM — {name} {stk:.0f}CE. Spot ₹{spot:.2f} above ₹{stk:.0f}. Close immediately — unlimited downside!"
                alerts.append(f"🔴 {msg}")
                telegram_alerts.append(("CRITICAL", f"{position_summary}\n🔴 {msg}"))
            
            # For naked options, also warn at 80% ITM
            if opt == "PE" and not breach_triggered:
                pct_itm = (stk - spot) / stk * 100
                if pct_itm > 1:
                    msg = f"⚠️ NAKED PUT at {pct_itm:.1f}% ITM — {name} {stk:.0f}PE. Spot ₹{spot:.2f} approaching ₹{stk:.0f}. Consider rolling."
                    alerts.append(msg)
                    telegram_alerts.append(("WARNING", f"{position_summary}\n⚠️ {msg}"))
            if opt == "CE" and not breach_triggered:
                pct_itm = (spot - stk) / stk * 100
                if pct_itm > 1:
                    msg = f"⚠️ NAKED CALL at {pct_itm:.1f}% ITM — {name} {stk:.0f}CE. Spot ₹{spot:.2f} approaching ₹{stk:.0f}. Consider rolling."
                    alerts.append(msg)
                    telegram_alerts.append(("WARNING", f"{position_summary}\n⚠️ {msg}"))
            
            status = "🔴 BREACH" if breach_triggered else "✅ SAFE"
            print(f"  Status: {status}")
        
        total_upl += pnl
        print()
    
    # Summary
    print(f"  ─{'─'*58}")
    print(f"  TOTAL UNREALIZED P&L: ₹{total_upl:+,.0f}")
    
    # VIX
    try:
        vix = yf.download("^INDIAVIX", period="1mo", progress=False)
        if not vix.empty:
            v = float(vix["Close"].values.flatten()[-1])
            print(f"  India VIX: {v:.1f}" + (" ⚠️ Elevated" if v > 22 else " ✅ Normal"))
    except:
        pass
    
    # Print alerts
    print(f"\n{'='*60}")
    print(f"  ALERTS ({len(telegram_alerts)})")
    print(f"{'='*60}")
    if telegram_alerts:
        for level, msg in telegram_alerts:
            print(f"  [{level}] {msg.split(chr(10))[-1][:120]}")
        # Send to Telegram
        for level, msg in telegram_alerts:
            send_telegram(msg, level)
    else:
        print("  ✅ All positions within safe parameters")
    
    # If not in CI/actions, also print stats
    if not os.environ.get("GITHUB_ACTIONS"):
        print(f"\n  Positions: {len(groups)} | Total P&L: ₹{total_upl:+,.0f}")
    
    obj.terminateSession(CREDS["client_code"])
    return telegram_alerts

if __name__ == "__main__":
    main()
