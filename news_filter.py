"""
news_filter.py - high-impact economic news guard.

Data source: the free Forex Factory weekly calendar mirror
(nfs.faireconomy.media) - JSON first, XML as fallback. No API key needed.
The feed is cached and fetched at most once per NEWS_CACHE_SECONDS; if a
refresh fails the last good data keeps being used.

Signals are blocked from NEWS_BLOCK_MINUTES_BEFORE minutes before to
NEWS_BLOCK_MINUTES_AFTER minutes after every "High" impact event that
affects one of the symbol's currencies (NFP, CPI, FOMC, rate decisions ...).
"""
from __future__ import annotations

import logging
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

import requests

import config

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TelegramSignalBot/1.0)",
    "Accept": "application/json, application/xml, text/xml, */*",
}


@dataclass(frozen=True)
class NewsEvent:
    title: str
    currency: str
    time_utc: datetime
    impact: str
    forecast: str = ""
    previous: str = ""


_lock = threading.RLock()
_events: List[NewsEvent] = []
_last_success: Optional[datetime] = None
_last_attempt_mono: float = 0.0
_last_error: str = ""


# --------------------------------------------------------------------------- #
# Fetching / parsing
# --------------------------------------------------------------------------- #
def _fetch_json() -> List[NewsEvent]:
    resp = requests.get(config.NEWS_JSON_URL, headers=_HEADERS, timeout=12)
    resp.raise_for_status()
    events: List[NewsEvent] = []
    for item in resp.json():
        try:
            dt = datetime.fromisoformat(str(item["date"]))
        except (KeyError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        events.append(
            NewsEvent(
                title=str(item.get("title", "")).strip(),
                currency=str(item.get("country", "")).strip().upper(),
                time_utc=dt.astimezone(timezone.utc),
                impact=str(item.get("impact", "")).strip().capitalize(),
                forecast=str(item.get("forecast", "") or "").strip(),
                previous=str(item.get("previous", "") or "").strip(),
            )
        )
    return events


def _text(node: ET.Element, tag: str) -> str:
    child = node.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _fetch_xml() -> List[NewsEvent]:
    resp = requests.get(config.NEWS_XML_URL, headers=_HEADERS, timeout=12)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    offset = timedelta(hours=config.NEWS_XML_UTC_OFFSET_HOURS)
    events: List[NewsEvent] = []
    for node in root.findall("event"):
        date_s, time_s = _text(node, "date"), _text(node, "time")
        try:
            naive = datetime.strptime(f"{date_s} {time_s}", "%m-%d-%Y %I:%M%p")
        except ValueError:
            continue  # "All Day" / "Tentative" events cannot be placed precisely
        dt = (naive - offset).replace(tzinfo=timezone.utc)
        events.append(
            NewsEvent(
                title=_text(node, "title"),
                currency=_text(node, "country").upper(),
                time_utc=dt,
                impact=_text(node, "impact").capitalize(),
                forecast=_text(node, "forecast"),
                previous=_text(node, "previous"),
            )
        )
    return events


def refresh(force: bool = False) -> bool:
    """Refresh the cache if stale. Returns True if data is available afterwards."""
    global _events, _last_success, _last_attempt_mono, _last_error
    with _lock:
        now = datetime.now(timezone.utc)
        fresh = _last_success is not None and (now - _last_success).total_seconds() < config.NEWS_CACHE_SECONDS
        if fresh and not force:
            return True
        mono = time.monotonic()
        if not force and _last_attempt_mono and mono - _last_attempt_mono < config.NEWS_RETRY_SECONDS:
            return bool(_events)
        _last_attempt_mono = mono

        errors: List[str] = []
        for name, fetcher in (("json", _fetch_json), ("xml", _fetch_xml)):
            try:
                events = fetcher()
                if not events:
                    raise ValueError("feed returned no events")
                _events = events
                _last_success = now
                _last_error = ""
                log.info("News calendar refreshed via %s (%d events)", name, len(events))
                return True
            except Exception as exc:  # noqa: BLE001 - network/parse errors of any kind
                errors.append(f"{name}: {exc}")
        _last_error = "; ".join(errors)
        log.warning("News calendar refresh failed (%s). Using %d cached events.", _last_error, len(_events))
        return bool(_events)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def get_events() -> List[NewsEvent]:
    refresh()
    with _lock:
        return list(_events)


def high_impact_events() -> List[NewsEvent]:
    return sorted((e for e in get_events() if e.impact == "High"), key=lambda e: e.time_utc)


def feed_available() -> bool:
    refresh()
    with _lock:
        return bool(_events)


def check_news_block(currencies: Iterable[str], now: Optional[datetime] = None) -> Optional[NewsEvent]:
    """Return the blocking high-impact event if `now` is inside a blackout window."""
    now = now or datetime.now(timezone.utc)
    wanted = {c.upper() for c in currencies}
    before = timedelta(minutes=config.NEWS_BLOCK_MINUTES_BEFORE)
    after = timedelta(minutes=config.NEWS_BLOCK_MINUTES_AFTER)
    for event in high_impact_events():
        if wanted and event.currency not in wanted:
            continue
        if event.time_utc - before <= now <= event.time_utc + after:
            return event
    return None


def upcoming(hours: float = 168, currencies: Optional[Iterable[str]] = None,
             limit: int = 15, now: Optional[datetime] = None) -> List[NewsEvent]:
    """High-impact events from now until now+hours (events within the after-window still count)."""
    now = now or datetime.now(timezone.utc)
    wanted = {c.upper() for c in currencies} if currencies else None
    start = now - timedelta(minutes=config.NEWS_BLOCK_MINUTES_AFTER)
    end = now + timedelta(hours=hours)
    out = [
        e for e in high_impact_events()
        if start <= e.time_utc <= end and (wanted is None or e.currency in wanted)
    ]
    return out[:limit]


def humanize_delta(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    sign = "in " if total >= 0 else ""
    suffix = "" if total >= 0 else " ago"
    total = abs(total)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        body = f"{days}d {hours}h"
    elif hours:
        body = f"{hours}h {minutes}m"
    else:
        body = f"{minutes}m"
    return f"{sign}{body}{suffix}"


def status() -> Dict[str, object]:
    with _lock:
        return {
            "events": len(_events),
            "high_impact": sum(1 for e in _events if e.impact == "High"),
            "last_success": _last_success.isoformat(timespec="seconds") if _last_success else None,
            "last_error": _last_error,
        }
