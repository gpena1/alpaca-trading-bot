from bot.strategies.base import Signal, SignalAction, Strategy
from bot.strategies.mean_reversion import MeanReversionStrategy
from bot.strategies.momentum_breakout import MomentumBreakoutStrategy
from bot.strategies.trend_following import TrendFollowingStrategy

STRATEGY_REGISTRY = {
    "trend_following": TrendFollowingStrategy,
    "mean_reversion": MeanReversionStrategy,
    "momentum_breakout": MomentumBreakoutStrategy,
}

__all__ = [
    "Signal",
    "SignalAction",
    "Strategy",
    "STRATEGY_REGISTRY",
]
