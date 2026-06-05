"""Telegram notification and interactive command interface — shared across all bots."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, UTC
from typing import TYPE_CHECKING, Optional

import aiohttp
import requests

import config

if TYPE_CHECKING:
    from main import ScalpingBot

# Absolute journal dirs for all bots (used by /posicion, /stat_bots)
_BOT_DIRS = {
    "btc-short": "/opt/bots/scalping-ema200",
    "btc-long":  "/opt/bots/scalping-ema200-long",
    "eth":       "/opt/bots/scalping-eth",
}

_CLOSE_SIGNAL_DIR = "/opt/bots"
_BOT_CLOSE_KEYS   = ("btc-short", "btc-long", "eth")

# Local journal dir relative to each bot's CWD
_LOCAL_JOURNAL_DIR = "journals"

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_tg_offset: int = 0
_session = requests.Session()

HEARTBEAT_SEG: int = 3600


# ------------------------------------------------------------------
# Low-level senders
# ------------------------------------------------------------------

async def tg_send(text: str, parse_mode: str = "Markdown") -> None:
    """Send message. Silent failure if no token configured."""
    if not config.TG_TOKEN or not config.TG_CHAT_ID:
        return
    url = _API.format(token=config.TG_TOKEN, method="sendMessage")
    payload: dict = {"chat_id": config.TG_CHAT_ID, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        def _post() -> None:
            r = _session.post(url, json=payload, timeout=30)
            if not r.ok:
                logger.warning("tg_send HTTP %s: %s", r.status_code, r.text[:300])
        await asyncio.to_thread(_post)
    except Exception as exc:
        logger.error("tg_send error: %s", exc)


async def tg_send_document(filename: str, content: bytes, caption: str = "") -> None:
    """Send bytes as a document attachment."""
    if not config.TG_TOKEN or not config.TG_CHAT_ID:
        return
    url = _API.format(token=config.TG_TOKEN, method="sendDocument")
    try:
        def _post() -> None:
            data: dict = {"chat_id": str(config.TG_CHAT_ID)}
            if caption:
                data["caption"] = caption
            files = {"document": (filename, content, "application/octet-stream")}
            r = _session.post(url, data=data, files=files, timeout=15)
            if not r.ok:
                logger.warning("tg_send_document HTTP %s", r.status_code)
        await asyncio.to_thread(_post)
    except Exception as exc:
        logger.error("tg_send_document error: %s", exc)


def _get_updates_sync(offset: int, timeout: int = 25) -> list[dict]:
    url = _API.format(token=config.TG_TOKEN, method="getUpdates")
    params = {"offset": offset, "timeout": timeout, "limit": 10}
    r = _session.get(url, params=params, timeout=timeout + 5)
    r.raise_for_status()
    data = r.json()
    if data.get("ok"):
        return data["result"]
    return []


# ------------------------------------------------------------------
# Message builders
# ------------------------------------------------------------------

def _build_status(bot: "ScalpingBot") -> str:
    ema = bot.indicators.ema200_1h()
    rsi_pair = bot.indicators.rsi14_1m()
    rsi_curr = rsi_pair[1] if rsi_pair else None
    cooldown_left = max(0.0, bot._cooldown_until - time.monotonic())
    pausa = " | ⏸️ *PAUSADO*" if bot._paused else ""

    lines = [
        "📊 *STATUS*",
        f"Estado: `{bot.state.name}`{pausa}",
        f"Balance: `${bot.broker.balance:.2f}`",
        f"EMA200(1h): `{ema:.2f}`" if ema is not None else "EMA200(1h): `N/A`",
        f"RSI(1m): `{rsi_curr:.2f}`" if rsi_curr is not None else "RSI(1m): `N/A`",
        f"Cooldown: `{cooldown_left:.0f}s`",
    ]
    return "\n".join(lines)


def _build_resumen(bot: "ScalpingBot") -> str:
    s = bot.stats
    trades = s._trades
    n = s.count
    n1 = max(n, 1)

    total_pnl = s.total_pnl
    pnl_pct = (total_pnl / s.initial_balance * 100.0) if s.initial_balance else 0.0
    streak_n, streak_label = s.streak
    reasons = s.exit_reasons

    best  = max((t["pnl_usd"] for t in trades), default=0.0)
    worst = min((t["pnl_usd"] for t in trades), default=0.0)

    tp_p  = reasons.get("TP",            0) / n1 * 100
    sl_p  = reasons.get("SL",            0) / n1 * 100
    man_p = reasons.get("MANUAL_CERRAR", 0) / n1 * 100

    sep = "━━━━━━━━━━━━━━━"
    lines = [
        f"📊 *RESUMEN*\n{sep}",
        f"💰 PnL: `${total_pnl:+.2f}` ({pnl_pct:+.2f}%)",
        f"Balance: `${bot.broker.balance:.2f}`",
        f"🎯 WR: `{s.win_rate:.1f}%` ({n}t)",
        f"📈 Avg W: `${s.avg_win:.2f}` | Avg L: `${s.avg_loss:.2f}` | Payoff: `{s.payoff_ratio:.2f}`",
        sep,
        f"🚪 TP:`{tp_p:.0f}%` SL:`{sl_p:.0f}%` Manual:`{man_p:.0f}%`",
        f"🏆 Mejor: `${best:.2f}` | 💀 Peor: `${worst:.2f}`",
        f"🔥 Racha: `{streak_n} {streak_label}`",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------
# JSONL helpers
# ------------------------------------------------------------------

def _load_local_trades() -> list[dict]:
    """Read every .jsonl in the local bot's journals/ dir (relative CWD)."""
    trades: list[dict] = []
    if not os.path.isdir(_LOCAL_JOURNAL_DIR):
        return trades
    for fname in sorted(os.listdir(_LOCAL_JOURNAL_DIR)):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(_LOCAL_JOURNAL_DIR, fname)
        try:
            with open(fpath, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        trades.append(json.loads(line))
        except Exception as exc:
            logger.warning("_load_local_trades %s: %s", fpath, exc)
    return trades


def _load_all_trades(journal_dir: str) -> list[dict]:
    """Read all trade records from every JSONL file in journal_dir."""
    trades: list[dict] = []
    try:
        for fname in sorted(os.listdir(journal_dir)):
            if not fname.endswith(".jsonl"):
                continue
            path = os.path.join(journal_dir, fname)
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            trades.append(json.loads(line))
            except Exception as exc:
                logger.warning("_load_all_trades %s: %s", path, exc)
    except FileNotFoundError:
        pass
    return trades


def _load_today_trades(journal_dir: str) -> list[dict]:
    """Read today's trades from a bot's journal directory."""
    today = datetime.now(tz=config.TZ_LOCAL).strftime("%Y-%m-%d")
    path = os.path.join(journal_dir, "journals", f"trades_{today}.jsonl")
    trades: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    trades.append(json.loads(line))
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("_load_today_trades %s: %s", path, exc)
    return trades


def _is_corrupt(t: dict) -> bool:
    """Return True if a trade record is corrupt and should be excluded from stats."""
    return (
        t.get("entry", 0.0) == 0.0
        or t.get("exit_price", 0.0) == 0.0
        or abs(t.get("pnl_usd", 0.0)) > 10
    )


# ------------------------------------------------------------------
# /stats /lado /ultimo — local-bot views
# ------------------------------------------------------------------

def _build_stats() -> str:
    trades = _load_local_trades()
    n = len(trades)
    if n == 0:
        return "📊 *STATS*\nSin trades registrados aún."

    wins   = [t["pnl_usd"] for t in trades if t.get("pnl_usd", 0) > 0]
    losses = [abs(t["pnl_usd"]) for t in trades if t.get("pnl_usd", 0) < 0]

    wr      = len(wins) / n * 100
    avg_win  = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    payoff   = avg_win / avg_loss        if avg_loss > 0 else 0.0
    be_wr    = 100.0 / (1 + payoff)     if payoff > 0   else 50.0
    net_pnl  = sum(t.get("pnl_usd", 0) for t in trades)

    sep = "━━━━━━━━━━━━━━━"
    lines = [
        f"📊 *STATS* ({n} trades)\n{sep}",
        f"WR: `{wr:.1f}%` | BE\\_WR: `{be_wr:.1f}%`",
        f"Avg W: `${avg_win:.2f}` | Avg L: `${avg_loss:.2f}`",
        f"Payoff: `{payoff:.2f}`",
        f"Net PnL: `${net_pnl:+.2f}`",
        sep,
    ]
    if wr < be_wr:
        lines.append("⚠️ *WR < BE\\_WR — estrategia en pérdida*")
    return "\n".join(lines)


def _build_lado() -> str:
    trades = _load_local_trades()
    if not trades:
        return "📊 *LADO*\nSin trades registrados aún."

    def _side_block(label: str, subset: list[dict]) -> str:
        n = len(subset)
        if n == 0:
            return f"*{label}* — sin trades"
        wins   = [t["pnl_usd"] for t in subset if t.get("pnl_usd", 0) > 0]
        losses = [abs(t["pnl_usd"]) for t in subset if t.get("pnl_usd", 0) < 0]
        wr       = len(wins) / n * 100
        avg_win  = sum(wins)   / len(wins)   if wins   else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        net_pnl  = sum(t.get("pnl_usd", 0) for t in subset)
        return (
            f"*{label}* — {n}t\n"
            f"  WR: `{wr:.1f}%` | Avg W: `${avg_win:.2f}` | Avg L: `${avg_loss:.2f}`\n"
            f"  Net PnL: `${net_pnl:+.2f}`"
        )

    longs  = [t for t in trades if t.get("side") == "LONG"]
    shorts = [t for t in trades if t.get("side") == "SHORT"]
    sep = "━━━━━━━━━━━━━━━"
    lines = [
        f"📊 *LADO* ({len(trades)} trades)\n{sep}",
        _side_block("LONG 📈", longs),
        _side_block("SHORT 📉", shorts),
    ]
    return "\n".join(lines)


def _fmt_ts_local(ts: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Parse an ISO timestamp string and return it formatted in Chile local time."""
    if not ts or ts == "N/A":
        return ts
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(config.TZ_LOCAL).strftime(fmt)
    except Exception:
        return ts[:16]


def _build_ultimo() -> str:
    trades = _load_local_trades()
    if not trades:
        return "🔍 *ÚLTIMO TRADE*\nSin trades registrados aún."

    t = trades[-1]
    pnl     = t.get("pnl_usd", 0.0)
    pnl_pct = t.get("pnl_pct", 0.0)
    emoji   = "✅" if pnl > 0 else "❌"
    ts      = t.get("timestamp", "N/A")

    sep = "━━━━━━━━━━━━━━━"
    lines = [
        f"🔍 *ÚLTIMO TRADE*\n{sep}",
        f"{emoji} `{t.get('side')}` — {t.get('exit_reason')}",
        f"Entry: `${t.get('entry', 0):.2f}` → Exit: `${t.get('exit_price', 0):.2f}`",
        f"PnL: `${pnl:+.4f}` ({pnl_pct:+.2f}%)",
        f"RSI entrada: `{t.get('rsi_at_entry', 0):.2f}`",
        f"ATR entrada: `{t.get('atr_at_entry', 0):.2f}`",
        f"Cierre: `{_fmt_ts_local(ts)}`",
    ]
    open_t = t.get("open_time")
    if open_t is not None:
        try:
            close_t = datetime.fromisoformat(ts).timestamp() if ts != "N/A" else time.time()
            secs = int(close_t - open_t)
            dur_str = f"{secs//60}m {secs%60}s"
        except Exception:
            dur_str = "N/A"
    else:
        dur_str = "N/A"
    lines.append(f"Duración: `{dur_str}`")
    return "\n".join(lines)


# ------------------------------------------------------------------
# /posicion — cross-bot consolidated view
# ------------------------------------------------------------------

async def _fetch_real_balance() -> Optional[float]:
    """Consult Binance Futures account balance (USDT) in real time."""
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
                    logger.warning("_fetch_real_balance HTTP %s", resp.status)
                    return None
                data = await resp.json()
                for asset in data:
                    if asset.get("asset") == "USDT":
                        return float(asset["availableBalance"])
    except Exception as exc:
        logger.warning("_fetch_real_balance failed: %r", exc)
    return None


async def _build_posicion(bot: "ScalpingBot") -> str:
    sep = "━━━━━━━━━━━━━━━"
    lines = [f"📍 *POSICIÓN — 3 BOTS*\n{sep}"]

    try:
        with open(config.BOT_STATES_FILE, encoding="utf-8") as fh:
            bot_states = json.load(fh)
    except Exception as exc:
        return f"📍 *POSICIÓN*\n⚠️ Error leyendo estados: {exc}"

    now = time.time()
    bot_labels = {
        "btc-short": "📕 BTC\\-SHORT",
        "btc-long":  "📗 BTC\\-LONG",
        "eth":       "🔷 ETH",
    }

    for bot_id, label in bot_labels.items():
        data = bot_states.get(bot_id, {})
        hb = data.get("heartbeat", 0)
        alive = (now - hb) < 120

        lines.append("")
        if not alive:
            lines.append(f"{label}: ⚠️ _sin heartbeat_")
            continue

        fsm    = data.get("state", "IDLE")
        price  = data.get("price")
        ema50  = data.get("ema50")
        ema200 = data.get("ema200")
        rsi    = data.get("rsi")
        regime = data.get("regime") or "N/A"
        alloc  = data.get("allocated_balance", 0.0)

        regime_emoji = "🟢" if regime == "BULL" else "🔴"
        fmt = lambda v, d=2: f"`{v:.{d}f}`" if v is not None else "`N/A`"

        lines += [
            f"{label} | {regime_emoji} `{regime}` | FSM: `{fsm}`",
            f"  Precio: {fmt(price)} | Capital: `${alloc:.2f}`",
            f"  EMA50: {fmt(ema50)} | EMA200: {fmt(ema200)} | RSI: {fmt(rsi)}",
        ]

        if fsm == "IN_POSITION":
            side       = data.get("side", "")
            entry      = data.get("entry_price")
            sl         = data.get("sl")
            tp         = data.get("tp")
            unrealized = data.get("unrealized_pnl")
            t_active   = data.get("trailing_active", False)
            t_max      = data.get("trailing_max")
            t_min      = data.get("trailing_min")

            side_emoji = "📗" if side == "LONG" else "📕"
            pnl_emoji  = ("+" if (unrealized or 0) >= 0 else "")
            unreal_str = f"`{pnl_emoji}${unrealized:.4f}`" if unrealized is not None else "`N/A`"

            lines += [
                f"  {side_emoji} `{side}` | Entry: {fmt(entry)} | SL: {fmt(sl)} | TP: {fmt(tp)}",
                f"  PnL no realizado: {unreal_str}",
            ]

            if t_active:
                if side == "LONG" and t_max is not None:
                    trail_exit = t_max * (1 - config.TRAILING_STOP_PCT)
                    lines.append(
                        f"  🎯 Trailing activo | máx: `{t_max:.2f}` | exit si ≤ `{trail_exit:.2f}`"
                    )
                elif side == "SHORT" and t_min is not None:
                    trail_exit = t_min * (1 + config.TRAILING_STOP_PCT)
                    lines.append(
                        f"  🎯 Trailing activo | mín: `{t_min:.2f}` | exit si ≥ `{trail_exit:.2f}`"
                    )

    # ── Últimas 10 operaciones ──────────────────────────────────────
    bot_name_map = {"btc-short": "BTC-S", "btc-long": "BTC-L", "eth": "ETH"}
    all_tagged: list[tuple[str, dict]] = []
    for bid, bdir in _BOT_DIRS.items():
        for trade in _load_all_trades(os.path.join(bdir, "journals")):
            all_tagged.append((bid, trade))

    all_tagged.sort(key=lambda x: x[1].get("timestamp", ""), reverse=True)
    last_10 = all_tagged[:10]

    lines.append(f"\n{sep}\n📋 *ÚLTIMAS 10 OPERACIONES*")
    if not last_10:
        lines.append("_(sin datos)_")
    else:
        for bid, t in last_10:
            pnl      = t.get("pnl_usd", 0.0)
            emoji    = "🟢" if pnl > 0 else "🔴"
            ts       = t.get("timestamp", "")
            ts_str   = _fmt_ts_local(ts, fmt="%m-%d %H:%M")
            side_t   = t.get("side", "?")
            entry_t  = t.get("entry", 0.0)
            exit_t   = t.get("exit_price", 0.0)
            exit_str = f"{exit_t:.0f}" if exit_t else "N/A"
            reason   = t.get("exit_reason", "?")
            sign     = "+" if pnl >= 0 else ""
            bname    = bot_name_map.get(bid, bid)
            lines.append(
                f"{emoji} `{ts_str}` \\[{bname}\\] `{side_t}` {entry_t:.0f}→{exit_str} `{sign}${pnl:.2f}` {reason}"
            )

    # ── Footer: balance real + PnL por bot (trades reales) ─────────
    all_trades_flat = [t for _, t in all_tagged]

    corrupted_set: set[int] = set()
    for t in all_trades_flat:
        if _is_corrupt(t):
            corrupted_set.add(id(t))

    bot_footer_labels = {
        "btc-short": "📕 btc-short",
        "btc-long":  "📗 btc-long",
        "eth":       "💎 eth",
    }
    bot_valid: dict[str, list[dict]] = {bid: [] for bid in bot_footer_labels}
    for bid, t in all_tagged:
        if id(t) not in corrupted_set and bid in bot_valid:
            bot_valid[bid].append(t)

    global_wins  = sum(1 for ts in bot_valid.values() for t in ts if t.get("pnl_usd", 0.0) > 0)
    global_total = sum(len(ts) for ts in bot_valid.values())
    wr_pct       = (global_wins / global_total * 100) if global_total > 0 else 0.0

    days_active = 0
    if all_trades_flat:
        first_ts = min(t.get("timestamp", "") for t in all_trades_flat)
        try:
            first_dt = datetime.fromisoformat(first_ts)
            if first_dt.tzinfo is None:
                first_dt = first_dt.replace(tzinfo=UTC)
            days_active = (datetime.now(tz=config.TZ_LOCAL) - first_dt).days
        except Exception:
            pass

    real_bal = await _fetch_real_balance()
    bal_str  = f"${real_bal:.2f} USDT" if real_bal is not None else "N/A"

    lines.append(f"\n{sep}")
    lines.append(f"💰 Balance real: `{bal_str}`")
    lines.append("📊 PnL por bot (trades reales):")
    for bid, label in bot_footer_labels.items():
        trades    = bot_valid[bid]
        fees_bot  = sum(t.get("fee_usd") or 0.0 for t in trades)
        pnl_bot   = sum(t.get("pnl_usd", 0.0) for t in trades)
        gross_bot = pnl_bot + fees_bot
        n_bot     = len(trades)
        gsign     = "+" if gross_bot >= 0 else ""
        nsign     = "+" if pnl_bot   >= 0 else ""
        lines.append(
            f"  {label}: bruto `{gsign}${gross_bot:.2f}` | fees `${fees_bot:.2f}` | neto `{nsign}${pnl_bot:.2f}` ({n_bot}t)"
        )
    if corrupted_set:
        lines.append(f"  ⚠️ {len(corrupted_set)} trades corruptos excluidos")
    lines.append(
        f"📈 WR global: `{global_wins}/{global_total} ({wr_pct:.0f}%)` | Activo: `{days_active}d`"
    )

    return "\n".join(lines)


# ------------------------------------------------------------------
# /stat_bots — cross-bot daily summary
# ------------------------------------------------------------------

def _build_stat_bots(bot: "ScalpingBot") -> str:
    sep = "━━━━━━━━━━━━━━━"
    lines = [f"📊 *ESTADO DE BOTS*\n{sep}"]

    total_balance = bot._total_balance
    if total_balance > 0:
        lines.append(f"💰 Balance Total: `${total_balance:.2f}`")
    else:
        lines.append(f"💰 Balance Total: `${bot.broker.balance:.2f}` _(aprox)_")

    try:
        with open(config.CB_FILE, encoding="utf-8") as fh:
            cb = json.load(fh)
        cb_ok     = cb.get("trading_enabled", True)
        cb_reason = cb.get("reason", "")
        cb_str    = "✅ ACTIVO" if cb_ok else f"🛑 BLOQUEADO: `{cb_reason}`"
    except FileNotFoundError:
        cb_str = "✅ ACTIVO"
    except Exception:
        cb_str = "❓ desconocido"
    lines.append(f"Circuit Breaker: {cb_str}")
    lines.append(sep)

    try:
        with open(config.BOT_STATES_FILE, encoding="utf-8") as fh:
            bot_states = json.load(fh)
    except Exception:
        bot_states = {}

    now = time.time()
    total_pnl    = 0.0
    total_trades = 0
    total_wins   = 0

    bot_labels = {
        "btc-short": "📕 BTC-SHORT",
        "btc-long":  "📗 BTC-LONG",
        "eth":       "🔷 ETH",
    }

    for bot_id, label in bot_labels.items():
        state_data = bot_states.get(bot_id, {})
        heartbeat  = state_data.get("heartbeat", 0)
        alive      = (now - heartbeat) < 120

        if not alive:
            lines.append(f"{label}: ⚠️ _sin heartbeat_")
            continue

        fsm   = state_data.get("state", "IDLE")
        side  = state_data.get("side")
        entry = state_data.get("entry_price")
        alloc = state_data.get("allocated_balance", 0.0)

        if fsm == "IN_POSITION" and side and entry:
            pos_str = f"IN\\-POS `{side}` @ `{entry:.2f}`"
        else:
            pos_str = "IDLE"

        bot_dir = _BOT_DIRS.get(bot_id, "")
        trades  = _load_today_trades(bot_dir)
        n       = len(trades)
        pnl     = sum(t.get("pnl_usd", 0.0) for t in trades)
        wins    = sum(1 for t in trades if t.get("pnl_usd", 0.0) > 0)
        wr      = wins / n * 100 if n else 0.0
        pnl_emoji = "+" if pnl >= 0 else ""

        total_pnl    += pnl
        total_trades += n
        total_wins   += wins

        lines.append(
            f"{label} | {pos_str}\n"
            f"  Capital: `${alloc:.2f}` | PnL: `{pnl_emoji}${pnl:.2f}`\n"
            f"  Trades: `{n}` | WR: `{wr:.1f}%`"
        )

    lines.append(sep)
    global_wr = total_wins / total_trades * 100 if total_trades else 0.0
    pnl_emoji = "+" if total_pnl >= 0 else ""
    lines.append(
        f"PnL Total: `{pnl_emoji}${total_pnl:.2f}` | "
        f"Trades: `{total_trades}` | WR Global: `{global_wr:.1f}%`"
    )
    return "\n".join(lines)


# ------------------------------------------------------------------
# /config — per-bot parameters
# ------------------------------------------------------------------

def _send_close_signal(bot_id: str) -> None:
    """Write close_signal_{bot_id}.json so the target bot closes its position."""
    path = os.path.join(_CLOSE_SIGNAL_DIR, f"close_signal_{bot_id}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"close": True, "ts": datetime.now(config.TZ_LOCAL).isoformat()}, fh)


def _parse_env_file(path: str) -> dict:
    """Parse key=value lines from a .env file; ignores comments and blank lines."""
    result: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                result[key.strip()] = val.strip()
    except Exception:
        pass
    return result


_SIDE_ICONS = {"AUTO": "🔄 AUTO", "LONG": "📗 LONG", "SHORT": "📕 SHORT"}
_BOT_LABELS = {
    "btc-short": "📕 BTC-SHORT",
    "btc-long":  "📗 BTC-LONG",
    "eth":       "🔷 ETH",
}


def _build_config(bot: "ScalpingBot") -> str:
    sep = "━━━━━━━━━━━━━━━"

    try:
        with open(config.BOT_STATES_FILE, encoding="utf-8") as fh:
            bot_states = json.load(fh)
    except Exception:
        bot_states = {}

    reversal = getattr(config, "REVERSAL_THRESHOLD", None)
    shared_lines = [
        f"SL: `{config.SL_PCT * 100:.2f}%` | TP: `{config.TP_PCT * 100:.2f}%`"
        f" | Trailing: `{config.TRAILING_STOP_PCT * 100:.2f}%`",
        f"RSI: oversold `{config.RSI_OVERSOLD}` / overbought `{config.RSI_OVERBOUGHT}`",
        f"Pos. Size: `{config.POSITION_SIZE_PCT * 100:.1f}%` | Cooldown: `{config.COOLDOWN_SECONDS}s`",
    ]
    if reversal is not None:
        shared_lines.append(f"CB Reversal: `{reversal * 100:.1f}%`")

    lines = [f"⚙️ *CONFIG — 3 BOTS*\n{sep}"]

    for bot_id, label in _BOT_LABELS.items():
        env = _parse_env_file(os.path.join(_BOT_DIRS[bot_id], ".env"))

        bot_name   = env.get("BOT_NAME", bot_id)
        symbol     = env.get("SYMBOL", "?")
        side_raw   = env.get("SIDE_FILTER", "AUTO").upper()
        paper_mode = env.get("PAPER_MODE", "false").lower() == "true"
        mode_str   = "PAPER" if paper_mode else "LIVE"

        side_str   = _SIDE_ICONS.get(side_raw, side_raw)
        paper_icon = "✅" if paper_mode else "🔴"

        state     = bot_states.get(bot_id, {})
        ema50     = state.get("ema50")
        ema50_str = f"{ema50:,.2f}" if ema50 is not None else "N/A"

        lines += [
            "",
            f"{label}  `{bot_name}` — {symbol}",
            f"  Modo: {paper_icon} `{mode_str}` | Side: {side_str}",
            f"  EMA50: `{ema50_str}`",
        ]

    lines += ["", sep] + shared_lines
    return "\n".join(lines)


# ------------------------------------------------------------------
# Command dispatcher
# ------------------------------------------------------------------

async def _handle_update(update: dict, bot: "ScalpingBot") -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    text: str = msg.get("text", "").strip().lower()
    chat_id = str(msg.get("chat", {}).get("id", ""))

    if chat_id != str(config.TG_CHAT_ID):
        return

    if text.startswith("/status"):
        await tg_send(_build_status(bot))

    elif text.startswith("/resumen"):
        await tg_send(_build_resumen(bot))

    elif text.startswith("/cerrar"):
        parts = text.split()
        arg   = parts[1].lower() if len(parts) > 1 else ""

        if arg in _BOT_CLOSE_KEYS or arg == "all":
            targets = list(_BOT_CLOSE_KEYS) if arg == "all" else [arg]
            sent:   list[str] = []
            errors: list[str] = []
            for bot_id in targets:
                try:
                    _send_close_signal(bot_id)
                    sent.append(bot_id)
                except Exception as exc:
                    errors.append(f"{bot_id}: {exc}")
            reply = f"🚨 Señal de cierre enviada a: `{', '.join(sent)}`"
            if errors:
                reply += f"\n⚠️ Errores: {'; '.join(errors)}"
            await tg_send(reply)
        else:
            trade = await bot.manual_close()
            if trade is not None:
                await tg_send(
                    f"✅ Posición cerrada manualmente\n"
                    f"PnL: `${trade['pnl_usd']:+.4f}` ({trade['pnl_pct']:+.2f}%)\n"
                    f"Balance: `${bot.broker.balance:.2f}`"
                )
            else:
                await tg_send("ℹ️ No hay posición abierta.")

    elif text.startswith("/pausa"):
        bot._paused = True
        logger.info("bot paused via Telegram")
        await tg_send("⏸️ Scalper *pausado* — no abrirá nuevas posiciones")

    elif text.startswith("/reanudar"):
        bot._paused = False
        logger.info("bot resumed via Telegram")
        await tg_send("▶️ Scalper *reanudado*")

    elif text.startswith("/down_trades"):
        trades = bot.stats._trades
        if not trades:
            await tg_send("📭 Sin trades registrados aún.")
        else:
            ts_str   = time.strftime("%Y%m%d_%H%M%S")
            filename = f"trades_{ts_str}.json"
            content  = json.dumps(trades, indent=2, ensure_ascii=False).encode("utf-8")
            await tg_send_document(filename, content,
                                   caption=f"📦 {len(trades)} trades exportados")

    elif text.startswith("/stats"):
        await tg_send(_build_stats())

    elif text.startswith("/lado"):
        await tg_send(_build_lado())

    elif text.startswith("/ultimo"):
        await tg_send(_build_ultimo())

    elif text.startswith("/config"):
        await tg_send(_build_config(bot))

    elif text.startswith("/posicion"):
        await tg_send(await _build_posicion(bot))

    elif text.startswith("/stat_bots"):
        await tg_send(_build_stat_bots(bot))

    elif text.startswith("/ayuda") or text.startswith("/help") or text.startswith("/start"):
        await tg_send(
            "Comandos disponibles:\n"
            "\n"
            "📊 Información\n"
            "/posicion — vista consolidada 3 bots (EMAs, RSI, trailing)\n"
            "/stat_bots — PnL diario, trades y WR de los 3 bots\n"
            "/status — estado del bot actual (FSM, EMA200, RSI, cooldown)\n"
            "/resumen — métricas completas del bot actual\n"
            "/stats — WR%, payoff, BE_WR, net PnL global (JSONL)\n"
            "/lado — desglose LONG vs SHORT\n"
            "/ultimo — detalle del último trade cerrado\n"
            "/config — parámetros activos del bot\n"
            "/down_trades — descargar historial de trades JSON\n"
            "\n"
            "🎮 Control\n"
            "/cerrar — cierra posición del bot actual (MANUAL)\n"
            "/cerrar btc-short|btc-long|eth|all — envía señal de cierre a bot(s) específico(s)\n"
            "/pausa — pausar nuevas entradas\n"
            "/reanudar — reanudar entradas\n"
            "\n"
            "/ayuda — esta ayuda",
            parse_mode="",
        )


# ------------------------------------------------------------------
# Long-running coroutines (called via create_task in main.py)
# ------------------------------------------------------------------

async def tg_poll(bot: "ScalpingBot") -> None:
    """Long-poll Telegram for commands."""
    global _tg_offset
    if not config.TG_TOKEN:
        logger.info("TG_TOKEN not configured — Telegram polling disabled")
        return

    if not config.TG_POLLING:
        logger.info("TG_POLLING=false — skipping Telegram polling for this bot")
        return

    # Drain backlog — skip messages sent before bot started
    try:
        updates = await asyncio.to_thread(_get_updates_sync, -1, 0)
        if updates:
            _tg_offset = updates[-1]["update_id"] + 1
    except Exception as exc:
        logger.warning("tg_poll backlog drain: %s", exc)

    logger.info("Telegram polling started (chat_id=%s)", config.TG_CHAT_ID)
    conflict_count = 0
    _retry = 0

    while True:
        try:
            updates = await asyncio.to_thread(_get_updates_sync, _tg_offset, 25)
            conflict_count = 0
            _retry = 0
            for update in updates:
                _tg_offset = update["update_id"] + 1
                try:
                    await _handle_update(update, bot)
                except Exception as exc:
                    logger.error("handle_update error: %s", exc)
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status == 409:
                conflict_count += 1
                if conflict_count == 1:
                    logger.warning("tg_poll 409 conflict — another instance active, waiting 60s")
                await asyncio.sleep(60)
            else:
                delay = 5 * 2 ** min(_retry, 2)
                logger.error("tg_poll HTTP %s — retry %d in %ds", exc, _retry + 1, delay)
                _retry += 1
                await asyncio.sleep(delay)
        except Exception as exc:
            delay = 5 * 2 ** min(_retry, 2)
            logger.error("tg_poll error (retry %d in %ds): %r", _retry + 1, delay, exc)
            _retry += 1
            await asyncio.sleep(delay)


async def heartbeat(bot: "ScalpingBot") -> None:
    """Send a status ping every HEARTBEAT_SEG seconds."""
    while True:
        await asyncio.sleep(HEARTBEAT_SEG)
        try:
            await tg_send(_build_status(bot))
        except Exception as exc:
            logger.error("heartbeat error: %s", exc)
