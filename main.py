"""
main.py - application entry point.

Wires handlers, schedules the background jobs (JobQueue / APScheduler) and starts polling:

  * track_trades_job   every TRACK_INTERVAL_SECONDS (60s)  - live price vs open trades
  * auto_scan_job      every SCAN_INTERVAL_SECONDS (300s)  - generate & publish signals
  * expiry_scan_job    every VIP_EXPIRY_SCAN_SECONDS (600s) - VIP expiry handling
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, ApplicationBuilder, ContextTypes

import admin_handlers
import bot_handlers
import config
import db
import tracker
import vip_manager

log = logging.getLogger("main")

USER_COMMANDS = [
    BotCommand("start", "Main menu"),
    BotCommand("signal", "Market analysis: /signal XAUUSD"),
    BotCommand("risk", "Position size guide: /risk 5000"),
    BotCommand("calendar", "High-impact news calendar"),
    BotCommand("vip_status", "Your VIP subscription"),
    BotCommand("help", "How the bot works"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand("grantvip", "Grant VIP: /grantvip <user_id> <days>"),
    BotCommand("revokevip", "Revoke VIP: /revokevip <user_id>"),
    BotCommand("broadcast", "Message all users"),
    BotCommand("stats", "Bot & trade statistics"),
    BotCommand("export_journal", "Download trade journal CSV"),
    BotCommand("ping", "Health check"),
]


def start_health_server() -> None:
    """Tiny HTTP endpoint so Render 'Web Service' health checks / uptime pingers work."""
    port = os.getenv("PORT")
    if not port:
        return

    class Health(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")

        def do_HEAD(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args) -> None:  # silence request logging
            return

    try:
        server = ThreadingHTTPServer(("0.0.0.0", int(port)), Health)
    except OSError as exc:
        log.warning("Health server could not bind to port %s: %s", port, exc)
        return
    threading.Thread(target=server.serve_forever, name="health-server", daemon=True).start()
    log.info("Health server listening on port %s", port)


async def post_init(app: Application) -> None:
    app.bot_data["started_at"] = time.time()
    try:
        await app.bot.set_my_commands(USER_COMMANDS)
        for admin_id in config.ADMIN_IDS:
            try:
                await app.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
            except TelegramError as exc:
                log.debug("Could not set admin commands for %s: %s", admin_id, exc)
    except TelegramError as exc:
        log.warning("Could not set bot commands: %s", exc)

    me = await app.bot.get_me()
    log.info("Bot started as @%s (id %s)", me.username, me.id)
    if not config.BOT_USERNAME and me.username:
        config.BOT_USERNAME = me.username  # used for teaser deep-links

    if config.NOTIFY_ADMINS_ON_START:
        await vip_manager.notify_admins(
            app.bot,
            f"🟢 <b>Signal bot online</b> (@{me.username})\n"
            f"Symbols: {', '.join(config.SYMBOLS)}\n"
            f"Auto-scan: {'ON' if config.AUTO_SCAN_ENABLED else 'OFF'} · "
            f"VIP channel: {'set' if config.VIP_CHANNEL_ID else 'NOT set'} · "
            f"Public channel: {'set' if config.PUBLIC_CHANNEL_ID else 'NOT set'}",
            parse_mode=ParseMode.HTML,
        )


async def post_shutdown(app: Application) -> None:
    db.close()
    log.info("Shutdown complete")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled exception while processing an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Something went wrong. Please try again in a moment.")
        except TelegramError:
            pass


def schedule_jobs(app: Application) -> None:
    jq = app.job_queue
    if jq is None:
        raise RuntimeError(
            "JobQueue is unavailable. Install dependencies with: "
            "pip install 'python-telegram-bot[job-queue]' apscheduler"
        )
    opts = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 30}
    jq.run_repeating(tracker.track_trades_job, interval=config.TRACK_INTERVAL_SECONDS, first=15,
                     name="track_trades", job_kwargs=opts)
    jq.run_repeating(tracker.auto_scan_job, interval=config.SCAN_INTERVAL_SECONDS, first=30,
                     name="auto_scan", job_kwargs=opts)
    jq.run_repeating(vip_manager.expiry_scan_job, interval=config.VIP_EXPIRY_SCAN_SECONDS, first=45,
                     name="vip_expiry", job_kwargs=opts)
    log.info("Jobs scheduled: tracker=%ss, scanner=%ss, vip_expiry=%ss",
             config.TRACK_INTERVAL_SECONDS, config.SCAN_INTERVAL_SECONDS, config.VIP_EXPIRY_SCAN_SECONDS)


def main() -> None:
    config.setup_logging()
    errors, warnings = config.validate_config()
    for w in warnings:
        log.warning(w)
    if errors:
        for e in errors:
            log.critical(e)
        sys.exit(1)

    db.init_db()

    app = (
        ApplicationBuilder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    bot_handlers.register(app)
    admin_handlers.register(app)
    app.add_error_handler(error_handler)
    schedule_jobs(app)

    start_health_server()
    log.info("Starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
