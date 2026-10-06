"""
db.py - thread-safe SQLite persistence layer.

A single shared connection guarded by an RLock is used. All calls are short
and local, so this is safe to call from async handlers and from worker
threads (asyncio.to_thread). Timestamps are stored as UTC ISO-8601 strings.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import config

log = logging.getLogger(__name__)

ACTIVE_STATUSES = ("OPEN", "TP1_HIT")
CLOSED_STATUSES = ("TP2_HIT", "SL_HIT", "BE_HIT", "INVALIDATED")

TRADE_COLUMNS = [
    "id", "symbol", "direction", "entry", "sl", "tp1", "tp2", "atr",
    "sl_pips", "tp1_pips", "tp2_pips", "rr", "status", "opened_at",
    "tp1_hit_at", "closed_at", "close_price", "result_pips",
    "vip_msg_id", "public_msg_id", "notes",
]

_UPDATABLE = set(TRADE_COLUMNS) - {"id"}

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    joined_at   TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vip_users (
    user_id          INTEGER PRIMARY KEY,
    start_date       TEXT NOT NULL,
    end_date         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active',   -- active | expired | revoked
    granted_by       INTEGER,
    warned_expiring  INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    direction     TEXT NOT NULL,                       -- BUY | SELL
    entry         REAL NOT NULL,
    sl            REAL NOT NULL,
    tp1           REAL NOT NULL,
    tp2           REAL NOT NULL,
    atr           REAL,
    sl_pips       REAL,
    tp1_pips      REAL,
    tp2_pips      REAL,
    rr            REAL,
    status        TEXT NOT NULL DEFAULT 'OPEN',        -- OPEN | TP1_HIT | TP2_HIT | SL_HIT | BE_HIT | INVALIDATED | CANCELLED
    opened_at     TEXT NOT NULL,
    tp1_hit_at    TEXT,
    closed_at     TEXT,
    close_price   REAL,
    result_pips   REAL,
    vip_msg_id    INTEGER,
    public_msg_id INTEGER,
    notes         TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_vip_status ON vip_users(status, end_date);
"""


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------- #
# Connection management
# --------------------------------------------------------------------------- #
def get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            directory = os.path.dirname(os.path.abspath(config.DB_PATH))
            os.makedirs(directory, exist_ok=True)
            conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            _conn = conn
        return _conn


def init_db() -> None:
    with _lock:
        get_conn().executescript(SCHEMA)
    log.info("Database ready at %s", config.DB_PATH)


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """Run a write statement. Returns lastrowid."""
    with _lock:
        cur = get_conn().execute(sql, tuple(params))
        return int(cur.lastrowid or 0)


def execute_rowcount(sql: str, params: Sequence[Any] = ()) -> int:
    """Run a write statement. Returns the number of affected rows."""
    with _lock:
        cur = get_conn().execute(sql, tuple(params))
        return int(cur.rowcount)


