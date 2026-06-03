# CHANGELOG

## 2026-06-02

### `872d078` — EMA50(1h) como filtro dual en los 3 bots

**Archivos:** `indicators.py`, `signal_engine.py` (scalping-ema200, scalping-ema200-long, scalping-eth)

Agrega el método `ema50_1h()` en `indicators.py` reutilizando el buffer H1 existente.
Modifica las condiciones de señal en `signal_engine.py`:

- **LONG**: `precio > EMA50 AND precio > EMA200 AND RSI < 35`
- **SHORT**: `precio < EMA50 AND precio < EMA200 AND RSI > 65`

El filtro dual elimina señales cuando el precio está en la zona de indecisión entre las dos EMAs.
Los logs ahora incluyen `ema50` en cada señal y en el dict devuelto al risk engine.

---

### (hotfix, sin commit) — Servicio duplicado de Telegram deshabilitado

**Servicio:** `scalper-ema200.service`

`scalper-ema200.service` (servicio legacy) seguía corriendo en paralelo con
`scalper-btc-short.service`, ambos con el mismo token Telegram, causando error 409
(conflicto de polling). Se detuvo y deshabilitó con `systemctl disable scalper-ema200`.
Servicios activos: `scalper-btc-short`, `scalper-btc-long`, `scalper-eth`.

---

### `5d1bfc2` — Trailing Stop tras TP en los 3 bots

**Archivos:** `config.py`, `execution.py`, `main.py` (scalping-ema200, scalping-ema200-long, scalping-eth)

Implementa trailing stop que se activa únicamente después de que el precio supera el TP original (1%).

**Lógica:**
- **LONG**: registra `_trailing_max`; si el precio baja 0.5% desde ese máximo → cierra.
- **SHORT**: registra `_trailing_min`; si el precio sube 0.5% desde ese mínimo → cierra.

**LiveBroker**: al activarse el trailing, cancela la orden TP del exchange (`DELETE /fapi/v1/allOpenOrders`) y re-coloca solo el SL como protección (`STOP_MARKET`).

**Notificaciones Telegram:**
- Al activarse: `🎯 Trailing Stop activado | entry | TP @ precio | 0.5% desde extremo`
- Al cerrar: `✅/❌ TRADE CERRADO — Trailing Stop | entry → exit | PnL`

**Config:** añade `TRAILING_STOP_PCT = 0.005` (configurable vía env var).

---

### `b42ae72` — /posicion consolidado con EMAs, RSI y trailing

**Archivos:** `main.py`, `telegram.py` (scalping-ema200, scalping-ema200-long, scalping-eth)

`_write_bot_state()` en `main.py` ahora persiste en `bot_states.json`:
`price`, `ema50`, `ema200`, `rsi`, `regime`, `sl`, `tp`, `qty`,
`trailing_active`, `trailing_max`, `trailing_min`, `unrealized_pnl`.

`/posicion` en Telegram muestra los 3 bots consolidados en un solo mensaje:

```
📕 BTC-SHORT | 🔴 BEAR | FSM: IDLE
  Precio: 66242 | Capital: $114.66
  EMA50: 69646 | EMA200: 73437 | RSI: 27.36

  — si IN_POSITION —
  📕 SHORT | Entry: 66500 | SL: 66832 | TP: 65835
  PnL no realizado: +$1.2400
  🎯 Trailing activo | mín: 65720 | exit si ≥ 66049
```
