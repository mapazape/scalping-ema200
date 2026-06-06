import logging
from typing import Optional

import config
from indicators import Indicators

logger = logging.getLogger(__name__)


class RiskEngine:
    def __init__(self, indicators: Indicators) -> None:
        self.ind = indicators

    def build_order(self, signal: dict, balance: float) -> Optional[dict]:
        """
        1. Filter by ATR range — reject if market is too quiet or too volatile.
        2. Fixed-percentage SL/TP (SL_PCT / TP_PCT), not ATR distance.
        3. Notional sizing: balance * POSITION_SIZE_PCT * MAX_LEVERAGE.
        4. Validate payoff >= PAYOFF_MIN before approving.
        """
        atr = self.ind.atr14_1m()
        if atr is None:
            logger.warning("ATR unavailable — skipping signal")
            return None
        if not (config.ATR_FILTER_MIN <= atr <= config.ATR_FILTER_MAX):
            logger.warning(
                "ATR %.2f outside filter [%.1f, %.1f] — rejecting",
                atr, config.ATR_FILTER_MIN, config.ATR_FILTER_MAX,
            )
            return None

        entry = signal["entry_price"]
        notional = balance * config.POSITION_SIZE_PCT * config.MAX_LEVERAGE
        if notional < config.MIN_BINANCE_NOTIONAL:
            logger.warning(
                "notional %.2f below exchange minimum %.2f — clamping up",
                notional, config.MIN_BINANCE_NOTIONAL,
            )
            notional = config.MIN_BINANCE_NOTIONAL
        qty = notional / entry

        if signal["side"] == "LONG":
            sl = entry * (1.0 - config.SL_PCT)
            tp = entry * (1.0 + config.TP_PCT)
        else:
            sl = entry * (1.0 + config.SL_PCT)
            tp = entry * (1.0 - config.TP_PCT)

        payoff = config.TP_PCT / config.SL_PCT
        if payoff < config.PAYOFF_MIN:
            logger.warning(
                "payoff %.2f < minimum %.2f — rejecting", payoff, config.PAYOFF_MIN
            )
            return None

        order = {
            **signal,
            "sl":       sl,
            "tp":       tp,
            "qty":      qty,
            "notional": notional,
            "atr":      atr,
            "payoff":   payoff,
        }
        logger.info(
            "order built: %s entry=%.2f sl=%.2f tp=%.2f qty=%.6f notional=%.2f atr=%.4f payoff=%.2f",
            signal["side"], entry, sl, tp, qty, notional, atr, payoff,
        )
        return order
