# Risk Management Framework — Nifty Weekly Strangle

## Core Principle: Lose Small, Win Often

The strangle has positive mathematical expectancy IF:
1. You win ~80-84% of the time (strikes at 2σ, fat-tail adjusted)
2. Your average loss is capped at <5× your average win

With stop at **2.5× credit** and target at **85% decay**:
```
E = 0.84(0.85C) - 0.16(2.5C) = 0.714C - 0.400C = +0.314C ✅
```
You are mathematically ahead after every ~3 trades.

---

## 1. Position Sizing (Kelly Criterion)

### Full Kelly
```
Kelly % = W - (1-W)/R
  W = 0.84 (win rate)
  R = 0.85C / 2.5C = 0.34 (win/loss ratio)
  
Kelly % = 0.84 - 0.16/0.34 = 0.84 - 0.47 = 0.37 (37%)
```

### Conservative Sizing
| Level | % of Capital | With ₹1,00,000 | Lots (75×) |
|-------|:------------:|:--------------:|:----------:|
| Full Kelly | 37% | ₹37,000 | 4-5 |
| Half-Kelly | 18% | ₹18,000 | 2-3 |
| Quarter-Kelly | 9% | ₹9,000 | 1 |
| **Recommended** | **10-15%** | **₹10-15,000** | **1-2** |

### Margin Requirement
Naked short options at 2σ require ~15-20% of notional as margin.
For 23,000 × 75 = ₹17.25L notional → ~₹2.5-3.5L margin per lot.
With 1 lot, margin ~₹2.5L. With SPAN + exposure, actual may be lower.

**Important:** Margin on short options at 2σ OTM is relatively low
because the strike is far from spot. SPAN margin benefits from the
long expiry and OTM status.

---

## 2. Stop Loss System (3-Layer Protection)

### Layer 1 — Premium Stop (Default)
```
Trigger: Total premium (put + call) reaches 2.5× initial credit
Action: Close both legs at market
Loss: 2.5× credit = ~₹250 per lot for typical ₹100 credit
```

**Backtest logic:** At 2σ, when premium doubles, the market has
moved approximately 1σ toward one strike. Doubling again to 2.5×
gives you a small buffer before gamma explodes.

### Layer 2 — Strike Breach (Hard Stop)
```
Trigger: Spot touches either short strike
Action: Close immediately — do not wait
Loss: Variable — ~3-4× credit if caught early
```

**Why:** Once spot touches your strike, gamma is maximum. Every
further point costs you ₹75 × delta (which is now 0.5 and rising).
A 50-point gap through your strike = ₹3,750 loss on a ₹100 credit.

### Layer 3 — Delta Stop (Advanced)
```
Trigger: Combined delta of the position > 0.25
Action: Close both legs
Loss: ~2× credit
```

**When to use:** In fast markets where premium jumps happen in
seconds and you can't get filled at Layer 1 prices.

---

## 3. Profit Taking

### Primary — 85% Decay
```
Close when remaining premium = 15% of credit
Profit = 85% of credit collected
```

At 7 DTE with 2σ strikes, theta decay is roughly:
- Day 1-2: ~15-20% of credit decays
- Day 3-4: ~25-30% more  
- Day 5-6: ~25-30% more
- Day 7 (expiry): final ~15-20%

**80% of decay happens in the last 4 days.** Entering on Tuesday
next week at 7 DTE means by Friday you've collected ~40% of profit.
By Monday ~65%. By Tuesday expiry ~85%.

### Secondary — Expiry (Favorable Only)
If at 1 DTE both strikes are >2% OTM (spot is well inside range),
let it expire worthless. Collect the full credit.

---

## 4. Rolling Rules

| Scenario | DTE | Action |
|----------|:---:|--------|
| One side tested, >7 DTE | 7+ | Roll tested side 1 strike further OTM for credit |
| One side breached, >3 DTE | 4-7 | Close entirely. Don't roll at 0 DTE gamma |
| Both sides OTM, <2 DTE | 1-2 | Let expire |
| One side ITM, <3 DTE | 1-3 | Close at loss. Don't hope. |

---

## 5. Black Swan Protocol

### Skip Conditions
- **VIX > 25**: Systematic regimes of elevated vol break strangle math
- **Budget day / RBI policy / Fed meeting**: Known event risk
- **After a stop-out**: Skip next week (let the market "reset")

### Expected Black Swan Frequency
| Severity | Move | Frequency | Loss Multiplier |
|----------|:----:|:---------:|:--------------:|
| 3σ | ~7.5% | Once/year | ~8-10× credit |
| 4σ | ~10% | Once/5 years | ~15-20× credit |

### How to Survive a Black Swan
1. **Never risk >5% of capital per trade.** A 10× loss wipes 50%
   of that trade's allocation, not 50% of your account.
2. **Have cash reserve.** Keep 50% of capital in cash/money market.
   If stopped out, you replenish from the reserve.
3. **Trade size = 1 lot.** The difference between ₹2.5L and ₹5L
   margin is the difference between a bad week and a blown account.

---

## 6. Tracking & Adjustment

### Trade Log Columns
| Entry Date | Expiry | Spot | Put Strike | Call Strike | Put Credit | Call Credit | Total Credit | Stop | Exit Date | Exit Reason | P&L |
|------------|--------|:----:|:----------:|:-----------:|:----------:|:-----------:|:------------:|:----:|-----------|-------------|:---:|

### Adjustment Triggers (after 20 trades)
| Observation | Action |
|-------------|--------|
| Win rate > 90% | Tighten to 1.8σ — too much credit left on table |
| Win rate < 75% | Widen to 2.2σ — strikes too close |
| Avg loss > 3.5× credit | Tighten stop from 2.5× to 2× |
| Expectancy < 0 | Pause. Market regime shift. Review. |

---

## 7. Mathematical Foundation

### Probability Density
Under the normal distribution:
```
P(|x| < 2σ) ≈ 95.4%
P(|x| > 2σ) ≈ 4.6%  (2.3% each side)
```

Under fat-tail adjustment (30% fatter tails, ×0.7):
```
Effective z = 2.0 × 0.7 = 1.4
P(|x| < 1.4σ) ≈ 83.8%
P(|x| > 1.4σ) ≈ 16.2%  (8.1% each side)
```

### Expectancy Calculation
```
With 2.5× stop and 85% profit target:

E = (0.838 × 0.85C) - (0.162 × 2.5C)
  = 0.712C - 0.405C
  = +0.307C per trade

A ₹100 credit strangle → +₹30.70 expected value per trade
Over 50 trades/year → +₹1,535 expected per lot

With 2 lots: +₹3,070/year
With conservative sizing and 80% win rate: still positive
```

### Law of Large Numbers
At 50 trades/year with 84% win rate:
- Expected wins: 42
- Expected losses: 8
- Standard deviation of win count: √(50×0.84×0.16) ≈ 2.6
- 95% confidence: 37-47 wins

The strategy is solidly positive. The risk is not in the math —
it's in staying disciplined through the 8 losing trades per year.
