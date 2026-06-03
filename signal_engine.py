import logging
from typing import Optional

import config
from indicators import Indicators

logger = logging.getLogger(__name__)


class SignalEngine:
    def __init__(self, indicators: Indicators) -> None:
        self.ind = indicators

    def evaluate(self) -> Optional[dict]:
        """
        Check for a scalping signal on the just-closed 1m candle.

        LONG  — price > EMA50(1h) AND price > EMA200(1h) + RSI < 35
        SHORT — price < EMA50(1h) AND price < EMA200(1h) + RSI > 65

        Returns a partial order dict or None.
        """
        ema200 = self.ind.ema200_1h()
        ema50 = self.ind.ema50_1h()
        rsi_pair = self.ind.rsi14_1m()
        close = self.ind.last_close_1m()

        if ema200 is None or ema50 is None or rsi_pair is None or close is None:
            return None

        _, rsi_curr = rsi_pair

        long_signal = (
            close > ema50
            and close > ema200
            and rsi_curr < config.RSI_OVERSOLD
        )
        short_signal = (
            close < ema50
            and close < ema200
            and rsi_curr > config.RSI_OVERBOUGHT
        )

        if long_signal:
            logger.info(
                "LONG signal | close=%.2f ema50=%.2f ema200=%.2f rsi=%.2f",
                close, ema50, ema200, rsi_curr,
            )
            return {
                "side": "LONG",
                "entry_price": close,
                "ema50": ema50,
                "ema200": ema200,
                "rsi_curr": rsi_curr,
            }

        if short_signal:
            logger.info(
                "SHORT signal | close=%.2f ema50=%.2f ema200=%.2f rsi=%.2f",
                close, ema50, ema200, rsi_curr,
            )
            return {
                "side": "SHORT",
                "entry_price": close,
                "ema50": ema50,
                "ema200": ema200,
                "rsi_curr": rsi_curr,
            }

        return None
