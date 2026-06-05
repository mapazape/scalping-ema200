import json
import logging
import os
import tempfile
import time
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

        ema200_dist = round((close - ema200) / ema200 * 100, 4)

        if long_signal:
            logger.info(
                "LONG signal | close=%.2f ema50=%.2f ema200=%.2f rsi=%.2f ema200_dist=%.4f%%",
                close, ema50, ema200, rsi_curr, ema200_dist,
            )
            signal = {
                "side": "LONG",
                "entry_price": close,
                "ema50": ema50,
                "ema200": ema200,
                "rsi_curr": rsi_curr,
                "ema200_dist": ema200_dist,
            }
            self._write_pending(signal)
            return signal

        if short_signal:
            logger.info(
                "SHORT signal | close=%.2f ema50=%.2f ema200=%.2f rsi=%.2f ema200_dist=%.4f%%",
                close, ema50, ema200, rsi_curr, ema200_dist,
            )
            signal = {
                "side": "SHORT",
                "entry_price": close,
                "ema50": ema50,
                "ema200": ema200,
                "rsi_curr": rsi_curr,
                "ema200_dist": ema200_dist,
            }
            self._write_pending(signal)
            return signal

        return None

    def _write_pending(self, signal: dict) -> None:
        try:
            try:
                with open(config.CB_FILE, encoding="utf-8") as f:
                    cb = json.load(f)
            except Exception:
                cb = {"trading_enabled": True}
            cb["fsm_state"] = "PENDING_VALIDATION"
            cb["bot_id"] = config.BOT_ID
            cb["signal"] = signal
            cb["pending_at"] = time.time()
            dir_path = os.path.dirname(config.CB_FILE) or "."
            with tempfile.NamedTemporaryFile(
                mode="w", dir=dir_path, delete=False, suffix=".tmp", encoding="utf-8"
            ) as tmp:
                json.dump(cb, tmp)
                tmp_path = tmp.name
            os.replace(tmp_path, config.CB_FILE)
            logger.info("circuit_breaker: PENDING_VALIDATION bot=%s", config.BOT_ID)
        except Exception as exc:
            logger.warning("_write_pending failed: %r", exc)
