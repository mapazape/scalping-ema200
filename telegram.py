"""Telegram notification and interactive command interface."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import TYPE_CHECKING

import aiohttp

import config
from trade_journal import JOURNAL_DIR

if TYPE_CHECKING:
    from main import ScalpingBot

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_tg_offset: int = 0

HEARTBEAT_SEG: int = 3600


# ------------------------------------------------------------------
# Low-level senders
# ------------------------------------------------------------------

async def tg_send(text: str) -> None:
    """Send message. Silent failure if no token configured."""
    if not config.TG_TOKEN or not config.TG_CHAT_ID:
        return
    url = _API.format(token=config.TG_TOKEN, method="sendMessage")
    payload = {"chat_id": config.TG_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    logger.warning("tg_send HTTP %s: %s", resp.status, await resp.text())
    except Exception as exc:
        logger.error("tg_send error: %s", exc)


async def tg_send_document(filename: str, content: bytes, caption: str = "") -> None:
    """Send bytes as a document attachment."""
    if not config.TG_TOKEN or not config.TG_CHAT_ID:
        return
    url = _API.format(token=config.TG_TOKEN, method="sendDocument")
    try:
        async with aiohttp.ClientSession() as session:
            form = aiohttp.FormData()
            form.add_field("chat_id", str(config.TG_CHAT_ID))
            if caption:
                form.add_field("caption", caption)
            form.add_field("document", content, filename=filename,
                           content_type="application/octet-stream")
            async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.warning("tg_send_document HTTP %s", resp.status)
    except Exception as exc:
        logger.error("tg_send_document error: %s", exc)


async def _get_updates(offset: int, timeout: int = 25) -> list[dict]:
    url = _API.format(token=config.TG_TOKEN, method="getUpdates")
    params = {"offset": offset, "timeout": timeout, "limit": 10}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=timeout + 5)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if data.get("ok"):
                return data["result"]
    return []


# ------------------------------------------------------------------
# Message builders
# ------------------------------------------------------------------

def _build_status(bot: ScalpingBot) -> str:
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


def _build_resumen(bot: ScalpingBot) -> str:
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
# JSONL helpers (read-only, no bot state required)
# ------------------------------------------------------------------

def _load_all_trades() -> list[dict]:
    """Read every .jsonl in journals/ and return sorted list of trade dicts."""
    trades: list[dict] = []
    if not os.path.isdir(JOURNAL_DIR):
        return trades
    for fname in sorted(os.listdir(JOURNAL_DIR)):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(JOURNAL_DIR, fname)
        try:
            with open(fpath, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        trades.append(json.loads(line))
        except Exception as exc:
            logger.warning("_load_all_trades %s: %s", fpath, exc)
    return trades


def _build_stats() -> str:
    trades = _load_all_trades()
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
    trades = _load_all_trades()
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


def _build_ultimo() -> str:
    trades = _load_all_trades()
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
        f"Cierre (UTC): `{ts}`",
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


def _build_config() -> str:
    paper = "✅ PAPER" if config.PAPER_MODE else "🔴 LIVE"
    sep = "━━━━━━━━━━━━━━━"
    lines = [
        f"⚙️ *CONFIG ACTIVA*\n{sep}",
        f"Modo: `{paper}`",
        f"Capital inicial: `${config.INITIAL_CAPITAL:.0f}`",
        sep,
        f"RSI Oversold: `{config.RSI_OVERSOLD}`",
        f"RSI Overbought: `{config.RSI_OVERBOUGHT}`",
        f"SL: `{config.SL_PCT * 100:.2f}%` | TP: `{config.TP_PCT * 100:.2f}%`",
        f"Pos. Size: `{config.POSITION_SIZE_PCT * 100:.1f}%`",
        f"Cooldown: `{config.COOLDOWN_SECONDS}s`",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------
# Command dispatcher
# ------------------------------------------------------------------

async def _handle_update(update: dict, bot: ScalpingBot) -> None:
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
        await tg_send(_build_config())

    elif text.startswith("/ayuda") or text.startswith("/help") or text.startswith("/start"):
        await tg_send(
            "*Comandos disponibles:*\n"
            "`/status` — estado actual (FSM, EMA200, RSI, cooldown)\n"
            "`/resumen` — resumen completo con métricas\n"
            "`/stats` — WR%, payoff, BE\\_WR, net PnL (JSONL)\n"
            "`/lado` — desglose LONG vs SHORT\n"
            "`/ultimo` — detalle del último trade cerrado\n"
            "`/config` — parámetros activos del bot\n"
            "`/down_trades` — descargar historial de trades JSON\n"
            "`/cerrar` — cerrar posición abierta (MANUAL)\n"
            "`/pausa` — pausar nuevas entradas\n"
            "`/reanudar` — reanudar entradas\n"
            "`/ayuda` — esta ayuda"
        )


# ------------------------------------------------------------------
# Long-running coroutines (called via create_task)
# ------------------------------------------------------------------

async def tg_poll(bot: ScalpingBot) -> None:
    """Long-poll Telegram for commands."""
    global _tg_offset
    if not config.TG_TOKEN:
        logger.info("TG_TOKEN not configured — Telegram polling disabled")
        return

    # Drain backlog — skip messages sent before bot started
    try:
        updates = await _get_updates(-1, timeout=0)
        if updates:
            _tg_offset = updates[-1]["update_id"] + 1
    except Exception as exc:
        logger.warning("tg_poll backlog drain: %s", exc)

    logger.info("Telegram polling started (chat_id=%s)", config.TG_CHAT_ID)
    conflict_count = 0
    _retry = 0

    while True:
        try:
            updates = await _get_updates(_tg_offset, timeout=25)
            conflict_count = 0
            _retry = 0
            for update in updates:
                _tg_offset = update["update_id"] + 1
                try:
                    await _handle_update(update, bot)
                except Exception as exc:
                    logger.error("handle_update error: %s", exc)
        except aiohttp.ClientResponseError as exc:
            if exc.status == 409:
                conflict_count += 1
                if conflict_count == 1:
                    logger.warning("tg_poll 409 conflict — another instance active, waiting 60s")
                await asyncio.sleep(60)
            else:
                delay = 5 * 2 ** min(_retry, 2)  # 5s, 10s, 20s
                logger.error("tg_poll HTTP %s — retry %d in %ds", exc, _retry + 1, delay)
                _retry += 1
                await asyncio.sleep(delay)
        except Exception as exc:
            delay = 5 * 2 ** min(_retry, 2)  # 5s, 10s, 20s
            logger.error("tg_poll error (retry %d in %ds): %r", _retry + 1, delay, exc)
            _retry += 1
            await asyncio.sleep(delay)


async def heartbeat(bot: ScalpingBot) -> None:
    """Send a status ping every HEARTBEAT_SEG seconds."""
    while True:
        await asyncio.sleep(HEARTBEAT_SEG)
        try:
            await tg_send(_build_status(bot))
        except Exception as exc:
            logger.error("heartbeat error: %s", exc)
