from __future__ import annotations

import logging
import os
import re
import time
from typing import Iterable, Protocol

from curl_cffi import requests


LOGGER = logging.getLogger("mops_sync")
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"
DEFAULT_LINE_NOTIFY_MAX_INDIVIDUAL = 10
DEFAULT_LINE_BROADCAST_MAX_CHARS = 4500
DEFAULT_LINE_BROADCAST_COMPANY_MAX_CHARS = 80
DEFAULT_LINE_BROADCAST_SUBJECT_MAX_CHARS = 240
DEFAULT_LINE_BROADCAST_MAX_ATTEMPTS = 2
DEFAULT_LINE_BROADCAST_RETRY_SECONDS = 30
DEFAULT_SITE_URL = "https://mops-sync-site.pages.dev/"


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
        site_url: str = DEFAULT_SITE_URL,
        notify_mode: str = "push",
        broadcast_max_chars: int = DEFAULT_LINE_BROADCAST_MAX_CHARS,
        broadcast_max_attempts: int = DEFAULT_LINE_BROADCAST_MAX_ATTEMPTS,
        broadcast_retry_seconds: int = DEFAULT_LINE_BROADCAST_RETRY_SECONDS,
        push_url: str = LINE_PUSH_URL,
        broadcast_url: str = LINE_BROADCAST_URL,
        timeout_seconds: int = 15,
    ) -> None:
        self.channel_access_token = channel_access_token.strip()
        self.target_ids = [target_id.strip() for target_id in target_ids if target_id and target_id.strip()]
        self.enabled = enabled
        self.max_individual = max(max_individual, 0)
        self.site_url = site_url.strip()
        self.notify_mode = normalize_notify_mode(notify_mode)
        self.broadcast_max_chars = max(broadcast_max_chars, 500)
        self.broadcast_max_attempts = max(broadcast_max_attempts, 1)
        self.broadcast_retry_seconds = max(broadcast_retry_seconds, 0)
        self.push_url = push_url
        self.broadcast_url = broadcast_url
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "LineNotifier":
        return cls(
            channel_access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""),
            target_ids=os.getenv("LINE_TARGET_IDS", "").split(","),
            enabled=parse_bool(os.getenv("LINE_NOTIFY_ENABLED", "false")),
            max_individual=parse_int(os.getenv("LINE_NOTIFY_MAX_INDIVIDUAL"), DEFAULT_LINE_NOTIFY_MAX_INDIVIDUAL),
            site_url=os.getenv("MOPS_SITE_URL", DEFAULT_SITE_URL),
            notify_mode=os.getenv("LINE_NOTIFY_MODE", "push"),
            broadcast_max_chars=parse_int(os.getenv("LINE_BROADCAST_MAX_CHARS"), DEFAULT_LINE_BROADCAST_MAX_CHARS),
            broadcast_max_attempts=parse_int(os.getenv("LINE_BROADCAST_MAX_ATTEMPTS"), DEFAULT_LINE_BROADCAST_MAX_ATTEMPTS),
            broadcast_retry_seconds=parse_int(os.getenv("LINE_BROADCAST_RETRY_SECONDS"), DEFAULT_LINE_BROADCAST_RETRY_SECONDS),
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
        if self.notify_mode == "broadcast":
            text = self.build_broadcast_text(messages)
            if not self._try_broadcast_text(text):
                self._fallback_push_text(text)
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
            texts.append(format_line_message(message, self.site_url))
        return texts

    def build_broadcast_text(self, messages: list[LineMessageLike]) -> str:
        sorted_messages = sorted(messages, key=line_message_sort_key, reverse=True)
        visible_messages = sorted_messages[: self.max_individual]
        lines = [f"\u76ee\u524d\u6709 {len(sorted_messages)} \u7b46\u65b0\u7684\u91cd\u5927\u5373\u6642\u8a0a\u606f!"]
        for index, message in enumerate(visible_messages, start=1):
            lines.extend(
                [
                    "",
                    f"{index}. \u516c\u53f8\u540d:{truncate_text(message.company_name, DEFAULT_LINE_BROADCAST_COMPANY_MAX_CHARS)}",
                    f"\u4e3b\u65e8:{truncate_text(message.subject, DEFAULT_LINE_BROADCAST_SUBJECT_MAX_CHARS)}",
                ]
            )
        if len(sorted_messages) > len(visible_messages):
            lines.extend(["", f"\u5176\u9918 {len(sorted_messages) - len(visible_messages)} \u7b46\u8acb\u67e5\u770b\u7db2\u7ad9\u3002"])
        if self.site_url:
            lines.extend(["", f"\u67e5\u770b\u7db2\u7ad9:{self.site_url}"])
        return truncate_broadcast_text("\n".join(lines), self.site_url, self.broadcast_max_chars)

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

    def _broadcast_text(self, text: str) -> None:
        response = requests.post(
            self.broadcast_url,
            headers={
                "Authorization": f"Bearer {self.channel_access_token}",
                "Content-Type": "application/json",
            },
            json={"messages": [{"type": "text", "text": text}]},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()

    def _try_broadcast_text(self, text: str) -> bool:
        for attempt in range(1, self.broadcast_max_attempts + 1):
            try:
                self._broadcast_text(text)
                return True
            except Exception as exc:
                status_code = http_status_code(exc)
                can_retry = status_code == 429 and attempt < self.broadcast_max_attempts
                if can_retry:
                    sleep_seconds = retry_after_seconds(exc, self.broadcast_retry_seconds)
                    LOGGER.warning(
                        "LINE broadcast rate limited, retry %s/%s after %s seconds",
                        attempt,
                        self.broadcast_max_attempts,
                        sleep_seconds,
                    )
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)
                    continue
                LOGGER.warning("LINE broadcast notification failed: %s", exc)
                return False
        return False

    def _fallback_push_text(self, text: str) -> None:
        if not self.target_ids:
            LOGGER.warning("LINE broadcast failed and LINE_TARGET_IDS is missing; no fallback recipients")
            return
        LOGGER.warning("LINE broadcast failed; fallback pushing summary to %s configured target(s)", len(self.target_ids))
        for target_id in self.target_ids:
            try:
                self._push_text(target_id, text)
            except Exception as exc:
                LOGGER.warning("LINE fallback push failed for target %s: %s", mask_identifier(target_id), exc)


