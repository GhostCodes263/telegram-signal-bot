"""
config.py - central configuration, environment loading and symbol metadata.

Every tunable value lives here so the rest of the code base never reads
os.environ directly. Values come from environment variables (optionally via
a local .env file loaded by python-dotenv).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Environment helpers
# --------------------------------------------------------------------------- #
def _get_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be a number, got {raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _get_int_list(name: str) -> List[int]:
    raw = os.getenv(name, "")
    out: List[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError as exc:
            raise ValueError(f"Environment variable {name} contains a non-integer value: {part!r}") from exc
    return out


# --------------------------------------------------------------------------- #
# Core settings
# --------------------------------------------------------------------------- #
BOT_TOKEN: str = _get_str("BOT_TOKEN")
ADMIN_IDS: List[int] = _get_int_list("ADMIN_IDS")
VIP_CHANNEL_ID: int = _get_int("VIP_CHANNEL_ID", 0)
PUBLIC_CHANNEL_ID: int = _get_int("PUBLIC_CHANNEL_ID", 0)
BOT_USERNAME: str = _get_str("BOT_USERNAME").lstrip("@")
SUPPORT_USERNAME: str = _get_str("SUPPORT_USERNAME").lstrip("@")

DB_PATH: str = _get_str("DB_PATH", "data/signal_bot.db")
LOG_LEVEL: str = _get_str("LOG_LEVEL", "INFO").upper()

# --------------------------------------------------------------------------- #
# Scheduler intervals (seconds)
# --------------------------------------------------------------------------- #
AUTO_SCAN_ENABLED: bool = _get_bool("AUTO_SCAN_ENABLED", True)
SCAN_INTERVAL_SECONDS: int = _get_int("SCAN_INTERVAL_SECONDS", 300)
TRACK_INTERVAL_SECONDS: int = _get_int("TRACK_INTERVAL_SECONDS", 60)
VIP_EXPIRY_SCAN_SECONDS: int = _get_int("VIP_EXPIRY_SCAN_SECONDS", 600)
INVALIDATION_CHECK_SECONDS: int = _get_int("INVALIDATION_CHECK_SECONDS", 300)
INVALIDATION_MIN_AGE_MINUTES: int = _get_int("INVALIDATION_MIN_AGE_MINUTES", 15)

# --------------------------------------------------------------------------- #
# Signal engine tuning
# --------------------------------------------------------------------------- #
ANALYSIS_CACHE_TTL: int = _get_int("ANALYSIS_CACHE_TTL", 20)  # seconds
TV_MAX_RETRIES: int = _get_int("TV_MAX_RETRIES", 3)
TREND_MIN_SCORE: int = _get_int("TREND_MIN_SCORE", 3)  # out of 4 indicator votes
RSI_BUY_MAX: float = _get_float("RSI_BUY_MAX", 72.0)
RSI_SELL_MIN: float = _get_float("RSI_SELL_MIN", 28.0)
REQUIRE_TV_SUMMARY_CONFIRM: bool = _get_bool("REQUIRE_TV_SUMMARY_CONFIRM", True)

ATR_SL_MULTIPLIER: float = _get_float("ATR_SL_MULTIPLIER", 1.5)
SL_BUFFER_PCT: float = _get_float("SL_BUFFER_PCT", 0.0002)  # 0.02% of price added to SL distance
SL_MIN_PCT: float = _get_float("SL_MIN_PCT", 0.0008)  # SL distance never below 0.08% of price
SL_MAX_PCT: float = _get_float("SL_MAX_PCT", 0.0060)  # SL distance never above 0.60% of price

SIGNAL_COOLDOWN_MINUTES: int = _get_int("SIGNAL_COOLDOWN_MINUTES", 60)
MAX_ACTIVE_TRADES: int = _get_int("MAX_ACTIVE_TRADES", 3)
MARKET_HOURS_FILTER: bool = _get_bool("MARKET_HOURS_FILTER", True)
SIGNAL_CMD_COOLDOWN_SECONDS: int = _get_int("SIGNAL_CMD_COOLDOWN_SECONDS", 8)

# --------------------------------------------------------------------------- #
# News guard
# --------------------------------------------------------------------------- #
NEWS_BLOCK_MINUTES_BEFORE: int = _get_int("NEWS_BLOCK_MINUTES_BEFORE", 30)
NEWS_BLOCK_MINUTES_AFTER: int = _get_int("NEWS_BLOCK_MINUTES_AFTER", 30)
NEWS_CACHE_SECONDS: int = _get_int("NEWS_CACHE_SECONDS", 3600)
NEWS_RETRY_SECONDS: int = _get_int("NEWS_RETRY_SECONDS", 600)
NEWS_FAIL_OPEN: bool = _get_bool("NEWS_FAIL_OPEN", True)  # allow signals if calendar is unreachable
NEWS_XML_UTC_OFFSET_HOURS: float = _get_float("NEWS_XML_UTC_OFFSET_HOURS", 0.0)
NEWS_JSON_URL: str = _get_str("NEWS_JSON_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
NEWS_XML_URL: str = _get_str("NEWS_XML_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.xml")

# --------------------------------------------------------------------------- #
# Channel / VIP behaviour
# --------------------------------------------------------------------------- #
PUBLIC_POST_LOSSES: bool = _get_bool("PUBLIC_POST_LOSSES", False)
NOTIFY_ADMINS_ON_START: bool = _get_bool("NOTIFY_ADMINS_ON_START", True)
VIP_EXPIRY_WARNING_DAYS: int = _get_int("VIP_EXPIRY_WARNING_DAYS", 3)
VIP_AUTO_KICK: bool = _get_bool("VIP_AUTO_KICK", True)

DISCLAIMER: str = (
    "Trading leveraged products carries a high risk of loss. Signals are for educational "
    "purposes only and are not financial advice."
)


# --------------------------------------------------------------------------- #
# Symbol catalog
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SymbolSpec:
    name: str  # internal / display symbol, e.g. XAUUSD
    label: str  # human readable name
    tv_symbol: str  # TradingView symbol
    screener: str
    exchange: str
    pip_size: float  # price distance of 1 pip
    decimals: int  # display precision
    pip_value_per_lot: float  # approx USD value of 1 pip for 1 standard lot
    default_sl_pips: float  # used by the /risk calculator
    currencies: Tuple[str, ...]  # currencies relevant for the news guard
    yahoo_tickers: Tuple[str, ...]  # candle sources for chart rendering (free, no key)


SYMBOL_CATALOG: Dict[str, SymbolSpec] = {
    "XAUUSD": SymbolSpec("XAUUSD", "Gold vs US Dollar", "XAUUSD", "forex", "OANDA",
                         0.1, 2, 10.0, 50.0, ("USD",), ("XAUUSD=X", "GC=F")),
    "EURUSD": SymbolSpec("EURUSD", "Euro vs US Dollar", "EURUSD", "forex", "OANDA",
                         0.0001, 5, 10.0, 15.0, ("EUR", "USD"), ("EURUSD=X",)),
    "GBPUSD": SymbolSpec("GBPUSD", "British Pound vs US Dollar", "GBPUSD", "forex", "OANDA",
                         0.0001, 5, 10.0, 20.0, ("GBP", "USD"), ("GBPUSD=X",)),
    "USDJPY": SymbolSpec("USDJPY", "US Dollar vs Japanese Yen", "USDJPY", "forex", "OANDA",
                         0.01, 3, 6.7, 15.0, ("USD", "JPY"), ("JPY=X",)),
    "AUDUSD": SymbolSpec("AUDUSD", "Australian Dollar vs US Dollar", "AUDUSD", "forex", "OANDA",
                         0.0001, 5, 10.0, 15.0, ("AUD", "USD"), ("AUDUSD=X",)),
}

_ENABLED = [s.strip().upper() for s in _get_str("SYMBOLS", "XAUUSD,EURUSD,GBPUSD,USDJPY,AUDUSD").split(",") if s.strip()]
SYMBOLS: Dict[str, SymbolSpec] = {n: SYMBOL_CATALOG[n] for n in _ENABLED if n in SYMBOL_CATALOG}

_ALIASES = {"GOLD": "XAUUSD", "XAU": "XAUUSD", "EU": "EURUSD", "GU": "GBPUSD", "UJ": "USDJPY", "AU": "AUDUSD"}


def normalize_symbol(raw: str) -> str:
    s = raw.strip().upper()
    for ch in ("/", "-", "_", " "):
        s = s.replace(ch, "")
    return _ALIASES.get(s, s)


def get_symbol(raw: str) -> Optional[SymbolSpec]:
    """Return the SymbolSpec for user input (accepts GOLD, xau/usd, eurusd ...)."""
    return SYMBOLS.get(normalize_symbol(raw))


def pips_between(spec: SymbolSpec, a: float, b: float) -> float:
    return round(abs(a - b) / spec.pip_size, 1)


def fmt_price(spec: SymbolSpec, price: float) -> str:
    return f"{price:.{spec.decimals}f}"


def is_forex_market_open(now: Optional[datetime] = None) -> bool:
    """Conservative FX/Gold weekly session: closed Fri 21:00 UTC -> Sun 22:00 UTC."""
    now = now or datetime.now(timezone.utc)
    wd, hour = now.weekday(), now.hour  # Mon=0 ... Sun=6
    if wd == 5:
        return False
    if wd == 4 and hour >= 21:
        return False
    if wd == 6 and hour < 22:
        return False
    return True


# --------------------------------------------------------------------------- #
# Validation / logging
# --------------------------------------------------------------------------- #
def validate_config() -> Tuple[List[str], List[str]]:
    """Return (errors, warnings). Errors are fatal, warnings are informational."""
    errors: List[str] = []
    warnings: List[str] = []
    if not BOT_TOKEN:
        errors.append("BOT_TOKEN is missing. Create a bot with @BotFather and set BOT_TOKEN.")
    if not ADMIN_IDS:
        warnings.append("ADMIN_IDS is empty - admin commands will be unavailable.")
    if not VIP_CHANNEL_ID:
        warnings.append("VIP_CHANNEL_ID is not set - automatic signal publishing is disabled.")
    if not PUBLIC_CHANNEL_ID:
        warnings.append("PUBLIC_CHANNEL_ID is not set - teasers and result posts are disabled.")
    if not SYMBOLS:
        errors.append("No valid symbols enabled. Check the SYMBOLS variable.")
    if TRACK_INTERVAL_SECONDS < 15:
        warnings.append("TRACK_INTERVAL_SECONDS is very low; TradingView may rate-limit you.")
    return errors, warnings


def setup_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        level=getattr(logging, LOG_LEVEL, logging.INFO),
    )
    # Keep third-party libraries quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
