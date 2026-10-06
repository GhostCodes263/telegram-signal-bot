"""
admin_handlers.py - admin-only commands.

/grantvip <user_id> <days>   /revokevip <user_id>   /broadcast <message>
/stats   /export_journal   /ping
"""
from __future__ import annotations

import asyncio
import csv
import functools
import html
import io
import logging
import time
from typing import Awaitable, Callable

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

import config
import db
import news_filter
import signal_engine
import vip_manager

log = logging.getLogger(__name__)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def admin_only(func: Handler) -> Handler:
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, msg = update.effective_user, update.effective_message
        if not user or not msg:
            return
        if not vip_manager.is_admin(user.id):
            log.warning("Unauthorized admin command from %s: %s", user.id, msg.text)
            await msg.reply_text("⛔ This command is for admins only.")
            return
        await func(update, context)

    return wrapper


@admin_only
async def cmd_grantvip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg, admin = update.effective_message, update.effective_user
    usage = "Usage: <code>/grantvip &lt;user_id&gt; &lt;days&gt;</code>"
    if len(context.args) != 2:
        await msg.reply_text(usage, parse_mode=ParseMode.HTML)
        return
    try:
        user_id, days = int(context.args[0]), int(context.args[1])
    except ValueError:
        await msg.reply_text("❌ user_id and days must be integers.\n" + usage, parse_mode=ParseMode.HTML)
        return
    if not 1 <= days <= 3650:
        await msg.reply_text("❌ days must be between 1 and 3650.")
        return

    start, end, extended = vip_manager.grant_vip(user_id, days, granted_by=admin.id)
    delivered, link = await vip_manager.send_welcome(context.bot, user_id, end, extended)
    verb = "extended" if extended else "granted"
    lines = [
        f"✅ VIP {verb} for <code>{user_id}</code>",
        f"Ends: <b>{end.strftime('%Y-%m-%d %H:%M')} UTC</b>",
        "📬 User notified." if delivered else
        "⚠️ Could not DM the user (they must press /start in the bot first).",
    ]
    if link and not delivered:
        lines.append(f"Invite link to forward manually:\n{link}")
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_revokevip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if len(context.args) != 1:
        await msg.reply_text("Usage: <code>/revokevip &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await msg.reply_text("❌ user_id must be an integer.")
        return
    if not vip_manager.revoke_vip(user_id):
        await msg.reply_text(f"ℹ️ User <code>{user_id}</code> has no VIP record.", parse_mode=ParseMode.HTML)
        return
    removed = await vip_manager.remove_from_vip_channel(context.bot, user_id)
    notified = await vip_manager.safe_send(
        context.bot, user_id, "🚫 Your VIP membership has been revoked by an admin."
    )
    await msg.reply_text(
        f"✅ VIP revoked for <code>{user_id}</code>.\n"
        f"{'Removed from VIP channel. ' if removed else 'Channel removal not performed. '}"
        f"{'User notified.' if notified else 'User could not be notified.'}",
        parse_mode=ParseMode.HTML,
    )


async def _run_broadcast(bot, admin_chat_id: int, text: str, user_ids: list) -> None:
    sent = failed = blocked = 0
    for uid in user_ids:
        try:
            await bot.send_message(chat_id=uid, text=text)
            sent += 1
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 1)
            try:
                await bot.send_message(chat_id=uid, text=text)
                sent += 1
            except TelegramError:
                failed += 1
        except Forbidden:
            blocked += 1
        except TelegramError as exc:
            failed += 1
            log.warning("Broadcast to %s failed: %s", uid, exc)
        await asyncio.sleep(0.05)  # stay well below Telegram's ~30 msg/s limit
    await vip_manager.safe_send(
        bot, admin_chat_id,
        f"📢 Broadcast finished.\n✅ Delivered: {sent}\n🚫 Blocked/inactive: {blocked}\n❌ Failed: {failed}",
    )


