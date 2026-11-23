"""Tests for the Telegram bot (Layer 7). No network: a fake transport stands
in for api.telegram.org, and its replies are shaped like the Bot API's
documented responses (invented fixtures, not captured traffic)."""
from __future__ import annotations

import json
import threading
import time

import pytest

from maya.api import telegram as tg
from maya.config import Settings

TOKEN = "123456:SECRET-TOKEN"
CHAT = 4242


class FakeHttp:
    """Records calls; answers getUpdates from a queue of batches."""

    def __init__(self, updates: list[list[dict]] | None = None, fail: Exception | None = None):
        self.calls: list[tuple[str, bytes | None]] = []
        self.updates = list(updates or [])
        self.fail = fail
        self.lock = threading.Lock()

    def __call__(self, url, data, headers, timeout):
        method = url.rsplit("/", 1)[-1]
        with self.lock:
            self.calls.append((method, data))
        if self.fail is not None:
            raise self.fail
        if method == "getUpdates":
            with self.lock:
                batch = self.updates.pop(0) if self.updates else []
            if not batch:
                time.sleep(0.02)   # a real long poll blocks; do not spin
            return json.dumps({"ok": True, "result": batch}).encode()
        if method == "getMe":
            return json.dumps({"ok": True, "result": {"username": "MayaBot",
                                                      "first_name": "Maya"}}).encode()
        return json.dumps({"ok": True, "result": {}}).encode()

    def sent(self, method: str = "sendMessage") -> list[bytes]:
        with self.lock:
            return [d for m, d in self.calls if m == method]


def _settings(**kw) -> Settings:
    base = dict(telegram_enabled=True, telegram_bot_token=TOKEN,
                telegram_chat_ids=str(CHAT), telegram_poll_s=0.05,
                telegram_chart_on_open=False)
    base.update(kw)
    return Settings(**base)


def _snap(state="running", position=None, reasons=()):
    return {"config": {"mode": "paper", "venue": "binance", "symbol": "BTCUSDT",
                       "timeframe": "1m", "leverage": 10},
            "account": {"equity": 100.0, "pnl_usdt": 0.0, "pnl_pct": 0.0, "state": state,
                        "halt_reasons": list(reasons), "trades_today": 0,
                        "consecutive_losses": 0},
            "position": position}


POS = {"symbol": "BTCUSDT", "direction": "long", "setup": "liquidity_sweep_choch",
       "entry_price": 100000.0, "stop_loss": 99500.0, "take_profit": 101500.0,
       "quantity": 0.001, "notional": 100.0, "leverage": 10, "rr_planned": 3.0,
       "moved_to_breakeven": False}


def _bot(http=None, snap=None, trades=None, halts=None, chart_fn=None, **kw):
    http = http or FakeHttp()
    state = {"snap": snap or _snap(), "trades": trades or []}
    halts = halts if halts is not None else []
    bot = tg.TelegramBot(_settings(**kw), lambda: state["snap"],
                         lambda n: state["trades"][:n], halts.append, chart_fn=chart_fn,
                         client=tg.TelegramClient(TOKEN, http=http))
    return bot, http, state, halts


def _drain(bot) -> list[tuple[str, str, bytes | None]]:
    out = []
    while not bot._outbox.empty():
        out.append(bot._outbox.get_nowait()[:3])      # (chat, text, photo); markup dropped
    return out


# -- client -------------------------------------------------------------------- #
def test_the_token_never_appears_in_an_error():
    http = FakeHttp(fail=OSError(f"connection to https://api.telegram.org/bot{TOKEN}/x failed"))
    client = tg.TelegramClient(TOKEN, http=http)
    with pytest.raises(tg.TelegramError) as exc:
        client.send_message(CHAT, "hi")
    assert TOKEN not in str(exc.value) and "<token>" in str(exc.value)


def test_photo_is_sent_as_multipart_with_the_png():
    http = FakeHttp()
    tg.TelegramClient(TOKEN, http=http).send_photo(CHAT, b"\x89PNGfake", "cap")
    body = http.sent("sendPhoto")[0]
    assert b"\x89PNGfake" in body and f"{CHAT}".encode() in body and b'name="photo"' in body


def test_telegram_error_reply_raises():
    def http(url, data, headers, timeout):
        return json.dumps({"ok": False, "description": "Forbidden: bot was blocked"}).encode()
    with pytest.raises(tg.TelegramError, match="blocked"):
        tg.TelegramClient(TOKEN, http=http).send_message(CHAT, "x")


