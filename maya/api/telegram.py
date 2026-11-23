"""Layer 7 — Telegram bot: alerts from the running agent, and read-only commands.

Alerts are written in plain words (what was bought or sold, why, what the stop
can lose and the target can gain). Commands: /status /today /trades /scan
/limits /stats /chart /explain /help, as tap buttons and a "/" menu, plus
``/halt confirm``. A summary of the previous day is sent after 00:00 UTC.

The spec lists Telegram as the Layer 7 interface. This is a *window* onto the
agent, built like the monitoring API (``maya.api.server``), and it shares that
module's safety rules:

* It never touches the trading path. A watcher thread reads the same snapshot
  the dashboard reads, every ``MAYA_TELEGRAM_POLL_S`` seconds, and reports what
  changed: a position opened, a trade closed, the system halted. If Telegram
  is down or slow, only the alerts are late. Trading is unaffected.
* **One** state-changing command exists, ``/halt confirm``, and it only moves
  the system to a safer state. There is no resume, no open-trade command, and
  no way to change a limit. The worst a stolen phone can do is stop trading.
* Only chats listed in ``MAYA_TELEGRAM_CHAT_IDS`` are answered. Everything
  else is ignored without a reply. With no chat ids the bot refuses to start.
* The bot token is a secret. It is never logged, and is scrubbed from any
  error text before that text is logged.

Standard library only (``urllib``), so there is no new dependency in a trading
process. Every network call has a timeout and runs off the trading thread.

**Not verified against Telegram from this repository's tests**: they use a fake
transport. ``python -m maya.check_telegram`` makes the real calls.
"""
from __future__ import annotations

import json
import os
import queue
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from loguru import logger

from maya.config import Settings

API = "https://api.telegram.org"
# Long-poll window for getUpdates. Telegram holds the request open this long
# when there is nothing new, so a command is answered within about a second.
LONG_POLL_S = 25
MAX_MESSAGE = 4000   # Telegram's hard limit is 4096 characters

Http = Callable[[str, bytes | None, dict[str, str], float], bytes]


