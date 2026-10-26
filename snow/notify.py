"""Optional alerts to Telegram and/or a Discord webhook, configured by environment variables."""

from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)


def _post(url: str, payload: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).close()


def notify(text: str) -> None:
    """Send ``text`` to every configured channel. Never raises: an alert must not stop a run."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    discord = os.environ.get("DISCORD_WEBHOOK_URL")
    try:
        if token and chat:
            _post(f"https://api.telegram.org/bot{token}/sendMessage", {"chat_id": chat, "text": text})
        if discord:
            _post(discord, {"content": text[:2000]})
    except Exception as exc:  # noqa: BLE001
        log.warning("notification failed: %s", exc)