# -- configuration --------------------------------------------------------------- #
def test_chat_ids_parse_and_the_bot_refuses_without_them():
    assert Settings(telegram_chat_ids="123, -456").telegram_chat_id_list == [123, -456]
    with pytest.raises(ValueError, match="MAYA_TELEGRAM_CHAT_IDS"):
        tg.TelegramBot(Settings(telegram_bot_token=TOKEN), dict, lambda n: [], print)


def test_defaults_are_off():
    s = Settings()
    assert s.telegram_enabled is False and s.telegram_bot_token == "" and s.telegram_chat_ids == ""


def test_startup_is_never_fatal_and_off_by_default():
    from maya.run import _start_telegram
    assert _start_telegram(Settings(), None, None, lambda: 0) is None
    # Enabled but misconfigured (no chat ids): logged, trading continues.
    assert _start_telegram(Settings(telegram_enabled=True, telegram_bot_token=TOKEN),
                           None, None, lambda: 0) is None


# -- watcher ----------------------------------------------------------------------- #
def test_changes_report_open_close_breakeven_and_halt_once_each():
    bot, *_ = _bot()
    flat, held = _snap(), _snap(position=POS)
    assert [k for k, _ in bot.changes(flat, held, [])] == ["opened"]
    be = _snap(position=POS | {"moved_to_breakeven": True, "stop_loss": 100100.0})
    assert [k for k, _ in bot.changes(held, be, [])] == ["breakeven"]
    trade = {"setup_name": "liquidity_sweep_choch", "direction": "long", "pnl_usdt": 1.5,
             "result": "win", "exit_reason": "take_profit", "timestamp_exit": 1_700_000_000_000}
    first = bot.changes(be, flat, [trade])
    assert [k for k, _ in first] == ["closed"] and "$1.50" in first[0][1]
    assert bot.changes(flat, flat, [trade]) == []          # never announced twice
    halted = _snap(state="halted", reasons=["daily_loss_limit"])
    msgs = bot.changes(flat, halted, [])
    assert msgs[0][0] == "state" and "daily_loss_limit" in msgs[0][1]


def test_trades_closed_before_start_are_not_announced():
    old = {"setup_name": "s", "direction": "short", "pnl_usdt": -1.0, "result": "loss",
           "exit_reason": "stop_loss", "timestamp_exit": 1_600_000_000_000}
    bot, http, _, _ = _bot(trades=[old])
    bot.start()
    try:
        assert bot.changes(_snap(), _snap(), [old]) == []
    finally:
        bot.stop("")


# -- commands --------------------------------------------------------------------- #
def test_unlisted_chats_get_nothing_and_cannot_halt():
    bot, http, _, halts = _bot()
    bot.handle(999, "/halt confirm")
    bot.handle(999, "/status")
    assert halts == [] and _drain(bot) == []


def test_halt_needs_confirm_and_tells_every_chat():
    bot, _, _, halts = _bot(telegram_chat_ids=f"{CHAT},77")
    bot.handle(CHAT, "/halt")
    assert halts == [] and "confirm" in _drain(bot)[0][1]
    bot.handle(CHAT, "/halt confirm")
    assert halts == ["halted from Telegram"]
    assert {cid for cid, _, _ in _drain(bot)} == {str(CHAT), "77"}


def test_status_trades_help_and_bot_suffix():
    bot, _, state, _ = _bot()
    bot.handle(CHAT, "/status@MayaBot")
    assert "RUNNING" in _drain(bot)[0][1]
    bot.handle(CHAT, "/trades")
    assert "No closed trades" in _drain(bot)[0][1]
    bot.handle(CHAT, "/help")
    assert "no resume" in _drain(bot)[0][1]


def test_chart_command_sends_a_photo_or_explains():
    pytest.importorskip("matplotlib")
    from tests.test_chart_patterns import _candles, _path
    from maya.viz.chart import chart_payload

    def chart_fn(tf, bars):
        if tf != "15m":
            raise ValueError("no candles held")
        closes = _path([(0, 100), (10, 110), (18, 104), (26, 110), (34, 103)])
        return chart_payload(_candles(closes), "BTCUSDT", tf)

    bot, *_ = _bot(chart_fn=chart_fn)
    bot.handle(CHAT, "/chart 15m")
    (_, text, photo), = _drain(bot)
    assert photo[:4] == b"\x89PNG" and text.startswith("BTCUSDT 15m")
    bot.handle(CHAT, "/chart 7m")
    (_, text, photo), = _drain(bot)
    assert photo is None and "No chart: no candles held" in text


