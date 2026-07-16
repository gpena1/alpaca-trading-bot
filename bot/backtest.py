"""Backtest every live strategy against historical data before trusting it
with real trades: Conservative's trend/mean-reversion/momentum strategies
(15-min bars) via run_backtest(), and Growth/Aggressive's DCA-ladder
strategy (weekly bars) via run_dca_backtest("growth"/"aggressive").

Walks bars forward with no lookahead -- each decision only ever sees bars
up to and including "now" -- and reuses the exact same live decision logic
(bot.main.build_strategies() for Conservative, bot.dca's compute_dca_status
sizing math for Growth/Aggressive), so the backtest can never silently
drift from what's actually deployed. Applies 0.05% slippage and $0
commission (Alpaca is commission-free) to every simulated fill.

The DCA-ladder backtest never produces a closed Trade (matches live: it's
buy-only, exits are manual via the options overlay) -- its metrics come
entirely from the equity curve (return, drawdown, Sharpe), which is the
honest way to evaluate a strategy designed to never close a position.

Two deliberate deviations from the generic guide the Conservative-tier
backtest was originally built from:

1. Timeframe: every symbol backtests on config.BAR_TIMEFRAME_MINUTES
   (15-min), matching what the live bot actually runs -- not the mixed
   15-min/1-hour/4-hour split the guide suggested, which would validate a
   different bot than the one in production.
2. No "correlation filter": nothing in bot/risk_manager.py implements one
   today. The combined-portfolio run reports performance under the real,
   existing risk rules (per-symbol + total exposure caps) instead of a
   feature that doesn't exist in this codebase.

Run: python3 -m bot.backtest
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

import config
from bot.broker import Broker, is_crypto
from bot.dca import DCA_LEVELS, compute_dca_status, traded_instruments_for_tier
from bot.main import build_strategies
from alpaca.data.timeframe import TimeFrame

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bot.backtest")

SLIPPAGE_PCT = 0.0005  # 0.05%, applied against the strategy on every fill
STARTING_CASH = 100_000.0
BACKTEST_MONTHS = 6
DRAWDOWN_FLAG_PCT = 15.0
OUTPUT_CHART = "backtest_results.png"


@dataclass
class Trade:
    symbol: str
    entry_time: object
    entry_price: float
    exit_time: object
    exit_price: float
    qty: float
    pnl: float


@dataclass
class SimResult:
    label: str
    trades: list
    equity_curve: list  # [(timestamp, equity), ...]
    final_equity: float
    open_unrealized: float


def _fill_price(price: float, is_buy: bool) -> float:
    return price * (1 + SLIPPAGE_PCT) if is_buy else price * (1 - SLIPPAGE_PCT)


def fetch_backtest_bars(broker: Broker, symbol: str, months: int = BACKTEST_MONTHS) -> pd.DataFrame:
    """Matches the live bot's actual timeframe (config.BAR_TIMEFRAME_MINUTES)
    for every symbol. Requests the last `months` up to now, but the account's
    data plan may not actually reach that recently -- callers should check
    the returned range against what was requested rather than assume it."""
    timeframe = TimeFrame(config.BAR_TIMEFRAME_MINUTES, TimeFrame.Minute.unit)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=months * 30.44)
    return broker.get_bars_range(symbol, is_crypto(symbol), timeframe, start, end)


def simulate_symbol(
    symbol: str,
    bars: pd.DataFrame,
    strategy,
    starting_cash: float = STARTING_CASH,
    allocation_pct: float = config.MAX_ALLOCATION_PCT_PER_SYMBOL,
    stop_loss_pct: float = config.STOP_LOSS_PCT,
    take_profit_pct: float = config.TAKE_PROFIT_PCT,
) -> SimResult:
    """Single-symbol backtest: the strategy gets the full starting_cash to
    itself (no other symbols competing for capital) -- see simulate_combined
    for the shared-capital, multi-symbol version."""
    cash = starting_cash
    qty = 0.0
    entry_price = 0.0
    entry_time = None
    trades: list[Trade] = []
    equity_curve: list[tuple] = []

    for i in range(1, len(bars)):
        window = bars.iloc[: i + 1]
        current_time = window.index[-1]
        current_price = float(window["close"].iloc[-1])

        if qty > 0 and entry_price > 0:
            change_pct = (current_price - entry_price) / entry_price
            if change_pct <= -stop_loss_pct or change_pct >= take_profit_pct:
                fill = _fill_price(current_price, is_buy=False)
                pnl = (fill - entry_price) * qty
                cash += fill * qty
                trades.append(Trade(symbol, entry_time, entry_price, current_time, fill, qty, pnl))
                qty, entry_price, entry_time = 0.0, 0.0, None
                equity_curve.append((current_time, cash))
                continue

        signal = strategy.generate_signal(window)

        if signal.action.value == "buy" and qty == 0:
            fill = _fill_price(current_price, is_buy=True)
            new_qty = (cash * allocation_pct) / fill
            if new_qty > 0:
                cash -= fill * new_qty
                qty, entry_price, entry_time = new_qty, fill, current_time
        elif signal.action.value == "sell" and qty > 0:
            fill = _fill_price(current_price, is_buy=False)
            pnl = (fill - entry_price) * qty
            cash += fill * qty
            trades.append(Trade(symbol, entry_time, entry_price, current_time, fill, qty, pnl))
            qty, entry_price, entry_time = 0.0, 0.0, None

        equity_curve.append((current_time, cash + qty * current_price))

    final_price = float(bars["close"].iloc[-1]) if len(bars) else starting_cash
    open_unrealized = (final_price - entry_price) * qty if qty > 0 else 0.0
    final_equity = cash + qty * final_price

    return SimResult(symbol, trades, equity_curve, final_equity, open_unrealized)


def simulate_combined(
    strategies: dict,
    symbol_bars: dict,
    starting_cash: float = STARTING_CASH,
    allocation_pct: float = config.MAX_ALLOCATION_PCT_PER_SYMBOL,
    max_total_exposure_pct: float = config.MAX_TOTAL_EXPOSURE_PCT,
    stop_loss_pct: float = config.STOP_LOSS_PCT,
    take_profit_pct: float = config.TAKE_PROFIT_PCT,
) -> SimResult:
    """All 5 symbols sharing one capital pool and one exposure cap, mirroring
    RiskManager's real sizing logic (allocation_pct capped by remaining
    exposure room) instead of just summing 5 independent single-symbol runs,
    which would misrepresent how the account's real exposure cap behaves
    when multiple positions are open at once."""
    cash = starting_cash
    positions: dict[str, dict] = {}
    latest_price: dict[str, float] = {}
    trades: list[Trade] = []
    equity_curve: list[tuple] = []

    events = []
    for symbol, bars in symbol_bars.items():
        for idx in range(len(bars)):
            events.append((bars.index[idx], symbol, idx))
    events.sort(key=lambda e: e[0])

    def current_equity() -> float:
        return cash + sum(p["qty"] * latest_price.get(s, p["entry_price"]) for s, p in positions.items())

    for ts, symbol, idx in events:
        bars = symbol_bars[symbol]
        window = bars.iloc[: idx + 1]
        current_price = float(window["close"].iloc[-1])
        latest_price[symbol] = current_price

        pos = positions.get(symbol)
        if pos:
            change_pct = (current_price - pos["entry_price"]) / pos["entry_price"]
            if change_pct <= -stop_loss_pct or change_pct >= take_profit_pct:
                fill = _fill_price(current_price, is_buy=False)
                pnl = (fill - pos["entry_price"]) * pos["qty"]
                cash += fill * pos["qty"]
                trades.append(Trade(symbol, pos["entry_time"], pos["entry_price"], ts, fill, pos["qty"], pnl))
                del positions[symbol]
                equity_curve.append((ts, current_equity()))
                continue

        if idx == 0:
            equity_curve.append((ts, current_equity()))
            continue

        signal = strategies[symbol].generate_signal(window)

        if signal.action.value == "buy" and symbol not in positions:
            equity = current_equity()
            exposure = sum(p["qty"] * latest_price.get(s, p["entry_price"]) for s, p in positions.items())
            room_left_pct = max_total_exposure_pct - (exposure / equity if equity > 0 else 1.0)
            alloc_pct = min(allocation_pct, max(room_left_pct, 0.0))
            target_dollars = equity * alloc_pct
            if target_dollars > 0:
                fill = _fill_price(current_price, is_buy=True)
                qty = target_dollars / fill
                if qty > 0 and cash >= fill * qty:
                    cash -= fill * qty
                    positions[symbol] = {"qty": qty, "entry_price": fill, "entry_time": ts}
        elif signal.action.value == "sell" and symbol in positions:
            pos = positions[symbol]
            fill = _fill_price(current_price, is_buy=False)
            pnl = (fill - pos["entry_price"]) * pos["qty"]
            cash += fill * pos["qty"]
            trades.append(Trade(symbol, pos["entry_time"], pos["entry_price"], ts, fill, pos["qty"], pnl))
            del positions[symbol]

        equity_curve.append((ts, current_equity()))

    open_unrealized = sum(
        (latest_price.get(s, p["entry_price"]) - p["entry_price"]) * p["qty"] for s, p in positions.items()
    )
    return SimResult("COMBINED", trades, equity_curve, current_equity(), open_unrealized)


def _dca_tranches_to_buy(qty: float, current_price: float, armed_level: int, tranche_dollars: float) -> int:
    """Mirrors bot/dca.py's evaluate_dca_entry exactly: tranches already
    bought are inferred from current *mark-to-market* position value (not
    cost basis), matching what the live bot actually does -- bugs and all,
    since the point of backtesting is to validate what will really run."""
    if tranche_dollars <= 0:
        return 0
    current_value = qty * current_price
    tranches_bought = round(current_value / tranche_dollars)
    return max(0, armed_level - tranches_bought)


def simulate_dca_symbol(
    symbol: str,
    bars: pd.DataFrame,
    starting_cash: float = STARTING_CASH,
    allocation_pct: float = config.DCA_MAX_ALLOCATION_PCT_PER_SYMBOL,
) -> SimResult:
    """Buy-only tranche accumulation on weekly bars, no lookahead -- same
    logic bot/dca.py's evaluate_dca_entry runs live, just walked forward
    over history instead of called once per live cycle. No sell/stop-loss
    exists for this strategy (matches live: exits are manual, via the
    options overlay described in the Portfolio Architecture doc), so unlike
    simulate_symbol this never produces a Trade -- metrics come entirely
    from the equity curve (return, drawdown, Sharpe), which is the honest
    way to evaluate a strategy that's designed to never close a position."""
    cash = starting_cash
    qty = 0.0
    equity_curve: list[tuple] = []
    tranche_dollars = (starting_cash * allocation_pct) / len(DCA_LEVELS)

    for i in range(1, len(bars)):
        window = bars.iloc[: i + 1]
        current_time = window.index[-1]
        current_price = float(window["close"].iloc[-1])
        status = compute_dca_status(window)

        if not status.error and status.armed_level > 0:
            tranches_to_buy = _dca_tranches_to_buy(qty, current_price, status.armed_level, tranche_dollars)
            if tranches_to_buy > 0:
                buy_dollars = tranche_dollars * tranches_to_buy
                if 0 < buy_dollars <= cash:
                    fill = _fill_price(current_price, is_buy=True)
                    qty += buy_dollars / fill
                    cash -= buy_dollars

        equity_curve.append((current_time, cash + qty * current_price))

    final_price = float(bars["close"].iloc[-1]) if len(bars) else starting_cash
    final_equity = cash + qty * final_price
    return SimResult(symbol, [], equity_curve, final_equity, 0.0)


