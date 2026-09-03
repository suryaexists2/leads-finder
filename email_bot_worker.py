"""
Command-based Telegram approval worker for the AI cold-email drafts.

Reads messages from the configured Telegram user and handles:
    /send <draft_id>  -> send the approved draft via existing SMTP
    /skip <draft_id>  -> mark the draft skipped (no send)
    /status           -> quick online status line

Only the configured TELEGRAM_USER_ID is allowed to act. Long-polls the Bot API.
Run separately from the lead scheduler:  python email_bot_worker.py
"""
import time

import requests

import telegram_bot
import email_automation

ALLOWED_IDS = {}


def _load_chat_users():
    token, userId = telegram_bot.get_config()
    if token and userId:
        try:
            return {int(str(userId).strip())}
        except Exception:
            return set()
    return set()


def _get_updates(token, offset, timeout=25):
    r = requests.post(
        f"https://api.telegram.org/bot{token}/getUpdates",
        json={"offset": offset, "timeout": timeout, "allowed_updates": ["message"]},
        timeout=timeout + 10,
    )
    if r.status_code == 200:
        return r.json().get("result", [])
    return []


def _reply(token, chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
    except Exception as e:
        email_automation.log(f"[BOT] reply failed: {e}")


def main():
    token, _ = telegram_bot.get_config()
    if not token:
        print("TELEGRAM_BOT_TOKEN not set. Exiting.")
        return
    allowed = _load_chat_users()
    email_automation.log("[BOT] command-approval worker started")
    offset = 0
    while True:
        try:
            updates = _get_updates(token, offset)
        except Exception as e:
            email_automation.log(f"[BOT] getUpdates error: {e}")
            time.sleep(10)
            continue
        for u in updates:
            offset = u.get("update_id", offset) + 1
            msg = u.get("message") or {}
            chat = msg.get("chat") or {}
            text = (msg.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            if allowed and chat.get("id") not in allowed:
                _reply(token, chat.get("id"), "Not authorized.")
                continue
            parts = text.split()
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else None
            if cmd == "/status":
                _reply(token, chat.get("id"), "⚙️ Email approval worker is running.")
            elif cmd == "/send":
                if not arg:
                    _reply(token, chat.get("id"), "Usage: /send <draft_id>")
                    continue
                res = email_automation.send_draft_email(arg)
                _reply(token, chat.get("id"),
                       ("✅ Sent." if res.get("ok") else f"❌ {res.get('reason')}"))
            elif cmd == "/skip":
                if not arg:
                    _reply(token, chat.get("id"), "Usage: /skip <draft_id>")
                    continue
                res = email_automation.skip_draft(arg)
                _reply(token, chat.get("id"),
                       ("✅ Skipped." if res.get("ok") else f"❌ {res.get('reason')}"))


if __name__ == "__main__":
    main()