def _urllib_http(url: str, data: bytes | None, headers: dict[str, str], timeout: float) -> bytes:
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    """The few Bot API calls Maya needs."""

    def __init__(self, token: str, http: Http | None = None) -> None:
        if not token:
            raise ValueError("MAYA_TELEGRAM_BOT_TOKEN is not set")
        self._token = token
        self._http = http or _urllib_http

    def scrub(self, text: str) -> str:
        """Remove the token from text that is about to be logged or raised."""
        return text.replace(self._token, "<token>")

    def call(self, method: str, params: dict[str, Any] | None = None,
             photo: bytes | None = None, timeout: float = 15) -> Any:
        url = f"{API}/bot{self._token}/{method}"
        if photo is not None:
            boundary = uuid.uuid4().hex
            parts: list[bytes] = []
            for k, v in (params or {}).items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"'
                             f"\r\n\r\n{v}\r\n".encode())
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; '
                         f'filename="chart.png"\r\nContent-Type: image/png\r\n\r\n'.encode()
                         + photo + b"\r\n")
            parts.append(f"--{boundary}--\r\n".encode())
            body = b"".join(parts)
            headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        else:
            body = json.dumps(params or {}).encode()
            headers = {"Content-Type": "application/json"}
        try:
            raw = self._http(url, body, headers, timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace") if exc.fp else ""
            raise TelegramError(self.scrub(f"{method}: HTTP {exc.code} {detail}")) from None
        except Exception as exc:  # noqa: BLE001 - network errors of every kind
            raise TelegramError(self.scrub(f"{method}: {exc}")) from None
        reply = json.loads(raw)
        if not reply.get("ok"):
            raise TelegramError(self.scrub(f"{method}: {reply.get('description', reply)}"))
        return reply.get("result")

    def send_message(self, chat_id: int, text: str,
                     reply_markup: dict[str, Any] | None = None) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text[:MAX_MESSAGE],
                                  "disable_web_page_preview": True}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        self.call("sendMessage", params)

    def set_my_commands(self, commands: list[tuple[str, str]]) -> None:
        """The menu Telegram shows when "/" is typed."""
        self.call("setMyCommands", {"commands": [
            {"command": c, "description": d} for c, d in commands]})

    def send_photo(self, chat_id: int, png: bytes, caption: str = "") -> None:
        self.call("sendPhoto", {"chat_id": chat_id, "caption": caption[:1000]},
                  photo=png, timeout=30)

    def get_updates(self, offset: int | None, timeout: int = LONG_POLL_S) -> list[dict]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", params, timeout=timeout + 10) or []


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def _money(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{'-' if v < 0 else ''}${abs(v):,.2f}"


def _price(v: float | None) -> str:
    return "-" if v is None else f"{v:,.2f}"


DAY_MS = 86_400_000
STATE_ICON = {"running": "🟢", "cooldown": "⏸", "halted": "🛑"}

# The setups in plain words. The "_cvd_flip" variants add a pressure flip.
SETUP_PLAIN = {
    "liquidity_sweep_choch": "price poked past a recent high/low, grabbing other traders' "
                             "stops, then turned back",
    "value_area_absorption": "price reached the edge of the busiest price zone and large "
                             "orders absorbed the push",
    "compression_breakout": "price broke out of a tight, quiet range",
}
EXIT_PLAIN = {
    "take_profit": "hit the target",
    "stop_loss": "hit the stop loss",
    "max_duration": "closed after the time limit",
    "shutdown": "closed because Maya was stopped",
}


def explain_setup(name: str | None) -> str:
    if not name:
        return "unknown setup"
    base = name.removesuffix("_cvd_flip")
    text = SETUP_PLAIN.get(base, name.replace("_", " "))
    return text + (", and buying/selling pressure just flipped" if base != name else "")


def _coin(symbol: str | None) -> str:
    return (symbol or "").removesuffix("USDT") or "?"


def _side(direction: str | None) -> str:
    return {"long": "LONG (bets price goes UP)",
            "short": "SHORT (bets price goes DOWN)"}.get(direction or "", str(direction))


def _signed(v: float | None) -> str:
    return ("+" if (v or 0) > 0 else "") + _money(v)


def _utc(ms: int | float | None) -> str:
    return f"{datetime.fromtimestamp((ms or 0) / 1000, tz=timezone.utc):%H:%M} UTC"


def format_status(s: dict[str, Any]) -> str:
    c, a, p = s.get("config", {}), s.get("account", {}), s.get("position")
    state = a.get("state", "?")
    lim = s.get("limits", {})
    if c.get("agent") == "scanner":
        what = f"scanning {len(s.get('scan') or [])} coins on {c.get('timeframe')}"
    else:
        what = f"{c.get('symbol')} on {c.get('timeframe')}"
    lines = [
        f"{STATE_ICON.get(state, '•')} Maya: {state.upper()}",
        f"{c.get('mode')} · {c.get('venue')} · {what} · {c.get('leverage')}x",
        f"Balance {_money(a.get('equity'))} ({_signed(a.get('pnl_usdt'))}, "
        f"{'+' if a.get('pnl_pct', 0) > 0 else ''}{a.get('pnl_pct', 0):.2f}% since start)",
        f"Today: {a.get('trades_today', 0)} trade(s)"
        + (f" of max {lim['max_trades_per_day']}" if lim else "")
        + f" · losses in a row {a.get('consecutive_losses', 0)}",
    ]
    if a.get("halt_reasons"):
        lines.append("Halt: " + ", ".join(a["halt_reasons"]))
    lines.append(format_position(p) if p else "No open trade: watching for a setup.")
    return "\n".join(lines)


def _risk_reward(p: dict[str, Any]) -> tuple[float, float]:
    qty = float(p.get("quantity") or 0)
    entry = float(p["entry_price"])
    return abs(entry - float(p["stop_loss"])) * qty, abs(float(p["take_profit"]) - entry) * qty


def format_position(p: dict[str, Any]) -> str:
    risk, reward = _risk_reward(p)
    be = " (moved to breakeven)" if p.get("moved_to_breakeven") else ""
    return (f"Open: {_side(p['direction'])} {_coin(p['symbol'])}\n"
            f"Entry {_price(p['entry_price'])}\n"
            f"Stop loss {_price(p['stop_loss'])}{be} · risk about {_money(risk)}\n"
            f"Target {_price(p['take_profit'])} · gain about {_money(reward)}\n"
            f"Size {p.get('quantity')} ({_money(p.get('notional'))} at {p.get('leverage')}x)")


def format_opened(p: dict[str, Any]) -> str:
    risk, reward = _risk_reward(p)
    rr = reward / risk if risk else 0.0
    return (f"🟢 NEW TRADE: {_side(p['direction'])} {_coin(p['symbol'])}\n"
            f"Why: {explain_setup(p.get('setup'))}.\n"
            f"Entry {_price(p['entry_price'])}\n"
            f"Stop loss {_price(p['stop_loss'])} → lose about {_money(risk)} if hit\n"
            f"Target {_price(p['take_profit'])} → gain about {_money(reward)} if hit\n"
            f"Reward/risk {rr:.1f} : 1 · size {p.get('quantity')} "
            f"({_money(p.get('notional'))} at {p.get('leverage')}x)\n"
            "Fees come on top. The stop sits at the exchange, so it works even if this PC is off.")


def format_trade(t: dict[str, Any]) -> str:
    result = (t.get("result") or "?").lower()
    icon = {"win": "✅", "loss": "❌"}.get(result, "➖")
    when = datetime.fromtimestamp((t.get("timestamp_exit") or 0) / 1000, tz=timezone.utc)
    coin = _coin(t.get("symbol")) + " " if t.get("symbol") else ""
    reason = EXIT_PLAIN.get(t.get("exit_reason") or "", t.get("exit_reason"))
    return (f"{icon} {result.upper()} {coin}{t.get('direction')}: {_signed(t.get('pnl_usdt'))} "
            f"({reason}) {when:%m-%d %H:%M} UTC")


def day_report(trades: list[dict[str, Any]], start_ms: int, title: str) -> str:
    """Trades that closed in [start_ms, start_ms + 1 day), summed up."""
    day = [t for t in trades if start_ms <= (t.get("timestamp_exit") or 0) < start_ms + DAY_MS]
    if not day:
        return f"📅 {title}: no closed trades."
    wins = sum(1 for t in day if t.get("result") == "win")
    losses = sum(1 for t in day if t.get("result") == "loss")
    pnl = sum(float(t.get("pnl_usdt") or 0) for t in day)
    fees = sum(float(t.get("fees_usdt") or 0) for t in day)
    lines = [f"📅 {title}: {len(day)} trade(s): {wins} win, {losses} loss",
             f"P&L {_signed(pnl)}" + (f" (fees paid {_money(fees)})" if fees else "")]
    lines += [format_trade(t) for t in sorted(day, key=lambda t: t["timestamp_exit"])]
    return "\n".join(lines)


def format_limits(s: dict[str, Any], now_ms: int) -> str:
    a, lim = s.get("account", {}), s.get("limits", {})
    if not lim:
        return "Limits are not available from this agent."
    eq = float(a.get("equity") or 0)

    def used(start_key: str) -> float:
        start = float(a.get(start_key) or 0)
        return max(0.0, (start - eq) / start * 100) if start else 0.0

    lines = [
        "🛡 Safety limits (fixed in code; nothing here can change them)",
        f"• Trades today: {a.get('trades_today', 0)} of {lim['max_trades_per_day']}",
        f"• Losses in a row: {a.get('consecutive_losses', 0)} of "
        f"{lim['max_consecutive_losses']}, then a {lim['cooldown_minutes']}-min pause",
        f"• Today's loss: {used('day_start_equity'):.1f}% of {lim['daily_loss_limit_pct']:g}% "
        f"max (day started at {_money(a.get('day_start_equity'))})",
        f"• This week's loss: {used('week_start_equity'):.1f}% of "
        f"{lim['weekly_loss_limit_pct']:g}% max",
        f"• Open trades: {a.get('open_positions', 0)} of {lim['max_concurrent_positions']}",
    ]
    state, until = a.get("state"), int(a.get("cooldown_until_ts") or 0)
    if state == "cooldown" and until > now_ms:
        lines.append(f"Now: paused until {_utc(until)} ({(until - now_ms) // 60_000 + 1} min left)")
    elif state == "halted":
        lines.append("Now: HALTED (" + ", ".join(a.get("halt_reasons") or ["?"]) + ")")
    else:
        lines.append(f"Now: {str(state).upper()}")
    return "\n".join(lines)


# Setups per coin per day on 1m candles in the 24-month backtest (README:
# baseline_1m_setups, 1,620 trades over BTC/ETH/SOL in the research window,
# about 0.9 per coin per day). Used only to say what "normal" looks like.
SETUPS_PER_COIN_DAY_1M = 0.9


def _ago(ms: int) -> str:
    mins = max(0, ms) // 60_000
    if mins < 1:
        return "under a minute"
    if mins < 90:
        return f"{mins} min"
    return f"{mins / 60:.1f} h" if mins < 48 * 60 else f"{mins / 1440:.1f} days"


def format_scan(s: dict[str, Any], now_ms: int | None = None,
                last_trade_ms: int | None = None) -> str:
    scan = s.get("scan")
    if scan is None:
        return ("The scan view is for the multi-coin scanner (python -m maya.scan_run). "
                f"This agent trades {s.get('config', {}).get('symbol')} only.")
    if not scan:
        return "No scan yet: the first one runs when the next candle closes."
    tf = s.get("config", {}).get("timeframe") or "1m"
    head = f"🔍 Last scan: {len(scan)} coins"
    if now_ms is not None and s.get("scan_ts"):
        head += f", checked {_ago(now_ms - int(s['scan_ts']))} ago"
    lines = [head]
    if not any(r.get("setup") for r in scan):
        msg = "No setups this minute."
        if tf == "1m":
            every_h = 24 / (SETUPS_PER_COIN_DAY_1M * len(scan))
            msg += (f" That is normal: across {len(scan)} coins a setup shows up about once "
                    f"every {every_h:.1f} h on average, sometimes two close together, "
                    "sometimes none for hours.")
        lines.append(msg)
    footer: list[str] = []
    if now_ms is not None:
        try:
            from maya.timeframe import timeframe_ms
            step = timeframe_ms(tf)
            footer.append(f"Next check in about {(step - now_ms % step) // 1000 + 3} s.")
        except Exception:  # noqa: BLE001 - an odd timeframe only loses this line
            pass
        footer.append(f"Last trade closed {_ago(now_ms - last_trade_ms)} ago."
                      if last_trade_ms else "No trade closed yet.")
    candidates = [r for r in scan if r.get("setup")]
    for r in candidates:
        verdict = "✅ tradable" if r.get("status") == "trade" else f"skipped: {r.get('status')}"
        lines.append(f"• {r['coin']} {r.get('direction')}: {explain_setup(r['setup'])}. "
                     f"Net reward/risk {r.get('net_rr')} → {verdict}")
    quiet = [r["coin"] for r in scan if not r.get("setup")
             and str(r.get("status", "")).startswith("no setup")]
    if quiet:
        lines.append("Watching (no setup yet): " + ", ".join(quiet))
    other: dict[str, list[str]] = {}
    for r in scan:
        if not r.get("setup") and r["coin"] not in quiet:
            other.setdefault(str(r.get("status")), []).append(r["coin"])
    lines += [f"{why}: {', '.join(coins)}" for why, coins in other.items()]
    if s.get("position"):
        lines.append(f"In a trade on {_coin(s['position']['symbol'])}: no new trade until it "
                     "closes (one at a time).")
    return "\n".join(lines + footer)


def format_stats(s: dict[str, Any]) -> str:
    def block(title: str, m: dict[str, Any] | None) -> str:
        if not m or not m.get("trades"):
            return f"{title}: no closed trades yet"
        pf = m.get("profit_factor")
        return (f"{title}: {m['trades']} trades, win rate {m.get('win_rate', 0):.0f}%\n"
                f"  P&L {_signed(m.get('total_pnl_usdt'))}, fees {_money(m.get('total_fees'))}, "
                f"profit factor {pf if pf is not None else '-'}\n"
                f"  average win {_money(m.get('avg_win'))}, average loss {_money(m.get('avg_loss'))}")
    return "\n".join(["📉 Results", block("This run", s.get("run")),
                      block("All time", s.get("lifetime")),
                      "Profit factor above 1 means wins outweigh losses. Backtests of this "
                      "strategy measured below 1 after fees, so treat this as a test."])


EXPLAIN = (
    "📖 Words Maya uses\n"
    "LONG: buys; makes money if the price goes up.\n"
    "SHORT: sells first; makes money if the price goes down.\n"
    "Stop loss: the price where Maya exits to cap a loss. It is placed at the exchange.\n"
    "Target: the price where Maya takes the profit.\n"
    "Reward/risk 3 : 1: the target pays $3 for every $1 the stop could lose.\n"
    "Breakeven: the stop moved to the entry price, so the worst case is about $0 plus fees.\n"
    "Fees: the exchange charges on entry and exit; every P&L shown includes them.\n"
    "Pause (cooldown): a timed break after losses in a row.\n"
    "Halt: no new trades. A daily or weekly loss halt lifts by itself at the next day or "
    "week; any other halt needs a person to restart Maya.\n"
    "Setups:\n"
    + "\n".join(f"• {k.replace('_', ' ')}: {v}" for k, v in SETUP_PLAIN.items()))

HELP = ("Maya commands (or tap the buttons):\n"
        "/status  balance, state, open trade\n"
        "/today  today's trades and P&L\n"
        "/trades  last 5 closed trades\n"
        "/scan  what Maya sees on each coin right now\n"
        "/limits  safety limits and how close Maya is to each\n"
        "/stats  win rate, P&L, fees\n"
        "/chart [coin] [tf]  chart with Maya's drawings (e.g. /chart ETH 15m)\n"
        "/explain  what the trading words mean\n"
        "/halt confirm  stop opening new trades (does not close a position)\n"
        "There is deliberately no resume or open-trade command.")

# Shown in Telegram's "/" menu.
COMMANDS = [
    ("status", "Balance, state, open trade"),
    ("today", "Today's trades and P&L"),
    ("trades", "Last 5 closed trades"),
    ("scan", "What Maya sees on each coin now"),
    ("limits", "Safety limits and how close Maya is"),
    ("stats", "Win rate, P&L, fees"),
    ("chart", "Chart with Maya's drawings"),
    ("explain", "What the trading words mean"),
    ("help", "All commands"),
    ("halt", "Stop new trades (send: /halt confirm)"),
]
# Buttons under the chat box. Halt is deliberately not a button: one stray
# tap must not stop trading.
BUTTONS = [["📊 Status", "📅 Today"], ["🔍 Scan", "🕯 Chart"], ["🧾 Trades", "🛡 Limits"],
           ["📉 Stats", "📖 Explain"], ["❓ Help"]]
_TF = re.compile(r"\d+[smhdw]")      # "1m", "15m", "4h": a timeframe, not a coin
KEYBOARD = {"keyboard": [[{"text": b} for b in row] for row in BUTTONS],
            "resize_keyboard": True, "is_persistent": True}
# Plain words a button (or a typed word) may stand for. "halt" is not here.
WORD_COMMANDS = {"status", "today", "trades", "scan", "limits", "stats", "chart",
                 "explain", "help", "start"}


def _command(text: str) -> tuple[str, list[str]] | None:
    """("/status", args) from "/status", "/status@MayaBot", "📊 Status" or "status"."""
    words = text.strip().split()
    if words and not words[0][:1].isalnum() and not words[0].startswith("/"):
        words = words[1:]                      # a button's leading emoji
    if not words:
        return None
    first = words[0].split("@")[0].lower()     # "/status@MayaBot" in group chats
    if not first.startswith("/"):
        if first not in WORD_COMMANDS:
            return None
        first = "/" + first
    return first, words[1:]


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #
class TelegramBot:
    """Watches the agent and answers commands from allowed chats."""

    def __init__(self, settings: Settings, snapshot_fn: Callable[[], dict[str, Any]],
                 trades_fn: Callable[[int], list[dict[str, Any]]],
                 halt_fn: Callable[[str], None],
                 # (timeframe, bars) or (timeframe, bars, coin) -> chart payload
                 chart_fn: Callable[..., dict[str, Any]] | None = None,
                 client: TelegramClient | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.chat_ids = settings.telegram_chat_id_list
        if not self.chat_ids:
            raise ValueError(
                "MAYA_TELEGRAM_CHAT_IDS is not set. The bot can halt a trading system, "
                "so it refuses to answer unknown chats. Find your chat id with: "
                "python -m maya.check_telegram")
        self.client = client or TelegramClient(settings.telegram_bot_token)
        self.snapshot_fn, self.trades_fn = snapshot_fn, trades_fn
        self.halt_fn, self.chart_fn = halt_fn, chart_fn
        self._clock = clock
        self._stop = threading.Event()
        # (chat id, text, photo, reply markup)
        self._outbox: queue.Queue[tuple[str, str, bytes | None, dict | None]] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._prev: dict[str, Any] | None = None
        self._last_exit_ts = 0
        self._offset: int | None = None
        self._day_start = self._today_start()

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _today_start(self) -> int:
        return self._now_ms() // DAY_MS * DAY_MS

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        try:
            snap = self.snapshot_fn()
            self._prev = snap
            trades = self.trades_fn(1)
            self._last_exit_ts = trades[0]["timestamp_exit"] if trades else 0
        except Exception as exc:  # noqa: BLE001 - never stop the agent over a bot
            logger.warning("telegram: initial snapshot failed: {}", exc)
            snap = None
        for name, target in (("send", self._send_loop), ("watch", self._watch_loop),
                             ("commands", self._command_loop)):
            t = threading.Thread(target=target, name=f"maya-telegram-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        self.notify("Maya started.\n" + (format_status(snap) + "\n" if snap else "")
                    + "Tap a button below, or send /help.", markup=KEYBOARD)
        logger.info("Telegram bot started for {} chat(s)", len(self.chat_ids))

    def stop(self, final: str = "Maya stopped.") -> None:
        if final:
            self.notify(final)
        self._stop.set()
        # Give the sender a moment to flush the final message.
        deadline = time.time() + 5
        while not self._outbox.empty() and time.time() < deadline:
            time.sleep(0.1)
        for t in self._threads:
            if t.name != "maya-telegram-commands":   # it may be inside a long poll
                t.join(timeout=2)

    # -- outbound ------------------------------------------------------------ #
    def notify(self, text: str, photo: bytes | None = None, chat_id: int | None = None,
               markup: dict | None = None) -> None:
        """Queue a message for every allowed chat (or one). Never blocks."""
        targets = [chat_id] if chat_id is not None else self.chat_ids
        for cid in targets:
            self._outbox.put((str(cid), text, photo, markup))

    def _send_loop(self) -> None:
        while not (self._stop.is_set() and self._outbox.empty()):
            try:
                cid, text, photo, markup = self._outbox.get(timeout=0.5)
            except queue.Empty:
                continue
            for attempt in range(3):
                try:
                    if photo is not None:
                        self.client.send_photo(int(cid), photo, text)
                    else:
                        self.client.send_message(int(cid), text, markup)
                    break
                except TelegramError as exc:
                    logger.warning("telegram send failed (try {}): {}", attempt + 1, exc)
                    if self._stop.wait(2 * (attempt + 1)):
                        break

    # -- watcher ------------------------------------------------------------- #
    def changes(self, prev: dict[str, Any] | None, snap: dict[str, Any],
                trades: list[dict[str, Any]]) -> list[tuple[str, str]]:
        """What happened between two snapshots, as (kind, message) pairs.

        Pure, so it can be tested without threads or a network.
        """
        out: list[tuple[str, str]] = []
        new = sorted((t for t in trades if (t.get("timestamp_exit") or 0) > self._last_exit_ts),
                     key=lambda t: t["timestamp_exit"])
        a1 = snap.get("account", {})
        for t in new:
            out.append(("closed", "Trade closed: " + format_trade(t)
                        + f"\nBalance now {_money(a1.get('equity'))}"))
            self._last_exit_ts = max(self._last_exit_ts, t["timestamp_exit"])
        if prev is None:
            return out
        p0, p1 = prev.get("position"), snap.get("position")
        key = lambda p: (p["symbol"], p["direction"], p["entry_price"]) if p else None  # noqa: E731
        if p1 and key(p1) != key(p0):
            out.append(("opened", format_opened(p1)))
        elif p0 and p1 and p1.get("moved_to_breakeven") and not p0.get("moved_to_breakeven"):
            out.append(("breakeven", f"🔒 Stop moved to breakeven on {_coin(p1['symbol'])}: "
                        f"worst case is now about $0 plus fees. SL {_price(p1['stop_loss'])}"))
        a0 = prev.get("account", {})
        if a1.get("state") != a0.get("state"):
            out.append(("state", self._state_message(a0.get("state"), a1, snap.get("limits", {}))))
        return out

    def _state_message(self, old: str | None, a: dict[str, Any], lim: dict[str, Any]) -> str:
        state, reasons = a.get("state"), a.get("halt_reasons") or []
        if state == "cooldown":
            mins = lim.get("cooldown_minutes")
            until = int(a.get("cooldown_until_ts") or 0)
            return (f"⏸ Paused{f' for {mins} min' if mins else ''} after "
                    f"{a.get('consecutive_losses', 0)} losses in a row (safety rule)."
                    + (f" Trading resumes about {_utc(until)}." if until else ""))
        if state == "halted":
            text = "🛑 HALTED: no new trades.\nReason: " + (", ".join(reasons) or "?")
            if reasons and all("daily" in r for r in reasons):
                text += "\nThis lifts by itself at 00:00 UTC."
            elif reasons and all("weekly" in r or "daily" in r for r in reasons):
                text += "\nThis lifts by itself when the new week starts."
            else:
                text += "\nA person must check and restart Maya."
            return text
        if state == "running":
            return f"▶️ Trading again (was {old})."
        return f"System state: {old} -> {state}"

    def _watch_loop(self) -> None:
        while not self._stop.wait(self.settings.telegram_poll_s):
            try:
                snap = self.snapshot_fn()
                trades = self.trades_fn(10)
                for kind, msg in self.changes(self._prev, snap, trades):
                    photo = None
                    if kind == "opened" and self.settings.telegram_chart_on_open:
                        held = snap.get("position") or {}
                        photo = self._chart_png("base", held.get("symbol"))
                    self.notify(msg, photo)
                self._prev = snap
                if (summary := self.daily_summary()) is not None:
                    self.notify(summary)
            except Exception as exc:  # noqa: BLE001 - keep watching
                logger.warning("telegram watcher: {}", exc)

    def daily_summary(self) -> str | None:
        """Yesterday's report, once, when the UTC day rolls over."""
        today = self._today_start()
        if today <= self._day_start:
            return None
        yesterday, self._day_start = self._day_start, today
        if not self.settings.telegram_daily_summary:
            return None
        day = datetime.fromtimestamp(yesterday / 1000, tz=timezone.utc)
        return day_report(self.trades_fn(100), yesterday, f"Day summary {day:%Y-%m-%d}")

    # -- commands ------------------------------------------------------------ #
    def handle(self, chat_id: int, text: str) -> None:
        """Answer one message. Unknown chats get no reply at all."""
        if chat_id not in self.chat_ids:
            logger.warning("telegram: ignored message from unlisted chat {}", chat_id)
            return
        parsed = _command(text)
        if parsed is None:
            self.notify("I did not understand that. Tap a button, or send /help.",
                        chat_id=chat_id, markup=KEYBOARD)
            return
        cmd, args = parsed
        if cmd in ("/start", "/help"):
            self.notify(HELP, chat_id=chat_id, markup=KEYBOARD)
        elif cmd == "/status":
            self.notify(format_status(self.snapshot_fn()), chat_id=chat_id)
        elif cmd == "/trades":
            trades = self.trades_fn(5)
            self.notify("\n".join(format_trade(t) for t in trades) if trades
                        else "No closed trades yet.", chat_id=chat_id)
        elif cmd == "/today":
            self.notify(day_report(self.trades_fn(100), self._today_start(), "Today (UTC)"),
                        chat_id=chat_id)
        elif cmd == "/scan":
            last = self.trades_fn(1)
            self.notify(format_scan(self.snapshot_fn(), self._now_ms(),
                                    last[0].get("timestamp_exit") if last else None),
                        chat_id=chat_id)
        elif cmd == "/limits":
            self.notify(format_limits(self.snapshot_fn(), self._now_ms()), chat_id=chat_id)
        elif cmd == "/stats":
            self.notify(format_stats(self.snapshot_fn()), chat_id=chat_id)
        elif cmd == "/explain":
            self.notify(EXPLAIN, chat_id=chat_id)
        elif cmd == "/chart":
            # "/chart", "/chart 15m", "/chart ETH", "/chart eth 15m": any order.
            tf = next((a for a in args if _TF.fullmatch(a.lower())), "base").lower()
            coin = next((a for a in args if not _TF.fullmatch(a.lower())), None)
            png, label, why = self._chart(tf, coin)
            if png is None:
                self.notify(f"No chart: {why}\nTry /chart, /chart 15m or /chart ETH 15m.",
                            chat_id=chat_id)
            else:
                self.notify(f"{label}: lines and zones are readings, not signals.", png,
                            chat_id=chat_id)
        elif cmd == "/halt":
            if args[:1] == ["confirm"]:
                self.halt_fn("halted from Telegram")
                logger.critical("HALT requested via Telegram (chat {})", chat_id)
                self.notify("Halted: no new trades will be opened. An open position "
                            "is NOT closed; manage it on the exchange.\n\n"
                            + format_status(self.snapshot_fn()))
            else:
                self.notify("Send /halt confirm to stop new trades. There is no resume "
                            "from Telegram.", chat_id=chat_id)
        else:
            self.notify("Unknown command.\n\n" + HELP, chat_id=chat_id)

    def _chart_png(self, tf: str, coin: str | None = None) -> bytes | None:
        return self._chart(tf, coin)[0]

    def _chart(self, tf: str, coin: str | None = None) -> tuple[bytes | None, str, str]:
        """(png, "ETHUSDT 15m", "") or (None, "", why there is none)."""
        if self.chart_fn is None:
            return None, "", "charts are not available from this agent."
        try:
            from maya.viz.chart import render_png
            payload = (self.chart_fn(tf, 150, coin) if coin
                       else self.chart_fn(tf, 150))
            if not payload.get("candles"):
                return None, "", "not enough candles yet."
            fd, path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            try:
                render_png(payload, path)
                with open(path, "rb") as fh:
                    png = fh.read()
            finally:
                os.unlink(path)
            return png, f"{payload.get('symbol', '')} {payload.get('timeframe', tf)}".strip(), ""
        except Exception as exc:  # noqa: BLE001 - a chart is never worth an error
            logger.warning("telegram chart failed: {}", exc)
            return None, "", f"{exc}."

    def skip_backlog(self) -> int:
        """Discard messages sent while Maya was not running.

        Telegram keeps undelivered updates for 24 hours. Without this, a
        "/halt confirm" typed yesterday would halt a fresh start today.
        """
        try:
            pending = self.client.get_updates(None, timeout=0)
        except TelegramError as exc:
            logger.warning("telegram: could not skip old messages: {}", exc)
            return 0
        if pending:
            self._offset = pending[-1]["update_id"] + 1
        return len(pending)

    def _command_loop(self) -> None:
        try:
            self.client.set_my_commands(COMMANDS)
        except TelegramError as exc:   # only the "/" menu is missing; commands still work
            logger.warning("telegram: could not set the command menu: {}", exc)
        skipped = self.skip_backlog()
        if skipped:
            logger.info("telegram: ignored {} message(s) sent before start", skipped)
        while not self._stop.is_set():
            try:
                updates = self.client.get_updates(self._offset)
            except TelegramError as exc:
                logger.warning("telegram getUpdates: {}", exc)
                self._stop.wait(5)
                continue
            for u in updates:
                self._offset = u["update_id"] + 1
                msg = u.get("message") or {}
                text, chat = msg.get("text"), (msg.get("chat") or {}).get("id")
                if text and chat is not None:
                    try:
                        self.handle(int(chat), text)
                    except Exception as exc:  # noqa: BLE001 - one bad command must not kill the loop
                        logger.warning("telegram command {!r} failed: {}", text, exc)
