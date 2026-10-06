"""
bot_handlers.py - user-facing commands and the inline keyboard menu.

Commands: /start, /help, /signal <symbol>, /risk <balance> [risk%], /calendar, /vip_status
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import chart_generator
import config
import db
import news_filter
import signal_engine
import vip_manager

log = logging.getLogger(__name__)

_last_signal_call: Dict[int, float] = {}


# --------------------------------------------------------------------------- #
# Keyboards
# --------------------------------------------------------------------------- #
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Live Signals", callback_data="menu:signals"),
         InlineKeyboardButton("🗓 News Calendar", callback_data="menu:calendar")],
        [InlineKeyboardButton("💰 Risk Calculator", callback_data="menu:risk"),
         InlineKeyboardButton("👑 VIP Status", callback_data="menu:vip")],
        [InlineKeyboardButton("❓ Help", callback_data="menu:help")],
    ])


def symbol_menu() -> InlineKeyboardMarkup:
    names = list(config.SYMBOLS)
    rows: List[List[InlineKeyboardButton]] = []
    for i in range(0, len(names), 2):
        rows.append([InlineKeyboardButton(n, callback_data=f"sig:{n}") for n in names[i:i + 2]])
    rows.append([InlineKeyboardButton("⬅️ Back to menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def back_menu(extra: Optional[List[InlineKeyboardButton]] = None) -> InlineKeyboardMarkup:
    rows = []
    if extra:
        rows.append(extra)
    rows.append([InlineKeyboardButton("⬅️ Back to menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def vip_cta_button() -> List[InlineKeyboardButton]:
    if config.SUPPORT_USERNAME:
        return [InlineKeyboardButton("👑 Get VIP Access", url=f"https://t.me/{config.SUPPORT_USERNAME}")]
    return [InlineKeyboardButton("👑 VIP Info", callback_data="menu:vip")]


# --------------------------------------------------------------------------- #
# Text builders
# --------------------------------------------------------------------------- #
def welcome_text(first_name: str) -> str:
    return (
        f"👋 <b>Welcome, {html.escape(first_name)}!</b>\n\n"
        "I deliver multi-timeframe <b>Forex &amp; Gold</b> signals backed by live TradingView data, "
        "with built-in news protection and live trade tracking.\n\n"
        f"📈 Markets: {', '.join(config.SYMBOLS)}\n"
        "Pick an option below 👇"
    )


def help_text() -> str:
    return (
        "<b>Commands</b>\n"
        "/start - main menu\n"
        "/signal &lt;symbol&gt; - analysis &amp; setup, e.g. <code>/signal XAUUSD</code>\n"
        "/risk &lt;balance&gt; [risk%] - position size guide, e.g. <code>/risk 5000 1</code>\n"
        "/calendar - upcoming high-impact news\n"
        "/vip_status - your subscription\n\n"
        "<b>How signals work</b>\n"
        "A setup is only issued when the 15M, 1H and 4H trends (EMA 20/50, MACD, RSI) all agree. "
        "Trades are paused 30 minutes before/after high-impact news.\n\n"
        f"<i>{html.escape(config.DISCLAIMER)}</i>"
    )


def format_analysis_text(spec: config.SymbolSpec, result: signal_engine.SignalResult) -> str:
    lines = [f"📊 <b>{spec.name}</b> — {html.escape(spec.label)}"]
    a = result.analysis
    if a:
        lines.append(f"💵 Price: <code>{config.fmt_price(spec, a.price)}</code>")
        for label, _ in signal_engine.TIMEFRAMES:
            snap, trend = a.snapshots[label], a.trends[label]
            rsi = f"{snap.rsi:.1f}" if snap.rsi is not None else "n/a"
            lines.append(
                f"{signal_engine.trend_icon(trend)} <b>{label}</b> {trend} · RSI {rsi} · "
                f"TV: {html.escape(snap.recommendation)}"
            )
    lines.append("")
    if result.status == signal_engine.SIGNAL:
        lines.append(f"✅ {html.escape(result.message)}")
    elif result.status == signal_engine.NO_SIGNAL:
        lines.append(f"⏳ No trade right now: {html.escape(result.message)}")
    elif result.status == signal_engine.NEWS_BLOCK:
        lines.append(f"📰 {html.escape(result.message)}")
    elif result.status == signal_engine.MARKET_CLOSED:
        lines.append(f"🌙 {html.escape(result.message)}")
    else:
        lines.append(f"⚠️ Market data problem: {html.escape(result.message)}")
    return "\n".join(lines)


def format_active_trade(trade: dict, spec: config.SymbolSpec) -> str:
    fp = lambda p: config.fmt_price(spec, p)  # noqa: E731
    state = "🛡 TP1 reached - SL at breakeven" if trade["status"] == "TP1_HIT" else "🟢 Running"
    return (
        f"\n📌 <b>Active trade #{trade['id']}</b> — {trade['direction']} ({state})\n"
        f"Entry <code>{fp(trade['entry'])}</code> · SL <code>{fp(trade['sl'])}</code>\n"
        f"TP1 <code>{fp(trade['tp1'])}</code> · TP2 <code>{fp(trade['tp2'])}</code>"
    )


def build_calendar_text() -> str:
    """Blocking (network). Run via asyncio.to_thread."""
    if not news_filter.feed_available():
        return "🗓 The economic calendar is temporarily unavailable. Please try again in a few minutes."
    events = news_filter.upcoming(hours=168, limit=15)
    if not events:
        return "🗓 No high-impact events found for the coming days. 🎉"
    now = db.utcnow()
    lines = ["🗓 <b>High-impact events (UTC)</b>", ""]
    for e in events:
        when = e.time_utc.strftime("%a %d %b %H:%M")
        delta = news_filter.humanize_delta(e.time_utc - now)
        extra = ""
        if e.forecast or e.previous:
            extra = f"\n    Fcst: {html.escape(e.forecast or '-')} · Prev: {html.escape(e.previous or '-')}"
        lines.append(f"🔴 <b>{html.escape(e.currency)}</b> {html.escape(e.title)}\n    {when} ({delta}){extra}")
    lines.append("")
    lines.append(f"🛑 Signals pause {config.NEWS_BLOCK_MINUTES_BEFORE}m before and "
                 f"{config.NEWS_BLOCK_MINUTES_AFTER}m after these releases.")
    return "\n".join(lines)


def build_risk_text(balance: float, risk_pct: float) -> str:
    risk_amt = balance * risk_pct / 100.0
    lines = [
        "💰 <b>Risk Calculator</b>",
        f"Balance: <b>${balance:,.2f}</b> · Risk per trade: <b>{risk_pct:g}%</b> = <b>${risk_amt:,.2f}</b>",
        "",
        "<b>Suggested lot size</b> (typical SL, approx. pip values):",
    ]
    for spec in config.SYMBOLS.values():
        lots = risk_amt / (spec.default_sl_pips * spec.pip_value_per_lot)
        lines.append(f"• <b>{spec.name}</b>: <code>{lots:.2f}</code> lots (SL {spec.default_sl_pips:g} pips)")
    lines += [
        "",
        "<b>Rule of thumb</b>",
        f"• 0.5% = ${balance * 0.005:,.2f} · 1% = ${balance * 0.01:,.2f} · 2% = ${balance * 0.02:,.2f}",
        "• lots = risk $ / (SL pips x pip value per lot)",
        "",
        "<i>Pip values are approximations (USDJPY varies with price). Always verify in your broker's "
        "calculator. Signals list their exact SL distance in pips.</i>",
    ]
    return "\n".join(lines)


def build_vip_text(user_id: int) -> str:
    if vip_manager.is_admin(user_id):
        return "🛡 <b>Admin account</b> — you have full access to all VIP features."
    rec = vip_manager.get_record(user_id)
    if rec and vip_manager.is_vip(user_id):
        return (
            "👑 <b>VIP Status: ACTIVE</b>\n"
            f"Started: {rec['start_date'][:16].replace('T', ' ')} UTC\n"
            f"Expires: {rec['end_date'][:16].replace('T', ' ')} UTC\n"
            f"⏳ Days left: <b>{vip_manager.days_left(rec)}</b>"
        )
    if rec:
        label = "EXPIRED" if rec["status"] in ("expired", "active") else rec["status"].upper()
        return (
            f"👑 <b>VIP Status: {label}</b>\n"
            f"Your last subscription ended {rec['end_date'][:16].replace('T', ' ')} UTC.\n"
            "Contact an admin to renew."
        )
    contact = f" @{config.SUPPORT_USERNAME}" if config.SUPPORT_USERNAME else " an admin"
    return (
        "👑 <b>VIP Status: not subscribed</b>\n\n"
        "VIP members receive:\n"
        "• Full signals: direction, entry, SL, TP1 &amp; TP2\n"
        "• Annotated 15M charts\n"
        "• Breakeven &amp; early-exit alerts\n\n"
        f"Contact{contact} to get access."
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _register_user(update: Update) -> None:
    user = update.effective_user
    if user and not user.is_bot:
        try:
            db.upsert_user(user.id, user.username, user.first_name)
        except Exception:  # noqa: BLE001
            log.exception("Could not register user %s", user.id)


def _rate_limited(user_id: int) -> bool:
    if vip_manager.is_admin(user_id):
        return False
    now = time.monotonic()
    if now - _last_signal_call.get(user_id, 0.0) < config.SIGNAL_CMD_COOLDOWN_SECONDS:
        return True
    _last_signal_call[user_id] = now
    return False


async def _edit_or_reply(update: Update, text: str, markup: Optional[InlineKeyboardMarkup] = None) -> None:
    """Edit the menu message when triggered from a button, otherwise send a new message."""
    query = update.callback_query
    if query and query.message:
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            log.debug("edit failed (%s), sending new message", exc)
        except TelegramError as exc:
            log.warning("edit failed: %s", exc)
    chat = update.effective_chat
    if chat:
        await chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def deliver_signal(bot, chat_id: int, user_id: int, raw_symbol: str) -> None:
    spec = config.get_symbol(raw_symbol)
    if spec is None:
        await bot.send_message(
            chat_id, f"❌ Unknown symbol <code>{html.escape(raw_symbol)}</code>.\n"
                     f"Supported: {', '.join(config.SYMBOLS)}",
            parse_mode=ParseMode.HTML, reply_markup=symbol_menu(),
        )
        return
    if _rate_limited(user_id):
        await bot.send_message(chat_id, "⏱ Please wait a few seconds between signal requests.")
        return

    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    result = await asyncio.to_thread(signal_engine.generate_signal, spec.name)
    vip = vip_manager.has_vip_access(user_id)
    active = db.get_active_trade_for_symbol(spec.name)

    text = format_analysis_text(spec, result)

    if vip:
        if active:
            text += "\n" + format_active_trade(active, spec)
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=back_menu())
        if not active and result.status == signal_engine.SIGNAL and result.signal is not None:
            caption = signal_engine.format_signal_caption(result.signal)
            caption += "\n\nℹ️ <i>Live analysis snapshot - not an automatically tracked trade.</i>"
            try:
                await bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
                chart = await asyncio.to_thread(chart_generator.render_signal_chart, spec, result.signal)
                await bot.send_photo(chat_id, photo=chart, caption=caption, parse_mode=ParseMode.HTML)
            except Exception:  # noqa: BLE001
                log.exception("Chart failed for manual signal")
                await bot.send_message(chat_id, caption, parse_mode=ParseMode.HTML)
    else:
        if result.status == signal_engine.SIGNAL:
            text += "\n\n🔒 <b>A setup is live!</b> Entry, SL and targets are VIP-only."
        elif active:
            text += "\n\n📌 A VIP trade is currently running on this pair. 🔒 Levels are VIP-only."
        else:
            text += "\n\n🔒 Upgrade to VIP to receive full signals with charts and alerts."
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                               reply_markup=back_menu(vip_cta_button()))


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _register_user(update)
    user, msg = update.effective_user, update.effective_message
    if not user or not msg:
        return
    if context.args and context.args[0].lower() == "vip":
        await msg.reply_text(build_vip_text(user.id), parse_mode=ParseMode.HTML, reply_markup=back_menu())
        return
    await msg.reply_text(welcome_text(user.first_name or "trader"), parse_mode=ParseMode.HTML,
                         reply_markup=main_menu())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _register_user(update)
    if update.effective_message:
        await update.effective_message.reply_text(help_text(), parse_mode=ParseMode.HTML,
                                                  reply_markup=back_menu())


async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _register_user(update)
    user, msg, chat = update.effective_user, update.effective_message, update.effective_chat
    if not (user and msg and chat):
        return
    if not context.args:
        await msg.reply_text("📊 Choose a market:", reply_markup=symbol_menu())
        return
    await deliver_signal(context.bot, chat.id, user.id, context.args[0])


async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _register_user(update)
    msg = update.effective_message
    if not msg:
        return
    usage = "Usage: <code>/risk &lt;balance&gt; [risk%]</code>\nExample: <code>/risk 5000 1</code>"
    if not context.args:
        await msg.reply_text(usage, parse_mode=ParseMode.HTML)
        return
    try:
        balance = float(context.args[0].replace(",", "").replace("$", ""))
        risk_pct = float(context.args[1].replace("%", "")) if len(context.args) > 1 else 1.0
    except ValueError:
        await msg.reply_text("❌ Invalid number.\n" + usage, parse_mode=ParseMode.HTML)
        return
    if balance <= 0 or balance > 1e9:
        await msg.reply_text("❌ Balance must be a positive number.", parse_mode=ParseMode.HTML)
        return
    if not 0 < risk_pct <= 10:
        await msg.reply_text("❌ Risk % must be between 0 and 10.", parse_mode=ParseMode.HTML)
        return
    await msg.reply_text(build_risk_text(balance, risk_pct), parse_mode=ParseMode.HTML,
                         reply_markup=back_menu())


async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _register_user(update)
    msg = update.effective_message
    if not msg:
        return
    text = await asyncio.to_thread(build_calendar_text)
    await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=back_menu())


async def cmd_vip_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _register_user(update)
    user, msg = update.effective_user, update.effective_message
    if not (user and msg):
        return
    await msg.reply_text(build_vip_text(user.id), parse_mode=ParseMode.HTML, reply_markup=back_menu())


# --------------------------------------------------------------------------- #
# Inline keyboard router
# --------------------------------------------------------------------------- #
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    _register_user(update)
    user, chat = update.effective_user, update.effective_chat
    if not (user and chat):
        return
    data = query.data

    if data == "menu:home":
        await _edit_or_reply(update, welcome_text(user.first_name or "trader"), main_menu())
    elif data == "menu:signals":
        await _edit_or_reply(update, "📊 <b>Choose a market to analyse:</b>", symbol_menu())
    elif data == "menu:calendar":
        await _edit_or_reply(update, "⏳ Loading calendar...")
        text = await asyncio.to_thread(build_calendar_text)
        await _edit_or_reply(update, text, back_menu())
    elif data == "menu:risk":
        await _edit_or_reply(
            update,
            "💰 <b>Risk Calculator</b>\n\nSend your balance with the command:\n"
            "<code>/risk 5000</code>  (1% risk)\n<code>/risk 5000 0.5</code>  (custom risk %)",
            back_menu(),
        )
    elif data == "menu:vip":
        await _edit_or_reply(update, build_vip_text(user.id), back_menu(vip_cta_button())
                             if not vip_manager.has_vip_access(user.id) else back_menu())
    elif data == "menu:help":
        await _edit_or_reply(update, help_text(), back_menu())
    elif data.startswith("sig:"):
        await deliver_signal(context.bot, chat.id, user.id, data.split(":", 1)[1])
    else:
        log.debug("Unhandled callback data: %s", data)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CommandHandler("risk", cmd_risk))
    app.add_handler(CommandHandler("calendar", cmd_calendar))
    app.add_handler(CommandHandler("vip_status", cmd_vip_status))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^(menu|sig):"))
