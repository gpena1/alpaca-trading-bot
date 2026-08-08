"""Walk-forward backtest for the cross-sectional momentum strategy.

Run: python3 -m bot.xs_backtest

Reports in-sample and out-of-sample halves separately, and benchmarks
against buy-and-hold SPY. That benchmark is the point: the existing trend
sleeve returns roughly 2% out-of-sample, which is worse than holding SPY
and doing nothing. Any new strategy that can't beat the benchmark isn't
worth running, however good its Sharpe looks in isolation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

import config_xs
from bot.strategies.cross_sectional import CrossSectionalMomentum

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bot.xs_backtest")


def _fill(price: float, is_buy: bool) -> float:
    return price * (1 + config_xs.XS_SLIPPAGE_PCT) if is_buy else price * (1 - config_xs.XS_SLIPPAGE_PCT)


def fetch_universe(broker, years: int = config_xs.XS_BACKTEST_YEARS) -> dict[str, pd.DataFrame]:
    # Imported here rather than at module scope so the simulation and
    # metrics functions can be imported and tested without Alpaca
    # credentials or a live connection.
    from bot.broker import daily_timeframe

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(years * 365.25))
    bars: dict[str, pd.DataFrame] = {}
    for symbol in config_xs.UNIVERSE:
        try:
            df = broker.get_bars_range(symbol, False, daily_timeframe(), start, end)
        except Exception:
            logger.exception("%s: fetch failed, excluding from universe", symbol)
            continue
        if df.empty or len(df) < 300:
            print(f"  {symbol}: only {len(df)} bars -- excluded")
            continue
        bars[symbol] = df
        print(f"  {symbol}: {len(df)} bars, {df.index[0].date()} to {df.index[-1].date()}")
    return bars


def align(symbol_bars: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """One DataFrame of closes, one column per symbol, on a shared index."""
    frame = pd.DataFrame({s: b["close"] for s, b in symbol_bars.items()})
    return frame.sort_index().dropna(how="all")


def simulate(
    closes: pd.DataFrame,
    strategy: CrossSectionalMomentum,
    starting_cash: float = config_xs.XS_STARTING_CASH,
    rebalance_every: int = config_xs.REBALANCE_EVERY_N_BARS,
) -> dict:
    cash = starting_cash
    holdings: dict[str, float] = {}
    equity_curve: list[tuple] = []
    rebalances = 0
    turnover_notional = 0.0
    bars_flat = 0

    start_idx = strategy.min_bars
    if len(closes) <= start_idx + 5:
        return {"equity_curve": [], "final_equity": starting_cash, "rebalances": 0,
                "turnover": 0.0, "pct_bars_flat": 0.0}

    for i in range(start_idx, len(closes)):
        ts = closes.index[i]
        row = closes.iloc[i]

        def mark() -> float:
            return cash + sum(q * float(row[s]) for s, q in holdings.items() if pd.notna(row[s]))

        if (i - start_idx) % rebalance_every == 0:
            window = {s: closes[[s]].iloc[: i + 1].rename(columns={s: "close"}) for s in closes.columns}
            window = {s: df.dropna() for s, df in window.items()}
            weights, _ = strategy.target_weights(window)

            equity = mark()
            desired = {}
            for s, w in weights.items():
                price = float(row[s]) if pd.notna(row[s]) else None
                if price and price > 0:
                    desired[s] = (equity * w) / price

            for s in list(holdings.keys()) + list(desired.keys()):
                price = float(row[s]) if s in row and pd.notna(row[s]) else None
                if not price or price <= 0:
                    continue
                current = holdings.get(s, 0.0)
                target = desired.get(s, 0.0)
                delta = target - current
                if abs(delta * price) < 1.0:
                    continue
                fill = _fill(price, is_buy=delta > 0)
                cash -= delta * fill
                turnover_notional += abs(delta * fill)
                new_qty = current + delta
                if abs(new_qty) < 1e-9:
                    holdings.pop(s, None)
                else:
                    holdings[s] = new_qty
            rebalances += 1

        if not holdings:
            bars_flat += 1
        equity_curve.append((ts, mark()))

    total_bars = len(closes) - start_idx
    return {
        "equity_curve": equity_curve,
        "final_equity": equity_curve[-1][1] if equity_curve else starting_cash,
        "rebalances": rebalances,
        "turnover": turnover_notional / starting_cash,
        "pct_bars_flat": (bars_flat / total_bars * 100) if total_bars else 0.0,
    }


def metrics(result: dict, starting_cash: float, label: str) -> dict:
    equities = [e for _, e in result["equity_curve"]] or [starting_cash]
    peak, max_dd = equities[0], 0.0
    for e in equities:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)

    returns = pd.Series(equities).pct_change().dropna()
    if len(returns) > 1 and returns.std() > 0:
        sharpe = (returns.mean() / returns.std()) * (config_xs.TRADING_DAYS_PER_YEAR**0.5)
    else:
        sharpe = 0.0

    years = max(len(equities) / config_xs.TRADING_DAYS_PER_YEAR, 1e-9)
    total_return = result["final_equity"] / starting_cash
    cagr = (total_return ** (1 / years) - 1) * 100 if total_return > 0 else -100.0

    return {
        "label": label,
        "total_return_pct": (total_return - 1) * 100,
        "cagr_pct": cagr,
        "sharpe": sharpe,
        "max_drawdown_pct": max_dd * 100,
        "rebalances": result.get("rebalances", 0),
        "turnover_x": result.get("turnover", 0.0),
        "pct_flat": result.get("pct_bars_flat", 0.0),
    }


def buy_and_hold(closes: pd.DataFrame, symbol: str, starting_cash: float, start_idx: int) -> dict:
    if symbol not in closes.columns:
        return {"equity_curve": [], "final_equity": starting_cash}
    series = closes[symbol].iloc[start_idx:].dropna()
    if series.empty:
        return {"equity_curve": [], "final_equity": starting_cash}
    qty = starting_cash / float(series.iloc[0])
    curve = [(ts, qty * float(p)) for ts, p in series.items()]
    return {"equity_curve": curve, "final_equity": curve[-1][1], "rebalances": 0,
            "turnover": 1.0, "pct_bars_flat": 0.0}


def print_table(rows: list[dict], title: str):
    headers = ["Run", "Return%", "CAGR%", "Sharpe", "MaxDD%", "Rebals", "Turnover", "Flat%"]
    widths = [30, 9, 8, 8, 8, 7, 9, 7]

    def fmt(cells):
        return " | ".join(str(c).rjust(w) for c, w in zip(cells, widths))

    print("\n" + "=" * 100)
    print(title.center(100))
    print("=" * 100)
    print(fmt(headers))
    print("-" * 100)
    for r in rows:
        print(
            fmt([
                r["label"][:30],
                f"{r['total_return_pct']:.1f}",
                f"{r['cagr_pct']:.1f}",
                f"{r['sharpe']:.2f}",
                f"{r['max_drawdown_pct']:.1f}",
                r["rebalances"],
                f"{r['turnover_x']:.1f}x",
                f"{r['pct_flat']:.0f}",
            ])
        )
    print("=" * 100)


def run():
    from bot.broker import Broker

    broker = Broker()
    strategy = CrossSectionalMomentum()

    print(f"Fetching {config_xs.XS_BACKTEST_YEARS}y of daily bars for "
          f"{len(config_xs.UNIVERSE)} symbols...")
    symbol_bars = fetch_universe(broker)
    if len(symbol_bars) < 10:
        print(f"\nOnly {len(symbol_bars)} symbols usable -- ranking needs a wide universe. Stopping.")
        return

    closes = align(symbol_bars)
    print(f"\nAligned universe: {len(closes.columns)} symbols, {len(closes)} bars, "
          f"{closes.index[0].date()} to {closes.index[-1].date()}")

    cash = config_xs.XS_STARTING_CASH
    split = len(closes) // 2
    halves = [
        ("FIRST HALF (in-sample -- do not trust)", closes.iloc[:split]),
        ("SECOND HALF (out-of-sample -- the real number)", closes.iloc[split:]),
    ]

    for title, half in halves:
        if len(half) <= strategy.min_bars + 5:
            print(f"\n{title}: only {len(half)} bars, need > {strategy.min_bars + 5}. "
                  "Increase XS_BACKTEST_YEARS.")
            continue
        strat_result = simulate(half, strategy)
        bench_result = buy_and_hold(half, "SPY", cash, strategy.min_bars)
        rows = [
            metrics(strat_result, cash, f"Cross-sectional top {strategy.top_n}"),
            metrics(bench_result, cash, "Buy and hold SPY"),
        ]
        print_table(rows, title)

    print("\nRead the second table only. If the strategy row doesn't clearly beat "
          "the SPY row there, the strategy isn't worth running.")
    print("Excludes dividends, borrow costs, and real fill quality. Discount accordingly.")


if __name__ == "__main__":
    run()
