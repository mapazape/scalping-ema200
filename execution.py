import decimal
import hashlib
import hmac
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)


def _wait_hermes_verdict(state_path: str, timeout: float = 120.0) -> bool:
    """Poll circuit_breaker.json until Hermes writes a verdict. Fail-safe: REJECTED."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(state_path, encoding="utf-8") as f:
                data = json.load(f)
            fsm = data.get("fsm_state", "IDLE")
            if fsm == "APPROVED_BY_HERMES":
                data["fsm_state"] = "IDLE"
                dir_path = os.path.dirname(state_path) or "."
                with tempfile.NamedTemporaryFile(
                    mode="w", dir=dir_path, delete=False, suffix=".tmp", encoding="utf-8"
                ) as tmp:
                    json.dump(data, tmp)
                    tmp_path = tmp.name
                os.replace(tmp_path, state_path)
                logger.info("Hermes: APPROVED — proceeding with order")
                return True
            elif fsm == "IDLE":
                logger.info("Hermes: REJECTED — order cancelled")
                return False
        except Exception as exc:
            logger.warning("_wait_hermes_verdict read error: %r", exc)
        time.sleep(1.0)
    logger.warning("_wait_hermes_verdict timeout %.1fs — fail-safe REJECTED", timeout)
    try:
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
        data["fsm_state"] = "IDLE"
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass
    return False


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
        self.balance: float = 0.0
        self.position: Optional[Position] = None
        self._bid: Optional[float] = None
        self._ask: Optional[float] = None
        self._trailing_active: bool = False
        self._trailing_max: float = 0.0
        self._trailing_min: float = float("inf")

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

        if not _wait_hermes_verdict(config.CB_FILE, timeout=120.0):
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
        self._trailing_active = False
        self._trailing_max = 0.0
        self._trailing_min = float("inf")
        logger.info(
            "OPEN %s entry=%.2f sl=%.2f tp=%.2f qty=%.6f fee=%.4f balance=%.2f",
            order["side"], entry, order["sl"], order["tp"], qty, fee_entry, self.balance,
        )
        return self.position

    def on_trailing_activated(self) -> None:
        pass

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
        Returns (exit_price, reason) or None.

        When price first reaches TP, activates trailing and returns
        (price, "TRAILING_ACTIVATED") — caller must NOT close the position.
        While trailing is active, tracks the extreme and returns
        (price, "Trailing Stop") when the 0.5% retracement triggers.
        """
        if self.position is None or self._bid is None or self._ask is None:
            return None

        pos = self.position

        if pos.side == "LONG":
            price = self._bid
            if price <= pos.sl:
                return price, "SL"
            activation_price = pos.entry_price * (1 + config.TRAILING_ACTIVATION_PCT)
            if not self._trailing_active:
                if price >= activation_price:
                    self._trailing_active = True
                    self._trailing_max = price
                    logger.info("Trailing Stop ACTIVATED | LONG | price=%.2f", price)
                    return price, "TRAILING_ACTIVATED"
            else:
                if price > self._trailing_max:
                    self._trailing_max = price
                trail_exit = self._trailing_max * (1 - config.TRAILING_STOP_PCT)
                if price <= trail_exit:
                    logger.info(
                        "Trailing Stop TRIGGERED | LONG | price=%.2f max=%.2f exit=%.2f",
                        price, self._trailing_max, trail_exit,
                    )
                    return price, "Trailing Stop"
        else:
            price = self._ask
            if price >= pos.sl:
                return price, "SL"
            activation_price = pos.entry_price * (1 - config.TRAILING_ACTIVATION_PCT)
            if not self._trailing_active:
                if price <= activation_price:
                    self._trailing_active = True
                    self._trailing_min = price
                    logger.info("Trailing Stop ACTIVATED | SHORT | price=%.2f", price)
                    return price, "TRAILING_ACTIVATED"
            else:
                if price < self._trailing_min:
                    self._trailing_min = price
                trail_exit = self._trailing_min * (1 + config.TRAILING_STOP_PCT)
                if price >= trail_exit:
                    logger.info(
                        "Trailing Stop TRIGGERED | SHORT | price=%.2f min=%.2f exit=%.2f",
                        price, self._trailing_min, trail_exit,
                    )
                    return price, "Trailing Stop"

        return None


