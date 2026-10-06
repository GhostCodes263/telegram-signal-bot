"""
tracker.py - dual-channel routing, auto scanner and live trade tracker.

* publish_signal():   stores the trade, posts the FULL signal + chart to the VIP
                      channel and a teaser to the PUBLIC channel.
* auto_scan_job():    periodically scans enabled symbols and publishes aligned signals.
* track_trades_job(): runs every TRACK_INTERVAL_SECONDS (default 60) and compares live
                      prices with all active trades:
                        - TP1 touched  -> VIP "move SL to breakeven" alert + public win post
                        - TP2 touched  -> trade closed, VIP + public win post
                        - SL touched   -> trade closed, VIP notice (public only if enabled)
                        - back at entry after TP1 -> closed at breakeven
                        - indicators flip against the trade -> early-exit alert, trade closed

Conservative rule: if SL and TP are both touched inside the same 1-minute sample, the
outcome is decided by where the current price is (profit side -> TP, otherwise SL).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import chart_generator
import config
import db
import signal_engine
from signal_engine import Quote, Signal

log = logging.getLogger(__name__)

_last_invalidation_check: Dict[int, float] = {}


# --------------------------------------------------------------------------- #
# Telegram helpers
# --------------------------------------------------------------------------- #
def _teaser_markup() -> Optional[InlineKeyboardMarkup]:
    if config.BOT_USERNAME:
        url = f"https://t.me/{config.BOT_USERNAME}?start=vip"
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔓 Unlock VIP Signals", url=url)]])
    if config.SUPPORT_USERNAME:
        url = f"https://t.me/{config.SUPPORT_USERNAME}"
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔓 Get VIP Access", url=url)]])
    return None


async def _send(bot: Bot, chat_id: int, text: str,
                reply_markup: Optional[InlineKeyboardMarkup] = None) -> Optional[int]:
    """Send an HTML message; returns message_id or None on failure/unconfigured chat."""
    if not chat_id:
        return None
    try:
        msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
                                     reply_markup=reply_markup)
        return msg.message_id
    except TelegramError as exc:
        log.error("Failed to send message to %s: %s", chat_id, exc)
        return None


def _fp(spec: config.SymbolSpec, price: float) -> str:
    return config.fmt_price(spec, price)


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #
async def publish_signal(bot: Bot, signal: Signal) -> Optional[int]:
    """Persist + broadcast a signal. Returns the trade id, or None if delivery to VIP failed."""
    spec = config.SYMBOLS[signal.symbol]
    rr = signal.rr_tp2
    trade_id = db.create_trade(
        signal.symbol, signal.direction, signal.entry, signal.sl, signal.tp1, signal.tp2,
        signal.atr, signal.sl_pips, signal.tp1_pips, signal.tp2_pips, rr,
    )
    caption = signal_engine.format_signal_caption(signal, trade_id)

    chart = None
    try:
        chart = await asyncio.to_thread(chart_generator.render_signal_chart, spec, signal)
    except Exception:  # noqa: BLE001
        log.exception("Chart generation failed for trade #%s", trade_id)

    vip_msg_id: Optional[int] = None
    if config.VIP_CHANNEL_ID:
        try:
            if chart is not None:
                msg = await bot.send_photo(chat_id=config.VIP_CHANNEL_ID, photo=chart,
                                           caption=caption, parse_mode=ParseMode.HTML)
            else:
                msg = await bot.send_message(chat_id=config.VIP_CHANNEL_ID, text=caption,
                                             parse_mode=ParseMode.HTML)
            vip_msg_id = msg.message_id
        except TelegramError as exc:
            log.error("VIP delivery failed for trade #%s: %s", trade_id, exc)
    if vip_msg_id is None:
        db.update_trade(trade_id, status="CANCELLED", closed_at=db.utcnow_iso(),
                        notes="VIP channel delivery failed or channel not configured")
        return None

    db.update_trade(trade_id, vip_msg_id=vip_msg_id)

    teaser = (
        f"🚨 <b>NEW VIP SIGNAL — {signal.symbol}</b>  <i>#{trade_id}</i>\n"
        f"━━━━━━━━━━━━━━\n"
        f"📊 15M · 1H · 4H trends aligned\n"
        f"⚖️ Risk/Reward up to 1:{rr:g}\n"
        f"🔒 Direction, entry, SL & targets are VIP-only.\n"
        f"━━━━━━━━━━━━━━\n"
        f"👇 Join VIP to trade the next one live."
    )
    public_msg_id = await _send(bot, config.PUBLIC_CHANNEL_ID, teaser, _teaser_markup())
    if public_msg_id:
        db.update_trade(trade_id, public_msg_id=public_msg_id)

    log.info("Published %s %s as trade #%s", signal.direction, signal.symbol, trade_id)
    return trade_id


# --------------------------------------------------------------------------- #
# Auto scanner
# --------------------------------------------------------------------------- #
async def auto_scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not config.AUTO_SCAN_ENABLED or not config.VIP_CHANNEL_ID:
        return
    try:
        if config.MARKET_HOURS_FILTER and not config.is_forex_market_open():
            log.debug("Auto scan skipped: market closed")
            return
        for symbol in list(config.SYMBOLS):
            if db.count_active_trades() >= config.MAX_ACTIVE_TRADES:
                log.debug("Auto scan stopped: max active trades reached")
                break
            if db.get_active_trade_for_symbol(symbol):
                continue
            last = db.last_trade_opened_at(symbol)
            if last and (db.utcnow() - last).total_seconds() < config.SIGNAL_COOLDOWN_MINUTES * 60:
                continue

            result = await asyncio.to_thread(signal_engine.generate_signal, symbol)
            if result.status == signal_engine.SIGNAL and result.signal is not None:
                await publish_signal(context.bot, result.signal)
            else:
                log.debug("No signal for %s: [%s] %s", symbol, result.status, result.message)
            await asyncio.sleep(1)  # be gentle with TradingView
    except Exception:  # noqa: BLE001
        log.exception("Auto scan job failed")


# --------------------------------------------------------------------------- #
# Result handlers
# --------------------------------------------------------------------------- #
async def _handle_tp1(bot: Bot, trade: Dict[str, Any], spec: config.SymbolSpec) -> None:
    db.update_trade(trade["id"], status="TP1_HIT", tp1_hit_at=db.utcnow_iso())
    pips = trade["tp1_pips"]
    await _send(
        bot, config.VIP_CHANNEL_ID,
        f"🎯 <b>TP1 HIT — {trade['symbol']}</b>  <i>#{trade['id']}</i>\n"
        f"✅ +{pips:g} pips banked\n\n"
        f"🛡 <b>Move your SL to BREAKEVEN</b> (<code>{_fp(spec, trade['entry'])}</code>) "
        f"and consider taking partial profits.\n"
        f"🏆 TP2 target: <code>{_fp(spec, trade['tp2'])}</code>",
    )
    await _send(
        bot, config.PUBLIC_CHANNEL_ID,
        f"✅ <b>TP1 SMASHED — {trade['symbol']}</b>  <i>#{trade['id']}</i>\n"
        f"💰 <b>+{pips:g} pips</b> in the VIP room 🎉\n"
        f"🔥 Want the next one live?",
        _teaser_markup(),
    )


async def _handle_tp2(bot: Bot, trade: Dict[str, Any], spec: config.SymbolSpec) -> None:
    if not trade.get("tp1_hit_at"):
        await _handle_tp1(bot, trade, spec)  # price gapped through TP1 - announce it first
    now = db.utcnow_iso()
    pips = trade["tp2_pips"]
    db.update_trade(trade["id"], status="TP2_HIT", closed_at=now, close_price=trade["tp2"], result_pips=pips)
    await _send(
        bot, config.VIP_CHANNEL_ID,
        f"🏆 <b>TP2 HIT — {trade['symbol']}</b>  <i>#{trade['id']}</i>\n"
        f"💎 +{pips:g} pips (1:2) — full target reached. Trade closed.",
    )
    await _send(
        bot, config.PUBLIC_CHANNEL_ID,
        f"🏆 <b>TP2 HIT — {trade['symbol']}</b>  <i>#{trade['id']}</i>\n"
        f"💎 <b>+{pips:g} pips</b> — full target smashed! 🚀\n"
        f"👑 VIP members caught the whole move.",
        _teaser_markup(),
    )


async def _handle_sl(bot: Bot, trade: Dict[str, Any], spec: config.SymbolSpec) -> None:
    pips = -float(trade["sl_pips"])
    db.update_trade(trade["id"], status="SL_HIT", closed_at=db.utcnow_iso(),
                    close_price=trade["sl"], result_pips=pips)
    await _send(
        bot, config.VIP_CHANNEL_ID,
        f"❌ <b>SL HIT — {trade['symbol']}</b>  <i>#{trade['id']}</i>\n"
        f"{pips:g} pips. Trade closed. Stay disciplined — the next setup is coming.",
    )
    if config.PUBLIC_POST_LOSSES:
        await _send(
            bot, config.PUBLIC_CHANNEL_ID,
            f"❌ <b>SL HIT — {trade['symbol']}</b>  <i>#{trade['id']}</i>  ({pips:g} pips)\n"
            f"Losses are part of trading — risk management keeps us in the game.",
        )


async def _handle_breakeven(bot: Bot, trade: Dict[str, Any], spec: config.SymbolSpec) -> None:
    db.update_trade(trade["id"], status="BE_HIT", closed_at=db.utcnow_iso(),
                    close_price=trade["entry"], result_pips=0.0)
    await _send(
        bot, config.VIP_CHANNEL_ID,
        f"🛡 <b>BREAKEVEN — {trade['symbol']}</b>  <i>#{trade['id']}</i>\n"
        f"Price returned to entry after TP1 (+{trade['tp1_pips']:g} pips was reached). "
        f"Trade closed at breakeven.",
    )


async def _handle_invalidation(bot: Bot, trade: Dict[str, Any], spec: config.SymbolSpec,
                               price: float, detail: str) -> None:
    sign = 1 if trade["direction"] == "BUY" else -1
    pips = round(sign * (price - trade["entry"]) / spec.pip_size, 1)
    db.update_trade(trade["id"], status="INVALIDATED", closed_at=db.utcnow_iso(),
                    close_price=price, result_pips=pips, notes=f"Early invalidation: {detail}")
    await _send(
        bot, config.VIP_CHANNEL_ID,
        f"⚠️ <b>EARLY EXIT — {trade['symbol']}</b>  <i>#{trade['id']}</i>\n"
        f"Indicators have reversed against this {trade['direction']} ({detail}).\n"
        f"📉 Setup invalidated — consider closing manually near "
        f"<code>{_fp(spec, price)}</code> ({pips:+g} pips).",
    )


# --------------------------------------------------------------------------- #
# Price evaluation
# --------------------------------------------------------------------------- #
async def _process_trade(bot: Bot, trade: Dict[str, Any], spec: config.SymbolSpec,
                         quote: Quote, now: datetime) -> bool:
    """Evaluate one trade. Returns True if the trade is still active afterwards."""
    is_buy = trade["direction"] == "BUY"
    sign = 1 if is_buy else -1
    entry, sl, tp1, tp2 = trade["entry"], trade["sl"], trade["tp1"], trade["tp2"]

    opened = db.parse_ts(trade["opened_at"])
    age = (now - opened).total_seconds() if opened else 10_000.0
    px = quote.price
    # Right after opening the 1-minute candle may contain pre-entry price action: use spot only.
    hi, lo = (quote.high, quote.low) if age >= 90 else (px, px)
    hi, lo = max(hi, px), min(lo, px)
    favourable = hi if is_buy else lo
    adverse = lo if is_buy else hi

    def reached(level: float) -> bool:
        return favourable >= level if is_buy else favourable <= level

    def breached(level: float) -> bool:
        return adverse <= level if is_buy else adverse >= level

    in_profit_now = (px - entry) * sign > 0
    status = trade["status"]

    if status == "OPEN":
        sl_hit, tp1_hit, tp2_hit = breached(sl), reached(tp1), reached(tp2)
        if sl_hit and not ((tp1_hit or tp2_hit) and in_profit_now):
            await _handle_sl(bot, trade, spec)
            return False
        if tp2_hit:
            await _handle_tp2(bot, trade, spec)
            return False
        if tp1_hit:
            await _handle_tp1(bot, trade, spec)
            return True
    elif status == "TP1_HIT":
        be_hit, tp2_hit = breached(entry), reached(tp2)
        if be_hit and not (tp2_hit and in_profit_now):
            await _handle_breakeven(bot, trade, spec)
            return False
        if tp2_hit:
            await _handle_tp2(bot, trade, spec)
            return False

    # Early invalidation (only for trades that have not reached TP1 yet)
    if status == "OPEN" and age >= config.INVALIDATION_MIN_AGE_MINUTES * 60:
        last = _last_invalidation_check.get(trade["id"], 0.0)
        if time.monotonic() - last >= config.INVALIDATION_CHECK_SECONDS:
            _last_invalidation_check[trade["id"]] = time.monotonic()
            try:
                reversed_, detail = await asyncio.to_thread(
                    signal_engine.trend_reversed, trade["symbol"], trade["direction"]
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Invalidation check failed for #%s: %s", trade["id"], exc)
                return True
            if reversed_:
                await _handle_invalidation(bot, trade, spec, px, detail)
                return False
    return True


async def track_trades_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every TRACK_INTERVAL_SECONDS: compare live prices with all active trades."""
    try:
        trades = db.get_active_trades()
        if not trades:
            _last_invalidation_check.clear()
            return

        by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        for t in trades:
            by_symbol.setdefault(t["symbol"], []).append(t)

        now = db.utcnow()
        for symbol, group in by_symbol.items():
            spec = config.SYMBOL_CATALOG.get(symbol)
            if spec is None:
                log.error("Active trade for unknown symbol %s", symbol)
                continue
            try:
                quote = await asyncio.to_thread(signal_engine.get_live_quote, symbol)
            except Exception as exc:  # noqa: BLE001
                log.warning("Live price unavailable for %s: %s", symbol, exc)
                continue
            for trade in group:
                try:
                    active = await _process_trade(context.bot, trade, spec, quote, now)
                    if not active:
                        _last_invalidation_check.pop(trade["id"], None)
                except Exception:  # noqa: BLE001
                    log.exception("Failed processing trade #%s", trade["id"])
    except Exception:  # noqa: BLE001
        log.exception("Trade tracker job failed")