def test_chart_takes_a_coin_and_timeframe_in_any_order():
    seen = []

    def chart_fn(tf, bars, coin=None):
        seen.append((tf, coin))
        raise ValueError("stop here")

    bot, *_ = _bot(chart_fn=chart_fn)
    for text in ("/chart", "/chart ETH", "/chart eth 15m", "/chart 4h SOL", "🕯 Chart"):
        bot.handle(CHAT, text)
    assert seen == [("base", None), ("base", "ETH"), ("15m", "eth"), ("4h", "SOL"),
                    ("base", None)]
    assert all("No chart: stop here" in t for _, t, _ in _drain(bot))


def test_scanner_chart_fetches_the_coin_it_is_asked_for():
    """Regression: /chart failed under the scanner ("it holds no single
    symbol's candles"). It now fetches the coin's candles on request."""
    from types import SimpleNamespace

    from maya.data.models import Candle
    from maya.viz.chart import agent_chart, scanner_chart

    candles = [Candle(i * 60_000, 100 + i % 5, 101 + i % 5, 99 + i % 5, 100 + i % 5, 1.0)
               for i in range(60)]
    asked = []

    def fetch(symbol, n):
        asked.append(symbol)
        return candles

    def market(coin, sym):
        return SimpleNamespace(market=SimpleNamespace(coin=coin, binance_symbol=sym),
                               intent=None)

    eth = market("ETH", "ETHUSDT")
    eth.intent = object()                                       # the scan's candidate
    agent = SimpleNamespace(settings=Settings(), scanner=SimpleNamespace(fetch_candles=fetch),
                            execution=SimpleNamespace(position=None),
                            last_scan=[market("BTC", "BTCUSDT"), eth,
                                       market("kPEPE", "1000PEPEUSDT")])
    assert agent_chart(agent)["symbol"] == "ETHUSDT"            # best candidate by default
    assert agent_chart(agent, coin="kpepe")["symbol"] == "1000PEPEUSDT"
    assert agent_chart(agent, coin="DOGE")["symbol"] == "DOGEUSDT"
    other = scanner_chart(agent, "15m", 50, "BTC", fetcher_for=lambda tf: fetch)
    assert other["timeframe"] == "15m" and asked[-1] == "BTCUSDT"
    with pytest.raises(ValueError):
        agent_chart(agent, "7x")


def test_messages_sent_while_maya_was_down_are_ignored():
    """Regression guard: Telegram keeps undelivered updates for 24h. A
    '/halt confirm' typed before startup must not halt the new run."""
    stale = [{"update_id": 10, "message": {"text": "/halt confirm", "chat": {"id": CHAT}}}]
    bot, http, _, halts = _bot(http=FakeHttp(updates=[stale]))
    assert bot.skip_backlog() == 1 and bot._offset == 11


def test_running_bot_delivers_start_command_reply_and_stop():
    live = [{"update_id": 1, "message": {"text": "/status", "chat": {"id": CHAT}}}]
    http = FakeHttp(updates=[[], live])          # backlog empty, then one command
    bot, http, _, halts = _bot(http=http)
    bot.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and len(http.sent()) < 2:
            time.sleep(0.05)
    finally:
        bot.stop("bye")
    texts = [json.loads(b)["text"] for b in http.sent()]
    assert texts[0].startswith("Maya started")
    assert any("RUNNING" in t and not t.startswith("Maya started") for t in texts)
    assert texts[-1] == "bye" and halts == []


# -- plain-language features -------------------------------------------------------- #
LIMITS = {"max_trades_per_day": 10, "max_consecutive_losses": 2, "cooldown_minutes": 60,
          "daily_loss_limit_pct": 10.0, "weekly_loss_limit_pct": 8.0,
          "max_concurrent_positions": 1}
NOW = 1_795_000_000.0                       # seconds; a fixed clock for the bot
DAY0 = int(NOW * 1000) // tg.DAY_MS * tg.DAY_MS


