from __future__ import annotations

import logging
import os
import re
from typing import Iterable, Protocol

from curl_cffi import requests


LOGGER = logging.getLogger("mops_sync")
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
DEFAULT_LINE_NOTIFY_MAX_INDIVIDUAL = 10


class LineMessageLike(Protocol):
    date: str
    time: str
    company_id: str
    company_name: str
    subject: str


class LineNotifier:
    def __init__(
        self,
        channel_access_token: str = "",
        target_ids: Iterable[str] = (),
        enabled: bool = False,
        max_individual: int = DEFAULT_LINE_NOTIFY_MAX_INDIVIDUAL,
        push_url: str = LINE_PUSH_URL,
        timeout_seconds: int = 15,
    ) -> None:
        self.channel_access_token = channel_access_token.strip()
        self.target_ids = [target_id.strip() for target_id in target_ids if target_id and target_id.strip()]
        self.enabled = enabled
        self.max_individual = max(max_individual, 0)
        self.push_url = push_url
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "LineNotifier":
        return cls(
            channel_access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""),
            target_ids=os.getenv("LINE_TARGET_IDS", "").split(","),
            enabled=parse_bool(os.getenv("LINE_NOTIFY_ENABLED", "false")),
            max_individual=parse_int(os.getenv("LINE_NOTIFY_MAX_INDIVIDUAL"), DEFAULT_LINE_NOTIFY_MAX_INDIVIDUAL),
        )

    def notify_new_messages(self, messages: list[LineMessageLike]) -> None:
        if not messages:
            return
        if not self.enabled:
            LOGGER.info("LINE notification disabled")
            return
        if not self.channel_access_token:
            LOGGER.warning("LINE notification skipped: LINE_CHANNEL_ACCESS_TOKEN is missing")
            return
        if not self.target_ids:
            LOGGER.warning("LINE notification skipped: LINE_TARGET_IDS is missing")
            return

        texts = self.build_notification_texts(messages)
        for target_id in self.target_ids:
            for text in texts:
                try:
                    self._push_text(target_id, text)
                except Exception as exc:
                    LOGGER.warning("LINE notification failed for target %s: %s", mask_identifier(target_id), exc)

    def build_notification_texts(self, messages: list[LineMessageLike]) -> list[str]:
        sorted_messages = sorted(messages, key=line_message_sort_key, reverse=True)
        texts: list[str] = []
        if len(sorted_messages) > self.max_individual:
            texts.append(
                "\u76ee\u524d\u6709 {count} \u7b46\u65b0\u7684\u91cd\u5927\u5373\u6642\u8a0a\u606f!\n"
                "\u5c07\u5148\u5217\u51fa\u524d {limit} \u7b46\uff0c\u5176\u9918\u8acb\u67e5\u770b\u7db2\u7ad9\u3002".format(
                    count=len(sorted_messages),
                    limit=self.max_individual,
                )
            )
        for message in sorted_messages[: self.max_individual]:
            texts.append(format_line_message(message))
        return texts

    def _push_text(self, target_id: str, text: str) -> None:
        response = requests.post(
            self.push_url,
            headers={
                "Authorization": f"Bearer {self.channel_access_token}",
                "Content-Type": "application/json",
            },
            json={"to": target_id, "messages": [{"type": "text", "text": text}]},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()


def format_line_message(message: LineMessageLike) -> str:
    return (
        "\u76ee\u524d\u6709\u65b0\u7684\u91cd\u5927\u5373\u6642\u8a0a\u606f!\n"
        f"\u516c\u53f8\u540d:{message.company_name}\n"
        f"\u4e3b\u65e8:{message.subject}"
    )


def line_message_sort_key(message: LineMessageLike) -> tuple[str, str, str, str]:
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


def parse_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip()) if value is not None and str(value).strip() else default
    except ValueError:
        LOGGER.warning("Invalid integer value %r, using default %s", value, default)
        return default


def mask_identifier(value: str) -> str:
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"
