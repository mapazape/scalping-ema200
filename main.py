"""
Scalping bot — FSM: IDLE ↔ IN_POSITION
Connects data_feed → indicators → signal_engine → risk_engine → execution → trade_journal.
"""
import asyncio
import glob
import hashlib
import hmac
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

import aiohttp

import config
import telegram as tg
from data_feed import DataFeed
from execution import PaperBroker
from indicators import Indicators
from risk_engine import RiskEngine
from signal_engine import SignalEngine
from trade_journal import TradeJournal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)-20s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("main")


# ------------------------------------------------------------------
# Binance Futures balance helper
# ------------------------------------------------------------------

async def fetch_futures_usdt_balance() -> Optional[float]:
    """Return available USDT balance from Binance Futures, or None on failure."""
    if not config.BINANCE_API_KEY or not config.BINANCE_API_SECRET:
        return None
    ts = int(time.time() * 1000)
    qs = f"timestamp={ts}"
    sig = hmac.new(
        config.BINANCE_API_SECRET.encode(),
        qs.encode(),
        hashlib.sha256,
    ).hexdigest()
    url = f"{config.BINANCE_FUTURES_REST_BASE}/fapi/v2/balance?{qs}&signature={sig}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"X-MBX-APIKEY": config.BINANCE_API_KEY},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning("fetch_futures_usdt_balance HTTP %s", resp.status)
                    return None
                data = await resp.json()
                for asset in data:
                    if asset.get("asset") == "USDT":
                        return float(asset["availableBalance"])
                logger.warning("fetch_futures_usdt_balance: USDT not found in response")
    except Exception as exc:
        logger.warning("fetch_futures_usdt_balance failed: %r", exc)
    return None


# ------------------------------------------------------------------
# Statistics tracker
# ------------------------------------------------------------------

