"""Telegram helper — send messages and poll for replies."""

import time
import requests

TELEGRAM_API = "https://api.telegram.org"


def send_message(token: str, chat_id: str, text: str) -> dict:
    r = requests.post(
        f"{TELEGRAM_API}/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    )
    return r.json()


def get_updates(token: str, offset: int = None, timeout: int = 30) -> list:
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(
        f"{TELEGRAM_API}/bot{token}/getUpdates",
        params=params,
        timeout=timeout + 5,
    )
    return r.json().get("result", [])


def poll_for_reply(token: str, chat_id: str, sent_at: int, wait_seconds: int = 1800) -> str | None:
    """
    Poll for a reply from chat_id after sent_at timestamp.
    Returns the reply text or None if timeout reached.
    """
    # Drain existing updates to get current offset
    updates = get_updates(token, timeout=0)
    offset = max((u["update_id"] for u in updates), default=0) + 1

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        remaining = int(deadline - time.time())
        if remaining <= 0:
            break
        poll_wait = min(30, remaining)
        updates = get_updates(token, offset=offset, timeout=poll_wait)
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message", {})
            if str(msg.get("chat", {}).get("id")) == str(chat_id):
                if msg.get("date", 0) >= sent_at:
                    return msg.get("text", "").strip()
    return None
