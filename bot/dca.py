"""Portfolio Architecture: tiered capital plan, category watchlist, and the
price-level DCA entry framework, transcribed from PORTFOLIO ARCHITECTURE.docx.

This module is the single source of truth for both the live dashboard
(display) and the DCA-ladder order logic (bot/main.py) -- the tier/category
data and the level math must never drift between the two.

Conservative tier is untouched by design: it keeps its existing
trend/mean-reversion/momentum strategies and risk parameters exactly as
they were before this plan existed. Only Growth and Aggressive trade the
categories mapped to them below; Precious Metals stays reference-only for
now (it doesn't cleanly belong to either, and Conservative already runs its
own GLD position under mean-reversion).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd
from alpaca.trading.enums import OrderSide

from bot.broker import Broker
from bot.portfolio import Portfolio
from bot.risk_manager import OrderDecision
from bot.strategies.mean_reversion import _rsi

logger = logging.getLogger(__name__)

RSI_PERIOD = 14
TWENTY_WEEK_MA = 20

TIERS = [
    {
        "key": "aggressive",
        "name": "Aggressive",
        "capital_source": "Brian's third $1k",
        "risk_level": "High",
        "strategy": "Leveraged ETFs, China plays, crypto, options",
    },
    {
        "key": "growth",
        "name": "Growth",
        "capital_source": "Brian's second $1.5k",
        "risk_level": "High",
        "strategy": "Individual stocks, BTC mining, energy",
    },
    {
        "key": "conservative",
        "name": "Conservative",
        "capital_source": "Brian's initial $2.5k",
        "risk_level": "Medium",
        "strategy": "Dividend ETFs, blue chip, capital preservation",
    },
]

WATCHLIST_CATEGORIES = [
    {
        "name": "China / International Value",
        "thesis": "US overvalued, China showing strength, energy capacity building",
        "instruments": [
            {"symbol": "BABA", "crypto": False, "label": "Chinese mega-cap value play"},
            {"symbol": "JD", "crypto": False, "label": "Chinese mega-cap value play"},
            {"symbol": "PDD", "crypto": False, "label": "Chinese mega-cap value play"},
            {"symbol": "KWEB", "crypto": False, "label": "China internet ETF (basket exposure)"},
            {"symbol": "NIO", "crypto": False, "label": "EV + energy infrastructure angle"},
            {"symbol": "BYD", "crypto": False, "label": "EV + energy infrastructure angle"},
            {"symbol": "FXI", "crypto": False, "label": "Broad China ETF for initial DCA position"},
        ],
    },
    {
        "name": "Clean / Renewable Energy",
        "thesis": "Energy is the AI bottleneck",
        "instruments": [
            {"symbol": "FSLR", "crypto": False, "label": "First Solar — US-based, strong fundamentals"},
            {"symbol": "NEE", "crypto": False, "label": "NextEra — largest renewable utility"},
            {"symbol": "ENPH", "crypto": False, "label": "Enphase — solar microinverters, beaten down, watch for reversal"},
            {"symbol": "PLUG", "crypto": False, "label": "Hydrogen, speculative, high beta"},
        ],
    },
    {
        "name": "Bitcoin Mining / AI Energy Infrastructure",
        "thesis": "BTC miners = accessible energy + AI hyperscaler demand",
        "instruments": [
            {"symbol": "MARA", "crypto": False, "label": "Pure play BTC miner"},
            {"symbol": "CLSK", "crypto": False, "label": "Pure play BTC miner"},
            {"symbol": "RIOT", "crypto": False, "label": "Pure play BTC miner"},
            {"symbol": "IREN", "crypto": False, "label": "Also pivoting to AI compute"},
            {"symbol": "CORZ", "crypto": False, "label": "Core Scientific — post-bankruptcy, AI data center pivot"},
            {"symbol": "VRT", "crypto": False, "label": "Vertiv — power infrastructure for data centers, less speculative"},
        ],
    },
    {
        "name": "Leveraged ETFs",
        "thesis": "Amplified exposure to high-conviction macro bets",
        "instruments": [
            {"symbol": "TQQQ", "crypto": False, "label": "3x Nasdaq (use only with price-level DCA, not blind monthly)"},
            {"symbol": "SOXL", "crypto": False, "label": "3x Semiconductors (AI chip exposure)"},
            {"symbol": "LABU", "crypto": False, "label": "3x Biotech (higher speculative tier)"},
            {"symbol": "FAS", "crypto": False, "label": "3x Financials (if rates start dropping)"},
        ],
    },
    {
        "name": "Crypto (Fundamentals-Based)",
        "thesis": "Coins with revenue mechanics, burn tokens, not just speculation",
        "instruments": [
            {"symbol": "BTC/USD", "crypto": True, "label": "Digital gold, base position"},
            {"symbol": "ETH/USD", "crypto": True, "label": "Revenue through gas fees, deflationary post-merge"},
            {"symbol": "BNB/USD", "crypto": True, "label": "Quarterly burn mechanism, exchange revenue tied"},
            {"symbol": "SOL/USD", "crypto": True, "label": "High throughput, ecosystem growth"},
        ],
        "note": "Avoid: meme coins, no-utility tokens.",
    },
    {
        "name": "Precious Metals / Miners",
        "thesis": "Inflation hedge, you already hold physical",
        "instruments": [
            {"symbol": "GLD", "crypto": False, "label": "Paper gold to complement physical"},
            {"symbol": "SGOL", "crypto": False, "label": "Paper gold to complement physical"},
            {"symbol": "GDXJ", "crypto": False, "label": "Junior gold miners, high leverage to gold price"},
            {"symbol": "AG", "crypto": False, "label": "First Majestic Silver — silver miner, high beta to silver price"},
            {"symbol": "WPM", "crypto": False, "label": "Wheaton Precious Metals — streaming model, lower risk than pure miners"},
        ],
    },
]

# Which categories each tier's bot actually trades. Conservative isn't
# listed here at all -- it keeps its existing symbols/strategies untouched.
# Precious Metals isn't listed under either tier: it doesn't match Growth's
# or Aggressive's stated strategy, and GLD already trades under Conservative's
# mean-reversion strategy, so it stays reference-only on the dashboard.
TRADED_CATEGORIES_BY_TIER = {
    "growth": [
        "Clean / Renewable Energy",
        "Bitcoin Mining / AI Energy Infrastructure",
    ],
    "aggressive": [
        "China / International Value",
        "Leveraged ETFs",
        "Crypto (Fundamentals-Based)",
    ],
}


def traded_instruments_for_tier(tier_key: str) -> list[dict]:
    """Flattened (symbol, crypto) list of everything a tier's bot trades."""
    category_names = TRADED_CATEGORIES_BY_TIER.get(tier_key, [])
    instruments = []
    for cat in WATCHLIST_CATEGORIES:
        if cat["name"] in category_names:
            instruments.extend(cat["instruments"])
    return instruments


