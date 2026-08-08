"""Backtest every live strategy against historical data via run_backtest().

Walks bars forward with no lookahead and reuses the exact live decision
logic (bot.main.build_strategies()), so the backtest can't silently drift
from what's deployed. Applies 0.05% slippage and $0 commission.

THREE CORRECTIONS over the previous version:

1. Sharpe annualisation was wrong for every equity symbol. The old code
   used periods_per_year = (365.25 * 24 * 3600) / bar_seconds, i.e. it
   assumed bars exist around the clock. That's true for crypto and false
   for equities, which only print during the ~6.5h session. With 15-minute
   bars, _bar_seconds() returns 900 (the median gap is intraday; overnight
   and weekend gaps are ignored), so the annualisation factor came out
   35,064 instead of ~6,550 -- inflating every equity Sharpe by
   sqrt(35064/6550) = 2.3x. That is the entire explanation for the
   USO "Sharpe 7.17" result. Now computed per symbol by asset class.

2. Shorts are simulated. The old sim could only be long or flat, so it
   could not evaluate the strategies as they now run.

3. Walk-forward split. In-sample optimisation is how the RSI 20/80 and
   breakout 80/150 parameters were chosen -- swept over the same window
   they were then scored on. The split reports the second half untouched,
   which is the only number worth acting on.

Run: python3 -m bot.backtest
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

import config
from bot.broker import Broker, daily_timeframe, is_crypto
from bot.main import build_strategies
from bot.strategies.base import TargetPosition

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bot.backtest")

SLIPPAGE_PCT = 0.0005
STARTING_CASH = 100_000.0
BACKTEST_YEARS = 3  # daily bars need years, not months, for a usable sample
DRAWDOWN_FLAG_PCT = 15.0
OUTPUT_CHART = "backtest_results.png"

TRADING_DAYS_PER_YEAR = 252
CRYPTO_DAYS_PER_YEAR = 365.25
EQUITY_SESSION_SECONDS = 6.5 * 3600


@dataclass
class Trade:
    symbol: str
    direction: str  # "long" or "short"
    entry_time: object
    entry_price: float
    exit_time: object
    exit_price: float
    qty: float
    pnl: float


@dataclass
class SimResult:
    label: str
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)
    final_equity: float = 0.0


def _fill_price(price: float, is_buy: bool) -> float:
    return price * (1 + SLIPPAGE_PCT) if is_buy else price * (1 - SLIPPAGE_PCT)


def _bar_seconds(bars: pd.DataFrame) -> float:
    diffs = bars.index.to_series().diff().dropna()
    return diffs.median().total_seconds() if len(diffs) else 86400.0


def periods_per_year(symbol: str, bars: pd.DataFrame) -> float:
    """Bars per year, by asset class -- NOT by wall-clock time.

    Crypto trades continuously, so calendar time is the right divisor.
    Equities print bars only during the session, so a 15-minute equity bar
    is ~26/day x 252 days, not 96/day x 365.
    """
    secs = _bar_seconds(bars)
    if is_crypto(symbol):
        return (CRYPTO_DAYS_PER_YEAR * 24 * 3600) / secs
    if secs >= 0.9 * 86400:  # daily or coarser
        return TRADING_DAYS_PER_YEAR
    return TRADING_DAYS_PER_YEAR * (EQUITY_SESSION_SECONDS / secs)


def fetch_backtest_bars(broker: Broker, symbol: str, years: int = BACKTEST_YEARS) -> pd.DataFrame:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(years * 365.25))
    return broker.get_bars_range(symbol, is_crypto(symbol), daily_timeframe(), start, end)


def simulate_symbol(
    symbol: str,
    bars: pd.DataFrame,
    strategy,
    starting_cash: float = STARTING_CASH,
    allocation_pct: float = config.MAX_ALLOCATION_PCT_PER_SYMBOL,
    stop_loss_pct: float = config.STOP_LOSS_PCT,
    label: str | None = None,
) -> SimResult:
    """Single-symbol long/short backtest.

    Sizing is off EQUITY, not remaining cash -- the live RiskManager sizes
    from account equity, and the old version's `cash * allocation_pct`
    quietly shrank position sizes after every loss, which is not what the
    deployed bot does.
    """
    take_profit_pct = config.TAKE_PROFIT_PCT_BY_STRATEGY.get(strategy.name)
    cash = starting_cash
    qty = 0.0  # signed
    entry_price = 0.0
    entry_time = None
    trades: list[Trade] = []
    equity_curve: list[tuple] = []

    def equity_at(price: float) -> float:
        return cash + qty * price

    def close(price: float, ts):
        nonlocal cash, qty, entry_price, entry_time
        fill = _fill_price(price, is_buy=qty < 0)
        pnl = (fill - entry_price) * qty
        cash += qty * fill
        trades.append(
            Trade(symbol, "long" if qty > 0 else "short", entry_time, entry_price, ts, fill, abs(qty), pnl)
        )
        qty, entry_price, entry_time = 0.0, 0.0, None

    for i in range(1, len(bars)):
        window = bars.iloc[: i + 1]
        ts = window.index[-1]
        price = float(window["close"].iloc[-1])

        if qty != 0 and entry_price > 0:
            change_pct = (price - entry_price) / entry_price
            if qty < 0:
                change_pct = -change_pct
            hit_stop = change_pct <= -stop_loss_pct
            hit_target = take_profit_pct is not None and change_pct >= take_profit_pct
            if hit_stop or hit_target:
                close(price, ts)
                equity_curve.append((ts, equity_at(price)))
                continue

        signal = strategy.generate_signal(window)
        target = signal.target
        current = (
            TargetPosition.LONG if qty > 0 else TargetPosition.SHORT if qty < 0 else TargetPosition.FLAT
        )

        if target != TargetPosition.HOLD and target != current:
            if qty != 0:
                close(price, ts)
            if target in (TargetPosition.LONG, TargetPosition.SHORT):
                equity = cash
                target_dollars = equity * allocation_pct
                fill = _fill_price(price, is_buy=target == TargetPosition.LONG)
                new_qty = target_dollars / fill
                if target == TargetPosition.SHORT:
                    new_qty = -new_qty
                if new_qty != 0:
                    cash -= new_qty * fill
                    qty, entry_price, entry_time = new_qty, fill, ts

        equity_curve.append((ts, equity_at(price)))

    final_price = float(bars["close"].iloc[-1]) if len(bars) else 0.0
    return SimResult(label or symbol, trades, equity_curve, cash + qty * final_price)


def compute_metrics(result: SimResult, starting_cash: float, bars_per_year: float) -> dict:
    trades = result.trades
    total = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]

    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = float("inf") if gross_win > 0 else 0.0

    equities = [e for _, e in result.equity_curve] or [starting_cash]
    peak, max_dd = equities[0], 0.0
    for e in equities:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)

    returns = pd.Series(equities).pct_change().dropna()
    if len(returns) > 1 and returns.std() > 0:
        sharpe = (returns.mean() / returns.std()) * (bars_per_year**0.5)
    else:
        sharpe = 0.0

    return {
        "label": result.label,
        "total_trades": total,
        "shorts": sum(1 for t in trades if t.direction == "short"),
        "win_rate": (len(wins) / total * 100) if total else None,
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_dd * 100,
        "sharpe_ratio": sharpe,
        "total_return_pct": (result.final_equity - starting_cash) / starting_cash * 100,
        "final_equity": result.final_equity,
    }


def print_summary_table(metrics_list: list[dict], title: str):
    headers = ["Symbol", "Trades", "Short", "Win%", "ProfFac", "MaxDD%", "Sharpe", "Return%"]
    widths = [26, 7, 6, 7, 8, 8, 8, 9]

    def fmt_row(cells):
        return " | ".join(str(c).rjust(w) for c, w in zip(cells, widths))

    print("\n" + "=" * 92)
    print(title.center(92))
    print("=" * 92)
    print(fmt_row(headers))
    print("-" * 92)
    for m in metrics_list:
        win = f"{m['win_rate']:.0f}" if m["win_rate"] is not None else "-"
        pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
        print(
            fmt_row(
                [
                    m["label"][:26],
                    m["total_trades"],
                    m["shorts"],
                    win,
                    pf,
                    f"{m['max_drawdown_pct']:.1f}",
                    f"{m['sharpe_ratio']:.2f}",
                    f"{m['total_return_pct']:.1f}",
                ]
            )
        )
    print("=" * 92)


def plot_equity_curves(results: list[SimResult], out_path: str = OUTPUT_CHART):
    fig, ax = plt.subplots(figsize=(11, 6))
    for result in results:
        if not result.equity_curve:
            continue
        times = [t for t, _ in result.equity_curve]
        equities = [e for _, e in result.equity_curve]
        ax.plot(times, equities, label=result.label, linewidth=1.0)
    ax.set_title(f"Backtest equity curves ({BACKTEST_YEARS}y, daily bars)")
    ax.set_xlabel("Date")
    ax.set_ylabel("Equity ($)")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"\nEquity curve chart saved to {out_path}")


def run_backtest():
    broker = Broker()
    strategies = build_strategies()

    all_results, in_sample, out_sample, warnings = [], [], [], []

    for symbol in config.SYMBOLS:
        print(f"Fetching {BACKTEST_YEARS}y of daily bars for {symbol}...")
        bars = fetch_backtest_bars(broker, symbol)
        if bars.empty or len(bars) < 150:
            print(f"  {symbol}: insufficient data ({len(bars)} bars) -- skipped")
            continue

        print(f"  {symbol}: {len(bars)} bars, {bars.index[0].date()} to {bars.index[-1].date()}")
        strategy = strategies[symbol]
        ppy = periods_per_year(symbol, bars)

        full = simulate_symbol(symbol, bars, strategy, label=f"{symbol} [{strategy.name}]")
        all_results.append(full)

        # Walk-forward: the second half is the only untouched sample if any
        # parameter in config was ever chosen by looking at this data.
        split = len(bars) // 2
        first = simulate_symbol(symbol, bars.iloc[:split], strategy, label=f"{symbol} 1st half")
        second = simulate_symbol(symbol, bars.iloc[split:], strategy, label=f"{symbol} 2nd half")
        in_sample.append(compute_metrics(first, STARTING_CASH, ppy))
        oos = compute_metrics(second, STARTING_CASH, ppy)
        out_sample.append(oos)

        # Flag on the out-of-sample half only. The full-period run includes the
        # in-sample data any parameter was fitted against, so a strong first
        # half can mask a losing second half.
        flag_label = f"{symbol} [{strategy.name}] out-of-sample"
        if oos["sharpe_ratio"] < 0:
            warnings.append(f"{flag_label}: NEGATIVE Sharpe ({oos['sharpe_ratio']:.2f})")
        if oos["max_drawdown_pct"] > DRAWDOWN_FLAG_PCT:
            warnings.append(
                f"{flag_label}: max drawdown {oos['max_drawdown_pct']:.1f}% "
                f"exceeds {DRAWDOWN_FLAG_PCT:.0f}%"
            )

    if in_sample:
        print_summary_table(in_sample, "FIRST HALF (in-sample -- do not trust)")
        print_summary_table(out_sample, "SECOND HALF (out-of-sample -- this is the real number)")
        plot_equity_curves(all_results)

    if warnings:
        print("\nFLAGGED -- adjust before trusting these strategies:")
        for w in warnings:
            print(f"  - {w}")
    else:
        print("\nNo strategies flagged.")

    print(
        "\nNote: paper/backtest results exclude borrow costs on shorts, real fill "
        "quality, and market impact. Discount accordingly."
    )
    return out_sample


if __name__ == "__main__":
    run_backtest()
