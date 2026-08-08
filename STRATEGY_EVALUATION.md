# Strategy Evaluation — Automated Trading System

**Status: built, instrumented, tested, not funded.**
Date: August 2026 · Author: Gabriel Peña

---

## What was built

An automated trading system running against Alpaca's paper-trading API, in Python.

**Execution.** A broker layer wrapping Alpaca for market data and order submission,
with request timeouts, parallelized account calls, and native GTC stop orders
submitted alongside each entry rather than simulated in-process. Positions are
tracked as signed quantities, so long and short are the same code path.

**Risk accounting.** A risk manager sitting between strategy intent and order
submission. It enforces per-symbol and total-portfolio exposure caps on *absolute*
value, so a long and a short don't net out to an apparent zero exposure. It applies
stop-loss and take-profit exits sign-aware, and it splits a position flip
(long → short) into two explicit orders, because Alpaca rejects a single equity
order that crosses through zero.

**Strategies.** Three per-symbol strategies — trend following, mean reversion, and
momentum breakout — across SPY, QQQ, GLD, USO, and BTC/USD on daily bars. Equities
can go long or short; crypto is gated long-or-flat, because Alpaca does not support
shorting crypto.

**Interface.** A web dashboard for positions, equity, and history. It is read-only
with respect to trading: it never calls the risk manager or submits orders.

**Instrumentation.** This is the part that matters, and it is what the rest of this
document rests on:

- **Walk-forward validation.** Every backtest splits its history in half and reports
  the halves separately. The second half is the only sample untouched by any
  parameter choice, and it is labeled as the real number.
- **Correct Sharpe annualization.** Periods-per-year is computed by asset class, not
  wall-clock. Crypto trades continuously; equities print bars only during the
  session. Getting this wrong inflates equity Sharpe by roughly 2.3×.
- **Total-return benchmarks.** Bars are split- and dividend-adjusted. A price-only
  benchmark understates buy-and-hold by its entire dividend yield.
- **Cost assumptions.** 0.05% slippage, $0 commission, stated explicitly rather than
  assumed away.
- **Automatic flagging.** Negative out-of-sample Sharpe or excessive drawdown is
  flagged in the output, computed on the out-of-sample half — not the full period,
  where a strong in-sample run can mask a losing one.

---

## What was tested

### Test 1 — Per-symbol strategies

Three years of daily bars, walk-forward split, on split- and dividend-adjusted
bars. Out-of-sample results:

| Symbol | Strategy | Trades | Return% | Sharpe | MaxDD% |
|---|---|---|---|---|---|
| SPY | trend_following | 4 | +2.4 | 0.65 | 2.5 |
| QQQ | trend_following | 7 | +1.7 | 0.33 | 3.5 |
| GLD | mean_reversion | 9 | −4.5 | −0.92 | 7.4 |
| USO | mean_reversion | 10 | +0.8 | 0.13 | 8.2 |
| BTC/USD | momentum_breakout | 6 | −3.9 | −0.85 | 5.4 |

Two strategies lose money out-of-sample with negative Sharpe. SPY and QQQ are
positive but thin, and both trail buy-and-hold over the same window. USO's 0.13
Sharpe across 10 trades is indistinguishable from noise.

*These figures were regenerated after the dividend-adjustment fix. An earlier
version of this document reported SPY at +2.2 / 0.60 Sharpe and QQQ at +1.6 /
0.31, measured on unadjusted closes. Only the dividend-paying equity ETFs moved:
GLD and USO hold no dividend-paying assets, and BTC/USD is crypto, which does not
route through the adjusted-bars request path. That the three unaffected symbols
are unchanged to the decimal is the check that the fix did what it claims. The
correction is small here — unlike the cross-sectional test, these strategies are
in-market only part of the time and frequently short, so they capture less of the
dividend stream than a continuously-held benchmark does. The conclusion is
unchanged.*

### Test 2 — Cross-sectional momentum