DCA_LEVELS = [
    {
        "level": 1,
        "label": "Initial position",
        "pct_of_target": 25,
        "trigger": "Price hits 20-week moving average from above, or RSI drops below 40 on weekly chart",
    },
    {
        "level": 2,
        "label": "Add",
        "pct_of_target": 25,
        "trigger": "Price drops 15% below Level 1 entry",
    },
    {
        "level": 3,
        "label": "Conviction add",
        "pct_of_target": 25,
        "trigger": "Price drops 30% below Level 1 entry + volume spike (capitulation signal)",
    },
    {
        "level": 4,
        "label": "Final add",
        "pct_of_target": 25,
        "trigger": "Price drops 50% below Level 1 OR reversal confirmation (two consecutive weekly closes above 20-week MA)",
    },
]

OPTIONS_LAYER = [
    "Cash-Secured Puts — sell puts at your Level 2/3 entry prices to get paid while waiting",
    "Covered Calls — once in profit on a position, sell calls 10-15% above current price monthly",
    "Goal: Options premium funds new DCA entries",
]


@dataclass
class DcaStatus:
    price: float | None
    level1: float | None
    level2: float | None
    level3: float | None
    level4: float | None
    weekly_rsi: float | None
    armed_level: int  # 0 = above Level 1 (not yet triggered), 1-4 = highest triggered level
    pct_from_level1: float | None
    error: str | None = None


