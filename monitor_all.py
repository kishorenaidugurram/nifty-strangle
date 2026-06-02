#!/usr/bin/env python3
"""
Monitor ALL open positions via Angel One REST API.
Auto-discovers spreads, computes risk, alerts on breach/stop/profit.
No hardcoded positions — works dynamically.

Run: source ~/.hermes/.env && python3 monitor_all.py
Or via GH Actions every 30 min.
"""
import os, sys, json, math, pyotp
from datetime import datetime
from SmartApi import SmartConnect
import yfinance as yf
import requests

CREDS = {
    "api_key": os.environ.get("ANGEL_API_KEY", "2siOJ0EZ"),
    "client_code": os.environ.get("ANGEL_CLIENT_CODE", "G188451"),
    "pin": os.environ.get("ANGEL_PIN", "1980"),
    "totp": os.environ.get("ANGEL_TOTP_SECRET", "LIONHZIIQLSN7MZEDLRSPE5HE4"),
}

STOP_MULT = 2.5
PROFIT_TARGET_PCT = 0.15
VIX_WARN = 22
VIX_SKIP = 25

def get_nifty_spot():
    d = yf.download("^NSEI", period="2d", progress=False, auto_adjust=True)
    return float(d["Close"].values.flatten()[-1]) if not d.empty else 0

def get_stock_spot(ticker):
    d = yf.download(f"{ticker}.NS", period="2d", progress=False, auto_adjust=True)
    return float(d["Close"].values.flatten()[-1]) if not d.empty else 0

def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def compute_sigma(spot, strike, dte, vol=0.0092):
    sd = spot * vol * math.sqrt(max(dte, 1))
    return abs(spot - strike) / sd if sd > 0 else 99