def simulate_dca_combined(
    tier_key: str,
    symbol_bars: dict,
    starting_cash: float = STARTING_CASH,
    allocation_pct: float = config.DCA_MAX_ALLOCATION_PCT_PER_SYMBOL,
    max_total_exposure_pct: float = config.DCA_MAX_TOTAL_EXPOSURE_PCT,
) -> SimResult:
    """Shared-capital version across a tier's full symbol list, mirroring
    simulate_combined's approach for Conservative but using tranche sizing
    instead of a single-shot BUY signal."""
    cash = starting_cash
    positions: dict[str, float] = {}  # symbol -> qty
    latest_price: dict[str, float] = {}
    equity_curve: list[tuple] = []

    events = []
    for symbol, bars in symbol_bars.items():
        for idx in range(len(bars)):
            events.append((bars.index[idx], symbol, idx))
    events.sort(key=lambda e: e[0])

    def current_equity() -> float:
        return cash + sum(qty * latest_price.get(s, 0.0) for s, qty in positions.items())

    for ts, symbol, idx in events:
        bars = symbol_bars[symbol]
        window = bars.iloc[: idx + 1]
        current_price = float(window["close"].iloc[-1])
        latest_price[symbol] = current_price

        if idx < 1:
            equity_curve.append((ts, current_equity()))
            continue

        status = compute_dca_status(window)
        if not status.error and status.armed_level > 0:
            equity = current_equity()
            exposure = sum(qty * latest_price.get(s, 0.0) for s, qty in positions.items())
            room_left_pct = max_total_exposure_pct - (exposure / equity if equity > 0 else 1.0)
            if room_left_pct > 0:
                tranche_dollars = (equity * min(allocation_pct, room_left_pct)) / len(DCA_LEVELS)
                qty_held = positions.get(symbol, 0.0)
                tranches_to_buy = _dca_tranches_to_buy(qty_held, current_price, status.armed_level, tranche_dollars)
                if tranches_to_buy > 0:
                    buy_dollars = tranche_dollars * tranches_to_buy
                    if 0 < buy_dollars <= cash:
                        fill = _fill_price(current_price, is_buy=True)
                        positions[symbol] = qty_held + buy_dollars / fill
                        cash -= buy_dollars

        equity_curve.append((ts, current_equity()))

    return SimResult(f"{tier_key.upper()} COMBINED", [], equity_curve, current_equity(), 0.0)


