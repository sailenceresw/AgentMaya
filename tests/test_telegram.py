"""Tests for the Telegram bot (Layer 7) — all offline, no real network."""
from maya.config import RunMode, Settings
from maya.execution.engine import TradeResult
from maya.execution.models import Position
from maya.monitor.telegram import TelegramBot


def _pos():
    return Position("BTCUSDT", "long", "value_area_absorption", 100, 99, 102,
                    quantity=1.0, notional=100, leverage=5, opened_ts=0,
                    confluence_score=8, rr_planned=4.0)


def test_disabled_bot_is_noop():
    bot = TelegramBot(Settings(mode=RunMode.SIM))  # no token/chat -> disabled
    assert bot.enabled is False
    assert bot.send("hi") is False            # never raises, returns False
    assert bot.poll_commands() == []
    bot.trade_opened(_pos())                   # no-ops, no exception


def test_enabled_sends_via_transport(monkeypatch):
    settings = Settings(mode=RunMode.TESTNET, telegram_enabled=True)
    # inject creds without env
    object.__setattr__(settings, "telegram_bot_token", "T")
    object.__setattr__(settings, "telegram_chat_id", "42")
    bot = TelegramBot(settings)
    assert bot.enabled is True

    sent = {}

    def fake_call(method, params, timeout=10):
        sent["method"] = method
        sent["params"] = params
        return {"ok": True, "result": []}

    monkeypatch.setattr(bot, "_call", fake_call)
    assert bot.send("hello") is True
    assert sent["method"] == "sendMessage"
    assert sent["params"]["chat_id"] == "42"
    assert sent["params"]["text"] == "hello"

    # Alert helpers route through the same transport.
    r = TradeResult(_pos(), 102.0, pnl_usdt=2.0, fees_usdt=0.1, rr_realized=2.0,
                    result="win", exit_ts=1, reason="take_profit")
    bot.trade_closed(r, equity=1002.0)
    assert sent["method"] == "sendMessage"


def test_poll_commands_filters_chat_and_parses(monkeypatch):
    settings = Settings(mode=RunMode.TESTNET, telegram_enabled=True)
    object.__setattr__(settings, "telegram_bot_token", "T")
    object.__setattr__(settings, "telegram_chat_id", "42")
    bot = TelegramBot(settings)

    updates = {"ok": True, "result": [
        {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/status"}},
        {"update_id": 2, "message": {"chat": {"id": 99}, "text": "/halt"}},   # other chat
        {"update_id": 3, "message": {"chat": {"id": 42}, "text": "/halt now"}},
        {"update_id": 4, "message": {"chat": {"id": 42}, "text": "hello"}},    # not a cmd
    ]}
    monkeypatch.setattr(bot, "_call", lambda m, p, timeout=10: updates)
    cmds = bot.poll_commands()
    assert cmds == ["status", "halt"]        # other-chat and non-command dropped
    assert bot._offset == 5                   # advanced past the last update