def format_line_message(message: LineMessageLike, site_url: str = DEFAULT_SITE_URL) -> str:
    text = (
        "\u76ee\u524d\u6709\u65b0\u7684\u91cd\u5927\u5373\u6642\u8a0a\u606f!\n"
        f"\u516c\u53f8\u540d:{message.company_name}\n"
        f"\u4e3b\u65e8:{message.subject}"
    )
    return f"{text}\n\u67e5\u770b\u7db2\u7ad9:{site_url.strip()}" if site_url.strip() else text


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


def http_status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is not None:
        return int(status_code)
    match = re.search(r"\bHTTP Error (\d{3})\b", str(exc))
    return int(match.group(1)) if match else None


def retry_after_seconds(exc: Exception, default: int) -> int:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("Retry-After") or headers.get("retry-after")
    seconds = parse_int(value, default) if value is not None else default
    return min(max(seconds, 0), 120)


def truncate_text(value: str, max_chars: int) -> str:
    text = str(value or "").strip()
    if max_chars <= 1 or len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1]}\u2026"


def truncate_broadcast_text(text: str, site_url: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = f"\n\n\u8a0a\u606f\u904e\u9577\uff0c\u8acb\u67e5\u770b\u7db2\u7ad9:{site_url}" if site_url else "\n\n\u8a0a\u606f\u904e\u9577\uff0c\u8acb\u67e5\u770b\u7db2\u7ad9\u3002"
    allowed = max(max_chars - len(suffix) - 1, 0)
    return f"{text[:allowed]}\u2026{suffix}"


def normalize_notify_mode(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "broadcast":
        return "broadcast"
    return "push"


def mask_identifier(value: str) -> str:
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"
