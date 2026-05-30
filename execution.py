import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

import config

logger = logging.getLogger(__name__)


@dataclass
class Position:
    side: str
    entry_price: float
    sl: float
    tp: float
    qty: float
    fee_entry: float
    ema200_at_entry: float
    rsi_at_entry: float
    atr_at_entry: float
    open_time: float = field(default_factory=time.time)


class PaperBroker:
    def __init__(self) -> None:
        self.balance: float = config.INITIAL_CAPITAL
        self.position: Optional[Position] = None
        self._bid: Optional[float] = None
        self._ask: Optional[float] = None

    # ------------------------------------------------------------------
    # Market data feed
    # ------------------------------------------------------------------

    def update_book(self, data: dict) -> None:
        """Ingest a bookTicker WebSocket message."""
        self._bid = float(data["b"])
        self._ask = float(data["a"])

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def open_position(self, order: dict) -> Optional[Position]:
        if self.position is not None:
            logger.warning("open_position called while position already open — skipped")
            return None

        entry = order["entry_price"]
        qty = order["qty"]  # sized by RiskEngine (notional / entry)
        fee_entry = entry * qty * config.TAKER_FEE

        self.position = Position(
            side=order["side"],
            entry_price=entry,
            sl=order["sl"],
            tp=order["tp"],
            qty=qty,
            fee_entry=fee_entry,
            ema200_at_entry=order.get("ema200", 0.0),
            rsi_at_entry=order.get("rsi_curr", 0.0),
            atr_at_entry=order.get("atr", 0.0),
        )
        logger.info(
            "OPEN %s entry=%.2f sl=%.2f tp=%.2f qty=%.6f fee=%.4f balance=%.2f",
            order["side"], entry, order["sl"], order["tp"], qty, fee_entry, self.balance,
        )
        return self.position

    def close_position(self, exit_price: float, reason: str) -> Optional[dict]:
        if self.position is None:
            return None

        pos = self.position
        fee_exit = exit_price * pos.qty * config.TAKER_FEE
        total_fees = pos.fee_entry + fee_exit

        if pos.side == "LONG":
            gross_pnl = (exit_price - pos.entry_price) * pos.qty
        else:
            gross_pnl = (pos.entry_price - exit_price) * pos.qty

        net_pnl = gross_pnl - total_fees
        cost_basis = pos.entry_price * pos.qty
        pnl_pct = (net_pnl / cost_basis) * 100.0 if cost_basis else 0.0

        self.balance += net_pnl
        self.position = None

        result = {
            "side":            pos.side,
            "entry_price":     pos.entry_price,
            "exit_price":      exit_price,
            "sl":              pos.sl,
            "tp":              pos.tp,
            "qty":             pos.qty,
            "pnl_usd":        round(net_pnl, 6),
            "pnl_pct":        round(pnl_pct, 4),
            "exit_reason":    reason,
            "ema200_at_entry": pos.ema200_at_entry,
            "rsi_at_entry":   pos.rsi_at_entry,
            "atr_at_entry":   pos.atr_at_entry,
            "open_time":      pos.open_time,
        }
        logger.info(
            "CLOSE %s exit=%.2f reason=%s pnl=%.4f USD (%.2f%%) balance=%.2f",
            pos.side, exit_price, reason, net_pnl, pnl_pct, self.balance,
        )
        return result

    # ------------------------------------------------------------------
    # Real-time SL/TP check — called on every bookTicker tick
    # ------------------------------------------------------------------

    def close_at_market(self, reason: str = "MANUAL") -> Optional[dict]:
        """Close the open position at the current best bid/ask. Used by /cerrar."""
        if self.position is None:
            return None
        if self.position.side == "LONG":
            price = self._bid if self._bid is not None else self.position.entry_price
        else:
            price = self._ask if self._ask is not None else self.position.entry_price
        return self.close_position(price, reason)

    def save_state(self, path: str) -> None:
        if self.position is None:
            return
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(asdict(self.position), fh)

    def restore_state(self, path: str) -> Optional[Position]:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.position = Position(**data)
            return self.position
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.warning("restore_state failed: %r", exc)
            return None

    def check_sl_tp(self) -> Optional[tuple[float, str]]:
        """
        Returns (exit_price, reason) if SL or TP is triggered, else None.

        LONGs exit at bid (we sell into the bid).
        SHORTs exit at ask (we buy back at the ask).
        """
        if self.position is None or self._bid is None or self._ask is None:
            return None

        pos = self.position

        if pos.side == "LONG":
            price = self._bid
            if price <= pos.sl:
                return price, "SL"
            if price >= pos.tp:
                return price, "TP"
        else:
            price = self._ask
            if price >= pos.sl:
                return price, "SL"
            if price <= pos.tp:
                return price, "TP"

        return None
