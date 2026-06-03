import logging
from collections import deque
from typing import Optional

import pandas as pd
try:
    import pandas_ta as ta
except ImportError:
    import pandas_ta_classic as ta

import config

logger = logging.getLogger(__name__)


class Indicators:
    def __init__(self) -> None:
        self._h1: deque[dict] = deque(maxlen=config.H1_BUFFER_SIZE)
        self._m1: deque[dict] = deque(maxlen=config.M1_BUFFER_SIZE)
        # Track last seen open_time to deduplicate on WebSocket reconnect
        self._last_h1_time: Optional[int] = None
        self._last_m1_time: Optional[int] = None

    # ------------------------------------------------------------------
    # Bootstrap loader
    # ------------------------------------------------------------------

    def load_bootstrap(self, h1_candles: list[dict], m1_candles: list[dict]) -> None:
        for c in h1_candles:
            self._h1.append(c)
        for c in m1_candles:
            self._m1.append(c)
        if self._h1:
            self._last_h1_time = self._h1[-1]["open_time"]
        if self._m1:
            self._last_m1_time = self._m1[-1]["open_time"]
        logger.info(
            "indicators bootstrap: %d×1h  %d×1m candles",
            len(self._h1),
            len(self._m1),
        )

    # ------------------------------------------------------------------
    # Inbound candle updates
    # ------------------------------------------------------------------

    def update_1h(self, candle: dict) -> None:
        if candle["is_closed"]:
            if self._last_h1_time != candle["open_time"]:
                self._h1.append(candle)
                self._last_h1_time = candle["open_time"]
        else:
            # Update the in-progress candle so EMA stays current
            if self._h1 and self._h1[-1]["open_time"] == candle["open_time"]:
                self._h1[-1] = candle

    def update_1m(self, candle: dict) -> None:
        if candle["is_closed"]:
            if self._last_m1_time != candle["open_time"]:
                self._m1.append(candle)
                self._last_m1_time = candle["open_time"]
        else:
            if self._m1 and self._m1[-1]["open_time"] == candle["open_time"]:
                self._m1[-1] = candle

    # ------------------------------------------------------------------
    # Indicator calculations
    # ------------------------------------------------------------------

    def ema200_1h(self) -> Optional[float]:
        if len(self._h1) < 200:
            return None
        series = pd.Series([c["close"] for c in self._h1], dtype=float)
        ema = ta.ema(series, length=200)
        if ema is None or ema.empty:
            return None
        val = ema.iloc[-1]
        return float(val) if pd.notna(val) else None

    def ema50_1h(self) -> Optional[float]:
        if len(self._h1) < 50:
            return None
        series = pd.Series([c["close"] for c in self._h1], dtype=float)
        ema = ta.ema(series, length=50)
        if ema is None or ema.empty:
            return None
        val = ema.iloc[-1]
        return float(val) if pd.notna(val) else None

    def rsi14_1m(self) -> Optional[tuple[float, float]]:
        """Return (rsi_prev, rsi_curr) from the last two closed 1m candles."""
        if len(self._m1) < config.RSI_PERIOD + 2:
            return None
        series = pd.Series([c["close"] for c in self._m1], dtype=float)
        rsi = ta.rsi(series, length=config.RSI_PERIOD)
        if rsi is None:
            return None
        valid = rsi.dropna()
        if len(valid) < 2:
            return None
        return float(valid.iloc[-2]), float(valid.iloc[-1])

    def atr14_1m(self) -> Optional[float]:
        if len(self._m1) < config.ATR_PERIOD + 1:
            return None
        df = pd.DataFrame(list(self._m1), columns=["open_time","open","high","low","close","volume","close_time","is_closed"])
        atr = ta.atr(df["high"], df["low"], df["close"], length=config.ATR_PERIOD)
        if atr is None or atr.empty:
            return None
        val = atr.iloc[-1]
        return float(val) if pd.notna(val) else None

    def last_close_1m(self) -> Optional[float]:
        return self._m1[-1]["close"] if self._m1 else None
