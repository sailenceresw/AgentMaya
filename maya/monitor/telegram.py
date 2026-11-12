"""Telegram bot — alerts and remote commands (spec §10.1).

Outbound: trade open/close, halts, daily summaries.
Inbound : /status /halt /resume /close_all /stats /risk (polled between candles).

Implemented with the stdlib (urllib) so it adds no dependency. When disabled
(no token/chat id, or MAYA_TELEGRAM_ENABLED=false) every method is a safe no-op,
so sim/paper/backtest runs are unaffected. All network calls fail soft.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from loguru import logger

from maya.config import Settings
from maya.execution.engine import TradeResult
from maya.execution.models import Position

_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id
        self.enabled = settings.telegram_enabled and bool(self.token and self.chat_id)
        self._offset = 0
        if self.enabled:
            logger.info("Telegram alerts enabled (chat {})", self.chat_id)

    # -- transport --------------------------------------------------------- #
    def _call(self, method: str, params: dict, timeout: int = 10) -> dict | None:
        url = _API.format(token=self.token, method=method)
        data = urllib.parse.urlencode(params).encode()
        try:
            with urllib.request.urlopen(url, data=data, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001 - alerts must never crash trading
            logger.warning("Telegram {} failed: {}", method, exc)
            return None

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        r = self._call("sendMessage", {"chat_id": self.chat_id, "text": text,
                                       "parse_mode": "HTML"})
        return bool(r and r.get("ok"))

    # -- outbound alerts --------------------------------------------------- #
    def trade_opened(self, pos: Position) -> None:
        self.send(
            f"🟢 <b>OPEN {pos.direction.upper()}</b> {pos.symbol}\n"
            f"setup: {pos.setup_name} (score {pos.confluence_score})\n"
            f"entry {pos.entry_price:.2f} · SL {pos.stop_loss:.2f} · "
            f"TP {pos.take_profit:.2f}\nsize {pos.quantity:.5f} @ {pos.leverage}x"
        )

    def trade_closed(self, r: TradeResult, equity: float) -> None:
        icon = {"win": "✅", "loss": "❌", "breakeven": "➖"}.get(r.result, "•")
        self.send(
            f"{icon} <b>CLOSE</b> {r.position.symbol} {r.position.setup_name}\n"
            f"{r.result} · PnL {r.pnl_usdt:+.2f} USDT · R {r.rr_realized:+.2f}\n"
            f"reason: {r.reason} · equity ${equity:.2f}"
        )

    def halted(self, reasons: list[str]) -> None:
        self.send("🛑 <b>KILL SWITCH — HALTED</b>\n" + "\n".join(f"• {x}" for x in reasons))

    def summary(self, text: str) -> None:
        self.send("📊 " + text)

    # -- inbound commands -------------------------------------------------- #
    def poll_commands(self) -> list[str]:
        """Return new slash-commands addressed to the configured chat."""
        if not self.enabled:
            return []
        r = self._call("getUpdates", {"offset": self._offset, "timeout": 0}, timeout=15)
        cmds: list[str] = []
        if r and r.get("ok"):
            for upd in r["result"]:
                self._offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                if str(msg.get("chat", {}).get("id")) != str(self.chat_id):
                    continue
                text = (msg.get("text") or "").strip()
                if text.startswith("/"):
                    cmds.append(text.split()[0].lower().lstrip("/"))
        return cmds
