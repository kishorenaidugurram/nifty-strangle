# Nifty Weekly Strangle — Systematic Short Strangle Strategy

## Strategy

Sell an out-of-the-money Put and an out-of-the-money Call on Nifty every
Tuesday at 3:25 PM (25 min before weekly expiry), expiring the **next** Tuesday
(7 DTE). Strikes at **2 standard deviations** from spot.

## Why This Works

| Metric | Normal (2σ) | Fat-Tail Adjusted | 
|--------|:-----------:|:-----------------:|
| Moves inside range | 95.4% | ~84% |
| Breaches one side | 4.6% | ~16% |
| Breakeven win/loss ratio | 1:21 | 1:5.2 |

Even with fat tails, you need losses to be <5× your wins. With a stop at
**2.5× credit**, and profit target at **85% decay**, the expectancy is:

```
E = 0.84(0.85) - 0.16(2.5) = 0.714 - 0.400 = +0.314 per Re 1 credit
```

Positive expectancy over time. You lose small (stopped at 2.5× credit)
and win often (84% of trades hit 85% profit).

## Execution Schedule

| Day | Time | Action |
|-----|:---:|--------|
| **Tuesday** | **3:25 PM** | Enter: Sell 2σ Put + 2σ Call for next Tue expiry |
| Wed-Mon | — | Monitor. Set stop at 2.5× credit. |
| **Next Tue** | **3:25 PM** | Close/expire. Enter next week's strangle. |

## Strike Selection

```
Put Strike  = round_to_50(spot - σ_daily × √7 × 2.0)
Call Strike = round_to_50(spot + σ_daily × √7 × 2.0)

where σ_daily = blended volatility (60% 6mo + 40% recent 20d)
```

Current parameters (Jun 1, 2026):
- σ_daily ≈ 240 pts (1.03%)
- 7-day 2σ ≈ 1,269 pts
- 2σ Put ≈ 22,100 | 2σ Call ≈ 24,650

## Risk Framework

### Position Sizing (Half-Kelly)
```
Capital × 22% = max risk per trade
With Rs 1,00,000 → ~2-3 lots max
```

### Stop Loss (3-Layer)
1. **Premium Stop** (default): Close when premium reaches 2.5× credit
2. **Strike Stop**: Close if spot touches either strike
3. **Delta Stop**: Close if combined delta > 0.25

### Profit Target
Book when 15% of credit remains (= 85% profit captured).
Only let expire if < 1 DTE and both strikes > 2% OTM.

### Black Swan Protection
- Skip if VIX > 25
- Max single-trade risk: 5% of capital
- After stop-out: wait 1 week before re-entering

## Files

| File | Purpose |
|------|---------|
| `strangle_calculator.py` | Strike calculator + Angel One trade entry |
| `risk_framework.md` | Full risk management reference |
| `trade_log.csv` | Every trade logged automatically |