def compute_dca_status(bars: pd.DataFrame) -> DcaStatus:
    """Pure price-level version of the doc's rules: Level 3's volume-spike
    and Level 4's reversal-confirmation alternate triggers are simplified
    away in favor of the plain drawdown thresholds (-30%, -50%). This keeps
    both the dashboard display and the live order logic simple and
    state-free; flagged here since it's a deliberate scope cut, not an
    oversight."""
    if bars.empty:
        return DcaStatus(None, None, None, None, None, None, 0, None, error="no price data")

    price = float(bars["close"].iloc[-1])

    if len(bars) < TWENTY_WEEK_MA:
        return DcaStatus(price, None, None, None, None, None, 0, None, error="insufficient weekly history")

    level1 = float(bars["close"].rolling(TWENTY_WEEK_MA).mean().iloc[-1])
    level2 = level1 * 0.85
    level3 = level1 * 0.70
    level4 = level1 * 0.50

    weekly_rsi = None
    if len(bars) >= RSI_PERIOD + 1:
        weekly_rsi = float(_rsi(bars["close"], RSI_PERIOD).iloc[-1])

    if price <= level4:
        armed = 4
    elif price <= level3:
        armed = 3
    elif price <= level2:
        armed = 2
    elif price <= level1 or (weekly_rsi is not None and weekly_rsi < 40):
        armed = 1
    else:
        armed = 0

    pct_from_level1 = (price - level1) / level1 * 100 if level1 else None

    return DcaStatus(price, level1, level2, level3, level4, weekly_rsi, armed, pct_from_level1)


def evaluate_dca_entry(
    broker: Broker,
    portfolio: Portfolio,
    symbol: str,
    crypto: bool,
    allocation_pct: float,
    max_total_exposure_pct: float,
) -> OrderDecision | None:
    """Buy-only tranche accumulation: no sell logic exists in the doc for
    these positions (exits are manual, via the options overlay), so unlike
    the existing strategies this never closes a position.

    Stateless by design -- tranches already bought are inferred each cycle
    from live position value vs. the computed per-tranche dollar size, so a
    restart or a missed cycle can't cause double-buying or lost state.
    """
    try:
        bars = broker.get_weekly_bars(symbol, crypto=crypto)
        status = compute_dca_status(bars)
    except Exception:
        logger.exception("%s: failed to fetch weekly bars for DCA evaluation", symbol)
        return None

    if status.error or status.armed_level == 0:
        return None

    equity = portfolio.equity()
    if equity <= 0:
        return None

    if portfolio.exposure_pct() >= max_total_exposure_pct:
        logger.info("%s: DCA buy skipped, total exposure cap reached", symbol)
        return None

    target_dollars = equity * allocation_pct
    tranche_dollars = target_dollars / len(DCA_LEVELS)
    if tranche_dollars <= 0:
        return None

    position = portfolio.position_for(symbol)
    current_value = position.market_value if position else 0.0
    tranches_bought = round(current_value / tranche_dollars)

    tranches_to_buy = status.armed_level - tranches_bought
    if tranches_to_buy <= 0:
        return None

    buy_dollars = tranche_dollars * tranches_to_buy
    qty = round(buy_dollars / status.price, 6)
    if qty <= 0:
        return None

    return OrderDecision(
        symbol=symbol,
        side=OrderSide.BUY,
        qty=qty,
        reason=(
            f"DCA Level {status.armed_level} armed (tranche {tranches_bought + 1}"
            f"-{status.armed_level} of {len(DCA_LEVELS)}), price ${status.price:.2f} "
            f"vs Level 1 ${status.level1:.2f}"
        ),
    )