def compute_metrics(result: SimResult, starting_cash: float, bar_seconds: float) -> dict:
    trades = result.trades
    total_trades = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]

    win_rate = (len(wins) / total_trades * 100) if total_trades else None
    avg_win = (sum(t.pnl for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.pnl for t in losses) / len(losses)) if losses else 0.0
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = float("inf") if gross_win > 0 else 0.0

    equities = [e for _, e in result.equity_curve] or [starting_cash]
    peak = equities[0]
    max_dd = 0.0
    for e in equities:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)

    returns = pd.Series(equities).pct_change().dropna()
    periods_per_year = (365.25 * 24 * 3600) / bar_seconds if bar_seconds else 252
    if len(returns) > 1 and returns.std() > 0:
        sharpe = (returns.mean() / returns.std()) * (periods_per_year**0.5)
    else:
        sharpe = 0.0

    total_return_pct = (result.final_equity - starting_cash) / starting_cash * 100

    return {
        "label": result.label,
        "total_trades": total_trades,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_dd * 100,
        "sharpe_ratio": sharpe,
        "total_return_pct": total_return_pct,
        "final_equity": result.final_equity,
        "open_unrealized": result.open_unrealized,
    }


def _bar_seconds(bars: pd.DataFrame) -> float:
    diffs = bars.index.to_series().diff().dropna()
    return diffs.median().total_seconds() if len(diffs) else 900.0


