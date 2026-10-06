"""
signal_engine.py - market data, multi-timeframe alignment and SL/TP engine.

Data source: TradingView technical analysis via the free `tradingview-ta`
package (no API key). For each symbol the 15M, 1H and 4H indicator sets are
read (RSI, MACD, EMA20/EMA50, ATR). A signal is generated ONLY when all three
timeframes agree on the same direction.

All functions here are synchronous / blocking (network I/O). Call them from
async code with `asyncio.to_thread(...)`.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from tradingview_ta import Interval, TA_Handler

import config
import news_filter

log = logging.getLogger(__name__)

# (label, tradingview interval) - order matters: entry timeframe first.
TIMEFRAMES: Tuple[Tuple[str, str], ...] = (
    ("15M", Interval.INTERVAL_15_MINUTES),
    ("1H", Interval.INTERVAL_1_HOUR),
    ("4H", Interval.INTERVAL_4_HOURS),
)

# Result statuses
SIGNAL = "SIGNAL"
NO_SIGNAL = "NO_SIGNAL"
NEWS_BLOCK = "NEWS_BLOCK"
MARKET_CLOSED = "MARKET_CLOSED"
ERROR = "ERROR"


class MarketDataError(RuntimeError):
    """Raised when TradingView data cannot be retrieved."""


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #
@dataclass
class Snapshot:
    label: str
    close: float
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    rsi: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    ema20: Optional[float] = None
    ema50: Optional[float] = None
    atr: Optional[float] = None
    recommendation: str = "N/A"


@dataclass
class Quote:
    price: float
    high: float
    low: float


@dataclass
class Analysis:
    symbol: str
    snapshots: Dict[str, Snapshot]
    trends: Dict[str, str]  # label -> BULL | BEAR | NEUTRAL
    price: float
    atr: Optional[float]
    aligned: Optional[str]  # BUY | SELL | None


@dataclass
class Signal:
    symbol: str
    direction: str  # BUY | SELL
    entry: float
    sl: float
    tp1: float
    tp2: float
    atr: Optional[float]
    sl_pips: float
    tp1_pips: float
    tp2_pips: float
    trends: Dict[str, str]
    rsi_15m: Optional[float] = None
    rr_tp1: float = 1.0
    rr_tp2: float = 2.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class SignalResult:
    status: str
    symbol: str
    message: str = ""
    signal: Optional[Signal] = None
    analysis: Optional[Analysis] = None
    news_event: Optional[news_filter.NewsEvent] = None


# --------------------------------------------------------------------------- #
# TradingView access (with tiny TTL cache)
# --------------------------------------------------------------------------- #
_cache: Dict[Tuple[str, str], Tuple[float, Snapshot]] = {}
_cache_lock = threading.Lock()


def _num(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        f = float(value)  # type: ignore[arg-type]
        return f if f == f else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _fetch_snapshot(spec: config.SymbolSpec, label: str, interval: str, use_cache: bool = True) -> Snapshot:
    key = (spec.name, interval)
    if use_cache:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and time.monotonic() - hit[0] < config.ANALYSIS_CACHE_TTL:
                return hit[1]

    handler = TA_Handler(
        symbol=spec.tv_symbol,
        screener=spec.screener,
        exchange=spec.exchange,
        interval=interval,
        timeout=15,
    )
    analysis = None
    last_exc: Optional[Exception] = None
    for attempt in range(1, max(1, config.TV_MAX_RETRIES) + 1):
        try:
            analysis = handler.get_analysis()
            break
        except Exception as exc:  # noqa: BLE001 - library raises many exception types
            last_exc = exc
            log.warning("TradingView fetch failed for %s %s (attempt %d): %s", spec.name, label, attempt, exc)
            time.sleep(1.5 * attempt)
    if analysis is None:
        raise MarketDataError(f"TradingView unavailable for {spec.name} {label}: {last_exc}")

    ind = analysis.indicators or {}
    close = _num(ind.get("close"))
    if close is None:
        raise MarketDataError(f"No close price returned for {spec.name} {label}")

    summary = analysis.summary or {}
    snap = Snapshot(
        label=label,
        close=close,
        open=_num(ind.get("open")),
        high=_num(ind.get("high")),
        low=_num(ind.get("low")),
        rsi=_num(ind.get("RSI")),
        macd=_num(ind.get("MACD.macd")),
        macd_signal=_num(ind.get("MACD.signal")),
        ema20=_num(ind.get("EMA20")),
        ema50=_num(ind.get("EMA50")),
        atr=_num(ind.get("ATR")),
        recommendation=str(summary.get("RECOMMENDATION", "N/A")),
    )
    with _cache_lock:
        _cache[key] = (time.monotonic(), snap)
    return snap


def get_live_quote(symbol: str) -> Quote:
    """Latest price plus the current 1-minute candle's high/low (used by the tracker)."""
    spec = config.get_symbol(symbol)
    if spec is None:
        raise MarketDataError(f"Unknown symbol {symbol}")
    snap = _fetch_snapshot(spec, "1M", Interval.INTERVAL_1_MINUTE, use_cache=False)
    high = snap.high if snap.high is not None else snap.close
    low = snap.low if snap.low is not None else snap.close
    return Quote(price=snap.close, high=max(high, snap.close), low=min(low, snap.close))


