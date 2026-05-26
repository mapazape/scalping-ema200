"""Telegram notification and interactive command interface."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

import aiohttp

import config

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
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
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

    elif text.startswith("/ayuda") or text.startswith("/help") or text.startswith("/start"):
        await tg_send(
            "*Comandos disponibles:*\n"
            "`/status` — estado actual (FSM, EMA200, RSI, cooldown)\n"
            "`/resumen` — resumen completo con métricas\n"
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

    while True:
        try:
            updates = await _get_updates(_tg_offset, timeout=25)
            conflict_count = 0
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
                logger.error("tg_poll HTTP %s", exc)
                await asyncio.sleep(5)
        except Exception as exc:
            logger.error("tg_poll error: %s", exc)
            await asyncio.sleep(5)


async def heartbeat(bot: ScalpingBot) -> None:
    """Send a status ping every HEARTBEAT_SEG seconds."""
    while True:
        await asyncio.sleep(HEARTBEAT_SEG)
        try:
            await tg_send(_build_status(bot))
        except Exception as exc:
            logger.error("heartbeat error: %s", exc)
