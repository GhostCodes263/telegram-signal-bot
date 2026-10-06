# Telegram Forex & Gold Signal Bot

A modular, async Telegram bot that runs a **dual-channel signal ecosystem**:

- **VIP channel** – full signals (symbol, direction, entry, SL, TP1, TP2, R:R) with an annotated 15M candlestick chart.
- **Public channel** – auto-generated teasers when a signal drops, plus celebratory result posts (+pips) when TP1 / TP2 is hit.

Market data is 100% free and needs **no API keys**: TradingView indicators via `tradingview-ta`, candles for charts from Yahoo Finance's public endpoint, and the economic calendar from a free Forex Factory mirror.

> ⚠️ **Disclaimer:** Educational software. Trading leveraged products carries a high risk of loss. Nothing this bot outputs is financial advice. Forward-test on a demo account before charging anyone for signals, and check the laws in your jurisdiction about distributing trading signals.

---

## Features

| Area | What it does |
|---|---|
| **Signal engine** (`signal_engine.py`) | Reads RSI, MACD, EMA20/50, ATR on **15M / 1H / 4H**. A signal fires **only if all three timeframes agree** (4-vote trend score per timeframe, plus RSI exhaustion and TradingView-summary filters). |
| **SL/TP engine** | SL = `ATR × 1.5 + 0.02% buffer`, clamped to 0.08%–0.6% of price. TP1 = 1R, TP2 = 2R. Pips use correct pip sizes (Gold = 0.1, JPY = 0.01, others 0.0001). |
| **Charts** (`chart_generator.py`) | Matplotlib candlesticks with Entry (blue), SL (red), TP1 (green), TP2 (bright green). Falls back to a levels-only chart if candle data is unreachable. |
| **News guard** (`news_filter.py`) | Pulls high-impact events (NFP, CPI, FOMC, …) and blocks signals 30 min before/after, per currency involved in the pair. |
| **Tracker** (`tracker.py`) | Every 60 s compares live prices to open trades. TP1 → "move SL to breakeven" alert + public win post. TP2 / SL / breakeven closes. If 15M **and** 1H trends flip against a trade → early-exit alert. |
| **VIP system** (`vip_manager.py`) | SQLite subscriptions, single-use invite links on grant, expiry scanner (flags, notifies user + admins, warns 3 days before, removes from VIP channel). |
| **Interfaces** | Inline-keyboard menu for users; admin command set. |

### User commands
`/start` · `/signal <symbol>` · `/risk <balance> [risk%]` · `/calendar` · `/vip_status` · `/help`

### Admin commands
`/grantvip <user_id> <days>` · `/revokevip <user_id>` · `/broadcast <message>` · `/stats` · `/export_journal` · `/ping`

Non-VIP users running `/signal` see the multi-timeframe analysis but not the entry/SL/TP levels. VIPs and admins see the full setup and chart. Manual `/signal` results are snapshots and are **not** tracked; only signals published by the auto-scanner are stored and tracked.

---

## Project layout

```
telegram-signal-bot/
├── config.py          # env loading, symbol catalog, helpers
├── db.py              # SQLite layer (users, vip_users, trades)
├── signal_engine.py   # TradingView data, MTF alignment, SL/TP
├── news_filter.py     # economic calendar + blackout windows
├── chart_generator.py # matplotlib candlestick charts
├── vip_manager.py     # VIP grants, checks, expiry scanner
├── bot_handlers.py    # user commands + inline menu
├── admin_handlers.py  # admin commands
├── tracker.py         # publishing, auto-scan, live trade tracking
├── main.py            # entry point + job scheduling
├── requirements.txt
├── .env.example
└── README.md
```

---

## Setup