# --------------------------------------------------------------------------- #
# Trend logic
# --------------------------------------------------------------------------- #
def classify_trend(s: Snapshot) -> str:
    """BULL / BEAR / NEUTRAL from EMA20/50, price vs EMA20, MACD and RSI (4 votes)."""
    values = (s.close, s.ema20, s.ema50, s.macd, s.macd_signal, s.rsi)
    if any(v is None for v in values):
        return "NEUTRAL"
    assert s.ema20 is not None and s.ema50 is not None and s.macd is not None
    assert s.macd_signal is not None and s.rsi is not None
    bull = sum([s.ema20 > s.ema50, s.close > s.ema20, s.macd > s.macd_signal, s.rsi > 50])
    bear = sum([s.ema20 < s.ema50, s.close < s.ema20, s.macd < s.macd_signal, s.rsi < 50])
    if s.ema20 > s.ema50 and bull >= config.TREND_MIN_SCORE:
        return "BULL"
    if s.ema20 < s.ema50 and bear >= config.TREND_MIN_SCORE:
        return "BEAR"
    return "NEUTRAL"


def trend_icon(trend: str) -> str:
    return {"BULL": "🟢", "BEAR": "🔴"}.get(trend, "⚪")


def analyze(symbol: str, use_cache: bool = True) -> Analysis:
    spec = config.get_symbol(symbol)
    if spec is None:
        raise MarketDataError(f"Unknown symbol {symbol}")
    snapshots: Dict[str, Snapshot] = {}
    trends: Dict[str, str] = {}
    for label, interval in TIMEFRAMES:
        snap = _fetch_snapshot(spec, label, interval, use_cache=use_cache)
        snapshots[label] = snap
        trends[label] = classify_trend(snap)

    values = set(trends.values())
    aligned: Optional[str] = None
    if values == {"BULL"}:
        aligned = "BUY"
    elif values == {"BEAR"}:
        aligned = "SELL"

    entry_tf = snapshots[TIMEFRAMES[0][0]]
    return Analysis(
        symbol=spec.name,
        snapshots=snapshots,
        trends=trends,
        price=entry_tf.close,
        atr=entry_tf.atr,
        aligned=aligned,
    )


def _tv_summary_conflicts(analysis: Analysis, direction: str) -> Optional[str]:
    """TradingView's own summary must not point the opposite way on any timeframe."""
    opposite = ("SELL", "STRONG_SELL") if direction == "BUY" else ("BUY", "STRONG_BUY")
    for label, snap in analysis.snapshots.items():
        if snap.recommendation in opposite:
            return f"TradingView {label} summary is {snap.recommendation}"
    return None


# --------------------------------------------------------------------------- #
# SL / TP engine
# --------------------------------------------------------------------------- #
def build_signal(spec: config.SymbolSpec, direction: str, entry: float, atr: Optional[float],
                 trends: Dict[str, str], rsi_15m: Optional[float] = None) -> Signal:
    """
    SL distance = ATR * multiplier + percentage buffer, clamped to [min%, max%] of price.
    TP1 = 1R, TP2 = 2R (R = SL distance).
    """
    dist = (atr or 0.0) * config.ATR_SL_MULTIPLIER + entry * config.SL_BUFFER_PCT
    dist = max(dist, entry * config.SL_MIN_PCT)
    dist = min(dist, entry * config.SL_MAX_PCT)

    sign = 1 if direction == "BUY" else -1
    d = spec.decimals
    entry_r = round(entry, d)
    sl = round(entry - sign * dist, d)
    tp1 = round(entry + sign * dist, d)
    tp2 = round(entry + sign * 2 * dist, d)

    return Signal(
        symbol=spec.name,
        direction=direction,
        entry=entry_r,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        atr=atr,
        sl_pips=config.pips_between(spec, entry_r, sl),
        tp1_pips=config.pips_between(spec, entry_r, tp1),
        tp2_pips=config.pips_between(spec, entry_r, tp2),
        trends=dict(trends),
        rsi_15m=rsi_15m,
    )