A structurally different approach: rank a 29-ETF universe spanning equities,
sectors, regions, commodities, and fixed income by momentum averaged over three
lookbacks; hold the top 5, equal-weighted, rebalanced monthly, with a 200-day
regime filter and an absolute-momentum gate. Dividend-adjusted, benchmarked
against buy-and-hold SPY.

Out-of-sample, with the regime filter on:

| Run | Return% | CAGR% | Sharpe | MaxDD% | Turnover |
|---|---|---|---|---|---|
| Cross-sectional top 5 | 38.3 | 17.6 | 0.96 | 16.7 | 15.6× |
| **Buy and hold SPY** | **48.5** | **21.8** | **1.29** | 18.7 | 1.0× |

With the regime filter disabled, to separate the ranking from the filter:

| Run | Return% | CAGR% | Sharpe | MaxDD% | Turnover |
|---|---|---|---|---|---|
| Cross-sectional top 5 | 45.0 | 20.4 | 1.07 | 16.7 | 17.0× |
| **Buy and hold SPY** | **48.5** | **21.8** | **1.29** | 18.7 | 1.0× |

The strategy loses to buy-and-hold in both configurations, on both return and
Sharpe. The regime filter — included specifically as downside protection — cost
about 7 points of return over this sample.

### Test 4 — Carver multi-speed continuous forecast

A continuous-forecast trend system in the style of Rob Carver's *Systematic
Trading*: several EWMA speed pairs, each scaled by its published forecast scalar
and capped at ±20, combined into one number that sizes the position by both
conviction and inverse volatility. The scalars are the published values, not
fitted to this sample. Five symbols (SPY, QQQ, IWM, EFA, TLT), swept across three
aggression multipliers, benchmarked against SPY at *matched leverage*.

Out-of-sample:

| Run | Return% | CAGR% | Sharpe | MaxDD% | Trades | Shorts |
|---|---|---|---|---|---|---|
| carver_trend @ 1.0x | +1.0 | 0.5 | 0.20 | 2.9 | 126 | 63 |
| carver_trend @ 1.5x | −0.1 | −0.0 | 0.02 | 6.2 | 125 | 64 |
| carver_trend @ 2.0x | −0.0 | −0.0 | 0.03 | 8.4 | 125 | 64 |
| **SPY @ 1.0x** | **46.4** | **22.1** | **1.31** | 18.7 | 0 | 0 |
| **SPY @ 1.5x** | **73.9** | **33.6** | **1.31** | 27.0 | 0 | 0 |
| **SPY @ 2.0x** | **104.0** | **45.3** | **1.31** | 34.7 | 0 | 0 |

In-sample the system is negative at every leverage: −4.9%, −6.5%, and −7.5%, at
Sharpe −0.80, −0.78, and −0.71. It works in neither half.

Three things need stating precisely, because the headline numbers overstate the
case against it:

**1. The return gap is partly a sizing artifact, so the Sharpe gap is the actual
finding.** At 1.0x the book runs roughly 23% gross — conviction averages 0.30,
multiplied by a `TARGET_ANNUAL_VOL / realised` term near 0.16 for SPY, giving
about 4.7% per position across five symbols. Comparing +1.0% against SPY's +46.4%
is comparing a 23%-exposure book to a 100%-exposure one, and the drawdowns confirm
it: 2.9% against 18.7%. A low-exposure book is not thereby a bad book. But Sharpe
is scale-invariant, and 0.20 against 1.31 does not depend on how large the
positions are. That is the comparison that holds.

**2. This is not a clean test of the method at its designed risk level.** The
framework's published scalars are meant to produce an average absolute forecast
near 10.0. Observed average forecast was ~3.0 — the system ran at roughly 30% of
its intended conviction before the volatility budget was applied. Either the 1.4
diversification multiplier is too low for these speed pairs, or the scalars do not
reproduce their published scale on this data. Correcting it would raise returns and
drawdown together and should leave Sharpe near where it is, but until it is
corrected, this result is evidence about *this implementation*, not about Carver's
method.