# ---------------------------------------------------------------------------
# Live broker — real orders via Binance Futures REST API
# ---------------------------------------------------------------------------

_FAPI_BASE = "https://fapi.binance.com"


class LiveBroker:
    """Sends real orders to Binance Futures. Same public interface as PaperBroker."""

    def __init__(self) -> None:
        self.balance: float = 0.0
        self.position: Optional[Position] = None
        self._bid: Optional[float] = None
        self._ask: Optional[float] = None
        self._trailing_active: bool = False
        self._trailing_max: float = 0.0
        self._trailing_min: float = float("inf")
        self._step_size, self._min_qty = self._fetch_lot_size()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        return {"X-MBX-APIKEY": config.BINANCE_API_KEY}

    def _sign(self, params: dict) -> dict:
        p = {**params, "timestamp": int(time.time() * 1000)}
        qs = "&".join(f"{k}={v}" for k, v in p.items())
        sig = hmac.new(
            config.BINANCE_API_SECRET.encode(),
            qs.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {**p, "signature": sig}

    def _request(self, method: str, path: str, params: dict) -> dict:
        signed = self._sign(params)
        resp = requests.request(
            method,
            f"{_FAPI_BASE}{path}",
            params=signed,
            headers=self._headers(),
            timeout=10,
        )
        data = resp.json()
        if resp.status_code != 200:
            raise RuntimeError(
                f"Binance {method} {path} → HTTP {resp.status_code}: {data}"
            )
        # Binance returns HTTP 200 for application-level errors; detect them explicitly.
        if isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] < 0:
            raise RuntimeError(
                f"Binance {method} {path} → API error {data['code']}: {data.get('msg')}"
            )
        return data

    def _fetch_lot_size(self) -> tuple[float, float]:
        try:
            resp = requests.get(f"{_FAPI_BASE}/fapi/v1/exchangeInfo", timeout=10)
            for sym in resp.json().get("symbols", []):
                if sym["symbol"] == config.SYMBOL:
                    for f in sym["filters"]:
                        if f["filterType"] == "LOT_SIZE":
                            step = float(f["stepSize"])
                            min_qty = float(f["minQty"])
                            logger.info(
                                "LOT_SIZE for %s: stepSize=%s minQty=%s",
                                config.SYMBOL, step, min_qty,
                            )
                            return step, min_qty
        except Exception as exc:
            logger.warning("_fetch_lot_size failed: %r — defaulting step=0.001 min=0.001", exc)
        return 0.001, 0.001

    def _round_qty(self, qty: float) -> float:
        step = decimal.Decimal(str(self._step_size))
        q = decimal.Decimal(str(qty))
        return float((q // step) * step)

    # ------------------------------------------------------------------
    # Market data feed
    # ------------------------------------------------------------------

    def update_book(self, data: dict) -> None:
        self._bid = float(data["b"])
        self._ask = float(data["a"])

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def open_position(self, order: dict) -> Optional[Position]:
        if self.position is not None:
            logger.warning("open_position called while position already open — skipped")
            return None

        if not _wait_hermes_verdict(config.CB_FILE, timeout=120.0):
            return None

        side_map = {"LONG": "BUY", "SHORT": "SELL"}
        qty = self._round_qty(order["qty"])
        if qty <= 0:
            qty = self._min_qty
            logger.warning(
                "open_position: qty rounded to 0 — using min_qty=%.6f", qty
            )

        # 1. MARKET entry order
        try:
            entry_resp = self._request("POST", "/fapi/v1/order", {
                "symbol":   config.SYMBOL,
                "side":     side_map[order["side"]],
                "type":     "MARKET",
                "quantity": qty,
            })
            logger.info(
                "MARKET entry: orderId=%s status=%s avgPrice=%s executedQty=%s",
                entry_resp.get("orderId"), entry_resp.get("status"),
                entry_resp.get("avgPrice"), entry_resp.get("executedQty"),
            )
        except Exception as exc:
            logger.error("open_position: entry order failed: %r", exc)
            return None

        # Poll until FILLED (up to 5 × 0.5 s) to get the real avgPrice
        fill_resp = entry_resp
        if fill_resp.get("status") != "FILLED":
            order_id = entry_resp.get("orderId")
            if order_id is None:
                logger.error(
                    "open_position: entry response has no orderId — aborting. resp=%r", entry_resp
                )
                return None
            for attempt in range(5):
                time.sleep(0.5)
                try:
                    fill_resp = self._request("GET", "/fapi/v1/order", {
                        "symbol":  config.SYMBOL,
                        "orderId": order_id,
                    })
                    if fill_resp.get("status") == "FILLED":
                        break
                except Exception as exc:
                    logger.warning("open_position: fill poll attempt %d failed: %r", attempt + 1, exc)
            else:
                logger.warning(
                    "open_position: order %s not FILLED after 2.5s — using estimated price", order_id
                )

        raw_avg = fill_resp.get("avgPrice", "0")
        entry_price = float(raw_avg) if float(raw_avg) > 0 else float(order["entry_price"])
        fee_entry = entry_price * qty * config.TAKER_FEE

        # SL/TP handled entirely in software via check_sl_tp() — no exchange conditional orders
        self.position = Position(
            side=order["side"],
            entry_price=entry_price,
            sl=order["sl"],
            tp=order["tp"],
            qty=qty,
            fee_entry=fee_entry,
            ema200_at_entry=order.get("ema200", 0.0),
            rsi_at_entry=order.get("rsi_curr", 0.0),
            atr_at_entry=order.get("atr", 0.0),
        )
        self._trailing_active = False
        self._trailing_max = 0.0
        self._trailing_min = float("inf")
        logger.info(
            "OPEN %s entry=%.2f sl=%.2f tp=%.2f qty=%.6f fee=%.4f balance=%.2f",
            order["side"], entry_price, order["sl"], order["tp"],
            qty, fee_entry, self.balance,
        )
        return self.position

    def on_trailing_activated(self) -> None:
        """No-op: trailing stop is handled entirely in software via check_sl_tp()."""
        logger.info("trailing: activated — software loop handles exit")

    def _fetch_trade_fee(self, order_id: int) -> float:
        """Return total USDT commission for a given orderId via /fapi/v1/userTrades."""
        try:
            trades = self._request("GET", "/fapi/v1/userTrades", {
                "symbol":  config.SYMBOL,
                "orderId": order_id,
            })
            return sum(
                float(t.get("commission", 0))
                for t in trades
                if t.get("commissionAsset") == "USDT"
            )
        except Exception as exc:
            logger.warning("_fetch_trade_fee orderId=%s failed: %r", order_id, exc)
            return 0.0

    def close_position(self, exit_price: float, reason: str) -> Optional[dict]:
        if self.position is None:
            return None

        pos = self.position
        close_side = {"LONG": "SELL", "SHORT": "BUY"}[pos.side]

        # Market close (reduceOnly)
        actual_exit = exit_price
        close_order_id = None
        try:
            close_resp = self._request("POST", "/fapi/v1/order", {
                "symbol":     config.SYMBOL,
                "side":       close_side,
                "type":       "MARKET",
                "quantity":   pos.qty,
                "reduceOnly": "true",
            })
            close_order_id = close_resp.get("orderId")
            logger.info(
                "MARKET close: orderId=%s status=%s avgPrice=%s",
                close_order_id, close_resp.get("status"),
                close_resp.get("avgPrice"),
            )
            avg = float(close_resp.get("avgPrice") or 0)
            if avg > 0:
                actual_exit = avg
        except Exception as exc:
            logger.warning(
                "close_position: market close error (falling back to book price): %r", exc
            )

        # Real exit fee from exchange; fall back to estimate if unavailable
        if close_order_id:
            real_fee_exit = self._fetch_trade_fee(close_order_id)
            fee_exit = real_fee_exit if real_fee_exit > 0 else actual_exit * pos.qty * config.TAKER_FEE
        else:
            fee_exit = actual_exit * pos.qty * config.TAKER_FEE
        total_fees = pos.fee_entry + fee_exit
        fee_usd = round(total_fees, 6)

        if pos.side == "LONG":
            gross_pnl = (actual_exit - pos.entry_price) * pos.qty
        else:
            gross_pnl = (pos.entry_price - actual_exit) * pos.qty
        net_pnl = gross_pnl - total_fees
        cost_basis = pos.entry_price * pos.qty
        pnl_pct = (net_pnl / cost_basis) * 100.0 if cost_basis else 0.0

        self.position = None
        result = {
            "side":            pos.side,
            "entry_price":     pos.entry_price,
            "exit_price":      actual_exit,
            "sl":              pos.sl,
            "tp":              pos.tp,
            "qty":             pos.qty,
            "fee_usd":         fee_usd,
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
            pos.side, actual_exit, reason, net_pnl, pnl_pct, self.balance,
        )
        return result

    def close_at_market(self, reason: str = "MANUAL") -> Optional[dict]:
        if self.position is None:
            return None
        if self.position.side == "LONG":
            price = self._bid if self._bid is not None else self.position.entry_price
        else:
            price = self._ask if self._ask is not None else self.position.entry_price
        return self.close_position(price, reason)

    def check_sl_tp(self) -> Optional[tuple[float, str]]:
        """
        Software SL/TP/trailing — called on every bookTicker tick.
        Returns (exit_price, reason) or None.
        "TRAILING_ACTIVATED" → caller calls on_trailing_activated(); position stays open.
        """
        if self.position is None or self._bid is None or self._ask is None:
            return None
        pos = self.position
        if pos.side == "LONG":
            price = self._bid
            if price <= pos.sl:
                return price, "SL"
            activation_price = pos.entry_price * (1 + config.TRAILING_ACTIVATION_PCT)
            if not self._trailing_active:
                if price >= activation_price:
                    self._trailing_active = True
                    self._trailing_max = price
                    logger.info("Trailing Stop ACTIVATED | LONG | price=%.2f", price)
                    return price, "TRAILING_ACTIVATED"
            else:
                if price > self._trailing_max:
                    self._trailing_max = price
                trail_exit = self._trailing_max * (1 - config.TRAILING_STOP_PCT)
                if price <= trail_exit:
                    logger.info(
                        "Trailing Stop TRIGGERED | LONG | price=%.2f max=%.2f exit=%.2f",
                        price, self._trailing_max, trail_exit,
                    )
                    return price, "Trailing Stop"
        else:
            price = self._ask
            if price >= pos.sl:
                return price, "SL"
            activation_price = pos.entry_price * (1 - config.TRAILING_ACTIVATION_PCT)
            if not self._trailing_active:
                if price <= activation_price:
                    self._trailing_active = True
                    self._trailing_min = price
                    logger.info("Trailing Stop ACTIVATED | SHORT | price=%.2f", price)
                    return price, "TRAILING_ACTIVATED"
            else:
                if price < self._trailing_min:
                    self._trailing_min = price
                trail_exit = self._trailing_min * (1 + config.TRAILING_STOP_PCT)
                if price >= trail_exit:
                    logger.info(
                        "Trailing Stop TRIGGERED | SHORT | price=%.2f min=%.2f exit=%.2f",
                        price, self._trailing_min, trail_exit,
                    )
                    return price, "Trailing Stop"
        return None

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
