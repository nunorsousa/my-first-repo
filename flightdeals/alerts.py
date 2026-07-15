"""Telegram alerting via the Bot API (plain HTTPS, no SDK).

Without TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID set, alerts degrade to
console output ("[dry-run] ...") so the pipeline stays testable.

Helper: `python -m flightdeals.alerts --get-chat-id` prints the chat ids
your bot can see — message the bot once first, then run it.
"""

from __future__ import annotations

import argparse
import html
import logging
import sqlite3

import requests

from . import db
from .config import env, load_config
from .models import Deal

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT = 15

KIND_LABELS = {
    "error_fare": ("🚨", "Possible error fare"),
    "price_drop": ("💸", "Price drop"),
    "blog": ("📰", "Blog find"),
}


def telegram_configured() -> bool:
    return bool(env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"))


def _fmt_money(price: float, currency: str | None) -> str:
    amount = f"{price:,.0f}"
    return f"€{amount}" if (currency or "EUR") == "EUR" else f"{amount} {currency}"


def format_deal(deal: Deal) -> str:
    """HTML-mode Telegram message. All dynamic text is escaped."""
    emoji, label = KIND_LABELS.get(deal.kind, ("✈️", deal.kind))
    lines: list[str] = []

    if deal.origin and deal.destination:
        lines.append(f"{emoji} <b>{label}: {html.escape(deal.origin)} → {html.escape(deal.destination)}</b>")
    else:
        lines.append(f"{emoji} <b>{label}</b>")

    if deal.price is not None:
        price_line = f"<b>{html.escape(_fmt_money(deal.price, deal.currency))}</b>"
        if deal.cabin:
            price_line += f" · {html.escape(deal.cabin.title())}"
        if deal.discount_pct is not None and deal.baseline_price:
            price_line += (
                f" · {deal.discount_pct:.0f}% below typical "
                f"{html.escape(_fmt_money(deal.baseline_price, deal.currency))}"
            )
        lines.append(price_line)

    if deal.departure_date:
        dates = f"🗓 {html.escape(deal.departure_date)}"
        if deal.return_date:
            dates += f" → {html.escape(deal.return_date)}"
        lines.append(dates)

    lines.append(html.escape(deal.reason))

    footer = f"via {html.escape(deal.source)}"
    if deal.url:
        link_text = "read the post" if deal.kind == "blog" else "check this route"
        footer += f' · <a href="{html.escape(deal.url, quote=True)}">{link_text}</a>'
    lines.append(footer)
    return "\n".join(lines)


def send_message(text: str) -> bool:
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    response = requests.post(
        f"{API_BASE}/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=REQUEST_TIMEOUT,
    )
    payload = {}
    try:
        payload = response.json()
    except ValueError:
        pass
    if response.status_code == 200 and payload.get("ok"):
        return True
    log.error("telegram sendMessage failed: HTTP %s %s", response.status_code, str(payload)[:300])
    return False


def alert_deals(
    conn: sqlite3.Connection,
    stored_deals: list[tuple[int, Deal]],
    max_alerts: int,
    dry_run: bool = False,
) -> tuple[int, list[str]]:
    """Send alerts for freshly stored deals. Returns (sent_count, errors).

    Deals beyond max_alerts stay in the DB unalerted (visible on the
    dashboard) rather than spamming Telegram on a big first run.
    """
    errors: list[str] = []
    sent = 0
    configured = telegram_configured()

    if len(stored_deals) > max_alerts:
        log.warning(
            "capping alerts at %d of %d deals this run (alerts.max_alerts_per_run)",
            max_alerts,
            len(stored_deals),
        )

    for deal_id, deal in stored_deals[:max_alerts]:
        message = format_deal(deal)
        if dry_run or not configured:
            prefix = "[dry-run]" if dry_run else "[telegram not configured]"
            log.info("%s would send:\n%s", prefix, message)
            continue
        try:
            if send_message(message):
                db.mark_deal_alerted(conn, deal_id)
                sent += 1
            else:
                errors.append(f"telegram: send failed for deal {deal_id}")
        except requests.RequestException as exc:
            errors.append(f"telegram: {exc}")
    return sent, errors


def print_chat_ids() -> None:
    """Setup helper: shows chat ids visible to the bot via getUpdates."""
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Set TELEGRAM_BOT_TOKEN in .env first (get one from @BotFather).")
        return
    response = requests.get(f"{API_BASE}/bot{token}/getUpdates", timeout=REQUEST_TIMEOUT)
    updates = response.json().get("result", [])
    chats = {}
    for update in updates:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id"):
            name = chat.get("title") or chat.get("username") or chat.get("first_name") or "?"
            chats[chat["id"]] = (chat.get("type"), name)
    if not chats:
        print("No chats found. Send any message to your bot on Telegram, then run this again.")
        return
    print("Chats your bot can see — put the id you want in .env as TELEGRAM_CHAT_ID:")
    for chat_id, (chat_type, name) in chats.items():
        print(f"  {chat_id}   ({chat_type}: {name})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Telegram alert utilities")
    parser.add_argument("--get-chat-id", action="store_true", help="list chat ids the bot can see")
    parser.add_argument("--test-message", action="store_true", help="send a test message")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_config()  # loads .env

    if args.get_chat_id:
        print_chat_ids()
    elif args.test_message:
        if not telegram_configured():
            print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env")
        else:
            ok = send_message("✈️ flightdeals test message — your bot is wired up correctly.")
            print("sent!" if ok else "send failed — check the token and chat id")
    else:
        parser.print_help()
