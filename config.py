import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

TZ_LOCAL = ZoneInfo("America/Santiago")

SYMBOL: str = os.getenv("SYMBOL", "BTCUSDT")
PAPER_MODE: bool = os.getenv("PAPER_MODE", "false").lower() == "true"
# SIDE_FILTER controls which signals are acted on: SHORT, LONG, or AUTO (both)
SIDE_FILTER: str = os.getenv("SIDE_FILTER", "AUTO").upper()
RISK_PCT: float = float(os.getenv("RISK_PCT", "0.01"))  # fraction of balance risked per trade

# Bot identity for shared state management
BOT_NAME: str = os.getenv("BOT_NAME", "btc-short")
BOT_ID: str = os.getenv("BOT_ID", BOT_NAME)
STATE_FILE: str = os.getenv("STATE_FILE", "/opt/bots/scalping-ema200/state.json")

# Shared infrastructure files
CB_FILE: str = "/opt/bots/circuit_breaker.json"
BOT_STATES_FILE: str = "/opt/bots/bot_states.json"

# Telegram polling: only one instance should poll (set false on bot-2, bot-3)
TG_POLLING: bool = os.getenv("TG_POLLING", "true").lower() == "true"

RSI_PERIOD: int = 14
RSI_OVERSOLD: float = 35.0
RSI_OVERBOUGHT: float = 65.0

ATR_PERIOD: int = 14
ATR_MULT_SL: float = 1.5
ATR_MULT_TP: float = 3.0   # ATR_MULT_TP / ATR_MULT_SL = 2.0 payoff

PAYOFF_MIN: float = 1.9    # reject signal if realized payoff < this
TAKER_FEE: float = 0.0004  # 0.04%
COOLDOWN_SECONDS: int = 300  # cooldown after any closed trade

# Indicator buffer capacities
H1_BUFFER_SIZE: int = 250
M1_BUFFER_SIZE: int = 120

# REST bootstrap candle counts
H1_BOOTSTRAP_LIMIT: int = 200
M1_BOOTSTRAP_LIMIT: int = 50

BINANCE_WS_BASE: str = "wss://stream.binance.com:9443/stream"
BINANCE_REST_BASE: str = "https://api.binance.com/api/v3"
BINANCE_FUTURES_REST_BASE: str = "https://fapi.binance.com"

# Binance API credentials (required for live balance fetch)
BINANCE_API_KEY: str    = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET: str = os.getenv("BINANCE_API_SECRET", "")

# Position sizing
POSITION_SIZE_PCT: float    = float(os.getenv("POSITION_SIZE_PCT", "0.10"))
MAX_LEVERAGE: float          = float(os.getenv("MAX_LEVERAGE", "1.0"))
MIN_BINANCE_NOTIONAL: float  = float(os.getenv("MIN_BINANCE_NOTIONAL", "20.0"))  # Binance Futures hard floor
SL_PCT: float            = float(os.getenv("SL_PCT", "0.0050"))
TP_PCT: float            = float(os.getenv("TP_PCT", "0.0100"))
TRAILING_STOP_PCT: float = float(os.getenv("TRAILING_STOP_PCT", "0.005"))
TRAILING_ACTIVATION_PCT: float = float(os.getenv("TRAILING_ACTIVATION_PCT", "0.003"))
REVERSAL_THRESHOLD: float = float(os.getenv("REVERSAL_THRESHOLD", "0.02"))  # 2% move triggers circuit-breaker reversal check

# ATR used only as a market-condition filter (not for SL/TP sizing)
ATR_FILTER_MIN: float    = float(os.getenv("ATR_FILTER_MIN", "10.0"))
ATR_FILTER_MAX: float    = float(os.getenv("ATR_FILTER_MAX", "200.0"))

# Telegram
TG_TOKEN: str   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