def print_summary_table(metrics_list: list[dict]):
    headers = ["Symbol", "Trades", "Win%", "AvgWin", "AvgLoss", "ProfFac", "MaxDD%", "Sharpe", "Return%"]
    widths = [12, 7, 7, 9, 9, 8, 8, 8, 9]

    def fmt_row(cells):
        return " | ".join(str(c).rjust(w) for c, w in zip(cells, widths))

    print("\n" + "=" * 90)
    print("BACKTEST SUMMARY".center(90))
    print("=" * 90)
    print(fmt_row(headers))
    print("-" * 90)
    for m in metrics_list:
        win_rate = f"{m['win_rate']:.0f}" if m["win_rate"] is not None else "—"
        pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
        print(
            fmt_row(
                [
                    m["label"],
                    m["total_trades"],
                    win_rate,
                    f"${m['avg_win']:.0f}",
                    f"${m['avg_loss']:.0f}",
                    pf,
                    f"{m['max_drawdown_pct']:.1f}",
                    f"{m['sharpe_ratio']:.2f}",
                    f"{m['total_return_pct']:.1f}",
                ]
            )
        )
    print("=" * 90)


def plot_equity_curves(results: list[SimResult], out_path: str = OUTPUT_CHART):
    fig, ax = plt.subplots(figsize=(11, 6))
    for result in results:
        if not result.equity_curve:
            continue
        times = [t for t, _ in result.equity_curve]
        equities = [e for _, e in result.equity_curve]
        ax.plot(times, equities, label=result.label, linewidth=1.6 if result.label == "COMBINED" else 1.0)

    ax.set_title(f"Backtest Equity Curves ({BACKTEST_MONTHS}-month window)")
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

    symbol_bars = {}
    results = []
    metrics_list = []
    warnings = []
    requested_days = BACKTEST_MONTHS * 30.44

    for symbol in config.SYMBOLS:
        print(f"Fetching {BACKTEST_MONTHS} months of {config.BAR_TIMEFRAME_MINUTES}-min bars for {symbol}...")
        bars = fetch_backtest_bars(broker, symbol)
        if bars.empty or len(bars) < 30:
            print(f"  {symbol}: insufficient data ({len(bars)} bars) -- skipped")
            continue

        actual_days = (bars.index[-1] - bars.index[0]).total_seconds() / 86400
        if actual_days < requested_days * 0.9:
            print(
                f"  WARNING: only got {bars.index[0].date()} to {bars.index[-1].date()} "
                f"({actual_days:.0f} days) -- shorter than the requested {BACKTEST_MONTHS} months, "
                f"and not necessarily the most recent data. Results reflect this window, not 'the last 6 months.'"
            )

        symbol_bars[symbol] = bars
        strategy = strategies[symbol]
        result = simulate_symbol(symbol, bars, strategy)
        metrics = compute_metrics(result, STARTING_CASH, _bar_seconds(bars))
        metrics["label"] = f"{symbol} [{strategy.name}]"
        results.append(result)
        metrics_list.append(metrics)

        if metrics["sharpe_ratio"] < 0:
            warnings.append(f"{symbol} [{strategy.name}]: NEGATIVE Sharpe ratio ({metrics['sharpe_ratio']:.2f})")
        if metrics["max_drawdown_pct"] > DRAWDOWN_FLAG_PCT:
            warnings.append(
                f"{symbol} [{strategy.name}]: max drawdown {metrics['max_drawdown_pct']:.1f}% "
                f"exceeds {DRAWDOWN_FLAG_PCT:.0f}%"
            )

    if symbol_bars:
        combined_result = simulate_combined(strategies, symbol_bars)
        combined_metrics = compute_metrics(combined_result, STARTING_CASH, _bar_seconds(next(iter(symbol_bars.values()))))
        results.append(combined_result)
        metrics_list.append(combined_metrics)
        if combined_metrics["sharpe_ratio"] < 0:
            warnings.append(f"COMBINED portfolio: NEGATIVE Sharpe ratio ({combined_metrics['sharpe_ratio']:.2f})")
        if combined_metrics["max_drawdown_pct"] > DRAWDOWN_FLAG_PCT:
            warnings.append(f"COMBINED portfolio: max drawdown {combined_metrics['max_drawdown_pct']:.1f}% exceeds {DRAWDOWN_FLAG_PCT:.0f}%")

    print_summary_table(metrics_list)
    if results:
        plot_equity_curves(results)

    if warnings:
        print("\n⚠ FLAGGED -- adjust parameters before trusting these strategies live:")
        for w in warnings:
            print(f"  - {w}")
    else:
        print("\nNo strategies flagged: all Sharpe ratios positive, all drawdowns within the 15% threshold.")

    return metrics_list