def main():
    print(f"\n{'='*60}")
    print(f"  PORTFOLIO MONITOR — {datetime.now().strftime('%a %b %d, %H:%M:%S')}")
    print(f"{'='*60}")
    
    # Login
    try:
        obj = SmartConnect(api_key=CREDS["api_key"])
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
    from collections import defaultdict
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
    total_upl = 0
    
    for (name, expiry), legs in sorted(groups.items()):
        shorts = [p for p in legs if int(p.get("netqty", 0)) < 0]
        longs = [p for p in legs if int(p.get("netqty", 0)) > 0]
        
        # Determine strategy
        is_strangle = len(shorts) == 2 and len(longs) == 0 and name == "NIFTY"
        is_spread = len(shorts) == 1 and len(longs) == 1
        is_naked = len(shorts) == 1 and len(longs) == 0
        
        expiry_str = expiry
        expiry_dt = datetime.strptime(expiry, "%d%b%Y") if len(expiry) == 9 else datetime.now()
        dte = max(0, (expiry_dt - datetime.now()).days)
        
        # Get spot price
        if name == "NIFTY":
            spot = get_nifty_spot()
        elif name == "BANKNIFTY":
            spot = yf.download("^NSEBANK", period="2d", progress=False, auto_adjust=True)
            spot = float(spot["Close"].values.flatten()[-1]) if not spot.empty else 0
        else:
            spot = get_stock_spot(name)
        
        print(f"  ─{'─'*58}")
        
        if is_strangle:
            # Nifty short strangle — two shorts (PE + CE)
            put = [s for s in shorts if s["optiontype"] == "PE"]
            call = [s for s in shorts if s["optiontype"] == "CE"]
            if put and call:
                p = put[0]; c = call[0]
                put_stk = float(p["strikeprice"])
                call_stk = float(c["strikeprice"])
                put_ltp = float(p.get("ltp", 0))
                call_ltp = float(c.get("ltp", 0))
                sell_price_p = float(p.get("totalsellavgprice", 0) or 0)
                sell_price_c = float(c.get("totalsellavgprice", 0) or 0)
                lot = int(p.get("lotsize", 65))
                qty = abs(int(p.get("netqty", 0)))
                total_credit = sell_price_p + sell_price_c
                current_premium = put_ltp + call_ltp
                pnl = float(p.get("pnl", 0)) + float(c.get("pnl", 0))
                
                print(f"  NIFTY STRANGLE {put_stk:.0f}PE/{call_stk:.0f}CE ×{qty//lot} lot")
                print(f"  Credit: ₹{total_credit:.2f} | Current: ₹{current_premium:.2f} | PnL: ₹{pnl:+,.0f}")
                print(f"  Spot: ₹{spot:,.0f} | Range: [{put_stk:.0f}, {call_stk:.0f}] | DTE: {dte}")
                print(f"  Buffer: {(spot-put_stk)/spot*100:.1f}% / {(call_stk-spot)/spot*100:.1f}%")
                
                stop_level = total_credit * STOP_MULT
                if current_premium >= stop_level:
                    alerts.append(f"⚠️ NIFTY STRANGLE: Premium stop @ ₹{current_premium:.2f} (limit ₹{stop_level:.2f})")
                if spot <= put_stk or spot >= call_stk:
                    alerts.append(f"🔴 NIFTY STRANGLE: Strike breach! Spot ₹{spot:.0f} at {put_stk:.0f}/{call_stk:.0f}")
        
        elif is_spread:
            spread = shorts[0]; hedge = longs[0]
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
                buffer = (spot - short_stk) / spot * 100
            else:
                credit = buy_price - sell_price
                current_cost = buy_ltp - sell_ltp
                buffer = (short_stk - spot) / spot * 100
            
            pnl = float(spread.get("pnl", 0)) + float(hedge.get("pnl", 0))
            lot = int(spread.get("lotsize", 1))
            qty = abs(int(spread.get("netqty", 0)))
            
            width = abs(short_stk - long_stk)
            max_risk = width - credit
            cr_pct = credit / max_risk * 100 if max_risk > 0 else 0
            
            print(f"  {name} {direction} {short_stk:.0f}/{long_stk:.0f} ×{qty//lot} lot")
            print(f"  Credit: ₹{credit:.2f} | Current cost: ₹{current_cost:.2f} | PnL: ₹{pnl:+,.0f}")
            print(f"  Spot: ₹{spot:.2f} | Buffer: {buffer:.1f}% | C/R: {cr_pct:.0f}% | DTE: {dte}")
            
            stop_level = credit * STOP_MULT
            target_level = credit * PROFIT_TARGET_PCT
            
            if direction == "PCS":
                if spot <= short_stk:
                    alerts.append(f"🔴 {name}: Strike breach! Spot ₹{spot:.2f} below {short_stk:.0f}")
                if current_cost >= stop_level:
                    alerts.append(f"⚠️ {name}: Premium stop ₹{current_cost:.2f} (limit ₹{stop_level:.2f})")
                if current_cost <= target_level and dte > 1:
                    alerts.append(f"✅ {name}: Profit target hit ₹{current_cost:.2f} (target ₹{target_level:.2f})")
            else:
                if spot >= short_stk:
                    alerts.append(f"🔴 {name}: Strike breach! Spot ₹{spot:.2f} above {short_stk:.0f}")
                if current_cost >= stop_level:
                    alerts.append(f"⚠️ {name}: Premium stop ₹{current_cost:.2f} (limit ₹{stop_level:.2f})")
                if current_cost <= target_level and dte > 1:
                    alerts.append(f"✅ {name}: Profit target hit ₹{current_cost:.2f} (target ₹{target_level:.2f})")
        
        elif is_naked:
            s = shorts[0]
            stk = float(s["strikeprice"])
            opt = s["optiontype"]
            ltp = float(s.get("ltp", 0))
            sell_p = float(s.get("totalsellavgprice", 0) or 0)
            pnl = float(s.get("pnl", 0))
            lot = int(s.get("lotsize", 1))
            qty = abs(int(s.get("netqty", 0)))
            
            print(f"  {name} {stk:.0f}{opt} ×{qty//lot} lot (NAKED)")
            print(f"  Sell @ ₹{sell_p:.2f} | LTP: ₹{ltp:.2f} | PnL: ₹{pnl:+,.0f}")
            print(f"  Spot: ₹{spot:.2f} | DTE: {dte}")
            
            if opt == "PE" and spot <= stk:
                alerts.append(f"🔴 {name}: Naked put ITM! Spot ₹{spot:.2f} below {stk:.0f}")
            if opt == "CE" and spot >= stk:
                alerts.append(f"🔴 {name}: Naked call ITM! Spot ₹{spot:.2f} above {stk:.0f}")
        
        total_upl += pnl
        print()
    
    print(f"  ─{'─'*58}")
    print(f"  TOTAL UNREALIZED P&L: ₹{total_upl:+,.0f}")
    
    # VIX regime note
    try:
        vix = yf.download("^INDIAVIX", period="1mo", progress=False)
        if not vix.empty:
            v = float(vix["Close"].values.flatten()[-1])
            print(f"  India VIX: {v:.1f}" + (" ⚠️ Elevated" if v > VIX_WARN else " ✅ Normal"))
    except:
        pass
    
    # Alerts
    print(f"\n{'='*60}")
    print(f"  ALERTS ({len(alerts)})")
    print(f"{'='*60}")
    if alerts:
        for a in alerts:
            print(f"  {a}")
    else:
        print("  ✅ All positions within safe parameters")
    
    obj.terminateSession(CREDS["client_code"])
    return alerts

if __name__ == "__main__":
    main()