@dataclass
class Stats:
    initial_balance: float
    _trades: list[dict] = field(default_factory=list)

    def record(self, trade: dict) -> None:
        self._trades.append(trade)

    @property
    def count(self) -> int:
        return len(self._trades)

    @property
    def total_pnl(self) -> float:
        return sum(t["pnl_usd"] for t in self._trades)

    @property
    def win_rate(self) -> float:
        if not self._trades:
            return 0.0
        return sum(1 for t in self._trades if t["pnl_usd"] > 0) / len(self._trades) * 100.0

    @property
    def avg_win(self) -> float:
        wins = [t["pnl_usd"] for t in self._trades if t["pnl_usd"] > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [abs(t["pnl_usd"]) for t in self._trades if t["pnl_usd"] < 0]
        return sum(losses) / len(losses) if losses else 0.0

    @property
    def payoff_ratio(self) -> float:
        return self.avg_win / self.avg_loss if self.avg_loss > 0 else 0.0

    @property
    def exit_reasons(self) -> dict[str, int]:
        return dict(Counter(t["exit_reason"] for t in self._trades))

    @property
    def streak(self) -> tuple[int, str]:
        if not self._trades:
            return 0, "N/A"
        is_win = self._trades[-1]["pnl_usd"] > 0
        count = 0
        for t in reversed(self._trades):
            if (t["pnl_usd"] > 0) == is_win:
                count += 1
            else:
                break
        return count, "win" if is_win else "loss"


# ------------------------------------------------------------------
# FSM
# ------------------------------------------------------------------

class State(Enum):
    IDLE = auto()
    IN_POSITION = auto()


class ScalpingBot:
    def __init__(self) -> None:
        self.state: State = State.IDLE
        self._cooldown_until: float = 0.0
        self._paused: bool = False

        self.indicators = Indicators()
        self.broker = PaperBroker()
        self.signal_engine = SignalEngine(self.indicators)
        self.risk_engine = RiskEngine(self.indicators)
        self.journal = TradeJournal()
        self.stats = Stats(initial_balance=config.INITIAL_CAPITAL)
        self._hydrate_stats_from_journal()

        self.feed = DataFeed(
            on_kline_1m=self._on_kline_1m,
            on_kline_1h=self._on_kline_1h,
            on_book_ticker=self._on_book_ticker,
        )

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _hydrate_stats_from_journal(self) -> None:
        paths = sorted(glob.glob(f"{self.journal.journal_dir}/*.jsonl"))
        count = 0
        for path in paths:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("pnl_usd") is not None:
                            self.stats.record(entry)
                            count += 1
                    except json.JSONDecodeError:
                        logger.warning("journal: malformed line in %s", path)
        if count:
            logger.info("hydrated stats from journal: %d trades loaded", count)

    async def _refresh_balance(self) -> None:
        real = await fetch_futures_usdt_balance()
        if real is not None:
            self.broker.balance = real
            logger.info("balance refreshed from Binance Futures: %.2f USDT", real)
        else:
            logger.warning(
                "balance refresh unavailable — using current %.2f", self.broker.balance
            )

    async def run(self) -> None:
        logger.info(
            "starting | symbol=%s paper_mode=%s capital=%.2f",
            config.SYMBOL, config.PAPER_MODE, config.INITIAL_CAPITAL,
        )
        h1, m1 = await self.feed.bootstrap()
        self.indicators.load_bootstrap(h1, m1)

        await self._refresh_balance()
        self.stats.initial_balance = self.broker.balance
        logger.info("balance=%.2f | entering event loop", self.broker.balance)

        await tg.tg_send(
            f"🚀 *Bot iniciado*\n"
            f"Symbol: `{config.SYMBOL}` | Paper: `{config.PAPER_MODE}`\n"
            f"Capital: `${self.broker.balance:.2f}`"
        )

        asyncio.create_task(tg.tg_poll(self), name="tg_poll")
        asyncio.create_task(tg.heartbeat(self), name="tg_heartbeat")
        asyncio.create_task(self._log_periodic(), name="log_periodic")

        await self.feed.run()

    # ------------------------------------------------------------------
    # Public: manual close (used by /cerrar)
    # ------------------------------------------------------------------

    async def manual_close(self) -> Optional[dict]:
        if self.broker.position is None:
            return None
        trade = self.broker.close_at_market("MANUAL_CERRAR")
        if trade:
            self.stats.record(trade)
            self.journal.record(trade)
            await self._refresh_balance()
            self._transition(self.state, State.IDLE, reason="MANUAL_CERRAR")
        return trade

    # ------------------------------------------------------------------
    # WebSocket callbacks
    # ------------------------------------------------------------------

    async def _on_kline_1h(self, candle: dict) -> None:
        self.indicators.update_1h(candle)

    async def _on_kline_1m(self, candle: dict) -> None:
        self.indicators.update_1m(candle)
        if candle["is_closed"]:
            await self._on_closed_1m()

    async def _on_book_ticker(self, data: dict) -> None:
        self.broker.update_book(data)

        if self.state is not State.IN_POSITION:
            return

        trigger = self.broker.check_sl_tp()
        if trigger is None:
            return

        exit_price, reason = trigger
        trade = self.broker.close_position(exit_price, reason)
        if trade is None:
            return

        self.stats.record(trade)
        self.journal.record(trade)
        await self._refresh_balance()

        emoji = "✅" if trade["pnl_usd"] > 0 else "❌"
        await tg.tg_send(
            f"{emoji} *TRADE CERRADO* — `{reason}`\n"
            f"`{trade['side']}` | entry: `{trade['entry_price']:.2f}` → exit: `{exit_price:.2f}`\n"
            f"PnL: `${trade['pnl_usd']:+.4f}` ({trade['pnl_pct']:+.2f}%)\n"
            f"Balance: `${self.broker.balance:.2f}`"
        )

        if trade["pnl_usd"] < 0:
            self._cooldown_until = time.monotonic() + config.COOLDOWN_SECONDS
            logger.info("loss — cooldown %.0f s", config.COOLDOWN_SECONDS)

        self._transition(State.IN_POSITION, State.IDLE, reason=f"exit:{reason}")

    # ------------------------------------------------------------------
    # FSM core
    # ------------------------------------------------------------------

    async def _on_closed_1m(self) -> None:
        if self.state is not State.IDLE:
            return

        if self._paused:
            logger.debug("bot paused — skipping signal evaluation")
            return

        remaining = self._cooldown_until - time.monotonic()
        if remaining > 0:
            logger.debug("cooldown %.0f s remaining", remaining)
            return

        signal = self.signal_engine.evaluate()
        if signal is None:
            return

        order = self.risk_engine.build_order(signal, self.broker.balance)
        if order is None:
            return

        position = self.broker.open_position(order)
        if position is not None:
            self._transition(State.IDLE, State.IN_POSITION, reason=order["side"])
            emoji = "📗" if order["side"] == "LONG" else "📕"
            await tg.tg_send(
                f"{emoji} *TRADE ABIERTO* — `{order['side']}`\n"
                f"@ `{order['entry_price']:.2f}` | SL: `{order['sl']:.2f}` | TP: `{order['tp']:.2f}`\n"
                f"Notional: `${order['notional']:.2f}` | Qty: `{order['qty']:.6f}`"
            )

    async def _log_periodic(self) -> None:
        """Log key indicators every 60 s."""
        while True:
            await asyncio.sleep(60)
            try:
                price = self.indicators.last_close_1m()
                ema   = self.indicators.ema200_1h()
                rsi_pair = self.indicators.rsi14_1m()
                rsi   = rsi_pair[1] if rsi_pair else None
                if price is not None and ema is not None:
                    regime = "BULL" if price > ema else "BEAR"
                else:
                    regime = "N/A"
                logger.info(
                    "heartbeat | price=%.2f rsi=%s ema200=%s regime=%s fsm=%s",
                    price if price is not None else 0.0,
                    f"{rsi:.2f}" if rsi is not None else "N/A",
                    f"{ema:.2f}" if ema is not None else "N/A",
                    regime,
                    self.state.name,
                )
            except Exception as exc:
                logger.warning("_log_periodic error: %r", exc)

    def _transition(self, from_state: State, to_state: State, *, reason: str = "") -> None:
        logger.info(
            "FSM %s → %s  [%s]  balance=%.2f",
            from_state.name, to_state.name, reason, self.broker.balance,
        )
        self.state = to_state


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    bot = ScalpingBot()
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