@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    parts = (msg.text or "").split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await msg.reply_text("Usage: /broadcast <message>")
        return
    text = parts[1].strip()
    if len(text) > 4000:
        await msg.reply_text("❌ Message too long (max 4000 characters).")
        return
    user_ids = db.get_all_user_ids()
    if not user_ids:
        await msg.reply_text("ℹ️ No registered users yet.")
        return
    await msg.reply_text(f"📢 Broadcasting to {len(user_ids)} users...")
    context.application.create_task(
        _run_broadcast(context.bot, update.effective_chat.id, text, user_ids),
        update=update,
    )


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = db.trade_stats()
    vip = vip_manager.count_by_status()
    text = (
        "📊 <b>Bot Statistics</b>\n\n"
        f"👥 Users: <b>{db.count_users()}</b>\n"
        f"👑 VIP active: <b>{vip['active']}</b> · expired: {vip['expired']} · revoked: {vip['revoked']}\n\n"
        f"📈 Trades total: <b>{s['total']}</b> (active: {s['active']}, closed: {s['closed']})\n"
        f"🎯 TP1 reached: {s['tp1_reached']} · 🏆 TP2: {s['tp2']}\n"
        f"🛡 Breakeven: {s['be']} · ❌ SL: {s['sl']} · ⚠️ Early exit: {s['invalidated']}\n"
        f"✅ TP1 hit-rate (closed trades): <b>{s['win_rate']}%</b>\n"
        f"💰 Net pips (full-position basis): <b>{s['net_pips']:+g}</b>"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


@admin_only
async def cmd_export_journal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    rows = db.get_all_trades()
    if not rows:
        await msg.reply_text("ℹ️ The trade journal is empty.")
        return
    text_buf = io.StringIO()
    writer = csv.DictWriter(text_buf, fieldnames=db.TRADE_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    data = io.BytesIO(text_buf.getvalue().encode("utf-8-sig"))  # BOM keeps Excel happy
    filename = f"trade_journal_{db.utcnow().strftime('%Y%m%d_%H%M')}.csv"
    await msg.reply_document(document=data, filename=filename,
                             caption=f"📒 Trade journal — {len(rows)} trades")


@admin_only
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    t0 = time.perf_counter()
    sent = await msg.reply_text("🏓 Pong...")
    latency_ms = (time.perf_counter() - t0) * 1000

    started = context.application.bot_data.get("started_at", time.time())
    uptime = int(time.time() - started)
    days, rem = divmod(uptime, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    db_ok = db.ping()

    probe = next(iter(config.SYMBOLS))
    try:
        quote = await asyncio.to_thread(signal_engine.get_live_quote, probe)
        tv = f"✅ OK ({probe} {quote.price})"
    except Exception as exc:  # noqa: BLE001
        tv = f"❌ {html.escape(str(exc))[:120]}"

    news = await asyncio.to_thread(news_filter.status)
    news_line = (f"✅ {news['high_impact']} high-impact events cached" if news["events"]
                 else f"⚠️ unavailable {html.escape(str(news['last_error']))[:100]}")

    text = (
        "🏓 <b>Pong!</b>\n"
        f"⚡ Latency: <b>{latency_ms:.0f} ms</b>\n"
        f"⏱ Uptime: {days}d {hours}h {minutes}m\n"
        f"🗄 Database: {'✅ OK' if db_ok else '❌ ERROR'}\n"
        f"📡 TradingView: {tv}\n"
        f"📰 News feed: {news_line}\n"
        f"📌 Active trades: {db.count_active_trades()}\n"
        f"🔁 Auto-scan: {'ON' if config.AUTO_SCAN_ENABLED else 'OFF'} "
        f"(every {config.SCAN_INTERVAL_SECONDS}s)"
    )
    try:
        await sent.edit_text(text, parse_mode=ParseMode.HTML)
    except TelegramError:
        await msg.reply_text(text, parse_mode=ParseMode.HTML)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("grantvip", cmd_grantvip))
    app.add_handler(CommandHandler("revokevip", cmd_revokevip))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("export_journal", cmd_export_journal))
    app.add_handler(CommandHandler("ping", cmd_ping))