def generate_signal(symbol: str, check_news: bool = True, check_market_hours: bool = True) -> SignalResult:
    """Full pipeline: market hours -> news guard -> MTF analysis -> filters -> levels."""
    spec = config.get_symbol(symbol)
    if spec is None:
        return SignalResult(ERROR, symbol, f"Unsupported symbol: {symbol}")

    if check_market_hours and config.MARKET_HOURS_FILTER and not config.is_forex_market_open():
        return SignalResult(MARKET_CLOSED, spec.name, "Forex & Gold markets are closed for the weekend.")

    if check_news:
        event = news_filter.check_news_block(spec.currencies)
        if event is not None:
            return SignalResult(
                NEWS_BLOCK, spec.name,
                f"Signals paused around high-impact news: {event.currency} {event.title}",
                news_event=event,
            )
        if not config.NEWS_FAIL_OPEN and not news_filter.feed_available():
            return SignalResult(NEWS_BLOCK, spec.name,
                                "Economic calendar unavailable - signals paused (NEWS_FAIL_OPEN=false).")

    try:
        analysis = analyze(spec.name)
    except MarketDataError as exc:
        return SignalResult(ERROR, spec.name, str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error analysing %s", spec.name)
        return SignalResult(ERROR, spec.name, f"Unexpected error: {exc}")

    if analysis.aligned is None:
        trends = ", ".join(f"{k}: {v}" for k, v in analysis.trends.items())
        return SignalResult(NO_SIGNAL, spec.name, f"Timeframes are not aligned ({trends}).", analysis=analysis)

    direction = analysis.aligned
    rsi_15m = analysis.snapshots["15M"].rsi
    if rsi_15m is not None:
        if direction == "BUY" and rsi_15m > config.RSI_BUY_MAX:
            return SignalResult(NO_SIGNAL, spec.name,
                                f"Aligned bullish, but 15M RSI {rsi_15m:.1f} is overbought - waiting for a pullback.",
                                analysis=analysis)
        if direction == "SELL" and rsi_15m < config.RSI_SELL_MIN:
            return SignalResult(NO_SIGNAL, spec.name,
                                f"Aligned bearish, but 15M RSI {rsi_15m:.1f} is oversold - waiting for a pullback.",
                                analysis=analysis)

    if config.REQUIRE_TV_SUMMARY_CONFIRM:
        conflict = _tv_summary_conflicts(analysis, direction)
        if conflict:
            return SignalResult(NO_SIGNAL, spec.name, f"Aligned {direction}, but {conflict}.", analysis=analysis)

    signal = build_signal(spec, direction, analysis.price, analysis.atr, analysis.trends, rsi_15m)
    return SignalResult(SIGNAL, spec.name, f"{direction} setup confirmed on 15M / 1H / 4H.",
                        signal=signal, analysis=analysis)


def trend_reversed(symbol: str, direction: str) -> Tuple[bool, str]:
    """Early-invalidation check: both 15M and 1H trends flipped against the open trade."""
    analysis = analyze(symbol)
    opposing = "BEAR" if direction == "BUY" else "BULL"
    t15, t1h = analysis.trends["15M"], analysis.trends["1H"]
    detail = f"15M {t15}, 1H {t1h}, 4H {analysis.trends['4H']}"
    return (t15 == opposing and t1h == opposing), detail


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def format_signal_caption(signal: Signal, trade_id: Optional[int] = None) -> str:
    """Full VIP signal text (Telegram HTML)."""
    spec = config.SYMBOL_CATALOG[signal.symbol]
    fp = lambda p: config.fmt_price(spec, p)  # noqa: E731
    is_buy = signal.direction == "BUY"
    head = "🟢" if is_buy else "🔴"
    tag = f"  <i>#{trade_id}</i>" if trade_id is not None else ""
    tf_line = " · ".join(f"{label} {trend_icon(t)}" for label, t in signal.trends.items())
    rsi_line = f"\n📈 RSI (15M): <code>{signal.rsi_15m:.1f}</code>" if signal.rsi_15m is not None else ""
    return (
        f"{head} <b>{signal.direction} {signal.symbol}</b>{tag}\n"
        f"━━━━━━━━━━━━━━\n"
        f"📍 <b>Entry:</b> <code>{fp(signal.entry)}</code>\n"
        f"🛑 <b>SL:</b> <code>{fp(signal.sl)}</code> ({signal.sl_pips:g} pips)\n"
        f"🎯 <b>TP1:</b> <code>{fp(signal.tp1)}</code> (+{signal.tp1_pips:g} pips · 1:1)\n"
        f"🏆 <b>TP2:</b> <code>{fp(signal.tp2)}</code> (+{signal.tp2_pips:g} pips · 1:2)\n"
        f"⚖️ <b>R:R:</b> 1:{signal.rr_tp2:g}\n"
        f"━━━━━━━━━━━━━━\n"
        f"📊 {tf_line}{rsi_line}\n"
        f"⚠️ Risk 1-2% max. When TP1 hits, move SL to breakeven."
    )
