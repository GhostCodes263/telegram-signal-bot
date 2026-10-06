"""
vip_manager.py - VIP membership storage, access checks and the expiry scanner.

Table `vip_users` tracks start/end dates and status (active | expired | revoked).
The expiry scanner (JobQueue) flags expired subscriptions, notifies the user and
the admins, warns users shortly before expiry and (optionally) removes expired
members from the VIP channel.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import config
import db

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Access checks
# --------------------------------------------------------------------------- #
def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def get_record(user_id: int) -> Optional[Dict[str, Any]]:
    return db.query_one("SELECT * FROM vip_users WHERE user_id = ?", (user_id,))


def is_vip(user_id: int) -> bool:
    """True if the user has an active, non-expired subscription."""
    rec = get_record(user_id)
    if not rec or rec["status"] != "active":
        return False
    end = db.parse_ts(rec["end_date"])
    return bool(end and end > db.utcnow())


def has_vip_access(user_id: int) -> bool:
    """Admins always have access; everyone else needs an active subscription."""
    return is_admin(user_id) or is_vip(user_id)


def days_left(rec: Dict[str, Any]) -> int:
    end = db.parse_ts(rec["end_date"])
    if not end:
        return 0
    return max(0, math.ceil((end - db.utcnow()).total_seconds() / 86400))


def list_active() -> List[Dict[str, Any]]:
    return db.query_all(
        "SELECT * FROM vip_users WHERE status = 'active' ORDER BY end_date", ()
    )


def count_by_status() -> Dict[str, int]:
    rows = db.query_all("SELECT status, COUNT(*) AS n FROM vip_users GROUP BY status")
    out = {"active": 0, "expired": 0, "revoked": 0}
    for r in rows:
        out[r["status"]] = int(r["n"])
    return out


# --------------------------------------------------------------------------- #
# Grant / revoke
# --------------------------------------------------------------------------- #
def grant_vip(user_id: int, days: int, granted_by: Optional[int] = None) -> Tuple[datetime, datetime, bool]:
    """
    Grant or extend VIP. Returns (start, end, extended).
    If the user already has an active subscription, `days` are added to its end date.
    """
    now = db.utcnow().replace(microsecond=0)  # match the second-precision stored in SQLite
    rec = get_record(user_id)
    extended = False
    start = now
    base = now
    if rec and rec["status"] == "active":
        current_end = db.parse_ts(rec["end_date"])
        if current_end and current_end > now:
            base = current_end
            start = db.parse_ts(rec["start_date"]) or now
            extended = True
    end = base + timedelta(days=days)

    db.execute(
        """
        INSERT INTO vip_users (user_id, start_date, end_date, status, granted_by, warned_expiring, updated_at)
        VALUES (?, ?, ?, 'active', ?, 0, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            start_date = excluded.start_date,
            end_date = excluded.end_date,
            status = 'active',
            granted_by = excluded.granted_by,
            warned_expiring = 0,
            updated_at = excluded.updated_at
        """,
        (user_id, db.to_iso(start), db.to_iso(end), granted_by, db.utcnow_iso()),
    )
    log.info("VIP granted to %s until %s (extended=%s)", user_id, end.isoformat(), extended)
    return start, end, extended


def revoke_vip(user_id: int) -> bool:
    """Mark VIP as revoked. Returns False if the user had no VIP record."""
    changed = db.execute_rowcount(
        "UPDATE vip_users SET status = 'revoked', updated_at = ? WHERE user_id = ?",
        (db.utcnow_iso(), user_id),
    )
    if changed:
        log.info("VIP revoked for %s", user_id)
    return bool(changed)


# --------------------------------------------------------------------------- #
# Telegram helpers
# --------------------------------------------------------------------------- #
async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs: Any) -> bool:
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return True
    except TelegramError as exc:
        log.warning("Could not message %s: %s", chat_id, exc)
        return False


async def notify_admins(bot: Bot, text: str, parse_mode: Optional[str] = ParseMode.HTML) -> None:
    for admin_id in config.ADMIN_IDS:
        await safe_send(bot, admin_id, text, parse_mode=parse_mode)


async def create_invite_link(bot: Bot, user_id: int) -> Optional[str]:
    """Single-use VIP channel invite link valid for 24h (bot must be channel admin)."""
    if not config.VIP_CHANNEL_ID:
        return None
    try:
        link = await bot.create_chat_invite_link(
            chat_id=config.VIP_CHANNEL_ID,
            name=f"vip-{user_id}"[:32],
            member_limit=1,
            expire_date=db.utcnow() + timedelta(hours=24),
        )
        return link.invite_link
    except TelegramError as exc:
        log.warning("Could not create VIP invite link: %s", exc)
        return None


async def remove_from_vip_channel(bot: Bot, user_id: int) -> bool:
    """Kick (ban + unban) so the user can re-join after renewing."""
    if not (config.VIP_AUTO_KICK and config.VIP_CHANNEL_ID) or is_admin(user_id):
        return False
    try:
        await bot.ban_chat_member(chat_id=config.VIP_CHANNEL_ID, user_id=user_id)
        await bot.unban_chat_member(chat_id=config.VIP_CHANNEL_ID, user_id=user_id, only_if_banned=True)
        return True
    except TelegramError as exc:
        log.warning("Could not remove %s from VIP channel: %s", user_id, exc)
        return False


async def send_welcome(bot: Bot, user_id: int, end: datetime, extended: bool) -> Tuple[bool, Optional[str]]:
    """DM the user about a grant/extension. Returns (delivered, invite_link)."""
    link = await create_invite_link(bot, user_id)
    verb = "extended" if extended else "activated"
    text = (
        f"👑 <b>Your VIP membership has been {verb}!</b>\n"
        f"Valid until: <b>{end.strftime('%Y-%m-%d %H:%M')} UTC</b>\n\n"
    )
    if link:
        text += f"Join the VIP signal channel (single-use link, valid 24h):\n{link}\n\n"
    else:
        text += "Contact an admin if you cannot access the VIP channel.\n\n"
    text += "Use /vip_status any time to check your subscription."
    delivered = await safe_send(bot, user_id, text, parse_mode=ParseMode.HTML)
    return delivered, link


# --------------------------------------------------------------------------- #
# Expiry scanner (JobQueue)
# --------------------------------------------------------------------------- #
async def expiry_scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    bot = context.bot
    try:
        now = db.utcnow()
        now_iso = db.to_iso(now)

        # 1) Expired subscriptions
        expired = db.query_all(
            "SELECT * FROM vip_users WHERE status = 'active' AND end_date <= ?", (now_iso,)
        )
        for rec in expired:
            uid = rec["user_id"]
            db.execute(
                "UPDATE vip_users SET status = 'expired', updated_at = ? WHERE user_id = ?",
                (db.utcnow_iso(), uid),
            )
            removed = await remove_from_vip_channel(bot, uid)
            await safe_send(
                bot, uid,
                "⌛ <b>Your VIP membership has expired.</b>\n"
                "Contact an admin to renew and keep receiving full signals.",
                parse_mode=ParseMode.HTML,
            )
            await notify_admins(
                bot,
                f"⌛ VIP expired: <code>{uid}</code> (ended {rec['end_date']}). "
                f"{'Removed from VIP channel.' if removed else 'Channel removal not performed.'}",
            )
            log.info("VIP expired for %s", uid)

        # 2) Expiring-soon warnings (once per subscription)
        horizon = db.to_iso(now + timedelta(days=config.VIP_EXPIRY_WARNING_DAYS))
        soon = db.query_all(
            "SELECT * FROM vip_users WHERE status = 'active' AND warned_expiring = 0 "
            "AND end_date > ? AND end_date <= ?",
            (now_iso, horizon),
        )
        for rec in soon:
            uid = rec["user_id"]
            await safe_send(
                bot, uid,
                f"⏰ <b>VIP reminder:</b> your membership ends in {days_left(rec)} day(s) "
                f"({rec['end_date'][:16].replace('T', ' ')} UTC). Contact an admin to renew.",
                parse_mode=ParseMode.HTML,
            )
            db.execute("UPDATE vip_users SET warned_expiring = 1 WHERE user_id = ?", (uid,))
    except Exception:  # noqa: BLE001 - a scanner failure must never kill the job
        log.exception("VIP expiry scan failed")
