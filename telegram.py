"""Telegram notification and interactive command interface."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

import aiohttp

import config

if TYPE_CHECKING:
    from main import ScalpingBot

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_last_update_id: int = 0


# ------------------------------------------------------------------
# Low-level senders
# ------------------------------------------------------------------

async def tg_send(text: str) -> None:
    if not config.TG_TOKEN or not config.TG_CHAT_ID:
        return
    url = _API.format(token=config.TG_TOKEN, method="sendMessage")
    payload = {"chat_id": config.TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    logger.warning("tg_send HTTP %s: %s", resp.status, await resp.text())
    except Exception as exc:
        logger.error("tg_send error: %s", exc)


async def tg_send_document(path: str, caption: str = "") -> None:
    if not config.TG_TOKEN or not config.TG_CHAT_ID:
        return
    url = _API.format(token=config.TG_TOKEN, method="sendDocument")
    try:
        async with aiohttp.ClientSession() as session:
            with open(path, "rb") as fh:
                form = aiohttp.FormData()
                form.add_field("chat_id", str(config.TG_CHAT_ID))
                form.add_field("caption", caption)
                form.add_field("document", fh, filename=path.rsplit("/", 1)[-1])
                async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        logger.warning("tg_send_document HTTP %s", resp.status)
    except Exception as exc:
        logger.error("tg_send_document error: %s", exc)


async def _get_updates(offset: int) -> list[dict]:
    url = _API.format(token=config.TG_TOKEN, method="getUpdates")
    params = {"offset": offset, "timeout": 10, "limit": 10}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return data["result"]
    except Exception as exc:
        logger.error("getUpdates error: %s", exc)
    return []


# ------------------------------------------------------------------
# Message builders
# ------------------------------------------------------------------

def _build_status(bot: ScalpingBot) -> str:
    ema = bot.indicators.ema200_1h()
    rsi_pair = bot.indicators.rsi14_1m()
    rsi_curr = rsi_pair[1] if rsi_pair else None
    cooldown_left = max(0.0, bot._cooldown_until - time.monotonic())

    lines = [
        "<b>STATUS</b>",
        f"Estado: {bot.state.name}",
        f"Pausa: {'Si' if bot._paused else 'No'}",
        f"EMA200(1h): {ema:.2f}" if ema is not None else "EMA200(1h): N/A",
        f"RSI(1m): {rsi_curr:.2f}" if rsi_curr is not None else "RSI(1m): N/A",
        f"Cooldown: {cooldown_left:.0f}s",
        f"Balance: ${bot.broker.balance:.2f}",
    ]
    return "\n".join(lines)


def _build_resumen(bot: ScalpingBot) -> str:
    s = bot.stats
    balance = bot.broker.balance
    total_pnl = s.total_pnl
    pnl_pct = (total_pnl / s.initial_balance * 100.0) if s.initial_balance else 0.0
    streak_n, streak_label = s.streak
    reasons = s.exit_reasons

    lines = [
        "<b>RESUMEN</b>",
        f"Balance: ${balance:.2f}",
        f"PnL total: ${total_pnl:+.2f} ({pnl_pct:+.2f}%)",
        f"Trades: {s.count} | WR: {s.win_rate:.1f}%",
        f"Avg W: ${s.avg_win:.2f} | Avg L: ${s.avg_loss:.2f} | Payoff: {s.payoff_ratio:.2f}",
        f"TP={reasons.get('TP', 0)} SL={reasons.get('SL', 0)} Manual={reasons.get('MANUAL_CERRAR', 0)}",
        f"Racha: {streak_n} {streak_label}",
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
        return  # ignore messages from unknown chats

    if text.startswith("/status"):
        await tg_send(_build_status(bot))

    elif text.startswith("/resumen"):
        await tg_send(_build_resumen(bot))

    elif text.startswith("/cerrar"):
        trade = await bot.manual_close()
        if trade is not None:
            await tg_send(
                f"Posicion cerrada manualmente.\n"
                f"PnL: ${trade['pnl_usd']:+.4f} ({trade['pnl_pct']:+.2f}%)\n"
                f"Balance: ${bot.broker.balance:.2f}"
            )
        else:
            await tg_send("No hay posicion abierta.")

    elif text.startswith("/pausa"):
        bot._paused = True
        logger.info("bot paused via Telegram")
        await tg_send("Bot en pausa. Nuevas entradas suspendidas.\nUsa /reanudar para continuar.")

    elif text.startswith("/reanudar"):
        bot._paused = False
        logger.info("bot resumed via Telegram")
        await tg_send("Bot reanudado. Buscando senales...")

    elif text.startswith("/ayuda"):
        await tg_send(
            "<b>Comandos disponibles:</b>\n"
            "/status    — estado del bot, indicadores, cooldown\n"
            "/resumen   — estadisticas: PnL, WR, payoff, racha\n"
            "/cerrar    — cerrar posicion abierta a mercado\n"
            "/pausa     — suspender nuevas entradas\n"
            "/reanudar  — reanudar entradas\n"
            "/ayuda     — este mensaje"
        )


# ------------------------------------------------------------------
# Long-running coroutines (called via create_task)
# ------------------------------------------------------------------

async def tg_poll(bot: ScalpingBot) -> None:
    global _last_update_id
    if not config.TG_TOKEN:
        logger.info("TG_TOKEN not configured — Telegram polling disabled")
        return
    logger.info("Telegram polling started (chat_id=%s)", config.TG_CHAT_ID)
    while True:
        updates = await _get_updates(_last_update_id)
        for update in updates:
            _last_update_id = update["update_id"] + 1
            try:
                await _handle_update(update, bot)
            except Exception as exc:
                logger.error("handle_update error: %s", exc)
        await asyncio.sleep(2)


async def heartbeat(bot: ScalpingBot) -> None:
    """Send a status ping every hour so we know the bot is alive."""
    while True:
        await asyncio.sleep(3600)
        try:
            await tg_send(_build_status(bot))
        except Exception as exc:
            logger.error("heartbeat error: %s", exc)