def _trade(result, pnl, exit_ms, symbol="ETHUSDT", fees=0.1):
    return {"setup_name": "liquidity_sweep_choch", "direction": "long", "pnl_usdt": pnl,
            "result": result, "exit_reason": "take_profit" if result == "win" else "stop_loss",
            "timestamp_exit": exit_ms, "symbol": symbol, "fees_usdt": fees}


def test_buttons_and_plain_words_run_commands_but_never_halt():
    bot, _, _, halts = _bot()
    bot.handle(CHAT, "📊 Status")
    assert "RUNNING" in _drain(bot)[0][1]
    bot.handle(CHAT, "today")
    assert "Today" in _drain(bot)[0][1]
    bot.handle(CHAT, "halt confirm")                 # no slash: not a command
    bot.handle(CHAT, "🛑 halt confirm")
    assert halts == [] and all("did not understand" in t for _, t, _ in _drain(bot))
    assert not any("halt" in b.lower() for row in tg.BUTTONS for b in row)


def test_opened_alert_says_what_why_and_the_dollars_at_stake():
    bot, *_ = _bot()
    (kind, text), = bot.changes(_snap(), _snap(position=POS), [])
    assert kind == "opened" and "NEW TRADE" in text and "LONG (bets price goes UP) BTC" in text
    assert "lose about $0.50" in text and "gain about $1.50" in text     # 0.001 x 500 / 1500
    assert "grabbing other traders' stops" in text and "3.0 : 1" in text


def test_limits_show_how_close_maya_is_and_when_a_pause_ends():
    snap = _snap(state="cooldown") | {"limits": LIMITS}
    snap["account"] |= {"trades_today": 3, "consecutive_losses": 2, "equity": 95.0,
                        "day_start_equity": 100.0, "week_start_equity": 100.0,
                        "cooldown_until_ts": int(NOW * 1000) + 23 * 60_000, "open_positions": 0}
    text = tg.format_limits(snap, int(NOW * 1000))
    assert "Trades today: 3 of 10" in text and "Losses in a row: 2 of 2" in text
    assert "Today's loss: 5.0% of 10%" in text and "(24 min left)" in text


def test_pause_and_halt_alerts_explain_when_they_lift():
    bot, *_ = _bot()
    cool = _snap(state="cooldown") | {"limits": LIMITS}
    cool["account"] |= {"consecutive_losses": 2, "cooldown_until_ts": DAY0 + 3_600_000}
    (_, text), = bot.changes(_snap(), cool, [])
    assert "Paused for 60 min after 2 losses in a row" in text and "01:00 UTC" in text
    (_, text), = bot.changes(_snap(), _snap(state="halted", reasons=["daily loss limit"]), [])
    assert "lifts by itself at 00:00 UTC" in text
    (_, text), = bot.changes(_snap(), _snap(state="halted", reasons=["manual halt"]), [])
    assert "A person must check and restart Maya" in text
    (_, text), = bot.changes(cool, _snap(), [])
    assert "Trading again" in text


def test_today_counts_only_todays_trades_with_fees():
    trades = [_trade("win", 9.2, DAY0 + 1000), _trade("loss", -4.0, DAY0 + 2000),
              _trade("loss", -5.0, DAY0 - 1000)]                    # yesterday
    text = tg.day_report(trades, DAY0, "Today (UTC)")
    assert "2 trade(s): 1 win, 1 loss" in text and "P&L +$5.20" in text
    assert "fees paid $0.20" in text and "-$5.00" not in text
    assert "no closed trades" in tg.day_report([], DAY0, "Today")


def test_scan_explains_each_coin_and_says_when_it_is_single_coin():
    snap = _snap() | {"scan": [
        {"coin": "ETH", "setup": "liquidity_sweep_choch", "direction": "short", "net_rr": 3.2,
         "status": "trade"},
        {"coin": "SOL", "setup": "compression_breakout", "direction": "long", "net_rr": 0.8,
         "status": "fees eat too much of the target"},
        {"coin": "BTC", "setup": None, "direction": None, "net_rr": 0.0,
         "status": "no setup on the last closed candle"},
        {"coin": "XYZ", "setup": None, "direction": None, "net_rr": 0.0,
         "status": "no candles from Binance"}]}
    text = tg.format_scan(snap)
    assert "4 coins" in text and "ETH short" in text and "✅ tradable" in text
    assert "skipped: fees eat too much" in text and "Watching (no setup yet): BTC" in text
    assert "no candles from Binance: XYZ" in text and "No setups this minute" not in text
    assert "BTCUSDT only" in tg.format_scan(_snap())


