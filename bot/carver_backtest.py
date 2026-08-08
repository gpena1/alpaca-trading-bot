"""Walk-forward backtest for the Carver multi-speed trend system.

Run: python3 -m bot.carver_backtest

Two things this fixes relative to bot/aggressive_backtest.py:

1. CASH-DRAG CORRECTION. In the previous harness, gross exposure scaled
   with AGGRESSION, so the 1.0x run sat well under its own cap holding
   idle cash. That made Sharpe rise with leverage -- which pure leverage
   cannot do -- meaning the leverage axis was measuring exposure, not
   leverage, and rows weren't comparable to each other. Here the gross
   cap is FIXED and only position sizing scales, so the sweep is a clean
   leverage comparison.

2. BENCHMARK PARITY. SPY buy-and-hold is also reported at matching
   leverage, so an aggressive strategy is compared against an equally
   aggressive passive alternative rather than only against unlevered SPY.
   If a 2x strategy can't beat 2x SPY, leverage isn't the missing piece.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

import config_aggressive as cfg
from bot.strategies.carver_trend import CarverMultiSpeedTrend
from bot.strategies.sizing import KillSwitch, Target

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bot.carver_backtest")

FIXED_GROSS_CAP = 3.0  # fixed, so the sweep measures leverage cleanly


def _fill(price: float, is_buy: bool) -> float:
    return price * (1 + cfg.SLIPPAGE_PCT) if is_buy else price * (1 - cfg.SLIPPAGE_PCT)


def fetch(broker, symbols: list[str], years: int = cfg.BACKTEST_YEARS) -> dict[str, pd.DataFrame]:
    from bot.broker import daily_timeframe

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(years * 365.25))
    bars = {}
    for symbol in symbols:
        try:
            df = broker.get_bars_range(symbol, False, daily_timeframe(), start, end)
        except Exception:
            logger.exception("%s: fetch failed", symbol)
            continue
        if df.empty or len(df) < 400:
            print(f"  {symbol}: only {len(df)} bars -- excluded")
            continue
        bars[symbol] = df
        print(f"  {symbol}: {len(df)} bars, {df.index[0].date()} to {df.index[-1].date()}")
    return bars


def simulate(symbol_bars: dict, index: pd.DatetimeIndex, aggression: float) -> dict:
    strategy = CarverMultiSpeedTrend(aggression=aggression)
    cash = cfg.STARTING_CASH
    positions: dict[str, dict] = {}
    curve = []
    kill = KillSwitch()
    trades = shorts = stopped = 0
    forecast_sum = forecast_n = 0.0

    for i in range(strategy.min_bars, len(index)):
        ts = index[i]

        def price_of(sym):
            df = symbol_bars.get(sym)
            if df is None or ts not in df.index:
                return None
            p = float(df.loc[ts, "close"])
            return p if p > 0 else None

        def mark():
            total = cash
            for sym, st in positions.items():
                p = price_of(sym)
                if p:
                    total += st["qty"] * p
            return total

        equity = mark()
        allowed = kill.update(equity)

        for symbol in strategy.symbols:
            if symbol not in symbol_bars:
                continue
            window = symbol_bars[symbol].loc[:ts]
            if len(window) < strategy.min_bars:
                continue
            price = price_of(symbol)
            if price is None:
                continue

            state = positions.get(symbol)

            if state:
                is_long = state["qty"] > 0
                hit = state["stop"] is not None and (
                    (is_long and price <= state["stop"]) or (not is_long and price >= state["stop"])
                )
                if hit or not allowed:
                    cash += state["qty"] * _fill(price, is_buy=state["qty"] < 0)
                    positions.pop(symbol)
                    if hit:
                        stopped += 1
                    continue
                if state["stop_distance"]:
                    state["stop"] = (
                        max(state["stop"], price - state["stop_distance"])
                        if is_long
                        else min(state["stop"], price + state["stop_distance"])
                    )

            if not allowed:
                continue

            signal = strategy.generate(window)
            if signal.target == Target.HOLD:
                continue

            current = (
                Target.LONG if state and state["qty"] > 0
                else Target.SHORT if state and state["qty"] < 0
                else Target.FLAT
            )
            if signal.target == current and signal.target != Target.FLAT:
                continue

            if state:
                cash += state["qty"] * _fill(price, is_buy=state["qty"] < 0)
                positions.pop(symbol)

            if signal.target == Target.FLAT:
                continue

            gross = sum(abs(s["qty"]) * (price_of(sy) or 0) for sy, s in positions.items())
            room = FIXED_GROSS_CAP - (gross / equity if equity > 0 else 0)
            weight = min(signal.weight, max(room, 0.0))
            if weight <= 0:
                continue

            is_long = signal.target == Target.LONG
            fill = _fill(price, is_buy=is_long)
            qty = (equity * weight) / fill
            if not is_long:
                qty = -qty
            cash -= qty * fill

            stop = None
            if signal.stop_distance:
                stop = fill - signal.stop_distance if is_long else fill + signal.stop_distance
            positions[symbol] = {"qty": qty, "stop": stop, "stop_distance": signal.stop_distance}
            trades += 1
            if not is_long:
                shorts += 1

            f, _ = strategy.combined_forecast(window["close"])
            if f is not None:
                forecast_sum += abs(f)
                forecast_n += 1

        curve.append((ts, mark()))

    return {
        "equity_curve": curve,
        "final_equity": curve[-1][1] if curve else cfg.STARTING_CASH,
        "trades": trades,
        "shorts": shorts,
        "stopped": stopped,
        "kills": kill.trips,
        "avg_forecast": (forecast_sum / forecast_n) if forecast_n else 0.0,
    }


def levered_benchmark(symbol_bars, index, start_i, leverage: float) -> dict:
    """SPY held at `leverage`x, marked daily. Not a perfect model of a
    levered ETF (no financing cost, no daily-reset decay) so it FLATTERS
    the benchmark's competitor -- i.e. it is a conservative comparison
    for the strategy, which is the right direction to err."""
    df = symbol_bars.get("SPY")
    if df is None:
        return {"equity_curve": [], "final_equity": cfg.STARTING_CASH}
    series = df["close"].reindex(index).dropna().iloc[start_i:]
    if series.empty:
        return {"equity_curve": [], "final_equity": cfg.STARTING_CASH}
    returns = series.pct_change().fillna(0.0) * leverage
    equity = cfg.STARTING_CASH * (1 + returns).cumprod()
    return {"equity_curve": list(zip(series.index, equity)), "final_equity": float(equity.iloc[-1])}


def metrics(result, label):
    equities = [e for _, e in result["equity_curve"]] or [cfg.STARTING_CASH]
    peak, max_dd = equities[0], 0.0
    for e in equities:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)
    returns = pd.Series(equities).pct_change().dropna()
    sharpe = (
        (returns.mean() / returns.std()) * (cfg.TRADING_DAYS_PER_YEAR**0.5)
        if len(returns) > 1 and returns.std() > 0 else 0.0
    )
    years = max(len(equities) / cfg.TRADING_DAYS_PER_YEAR, 1e-9)
    total = result["final_equity"] / cfg.STARTING_CASH
    return {
        "label": label,
        "total_return_pct": (total - 1) * 100,
        "cagr_pct": (total ** (1 / years) - 1) * 100 if total > 0 else -100.0,
        "sharpe": sharpe,
        "max_drawdown_pct": max_dd * 100,
        "trades": result.get("trades", 0),
        "shorts": result.get("shorts", 0),
        "kills": result.get("kills", 0),
        "avg_forecast": result.get("avg_forecast", 0.0),
    }


def print_table(rows, title):
    headers = ["Run", "Return%", "CAGR%", "Sharpe", "MaxDD%", "Trades", "Shorts", "Kills", "AvgFcst"]
    widths = [30, 9, 8, 8, 8, 7, 7, 6, 8]

    def fmt(cells):
        return " | ".join(str(c).rjust(w) for c, w in zip(cells, widths))

    print("\n" + "=" * 108)
    print(title.center(108))
    print("=" * 108)
    print(fmt(headers))
    print("-" * 108)
    for r in rows:
        print(fmt([
            r["label"][:30], f"{r['total_return_pct']:.1f}", f"{r['cagr_pct']:.1f}",
            f"{r['sharpe']:.2f}", f"{r['max_drawdown_pct']:.1f}", r["trades"],
            r["shorts"], r["kills"],
            f"{r['avg_forecast']:.1f}" if r["avg_forecast"] else "-",
        ]))
    print("=" * 108)


def run():
    from bot.broker import Broker

    broker = Broker()
    symbols = sorted(set(cfg.TREND_SYMBOLS + ["SPY"]))
    print(f"Fetching up to {cfg.BACKTEST_YEARS}y of dividend-adjusted daily bars...")
    symbol_bars = fetch(broker, symbols)
    if len(symbol_bars) < 3:
        print("Not enough symbols. Stopping.")
        return

    index = pd.DatetimeIndex(sorted(set().union(*[set(d.index) for d in symbol_bars.values()])))
    print(f"\nAligned: {len(symbol_bars)} symbols, {len(index)} bars, "
          f"{index[0].date()} to {index[-1].date()}")

    split = len(index) // 2
    min_bars = CarverMultiSpeedTrend().min_bars

    for title, idx in [
        ("FIRST HALF (in-sample -- do not trust)", index[:split]),
        ("SECOND HALF (out-of-sample -- the real number)", index[split:]),
    ]:
        if len(idx) <= min_bars + 20:
            print(f"\n{title}: only {len(idx)} bars, need > {min_bars + 20}. "
                  "The 128-day slow EWMA makes this strategy data-hungry.")
            continue
        rows = []
        for aggression in cfg.AGGRESSION_SWEEP:
            rows.append(metrics(simulate(symbol_bars, idx, aggression),
                                f"carver_trend @ {aggression:.1f}x"))
        for leverage in cfg.AGGRESSION_SWEEP:
            rows.append(metrics(levered_benchmark(symbol_bars, idx, min_bars, leverage),
                                f"SPY @ {leverage:.1f}x"))
        print_table(rows, title)

    print("\nCompare each carver_trend row against the SPY row at the SAME leverage.")
    print("Gross cap is FIXED here, so this sweep is clean leverage -- unlike the")
    print("previous harness, where 1.0x carried idle cash and Sharpe rose with size.")
    print("Excludes borrow costs on shorts and financing on the levered benchmark.")


if __name__ == "__main__":
    run()
