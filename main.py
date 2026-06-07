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
import os
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

import aiohttp

import sys
sys.path.insert(0, "/opt/bots/shared")

import config
import telegram as tg
from data_feed import DataFeed
from execution import LiveBroker, PaperBroker
from indicators import Indicators
from risk_engine import RiskEngine
from signal_engine import SignalEngine
from trade_journal import TradeJournal

class _ChileFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        from datetime import datetime
        dt = datetime.fromtimestamp(record.created, tz=config.TZ_LOCAL)
        return dt.strftime(datefmt or "%Y-%m-%dT%H:%M:%S%z")


_handler = logging.StreamHandler()
_handler.setFormatter(_ChileFormatter(
    fmt="%(asctime)s %(levelname)-8s %(name)-20s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
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
        self._state_file = config.STATE_FILE
        self._cooldown_file = config.STATE_FILE.replace("state.json", "cooldown.json")
        self._total_balance: float = 0.0  # raw Binance balance before division

        self.indicators = Indicators()
        self.broker = PaperBroker() if config.PAPER_MODE else LiveBroker()
        self.signal_engine = SignalEngine(self.indicators)
        self.risk_engine = RiskEngine(self.indicators)
        self.journal = TradeJournal()
        self.stats = Stats(initial_balance=0.0)
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

    def _hydrate_position_from_state(self) -> None:
        restored = self.broker.restore_state(self._state_file)
        if restored is not None:
            self._transition(State.IDLE, State.IN_POSITION, reason="STATE_RESTORE")
            logger.info(
                "position restored: %s entry=%.2f sl=%.2f tp=%.2f",
                restored.side, restored.entry_price, restored.sl, restored.tp,
            )

    def _save_cooldown(self) -> None:
        expiry_wall = time.time() + (self._cooldown_until - time.monotonic())
        try:
            with open(self._cooldown_file, "w", encoding="utf-8") as fh:
                json.dump({"cooldown_until": expiry_wall}, fh)
        except Exception as exc:
            logger.warning("_save_cooldown failed: %r", exc)

    def _load_cooldown(self) -> None:
        try:
            with open(self._cooldown_file, encoding="utf-8") as fh:
                data = json.load(fh)
            remaining = data.get("cooldown_until", 0.0) - time.time()
            if remaining > 0:
                self._cooldown_until = time.monotonic() + remaining
                logger.info("cooldown restored: %.0f s remaining", remaining)
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("_load_cooldown failed: %r", exc)

    # ------------------------------------------------------------------
    # Shared bot state helpers
    # ------------------------------------------------------------------

    def _count_active_bots(self) -> int:
        """Count running bot instances from shared state file (heartbeat < 120s)."""
        try:
            with open(config.BOT_STATES_FILE, encoding="utf-8") as fh:
                states = json.load(fh)
            now = time.time()
            return max(
                sum(1 for v in states.values() if now - v.get("heartbeat", 0) < 120),
                1,
            )
        except Exception:
            return 1

    def _write_bot_state(self) -> None:
        """Write current FSM state to shared bot_states.json atomically."""
        try:
            try:
                with open(config.BOT_STATES_FILE, encoding="utf-8") as fh:
                    states = json.load(fh)
            except Exception:
                states = {}

            pos = self.broker.position
            price  = self.indicators.last_close_1m()
            ema200 = self.indicators.ema200_1h()
            ema50  = self.indicators.ema50_1h()
            rsi_pair = self.indicators.rsi14_1m()
            rsi    = rsi_pair[1] if rsi_pair else None
            regime = None
            if price is not None and ema200 is not None:
                regime = "BULL" if price > ema200 else "BEAR"

            unrealized_pnl = None
            if pos is not None and price is not None:
                gross = (
                    (price - pos.entry_price) * pos.qty if pos.side == "LONG"
                    else (pos.entry_price - price) * pos.qty
                )
                fee_exit = price * pos.qty * config.TAKER_FEE
                unrealized_pnl = round(gross - fee_exit, 4)

            trailing_active = self.broker._trailing_active
            trailing_max = self.broker._trailing_max if trailing_active else None
            raw_min = self.broker._trailing_min
            trailing_min = raw_min if (trailing_active and raw_min != float("inf")) else None

            states[config.BOT_NAME] = {
                "state":             self.state.name,
                "side":              pos.side if pos else None,
                "entry_price":       pos.entry_price if pos else None,
                "sl":                pos.sl if pos else None,
                "tp":                pos.tp if pos else None,
                "qty":               pos.qty if pos else None,
                "heartbeat":         time.time(),
                "allocated_balance": self.broker.balance,
                "total_balance":     self._total_balance,
                "price":             price,
                "ema200":            ema200,
                "ema50":             ema50,
                "rsi":               rsi,
                "regime":            regime,
                "trailing_active":   trailing_active,
                "trailing_max":      trailing_max,
                "trailing_min":      trailing_min,
                "unrealized_pnl":    unrealized_pnl,
            }

            dir_path = os.path.dirname(config.BOT_STATES_FILE) or "."
            with tempfile.NamedTemporaryFile(
                mode="w", dir=dir_path, delete=False, suffix=".tmp", encoding="utf-8"
            ) as tmp:
                json.dump(states, tmp)
                tmp_path = tmp.name
            os.replace(tmp_path, config.BOT_STATES_FILE)
        except Exception as exc:
            logger.warning("_write_bot_state failed: %r", exc)

    def _circuit_breaker_enabled(self) -> bool:
        """Return True when trading is allowed (circuit breaker not active)."""
        try:
            with open(config.CB_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            return bool(data.get("trading_enabled", True))
        except FileNotFoundError:
            return True
        except Exception as exc:
            logger.warning("circuit breaker check failed: %r", exc)
            return True

    async def _refresh_balance(self) -> None:
        real = await fetch_futures_usdt_balance()
        if real is not None:
            self._total_balance = real
            active = self._count_active_bots()
            allocated = real / active
            self.broker.balance = allocated
            logger.info(
                "balance: total=%.2f active_bots=%d allocated=%.2f USDT",
                real, active, allocated,
            )
        else:
            logger.warning(
                "balance refresh unavailable — using current %.2f", self.broker.balance
            )
        self._write_bot_state()

    async def run(self) -> None:
        logger.info(
            "starting | symbol=%s paper_mode=%s",
            config.SYMBOL, config.PAPER_MODE,
        )
        h1, m1 = await self.feed.bootstrap()
        self.indicators.load_bootstrap(h1, m1)

        await self._refresh_balance()
        self._hydrate_position_from_state()
        self._load_cooldown()
        self.stats.initial_balance = self.broker.balance
        logger.info("balance=%.2f | entering event loop", self.broker.balance)

        await tg.tg_send(
            f"🚀 *Bot iniciado*\n"
            f"Symbol: `{config.SYMBOL}` | Paper: `{config.PAPER_MODE}`\n"
            f"Capital: `${self.broker.balance:.2f}`"
        )

        if config.TG_POLLING:
            asyncio.create_task(tg.tg_poll(self), name="tg_poll")
            asyncio.create_task(tg.heartbeat(self), name="tg_heartbeat")
        asyncio.create_task(self._log_periodic(), name="log_periodic")
        asyncio.create_task(self._poll_close_signal(), name="poll_close_signal")

        await self.feed.run()

    # ------------------------------------------------------------------
    # Public: manual close (used by /cerrar)
    # ------------------------------------------------------------------

    async def manual_close(self) -> Optional[dict]:
        if self.broker.position is None:
            return None
        trade = self.broker.close_at_market("MANUAL_CERRAR")
        if trade:
            try:
                os.unlink(self._state_file)
            except FileNotFoundError:
                pass
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

        if reason == "TRAILING_ACTIVATED":
            self.broker.on_trailing_activated()
            pos = self.broker.position
            await tg.tg_send(
                f"🎯 *Trailing Stop activado*\n"
                f"`{pos.side}` | entry: `{pos.entry_price:.2f}` | TP @ `{exit_price:.2f}`\n"
                f"Trailing: `{config.TRAILING_STOP_PCT * 100:.1f}%` desde el extremo"
            )
            return

        trade = self.broker.close_position(exit_price, reason)
        if trade is None:
            return

        try:
            os.unlink(self._state_file)
        except FileNotFoundError:
            pass

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

        self._cooldown_until = time.monotonic() + config.COOLDOWN_SECONDS
        logger.info("cooldown %.0f s after close", config.COOLDOWN_SECONDS)
        self._save_cooldown()

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

        if not self._circuit_breaker_enabled():
            logger.debug("circuit breaker active — skipping signal evaluation")
            return

        signal = self.signal_engine.evaluate()
        if signal is None:
            return

        if config.SIDE_FILTER == "SHORT" and signal["side"] == "LONG":
            logger.debug("SIDE_FILTER=SHORT — ignoring LONG signal")
            return
        if config.SIDE_FILTER == "LONG" and signal["side"] == "SHORT":
            logger.debug("SIDE_FILTER=LONG — ignoring SHORT signal")
            return

        await self._refresh_balance()
        order = self.risk_engine.build_order(signal, self.broker.balance)
        if order is None:
            return

        position = self.broker.open_position(order)
        if position is not None:
            self.broker.save_state(self._state_file)
            self._transition(State.IDLE, State.IN_POSITION, reason=order["side"])
            emoji = "📗" if order["side"] == "LONG" else "📕"
            await tg.tg_send(
                f"{emoji} *TRADE ABIERTO* — `{order['side']}`\n"
                f"@ `{order['entry_price']:.2f}` | SL: `{order['sl']:.2f}` | TP: `{order['tp']:.2f}`\n"
                f"Notional: `${order['notional']:.2f}` | Qty: `{order['qty']:.6f}`"
            )

    async def _poll_close_signal(self) -> None:
        """Check /opt/bots/close_signal_{bot}.json every 5 s and close if found."""
        signal_path = os.path.join("/opt/bots", f"close_signal_{config.BOT_NAME}.json")
        while True:
            await asyncio.sleep(5)
            if not os.path.exists(signal_path):
                continue
            try:
                os.unlink(signal_path)
            except FileNotFoundError:
                continue
            except Exception as exc:
                logger.warning("_poll_close_signal unlink error: %r", exc)
                continue
            logger.info("close signal received for %s", config.BOT_NAME)
            if self.state is State.IN_POSITION:
                trade = await self.manual_close()
                if trade is not None:
                    await tg.tg_send(
                        f"🚨 *Cierre por señal remota* — `{config.BOT_NAME}`\n"
                        f"PnL: `${trade['pnl_usd']:+.4f}` ({trade['pnl_pct']:+.2f}%)\n"
                        f"Balance: `${self.broker.balance:.2f}`"
                    )
                else:
                    await tg.tg_send(
                        f"ℹ️ Señal de cierre recibida (`{config.BOT_NAME}`) — sin posición abierta."
                    )
            else:
                await tg.tg_send(
                    f"ℹ️ Señal de cierre recibida (`{config.BOT_NAME}`) — bot en IDLE."
                )

    async def _log_periodic(self) -> None:
        """Log key indicators every 60 s and refresh shared bot state heartbeat."""
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
                self._write_bot_state()
            except Exception as exc:
                logger.warning("_log_periodic error: %r", exc)

    def _transition(self, from_state: State, to_state: State, *, reason: str = "") -> None:
        logger.info(
            "FSM %s → %s  [%s]  balance=%.2f",
            from_state.name, to_state.name, reason, self.broker.balance,
        )
        self.state = to_state
        self._write_bot_state()


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    bot = ScalpingBot()
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
