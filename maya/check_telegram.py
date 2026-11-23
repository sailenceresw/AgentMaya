"""Telegram preflight: check the bot token, find your chat id, send a test.

    python -m maya.check_telegram            # who is the bot, and which chats wrote to it
    python -m maya.check_telegram --send     # send a test message to MAYA_TELEGRAM_CHAT_IDS

Setup, once:
  1. In Telegram, talk to @BotFather, send /newbot, and copy the token into
     .env as MAYA_TELEGRAM_BOT_TOKEN=...
  2. Send any message (e.g. "hi") to your new bot.
  3. Run this script. It prints your chat id. Put it in .env as
     MAYA_TELEGRAM_CHAT_IDS=<id>, then set MAYA_TELEGRAM_ENABLED=true.
"""
from __future__ import annotations

import argparse
import sys

from maya.api.telegram import TelegramClient, TelegramError
from maya.config import get_settings


def main(argv: list[str] | None = None, client: TelegramClient | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m maya.check_telegram",
                                description=__doc__.split("\n")[0])
    p.add_argument("--send", action="store_true", help="send a test message to the allowed chats")
    a = p.parse_args(argv)
    s = get_settings()
    if client is None:
        if not s.telegram_bot_token:
            print("MAYA_TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather first.",
                  file=sys.stderr)
            return 2
        client = TelegramClient(s.telegram_bot_token)
    try:
        me = client.call("getMe")
        print(f"bot: @{me.get('username')} ({me.get('first_name')}) - token works")
        # timeout=0 and no offset: looks without consuming anything.
        updates = client.get_updates(None, timeout=0)
    except TelegramError as exc:
        print(f"Telegram refused: {exc}", file=sys.stderr)
        return 1

    chats: dict[int, str] = {}
    for u in updates:
        chat = (u.get("message") or {}).get("chat") or {}
        if "id" in chat:
            chats[chat["id"]] = chat.get("username") or chat.get("first_name") or chat.get("title", "")
    if chats:
        print("chats that messaged the bot recently:")
        for cid, name in chats.items():
            print(f"  {cid}  {name}")
    else:
        print("no recent messages: send your bot any message, then run this again")

    allowed = s.telegram_chat_id_list
    print(f"MAYA_TELEGRAM_CHAT_IDS: {allowed or 'not set'}")
    if a.send:
        if not allowed:
            print("nothing to send to: set MAYA_TELEGRAM_CHAT_IDS first", file=sys.stderr)
            return 2
        for cid in allowed:
            try:
                client.send_message(cid, "Maya: Telegram is connected. Send /help for commands.")
                print(f"sent test message to {cid}")
            except TelegramError as exc:
                print(f"could not send to {cid}: {exc}", file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