DCA_BACKTEST_YEARS = 3
_DCA_FETCH_WORKERS = 5


def fetch_dca_backtest_bars(broker: Broker, symbol: str, crypto: bool, years: int = DCA_BACKTEST_YEARS) -> pd.DataFrame:
    """DCA-ladder bars: weekly timeframe, longer lookback than the 15-min
    strategies since the 20-week MA needs real runway before any signal is
    meaningful."""
    timeframe = TimeFrame(1, TimeFrame.Week.unit)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=years * 365.25)
    return broker.get_bars_range(symbol, crypto, timeframe, start, end)


def run_dca_backtest(tier_key: str):
    broker = Broker()
    instruments = traded_instruments_for_tier(tier_key)

    print(f"Fetching {DCA_BACKTEST_YEARS} years of weekly bars for {len(instruments)} {tier_key} symbols...")
    with ThreadPoolExecutor(max_workers=_DCA_FETCH_WORKERS) as pool:
        fetched = list(
            pool.map(lambda inst: fetch_dca_backtest_bars(broker, inst["symbol"], inst["crypto"]), instruments)
        )

    symbol_bars = {}
    results = []
    metrics_list = []
    warnings = []

    for inst, bars in zip(instruments, fetched):
        symbol = inst["symbol"]
        if bars.empty or len(bars) < 30:
            print(f"  {symbol}: insufficient data ({len(bars)} bars) -- skipped")
            continue

        symbol_bars[symbol] = bars
        result = simulate_dca_symbol(symbol, bars)
        metrics = compute_metrics(result, STARTING_CASH, _bar_seconds(bars))
        metrics["label"] = f"{symbol} [dca-ladder]"
        results.append(result)
        metrics_list.append(metrics)

        if metrics["sharpe_ratio"] < 0:
            warnings.append(f"{symbol} [dca-ladder]: NEGATIVE Sharpe ratio ({metrics['sharpe_ratio']:.2f})")
        if metrics["max_drawdown_pct"] > DRAWDOWN_FLAG_PCT:
            warnings.append(
                f"{symbol} [dca-ladder]: max drawdown {metrics['max_drawdown_pct']:.1f}% "
                f"exceeds {DRAWDOWN_FLAG_PCT:.0f}%"
            )

    if symbol_bars:
        combined_result = simulate_dca_combined(tier_key, symbol_bars)
        combined_metrics = compute_metrics(
            combined_result, STARTING_CASH, _bar_seconds(next(iter(symbol_bars.values())))
        )
        results.append(combined_result)
        metrics_list.append(combined_metrics)
        label = f"{tier_key.upper()} COMBINED"
        if combined_metrics["sharpe_ratio"] < 0:
            warnings.append(f"{label}: NEGATIVE Sharpe ratio ({combined_metrics['sharpe_ratio']:.2f})")
        if combined_metrics["max_drawdown_pct"] > DRAWDOWN_FLAG_PCT:
            warnings.append(f"{label}: max drawdown {combined_metrics['max_drawdown_pct']:.1f}% exceeds {DRAWDOWN_FLAG_PCT:.0f}%")

    print_summary_table(metrics_list)
    if results:
        plot_equity_curves(results, out_path=f"backtest_results_{tier_key}.png")

    if warnings:
        print(f"\n⚠ FLAGGED ({tier_key}) -- adjust parameters before trusting these positions live:")
        for w in warnings:
            print(f"  - {w}")
    else:
        print(f"\nNo {tier_key} DCA-ladder symbols flagged.")

    return metrics_list


if __name__ == "__main__":
    run_backtest()
    run_dca_backtest("growth")
    run_dca_backtest("aggressive")
