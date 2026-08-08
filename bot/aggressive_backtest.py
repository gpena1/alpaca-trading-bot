"""Walk-forward backtest for the three aggressive strategies.

Run: python3 -m bot.aggressive_backtest

Reports each strategy separately and combined, in-sample and
out-of-sample, across the aggression sweep, benchmarked against
buy-and-hold SPY on DIVIDEND-ADJUSTED bars.

Every guard the last two rounds proved necessary is in here:
  - walk-forward split, second half authoritative
  - correct annualisation (252, daily bars)
  - total-return benchmark, not price-only
  - shorts modelled, trailing ATR stops, time stops, kill switch
  - flags computed on the OUT-OF-SAMPLE half, not the full period

The aggression sweep is the point. Three strategies at three settings
gives a shape, not a single number: if Sharpe falls as aggression rises,
aggression is buying you nothing but variance, and the table will say so
more convincingly than any argument.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

import config_aggressive as cfg
from bot.strategies.aggressive_breakout import VolatilitySqueezeBreakout
from bot.strategies.aggressive_reversion import StretchReversion
from bot.strategies.aggressive_trend import AggressiveTrend
from bot.strategies.sizing import KillSwitch, Target, gross_cap, regime_is_risk_on

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bot.aggressive_backtest")

STRATEGY_CLASSES = [AggressiveTrend, VolatilitySqueezeBreakout, StretchReversion]


def _fill(price: float, is_buy: bool) -> float:
    return price * (1 + cfg.SLIPPAGE_PCT) if is_buy else price * (1 - cfg.SLIPPAGE_PCT)


def fetch_universe(broker, years: int = cfg.BACKTEST_YEARS) -> dict[str, pd.DataFrame]:
    from bot.broker import daily_timeframe

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(years * 365.25))
    bars: dict[str, pd.DataFrame] = {}
    for symbol in cfg.ALL_SYMBOLS:
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


def simulate(
    symbol_bars: dict[str, pd.DataFrame],
    strategies: list,
    index: pd.DatetimeIndex,
    starting_cash: float = cfg.STARTING_CASH,
    aggression: float = cfg.AGGRESSION,
) -> dict:
    """Portfolio simulation across all strategies and their symbols.

    Positions are signed. Each carries a trailing stop and, for the
    reversion strategy, a bar counter for the time stop.
    """
    cash = starting_cash
    positions: dict[tuple, dict] = {}  # (strategy, symbol) -> state
    equity_curve: list[tuple] = []
    kill_switch = KillSwitch()
    trades = 0
    shorts = 0
    stopped_out = 0
    time_stopped = 0

    min_bars = max(s.min_bars for s in strategies)
    spy = symbol_bars.get(cfg.REGIME_SYMBOL, pd.DataFrame())

    for i in range(min_bars, len(index)):
        ts = index[i]

        def price_of(sym: str) -> float | None:
            df = symbol_bars.get(sym)
            if df is None or ts not in df.index:
                return None
            value = float(df.loc[ts, "close"])
            return value if value > 0 else None

        def mark() -> float:
            total = cash
            for (_, sym), state in positions.items():
                p = price_of(sym)
                if p:
                    total += state["qty"] * p
            return total

        equity = mark()
        trading_allowed = kill_switch.update(equity)
        spy_window = spy["close"].iloc[: i + 1] if not spy.empty else None
        risk_on = regime_is_risk_on(spy_window)

        for strategy in strategies:
            for symbol in strategy.symbols:
                if symbol not in symbol_bars:
                    continue
                df = symbol_bars[symbol]
                window = df.loc[:ts]
                if len(window) < strategy.min_bars:
                    continue
                price = price_of(symbol)
                if price is None:
                    continue

                key = (strategy.name, symbol)
                state = positions.get(key)

                # --- exits that override the signal ---------------------
                if state:
                    state["bars_held"] += 1
                    is_long = state["qty"] > 0
                    move = (price - state["entry"]) / state["entry"]
                    if not is_long:
                        move = -move

                    forced = None
                    if state["stop"] is not None:
                        if is_long and price <= state["stop"]:
                            forced = "trailing stop"
                        elif not is_long and price >= state["stop"]:
                            forced = "trailing stop"
                    if forced is None and strategy.name == "stretch_reversion":
                        if state["bars_held"] >= cfg.REVERSION_TIME_STOP_BARS:
                            forced = "time stop"
                        elif move >= cfg.REVERSION_TAKE_PROFIT:
                            forced = "take profit"

                    if forced or not trading_allowed:
                        fill = _fill(price, is_buy=state["qty"] < 0)
                        cash += state["qty"] * fill
                        positions.pop(key)
                        if forced == "trailing stop":
                            stopped_out += 1
                        elif forced == "time stop":
                            time_stopped += 1
                        continue

                    # ratchet the trailing stop in the favourable direction
                    if state["stop_distance"]:
                        if is_long:
                            state["stop"] = max(state["stop"], price - state["stop_distance"])
                        else:
                            state["stop"] = min(state["stop"], price + state["stop_distance"])

                if not trading_allowed or not risk_on:
                    continue

                signal = strategy.generate(window)
                if signal.target == Target.HOLD:
                    continue

                current = (
                    Target.LONG
                    if state and state["qty"] > 0
                    else Target.SHORT
                    if state and state["qty"] < 0
                    else Target.FLAT
                )
                if signal.target == current:
                    continue

                if state:
                    fill = _fill(price, is_buy=state["qty"] < 0)
                    cash += state["qty"] * fill
                    positions.pop(key)

                if signal.target == Target.FLAT:
                    continue

                gross = sum(abs(s["qty"]) * (price_of(sym) or 0) for (_, sym), s in positions.items())
                room = gross_cap(aggression) - (gross / equity if equity > 0 else 0)
                if room <= 0:
                    continue

                weight = min(signal.weight, room)
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

                positions[key] = {
                    "qty": qty,
                    "entry": fill,
                    "stop": stop,
                    "stop_distance": signal.stop_distance,
                    "bars_held": 0,
                }
                trades += 1
                if not is_long:
                    shorts += 1

        equity_curve.append((ts, mark()))

    return {
        "equity_curve": equity_curve,
        "final_equity": equity_curve[-1][1] if equity_curve else starting_cash,
        "trades": trades,
        "shorts": shorts,
        "stopped_out": stopped_out,
        "time_stopped": time_stopped,
        "kill_switch_trips": kill_switch.trips,
    }


def metrics(result: dict, starting_cash: float, label: str) -> dict:
    equities = [e for _, e in result["equity_curve"]] or [starting_cash]
    peak, max_dd = equities[0], 0.0
    for e in equities:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)

    returns = pd.Series(equities).pct_change().dropna()
    sharpe = (
        (returns.mean() / returns.std()) * (cfg.TRADING_DAYS_PER_YEAR**0.5)
        if len(returns) > 1 and returns.std() > 0
        else 0.0
    )
    years = max(len(equities) / cfg.TRADING_DAYS_PER_YEAR, 1e-9)
    total = result["final_equity"] / starting_cash
    cagr = (total ** (1 / years) - 1) * 100 if total > 0 else -100.0

    return {
        "label": label,
        "total_return_pct": (total - 1) * 100,
        "cagr_pct": cagr,
        "sharpe": sharpe,
        "max_drawdown_pct": max_dd * 100,
        "trades": result.get("trades", 0),
        "shorts": result.get("shorts", 0),
        "kills": result.get("kill_switch_trips", 0),
    }


def buy_and_hold(symbol_bars: dict, index: pd.DatetimeIndex, start_i: int, cash: float) -> dict:
    df = symbol_bars.get("SPY")
    if df is None:
        return {"equity_curve": [], "final_equity": cash}
    series = df["close"].reindex(index).dropna().iloc[start_i:]
    if series.empty:
        return {"equity_curve": [], "final_equity": cash}
    qty = cash / float(series.iloc[0])
    curve = [(ts, qty * float(p)) for ts, p in series.items()]
    return {"equity_curve": curve, "final_equity": curve[-1][1]}


def print_table(rows: list[dict], title: str):
    headers = ["Run", "Return%", "CAGR%", "Sharpe", "MaxDD%", "Trades", "Shorts", "Kills"]
    widths = [34, 9, 8, 8, 8, 7, 7, 6]

    def fmt(cells):
        return " | ".join(str(c).rjust(w) for c, w in zip(cells, widths))

    print("\n" + "=" * 104)
    print(title.center(104))
    print("=" * 104)
    print(fmt(headers))
    print("-" * 104)
    for r in rows:
        print(
            fmt([
                r["label"][:34],
                f"{r['total_return_pct']:.1f}",
                f"{r['cagr_pct']:.1f}",
                f"{r['sharpe']:.2f}",
                f"{r['max_drawdown_pct']:.1f}",
                r["trades"],
                r["shorts"],
                r["kills"],
            ])
        )
    print("=" * 104)


def run():
    from bot.broker import Broker

    broker = Broker()
    print(f"Fetching up to {cfg.BACKTEST_YEARS}y of dividend-adjusted daily bars "
          f"for {len(cfg.ALL_SYMBOLS)} symbols...")
    symbol_bars = fetch_universe(broker)
    if len(symbol_bars) < 8:
        print(f"\nOnly {len(symbol_bars)} symbols usable. Stopping.")
        return

    index = sorted(set().union(*[set(df.index) for df in symbol_bars.values()]))
    index = pd.DatetimeIndex(index)
    print(f"\nAligned: {len(symbol_bars)} symbols, {len(index)} bars, "
          f"{index[0].date()} to {index[-1].date()}")

    split = len(index) // 2
    halves = [
        ("FIRST HALF (in-sample -- do not trust)", index[:split]),
        ("SECOND HALF (out-of-sample -- the real number)", index[split:]),
    ]

    flagged = []
    for title, idx in halves:
        rows = []
        for aggression in cfg.AGGRESSION_SWEEP:
            strategies = [cls(aggression=aggression) for cls in STRATEGY_CLASSES]
            min_bars = max(s.min_bars for s in strategies)
            if len(idx) <= min_bars + 20:
                print(f"\n{title}: only {len(idx)} bars, need > {min_bars + 20}.")
                break

            for strategy in strategies:
                result = simulate(symbol_bars, [strategy], idx, aggression=aggression)
                rows.append(metrics(result, cfg.STARTING_CASH,
                                    f"{strategy.name} @ {aggression:.1f}x"))

            combined = simulate(symbol_bars, strategies, idx, aggression=aggression)
            m = metrics(combined, cfg.STARTING_CASH, f"ALL THREE @ {aggression:.1f}x")
            rows.append(m)
            if "SECOND" in title:
                bench = metrics(buy_and_hold(symbol_bars, idx, min_bars, cfg.STARTING_CASH),
                                cfg.STARTING_CASH, "Buy and hold SPY")
                if m["sharpe"] < bench["sharpe"]:
                    flagged.append(
                        f"ALL THREE @ {aggression:.1f}x: Sharpe {m['sharpe']:.2f} "
                        f"below SPY's {bench['sharpe']:.2f}"
                    )

        if rows and "SECOND" in title:
            min_bars = max(cls().min_bars for cls in STRATEGY_CLASSES)
            rows.append(metrics(buy_and_hold(symbol_bars, idx, min_bars, cfg.STARTING_CASH),
                                cfg.STARTING_CASH, "Buy and hold SPY"))
        if rows:
            print_table(rows, title)

    if flagged:
        print("\nFLAGGED (out-of-sample):")
        for f in flagged:
            print(f"  - {f}")
    else:
        print("\nNothing flagged out-of-sample.")

    print("\nRead the second table only. Compare each ALL THREE row against the SPY row.")
    print("If Sharpe falls as aggression rises, aggression is buying variance, not return.")
    print("Excludes borrow costs on shorts and real fill quality at these turnover levels.")


if __name__ == "__main__":
    run()
