from __future__ import annotations

import logging
import os
import re
from typing import Iterable, Protocol

from curl_cffi import requests


LOGGER = logging.getLogger("mops_sync")
TELEGRAM_API_URL_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"
DEFAULT_TELEGRAM_MAX_ITEMS = 10
DEFAULT_TELEGRAM_MAX_CHARS = 3900
DEFAULT_TELEGRAM_COMPANY_MAX_CHARS = 80
DEFAULT_TELEGRAM_SUBJECT_MAX_CHARS = 240
DEFAULT_SITE_URL = "https://mops-sync-site.pages.dev/"


class TelegramMessageLike(Protocol):
    date: str
    time: str
    company_id: str
    company_name: str
    subject: str


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str = "",
        chat_ids: Iterable[str] = (),
        enabled: bool = False,
        max_items: int = DEFAULT_TELEGRAM_MAX_ITEMS,
        site_url: str = DEFAULT_SITE_URL,
        max_chars: int = DEFAULT_TELEGRAM_MAX_CHARS,
        api_url_template: str = TELEGRAM_API_URL_TEMPLATE,
        timeout_seconds: int = 15,
    ) -> None:
        self.bot_token = bot_token.strip()
        self.chat_ids = [chat_id.strip() for chat_id in chat_ids if chat_id and chat_id.strip()]
        self.enabled = enabled
        self.max_items = max(max_items, 1)
        self.site_url = site_url.strip()
        self.max_chars = max(max_chars, 500)
        self.api_url_template = api_url_template
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "TelegramNotifier":
        return cls(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            chat_ids=os.getenv("TELEGRAM_CHAT_IDS", "").split(","),
            enabled=parse_bool(os.getenv("TELEGRAM_NOTIFY_ENABLED", "false")),
            max_items=parse_int(os.getenv("TELEGRAM_NOTIFY_MAX_ITEMS"), DEFAULT_TELEGRAM_MAX_ITEMS),
            site_url=os.getenv("MOPS_SITE_URL", DEFAULT_SITE_URL),
            max_chars=parse_int(os.getenv("TELEGRAM_MESSAGE_MAX_CHARS"), DEFAULT_TELEGRAM_MAX_CHARS),
        )

    def notify_new_messages(self, messages: list[TelegramMessageLike]) -> bool:
        if not messages:
            return False
        if not self.enabled:
            LOGGER.info("Telegram notification disabled")
            return False
        if not self.bot_token:
            LOGGER.warning("Telegram notification skipped: TELEGRAM_BOT_TOKEN is missing")
            return False
        if not self.chat_ids:
            LOGGER.warning("Telegram notification skipped: TELEGRAM_CHAT_IDS is missing")
            return False

        text = self.build_summary_text(messages)
        sent_any = False
        for chat_id in self.chat_ids:
            try:
                self._send_text(chat_id, text)
                sent_any = True
            except Exception as exc:
                LOGGER.warning("Telegram notification failed for chat %s: %s", mask_identifier(chat_id), exc)
        return sent_any

    def build_summary_text(self, messages: list[TelegramMessageLike]) -> str:
        sorted_messages = sorted(messages, key=telegram_message_sort_key, reverse=True)
        visible_messages = sorted_messages[: self.max_items]
        lines = [f"目前有 {len(sorted_messages)} 筆新的重大即時訊息!"]
        for index, message in enumerate(visible_messages, start=1):
            lines.extend(
                [
                    "",
                    f"{index}. 公司名:{truncate_text(message.company_name, DEFAULT_TELEGRAM_COMPANY_MAX_CHARS)}",
                    f"主旨:{truncate_text(message.subject, DEFAULT_TELEGRAM_SUBJECT_MAX_CHARS)}",
                ]
            )
        if len(sorted_messages) > len(visible_messages):
            lines.extend(["", f"其餘 {len(sorted_messages) - len(visible_messages)} 筆請查看網站。"])
        if self.site_url:
            lines.extend(["", f"查看網站:{self.site_url}"])
        return truncate_message_text("\n".join(lines), self.site_url, self.max_chars)

    def _send_text(self, chat_id: str, text: str) -> None:
        response = requests.post(
            self.api_url_template.format(token=self.bot_token),
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": False,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()


def telegram_message_sort_key(message: TelegramMessageLike) -> tuple[str, str, str, str]:
    return (normalize_date_for_sort(message.date), normalize_time_for_sort(message.time), message.company_id, message.subject)


def normalize_date_for_sort(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits if len(digits) == 8 else value


def normalize_time_for_sort(value: str) -> str:
    parts = re.findall(r"\d+", value or "")
    if not parts:
        return ""
    hour = int(parts[0]) if len(parts) > 0 else 0
    minute = int(parts[1]) if len(parts) > 1 else 0
    second = int(parts[2]) if len(parts) > 2 else 0
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def truncate_text(value: object, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max(max_chars - 1, 0)].rstrip() + "…"


def truncate_message_text(text: str, site_url: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = f"\n\n內容較多，請查看網站:{site_url}" if site_url else "\n\n內容較多，請查看網站。"
    allowed = max(max_chars - len(suffix) - 1, 0)
    return text[:allowed].rstrip() + "…" + suffix


def parse_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def mask_identifier(identifier: str) -> str:
    if len(identifier) <= 8:
        return "***"
    return f"{identifier[:4]}...{identifier[-4:]}"
