from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mos_s import configure_console_encoding
from telegram_notify import TelegramNotifier


LOGGER = logging.getLogger("mops_sync")
DEFAULT_NEW_MESSAGES_PATH = Path("state") / "new_messages.json"


def new_messages_path() -> Path:
    return Path(os.getenv("MOPS_NEW_MESSAGES_OUTPUT", str(DEFAULT_NEW_MESSAGES_PATH)))


def load_new_messages(path: Path) -> list[SimpleNamespace]:
    if not path.exists():
        LOGGER.info("Telegram notification skipped: new message payload does not exist: %s", path)
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_messages = payload.get("messages", []) if isinstance(payload, dict) else []
    if not isinstance(raw_messages, list):
        LOGGER.warning("Telegram notification skipped: invalid new message payload format")
        return []
    return [message_from_payload(item) for item in raw_messages if isinstance(item, dict)]


def message_from_payload(item: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        date=str(item.get("date") or ""),
        time=str(item.get("time") or ""),
        company_id=str(item.get("company_id") or ""),
        company_name=str(item.get("company_name") or ""),
        subject=str(item.get("subject") or ""),
    )


def main() -> int:
    configure_console_encoding()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    messages = load_new_messages(new_messages_path())
    if not messages:
        LOGGER.info("Telegram notification skipped: no new messages")
        return 0

    sent = TelegramNotifier.from_env().notify_new_messages(messages)
    if sent:
        LOGGER.info("Telegram notification sent for %s message(s)", len(messages))
    else:
        LOGGER.warning("Telegram notification was not sent; check TELEGRAM_* secrets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