**3. Sharpe decays with leverage while the benchmark's stays flat.** SPY holds
exactly 1.31 at 1.0x, 1.5x, and 2.0x — the invariance that clean leverage should
produce, and a check that this harness measures what its column labels claim. The
strategy instead falls 0.20 → 0.02 → 0.03 under identical treatment. Since
leverage alone cannot move a Sharpe ratio, the decay is friction the strategy
generates itself: 126 trades with 63 shorts, paying slippage on churn that the
signal does not earn back.

## Why the numbers didn't support going further

**Neither approach beat doing nothing.** Buy-and-hold SPY returned 48.5% with a
Sharpe of 1.29 out-of-sample. The best strategy configuration returned 45.0% at
Sharpe 1.07, while turning over 17× the account's capital to get there. Every
modeled cost makes that comparison worse, and borrow costs and real fill quality
are *not* modeled.

**The encouraging early results were measurement error, not edge.** Three separate
instrumentation defects each flattered the results, and all three were found and
corrected:

1. *No walk-forward split.* The original harness reported a combined +10.5% measured
   on the same data the parameters were chosen against.
2. *Wall-clock Sharpe annualization.* Applying calendar time to 15-minute equity bars
   assumed ~35,000 periods per year against a true ~6,550, inflating equity Sharpe
   by roughly 2.3×. This is why early runs showed figures like 2.84 and 5.76.
3. *Price-only benchmarks.* On unadjusted closes the cross-sectional strategy
   appeared to beat SPY, 31.2% to 27.7%. On dividend-adjusted bars the same
   configuration *lost*, 38.3% to 48.5% — a swing of nearly 14 points, entirely
   attributable to how the benchmark was measured.

Each correction moved the results in the same direction: toward no edge. That
consistency is itself informative.

**The honest sample is small.** Alpaca's free data plan capped the history at
roughly six years (2020-07 to 2026-08) despite a ten-year request, giving 25
rebalances per half. A Sharpe computed over 25 portfolio decisions cannot be
distinguished from luck, and the two halves disagreed with each other in every test.

---

## What would need to be true before revisiting

Not "more tuning." Sweeping parameters until the in-sample numbers improve is the
failure mode this instrumentation was built to detect, and it would find it again.

1. **Longer history.** A paid data plan reaching 15+ years, covering at least two
   sustained drawdowns. The regime filter in particular cannot be fairly judged on a
   sample containing one bear market.
2. **An out-of-sample result that beats total-return buy-and-hold with margin to
   spare.** Not by 2–3 points — by enough to survive borrow costs, real fills, and
   the turnover the strategy actually generates.
3. **Costs modeled, not assumed.** At 17× turnover, spread and borrow are first-order
   terms, not footnotes.
4. **A structurally different source of return.** Every approach tested here is
   momentum in different clothing, and all of them failed in the same direction.
   Another momentum variant is not an independent test.
5. **The Carver forecast scale corrected.** An average absolute forecast near 10.0
   rather than the observed 3.0, so that implementation is being judged at the risk
   level it was designed for.

---

## Bottom line

The system works. Orders route, stops attach, risk limits hold, shorts execute,
and the accounting reconciles. What it does not have is a strategy with a
demonstrated edge, and the reason we know that is the validation harness, which
was built to be capable of returning bad news and did so every time it was asked —
including once against a result already committed to the repository, and once
against a benchmark comparison that had looked favourable until the benchmark was
measured correctly.

A trading system that cannot produce a negative result is not a trading system;
it is a spreadsheet that agrees with you. This one disagreed, and the disagreement
is the finding.

No capital should be allocated to these strategies. The infrastructure is worth
keeping: it is strategy-agnostic, and the next idea can be evaluated in days rather
than rebuilt from nothing.