### 1. Telegram
1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Create two channels: a **private VIP channel** and a **public channel**.
3. Add the bot as **administrator** to both (post messages permission; for the VIP channel also *invite users* and *ban users* so invite links and expiry removal work).
4. Get the numeric channel IDs (they start with `-100`). Forward a channel post to [@userinfobot](https://t.me/userinfobot) or [@RawDataBot](https://t.me/RawDataBot).
5. Get your own user ID from [@userinfobot](https://t.me/userinfobot) and put it in `ADMIN_IDS`.

### 2. Run locally
```bash
git clone <your-repo> telegram-signal-bot && cd telegram-signal-bot
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # then edit .env
python main.py
```
Python **3.9+** is required (3.11/3.12 recommended). On start the bot DMs every admin a "bot online" message.

### 3. Grant your first VIP
```
/grantvip 123456789 30
```
The user must have pressed `/start` in the bot at least once so it can DM them. They receive a single-use invite link to the VIP channel (valid 24 h).

---

## Deployment

### Render
1. Push the repo to GitHub.
2. Create a **Background Worker** (recommended, always-on) or a **Web Service**.
   - Build command: `pip install -r requirements.txt`
   - Start command: `python main.py`
3. Add every variable from `.env.example` under *Environment*.
4. **Persist your database:** attach a Render Disk (e.g. mounted at `/var/data`) and set `DB_PATH=/var/data/signal_bot.db`. Without a disk, SQLite is wiped on every deploy/restart and you lose VIP records and the journal.
5. If you use a **Web Service**, Render sets `PORT` and the bot automatically serves a `/` health endpoint. Free web services sleep when idle – ping the URL every ~10 minutes with UptimeRobot or use a paid always-on instance, otherwise the 60-second tracker stops while asleep.

### PythonAnywhere
- Use an **Always-on task** (paid plan): `python3 /home/<user>/telegram-signal-bot/main.py`. Create a virtualenv and `pip install -r requirements.txt` first.
- Free accounts restrict outbound internet to an allow-list that does not include TradingView, so the market data will not load there – use a paid account.
- Keep `DB_PATH` inside your home directory (the default relative path works if you start the task from the project folder; an absolute path is safer).

### Docker / VPS / systemd
Any host that can run `python main.py` and reach the internet works. Run only **one instance** per bot token (two pollers will fight over updates).

---

## How a signal is decided

1. **Market hours** – skipped from Fri 21:00 UTC to Sun 22:00 UTC.
2. **News guard** – skipped if a *High*-impact event for either currency of the pair is within ±30 min.
3. **Per-timeframe trend** (15M, 1H, 4H) – four votes: EMA20 vs EMA50, price vs EMA20, MACD vs signal, RSI vs 50. Needs ≥ `TREND_MIN_SCORE` (3) in one direction **and** EMA20/50 ordered the same way.
4. **Alignment** – all three timeframes BULL → BUY, all BEAR → SELL, otherwise nothing.
5. **Filters** – no BUY if 15M RSI > 72, no SELL if < 28; TradingView's own summary must not point the other way on any timeframe (`REQUIRE_TV_SUMMARY_CONFIRM`).
6. **Levels** – entry = latest 15M close, SL from ATR, TP1 = 1R, TP2 = 2R.
7. **Controls** – one active trade per symbol, a cooldown (`SIGNAL_COOLDOWN_MINUTES`), and a cap (`MAX_ACTIVE_TRADES`).

### Trade lifecycle
`OPEN → TP1_HIT` (SL moves to breakeven) `→ TP2_HIT` (closed) or `BE_HIT` (closed at entry)
`OPEN → SL_HIT`, or `OPEN → INVALIDATED` (early exit alert).

Journal `result_pips` is measured from entry to the final exit price for the *whole position* (TP2 = +2R, BE = 0, SL = −1R, early exit = pips at the alert price); the TP1 distance is stored separately in `tp1_pips`. The `/stats` "TP1 hit-rate" is the share of closed trades that reached TP1.

---

## Operating notes & known limitations

- **Unofficial data sources.** `tradingview-ta` scrapes TradingView's public scanner; Yahoo's chart endpoint and the Forex Factory mirror are also unofficial. They can change, throttle or go down. The bot caches, retries and degrades gracefully (levels-only chart, cached calendar), but verify prices against your broker before trading. Execution prices will differ from the signal.
- **Tracking granularity.** Live prices are polled each minute using the current 1-minute candle's high/low, so brief spikes between polls can be missed. If SL and TP are touched in the same sample, the outcome is decided by the current price side (conservative).
- **News feed.** The free calendar covers the current week and may rate-limit; the last good data is kept. Set `NEWS_FAIL_OPEN=false` to pause signals whenever the calendar is unreachable.
- **Pip values in `/risk`** are approximations (USDJPY depends on the rate). Lot sizes are guidance only.
- **Transparency.** By default only wins are posted to the public channel (as specified). Setting `PUBLIC_POST_LOSSES=true` also posts stop-outs – recommended if you market the channel's track record, since showing only winners can mislead your audience.
- **Broadcast** goes to everyone who has pressed `/start` (the `users` table). Users who blocked the bot are counted and skipped.
- **Single instance.** Run exactly one process per bot token.
- **Tuning.** All thresholds live in `.env` – backtest/forward-test before going live.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `BOT_TOKEN is missing` on start | Create `.env` from `.env.example`. |
| No signals ever appear | Normal when timeframes disagree. Run `/signal XAUUSD` to see per-timeframe trends; check `/ping` for TradingView status; lower `TREND_MIN_SCORE` cautiously. |
| `Failed to send message to -100…` | Add the bot as admin in the channel and verify the channel ID. |
| `JobQueue is unavailable` | `pip install "python-telegram-bot[job-queue]" apscheduler` |
| Charts show "levels only" | Yahoo candle fetch failed; signals still work. |
| VIP data vanished after redeploy | Use a persistent disk and set `DB_PATH`. |