def query_all(sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    with _lock:
        cur = get_conn().execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def query_one(sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
    with _lock:
        cur = get_conn().execute(sql, tuple(params))
        row = cur.fetchone()
        return dict(row) if row else None


def ping() -> bool:
    try:
        return query_one("SELECT 1 AS ok") is not None
    except sqlite3.Error:
        log.exception("DB ping failed")
        return False


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
def upsert_user(user_id: int, username: Optional[str], first_name: Optional[str]) -> None:
    now = utcnow_iso()
    execute(
        """
        INSERT INTO users (user_id, username, first_name, joined_at, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_seen = excluded.last_seen
        """,
        (user_id, username, first_name, now, now),
    )


def get_all_user_ids() -> List[int]:
    return [r["user_id"] for r in query_all("SELECT user_id FROM users ORDER BY joined_at")]


def count_users() -> int:
    row = query_one("SELECT COUNT(*) AS n FROM users")
    return int(row["n"]) if row else 0


# --------------------------------------------------------------------------- #
# Trades
# --------------------------------------------------------------------------- #
def create_trade(symbol: str, direction: str, entry: float, sl: float, tp1: float, tp2: float,
                 atr: Optional[float], sl_pips: float, tp1_pips: float, tp2_pips: float, rr: float) -> int:
    return execute(
        """
        INSERT INTO trades (symbol, direction, entry, sl, tp1, tp2, atr, sl_pips, tp1_pips, tp2_pips,
                            rr, status, opened_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
        """,
        (symbol, direction, entry, sl, tp1, tp2, atr, sl_pips, tp1_pips, tp2_pips, rr, utcnow_iso()),
    )


def update_trade(trade_id: int, **fields: Any) -> None:
    if not fields:
        return
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"Unknown trade column(s): {sorted(bad)}")
    assignments = ", ".join(f"{col} = ?" for col in fields)
    execute(f"UPDATE trades SET {assignments} WHERE id = ?", (*fields.values(), trade_id))


def get_trade(trade_id: int) -> Optional[Dict[str, Any]]:
    return query_one("SELECT * FROM trades WHERE id = ?", (trade_id,))


def get_active_trades() -> List[Dict[str, Any]]:
    marks = ",".join("?" for _ in ACTIVE_STATUSES)
    return query_all(f"SELECT * FROM trades WHERE status IN ({marks}) ORDER BY id", ACTIVE_STATUSES)


def get_active_trade_for_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    marks = ",".join("?" for _ in ACTIVE_STATUSES)
    return query_one(
        f"SELECT * FROM trades WHERE symbol = ? AND status IN ({marks}) ORDER BY id DESC LIMIT 1",
        (symbol, *ACTIVE_STATUSES),
    )


def count_active_trades() -> int:
    marks = ",".join("?" for _ in ACTIVE_STATUSES)
    row = query_one(f"SELECT COUNT(*) AS n FROM trades WHERE status IN ({marks})", ACTIVE_STATUSES)
    return int(row["n"]) if row else 0


def last_trade_opened_at(symbol: str) -> Optional[datetime]:
    row = query_one(
        "SELECT opened_at FROM trades WHERE symbol = ? AND status != 'CANCELLED' ORDER BY id DESC LIMIT 1",
        (symbol,),
    )
    return parse_ts(row["opened_at"]) if row else None


def get_all_trades() -> List[Dict[str, Any]]:
    cols = ", ".join(TRADE_COLUMNS)
    return query_all(f"SELECT {cols} FROM trades ORDER BY id")


def trade_stats() -> Dict[str, Any]:
    closed = ",".join(f"'{s}'" for s in CLOSED_STATUSES)
    row = query_one(
        f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status IN ('OPEN','TP1_HIT') THEN 1 ELSE 0 END) AS active,
            SUM(CASE WHEN status IN ({closed}) THEN 1 ELSE 0 END) AS closed,
            SUM(CASE WHEN status IN ({closed}) AND tp1_hit_at IS NOT NULL THEN 1 ELSE 0 END) AS tp1_reached,
            SUM(CASE WHEN status = 'TP2_HIT' THEN 1 ELSE 0 END) AS tp2,
            SUM(CASE WHEN status = 'SL_HIT' THEN 1 ELSE 0 END) AS sl,
            SUM(CASE WHEN status = 'BE_HIT' THEN 1 ELSE 0 END) AS be,
            SUM(CASE WHEN status = 'INVALIDATED' THEN 1 ELSE 0 END) AS invalidated,
            COALESCE(SUM(CASE WHEN status IN ({closed}) THEN result_pips END), 0) AS net_pips
        FROM trades
        WHERE status != 'CANCELLED'
        """
    ) or {}
    keys = ["total", "active", "closed", "tp1_reached", "tp2", "sl", "be", "invalidated"]
    out: Dict[str, Any] = {k: int(row.get(k) or 0) for k in keys}
    out["net_pips"] = round(float(row.get("net_pips") or 0.0), 1)
    out["win_rate"] = round(100.0 * out["tp1_reached"] / out["closed"], 1) if out["closed"] else 0.0
    return out
