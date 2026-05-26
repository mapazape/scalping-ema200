import asyncio
import json
import logging
from typing import Awaitable, Callable

import aiohttp
import websockets

import config

logger = logging.getLogger(__name__)

KlineCallback = Callable[[dict], Awaitable[None]]
BookCallback = Callable[[dict], Awaitable[None]]


def _parse_rest_candle(c: list) -> dict:
    return {
        "open_time": int(c[0]),
        "open":      float(c[1]),
        "high":      float(c[2]),
        "low":       float(c[3]),
        "close":     float(c[4]),
        "volume":    float(c[5]),
        "close_time": int(c[6]),
        "is_closed": True,
    }


def _parse_ws_candle(k: dict) -> dict:
    return {
        "open_time":  int(k["t"]),
        "open":       float(k["o"]),
        "high":       float(k["h"]),
        "low":        float(k["l"]),
        "close":      float(k["c"]),
        "volume":     float(k["v"]),
        "close_time": int(k["T"]),
        "is_closed":  bool(k["x"]),
    }


class DataFeed:
    def __init__(
        self,
        on_kline_1m: KlineCallback,
        on_kline_1h: KlineCallback,
        on_book_ticker: BookCallback,
    ):
        self.on_kline_1m = on_kline_1m
        self.on_kline_1h = on_kline_1h
        self.on_book_ticker = on_book_ticker

        sym = config.SYMBOL.lower()
        streams = f"{sym}@kline_1m/{sym}@kline_1h/{sym}@bookTicker"
        self._ws_url = f"{config.BINANCE_WS_BASE}?streams={streams}"

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    async def bootstrap(self) -> tuple[list[dict], list[dict]]:
        """Fetch historical OHLCV via REST before subscribing to WebSocket."""
        async with aiohttp.ClientSession() as session:
            h1 = await self._fetch_klines(session, "1h", config.H1_BOOTSTRAP_LIMIT)
            m1 = await self._fetch_klines(session, "1m", config.M1_BOOTSTRAP_LIMIT)
        logger.info("bootstrap: %d×1h  %d×1m candles loaded", len(h1), len(m1))
        return h1, m1

    async def _fetch_klines(
        self, session: aiohttp.ClientSession, interval: str, limit: int
    ) -> list[dict]:
        url = f"{config.BINANCE_REST_BASE}/klines"
        params = {"symbol": config.SYMBOL, "interval": interval, "limit": limit}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            raw = await resp.json()
        return [_parse_rest_candle(c) for c in raw]

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect and reconnect forever."""
        while True:
            try:
                await self._connect()
            except Exception as exc:
                logger.error("ws error: %s — reconnecting in 5 s", exc)
                await asyncio.sleep(5)

    async def _connect(self) -> None:
        logger.info("connecting: %s", self._ws_url)
        async with websockets.connect(
            self._ws_url,
            ping_interval=20,
            ping_timeout=10,
            max_size=2**20,
        ) as ws:
            logger.info("websocket connected")
            async for raw in ws:
                await self._dispatch(raw)

    async def _dispatch(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
            stream: str = msg.get("stream", "")
            data: dict = msg.get("data", {})

            if "@kline_1m" in stream:
                await self.on_kline_1m(_parse_ws_candle(data["k"]))
            elif "@kline_1h" in stream:
                await self.on_kline_1h(_parse_ws_candle(data["k"]))
            elif "@bookTicker" in stream:
                await self.on_book_ticker(data)
        except Exception as exc:
            logger.error("dispatch error: %s", exc)