def test_a_quiet_scan_says_it_is_normal_when_the_next_check_is_and_the_last_trade():
    now = DAY0 + 10 * 60_000 + 20_000                       # 00:10:20 UTC
    quiet = [{"coin": c, "setup": None, "direction": None, "net_rr": 0.0,
              "status": "no setup on the last closed candle"} for c in ("BTC", "ETH", "SOL")]
    snap = _snap() | {"scan": quiet, "scan_ts": now - 20_000}
    text = tg.format_scan(snap, now, last_trade_ms=now - 2 * 3_600_000)
    assert "3 coins, checked under a minute ago" in text
    assert "No setups this minute. That is normal" in text
    assert "about once every 8.9 h" in text                  # 24 / (0.9 x 3)
    assert "Next check in about 43 s" in text                 # 40 s to the close + 3 s grace
    assert text.endswith("Last trade closed 2.0 h ago.")     # footer after the coin list
    assert text.index("Watching") < text.index("Next check")
    assert "No trade closed yet" in tg.format_scan(snap, now)
    held = snap | {"position": POS}
    assert "In a trade on BTC" in tg.format_scan(held, now)


def test_daily_summary_is_sent_once_after_midnight():
    clock = {"t": NOW}
    trades = [_trade("win", 3.0, DAY0 + 5000)]
    bot = tg.TelegramBot(_settings(), _snap, lambda n: trades, print,
                         client=tg.TelegramClient(TOKEN, http=FakeHttp()),
                         clock=lambda: clock["t"])
    assert bot.daily_summary() is None
    clock["t"] = DAY0 / 1000 + 86_400 + 60                           # 00:01 next day
    text = bot.daily_summary()
    assert text and "Day summary" in text and "1 trade(s)" in text
    assert bot.daily_summary() is None                                # once only
    off = tg.TelegramBot(_settings(telegram_daily_summary=False), _snap, lambda n: trades,
                         print, client=tg.TelegramClient(TOKEN, http=FakeHttp()),
                         clock=lambda: clock["t"] + 86_400)
    clock["t"] += 86_400
    assert off.daily_summary() is None


def test_start_sets_the_menu_and_shows_the_buttons():
    http = FakeHttp(updates=[[]])
    bot, http, _, _ = _bot(http=http)
    bot.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not (http.sent() and http.sent("setMyCommands")):
            time.sleep(0.05)
    finally:
        bot.stop("")
    first = json.loads(http.sent()[0])
    assert first["text"].startswith("Maya started") and first["reply_markup"] == tg.KEYBOARD
    menu = json.loads(http.sent("setMyCommands")[0])["commands"]
    assert {"command": "limits", "description": "Safety limits and how close Maya is"} in menu


def test_snapshot_limits_come_from_risk_limits():
    """The /limits read-out must never disagree with what is enforced."""
    from maya.api.server import build_snapshot
    from maya.config import RiskLimits
    from maya.journal.store import TradeJournal
    from maya.nodes import TradingNodes
    journal = TradeJournal(":memory:")
    try:
        snap = build_snapshot(Settings(), TradingNodes(Settings(), journal), journal)
    finally:
        journal.close()
    assert snap["limits"]["max_trades_per_day"] == RiskLimits.MAX_TRADES_PER_DAY
    assert snap["limits"]["daily_loss_limit_pct"] == RiskLimits.DAILY_LOSS_LIMIT_PCT
    assert snap["account"]["day_start_equity"] > 0


# -- preflight CLI ------------------------------------------------------------------ #
def test_check_telegram_lists_chats_and_sends_a_test(capsys, monkeypatch):
    from maya import check_telegram
    from maya.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("MAYA_TELEGRAM_CHAT_IDS", str(CHAT))
    http = FakeHttp(updates=[[{"update_id": 3, "message": {
        "text": "hi", "chat": {"id": CHAT, "first_name": "Me"}}}]])
    try:
        rc = check_telegram.main(["--send"], client=tg.TelegramClient(TOKEN, http=http))
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert rc == 0 and "@MayaBot" in out and f"{CHAT}  Me" in out
    assert "sent test message" in out and len(http.sent()) == 1
