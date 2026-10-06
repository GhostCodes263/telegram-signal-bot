"""
chart_generator.py - 15M candlestick chart with Entry / SL / TP1 / TP2 lines.

Candle history comes from Yahoo Finance's public chart endpoint (free, no key).
Because Yahoo prices can differ slightly from OANDA/TradingView (notably gold
futures), the candles are shifted so the last close matches the signal's entry
price. If no candle data can be fetched, a levels-only chart is rendered so the
VIP message can still be sent with an image.

Rendering uses matplotlib's object-oriented API (no pyplot), which is safe to
run in worker threads.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List

import requests
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

import config
from signal_engine import Signal

log = logging.getLogger(__name__)

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TelegramSignalBot/1.0)"}

# Palette
BG = "#0f1419"
GRID = "#1f2933"
TEXT = "#cbd5e1"
UP = "#26a69a"
DOWN = "#ef5350"
C_ENTRY = "#2979ff"   # blue
C_SL = "#ff1744"      # red
C_TP1 = "#00c853"     # green
C_TP2 = "#39ff14"     # bright green


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float


def fetch_candles(spec: config.SymbolSpec, limit: int = 70) -> List[Candle]:
    """Fetch recent 15-minute candles. Returns [] if every source fails."""
    for ticker in spec.yahoo_tickers:
        try:
            resp = requests.get(
                YAHOO_URL.format(ticker=ticker),
                params={"interval": "15m", "range": "2d"},
                headers=_HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()["chart"]["result"][0]
            timestamps = result["timestamp"]
            quote = result["indicators"]["quote"][0]
            candles: List[Candle] = []
            for i, ts in enumerate(timestamps):
                o, h, l, c = quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i]
                if None in (o, h, l, c):
                    continue
                candles.append(Candle(int(ts), float(o), float(h), float(l), float(c)))
            if len(candles) >= 20:
                return candles[-limit:]
            log.warning("Yahoo returned too few candles for %s (%d)", ticker, len(candles))
        except Exception as exc:  # noqa: BLE001
            log.warning("Candle fetch failed for %s via %s: %s", spec.name, ticker, exc)
    return []


def align_candles(candles: List[Candle], anchor: float) -> List[Candle]:
    """Shift candles so the last close equals `anchor`. Reject wildly different data."""
    if not candles:
        return []
    delta = anchor - candles[-1].close
    if anchor and abs(delta) / anchor > 0.02:
        log.warning("Candle data differs from anchor by more than 2%% (delta=%s); discarding.", delta)
        return []
    return [Candle(c.ts, c.open + delta, c.high + delta, c.low + delta, c.close + delta) for c in candles]


def render_chart(spec: config.SymbolSpec, signal: Signal, candles: List[Candle]) -> io.BytesIO:
    fp = lambda p: config.fmt_price(spec, p)  # noqa: E731
    levels = [
        ("ENTRY", signal.entry, C_ENTRY),
        ("SL", signal.sl, C_SL),
        ("TP1", signal.tp1, C_TP1),
        ("TP2", signal.tp2, C_TP2),
    ]

    fig = Figure(figsize=(10, 6), dpi=110, facecolor=BG)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111, facecolor=BG)

    n = len(candles)
    prices = [lv[1] for lv in levels]
    if candles:
        prices += [c.high for c in candles] + [c.low for c in candles]
    lo, hi = min(prices), max(prices)
    span = max(hi - lo, signal.entry * 0.0005)
    pad = span * 0.08
    ax.set_ylim(lo - pad, hi + pad)

    body_min = span * 0.0015
    width = 0.62
    for i, c in enumerate(candles):
        color = UP if c.close >= c.open else DOWN
        ax.vlines(i, c.low, c.high, color=color, linewidth=1.0, zorder=2)
        body_low = min(c.open, c.close)
        body_h = max(abs(c.close - c.open), body_min)
        ax.add_patch(Rectangle((i - width / 2, body_low), width, body_h,
                               facecolor=color, edgecolor=color, zorder=3))

    base = n if n else 40
    right_pad = 16
    ax.set_xlim(-1, base + right_pad)

    # Risk / reward zones
    ax.axhspan(min(signal.entry, signal.sl), max(signal.entry, signal.sl), color=C_SL, alpha=0.08, zorder=1)
    ax.axhspan(min(signal.entry, signal.tp2), max(signal.entry, signal.tp2), color=C_TP1, alpha=0.07, zorder=1)

    for name, price, color in levels:
        ax.axhline(price, color=color, linestyle="--", linewidth=1.4, zorder=4)
        ax.text(base + 0.6, price, f" {name}  {fp(price)}", color=color, fontsize=9,
                fontweight="bold", va="center", ha="left", zorder=5,
                bbox=dict(boxstyle="round,pad=0.2", facecolor=BG, edgecolor=color, linewidth=0.8))

    if not candles:
        ax.text(base / 2, (lo + hi) / 2, "Live candle feed unavailable\n(levels only)",
                color=TEXT, fontsize=11, ha="center", va="center", alpha=0.6)
    else:
        step = max(1, n // 8)
        ticks = list(range(0, n, step))
        labels = [datetime.fromtimestamp(candles[i].ts, tz=timezone.utc).strftime("%H:%M") for i in ticks]
        ax.set_xticks(ticks)
        ax.set_xticklabels(labels)

    arrow = "BUY" if signal.direction == "BUY" else "SELL"
    ax.set_title(f"{signal.symbol} · 15M · {arrow} signal   (R:R 1:{signal.rr_tp2:g})",
                 color="white", fontsize=13, fontweight="bold", loc="left", pad=12)
    ax.grid(color=GRID, linewidth=0.6, alpha=0.8, zorder=0)
    ax.tick_params(colors=TEXT, labelsize=8)
    ax.yaxis.tick_left()
    ax.yaxis.set_major_formatter(lambda v, _pos: fp(v))
    for spine in ax.spines.values():
        spine.set_color(GRID)

    fig.text(0.99, 0.012, "UTC · educational use only · not financial advice",
             color=TEXT, fontsize=7, ha="right", alpha=0.6)
    fig.subplots_adjust(left=0.07, right=0.985, top=0.92, bottom=0.08)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    buf.seek(0)
    buf.name = f"{signal.symbol}_signal.png"
    return buf


def render_signal_chart(spec: config.SymbolSpec, signal: Signal) -> io.BytesIO:
    """Fetch candles, align them to the entry price and render the PNG (blocking)."""
    candles = align_candles(fetch_candles(spec), signal.entry)
    return render_chart(spec, signal, candles)
