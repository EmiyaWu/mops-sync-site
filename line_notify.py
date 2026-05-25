from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Iterable, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from curl_cffi import requests

try:
    import gspread
    from gspread.exceptions import WorksheetNotFound
except ImportError:  # pragma: no cover - exercised only when dependencies are missing
    gspread = None
    WorksheetNotFound = Exception


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
SUBSCRIBER_WORKSHEET_TITLE = "line_subscribers"
SUBSCRIBER_USER_ID = "user_id"
SUBSCRIBER_STATUS = "status"
NOTIFICATION_QUEUE_WORKSHEET_TITLE = "line_notify_queue"
NOTIFICATION_STATE_WORKSHEET_TITLE = "line_notify_state"
DEFAULT_LINE_NOTIFY_INTERVAL_SECONDS = 600
DEFAULT_LINE_NOTIFY_ACTIVE_START_HOUR = 0
DEFAULT_LINE_NOTIFY_ACTIVE_END_HOUR = 0
DEFAULT_LINE_NOTIFY_TIMEZONE = "Asia/Taipei"


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
        subscriber_store: "SubscriberStore | None" = None,
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
        self.subscriber_store = subscriber_store
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
            subscriber_store=SubscriberStore.from_env(),
        )

    def notify_new_messages(self, messages: list[LineMessageLike]) -> bool:
        if not messages:
            return False
        if not self.enabled:
            LOGGER.info("LINE notification disabled")
            return False
        if not self.channel_access_token:
            LOGGER.warning("LINE notification skipped: LINE_CHANNEL_ACCESS_TOKEN is missing")
            return False
        if self.notify_mode == "broadcast":
            text = self.build_broadcast_text(messages)
            return self._try_broadcast_text(text) or self._fallback_push_text(text)

        if not self.target_ids:
            LOGGER.warning("LINE notification skipped: LINE_TARGET_IDS is missing")
            return False

        texts = self.build_notification_texts(messages)
        sent_any = False
        for target_id in self.target_ids:
            for text in texts:
                try:
                    self._push_text(target_id, text)
                    sent_any = True
                except Exception as exc:
                    LOGGER.warning("LINE notification failed for target %s: %s", mask_identifier(target_id), exc)
        return sent_any

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

    def _fallback_push_text(self, text: str) -> bool:
        recipients = self._fallback_target_ids()
        if not recipients:
            LOGGER.warning("LINE broadcast failed and LINE_TARGET_IDS is missing; no fallback recipients")
            return False
        LOGGER.warning("LINE broadcast failed; fallback pushing summary to %s configured target(s)", len(recipients))
        sent_any = False
        for target_id in recipients:
            try:
                self._push_text(target_id, text)
                sent_any = True
            except Exception as exc:
                LOGGER.warning("LINE fallback push failed for target %s: %s", mask_identifier(target_id), exc)
        return sent_any

    def _fallback_target_ids(self) -> list[str]:
        subscriber_ids: list[str] = []
        if self.subscriber_store is not None:
            try:
                subscriber_ids = self.subscriber_store.active_user_ids()
                LOGGER.info("Loaded %s active LINE subscriber(s) for fallback", len(subscriber_ids))
            except Exception as exc:
                LOGGER.warning("LINE subscriber fallback list could not be loaded: %s", exc)
        return merge_unique_ids([*self.target_ids, *subscriber_ids])


class QueuedLineNotifier:
    def __init__(
        self,
        notifier: LineNotifier,
        queue_store: "LineNotificationQueueStore | None" = None,
        interval_seconds: int = DEFAULT_LINE_NOTIFY_INTERVAL_SECONDS,
        active_start_hour: int = DEFAULT_LINE_NOTIFY_ACTIVE_START_HOUR,
        active_end_hour: int = DEFAULT_LINE_NOTIFY_ACTIVE_END_HOUR,
        timezone_name: str = DEFAULT_LINE_NOTIFY_TIMEZONE,
    ) -> None:
        self.notifier = notifier
        self.queue_store = queue_store
        self.interval_seconds = max(interval_seconds, 0)
        self.active_start_hour = clamp_hour(active_start_hour, DEFAULT_LINE_NOTIFY_ACTIVE_START_HOUR)
        self.active_end_hour = clamp_hour(active_end_hour, DEFAULT_LINE_NOTIFY_ACTIVE_END_HOUR)
        self.timezone_name = timezone_name.strip() or DEFAULT_LINE_NOTIFY_TIMEZONE

    @classmethod
    def from_env(cls) -> "QueuedLineNotifier":
        return cls(
            notifier=LineNotifier.from_env(),
            queue_store=LineNotificationQueueStore.from_env(),
            interval_seconds=parse_int(os.getenv("LINE_NOTIFY_INTERVAL_SECONDS"), DEFAULT_LINE_NOTIFY_INTERVAL_SECONDS),
            active_start_hour=parse_int(os.getenv("LINE_NOTIFY_ACTIVE_START_HOUR"), DEFAULT_LINE_NOTIFY_ACTIVE_START_HOUR),
            active_end_hour=parse_int(os.getenv("LINE_NOTIFY_ACTIVE_END_HOUR"), DEFAULT_LINE_NOTIFY_ACTIVE_END_HOUR),
            timezone_name=os.getenv("LINE_NOTIFY_TIMEZONE", os.getenv("TZ", DEFAULT_LINE_NOTIFY_TIMEZONE)),
        )

    def notify_new_messages(self, messages: list[LineMessageLike]) -> bool:
        if self.queue_store is None:
            return self.notifier.notify_new_messages(messages)
        queued_count = self.queue_store.enqueue(messages)
        LOGGER.info("Queued %s LINE notification message(s)", queued_count)
        return self.flush_due()

    def flush_due(self, force: bool = False) -> bool:
        if self.queue_store is None:
            return False
        pending = self.queue_store.pending_messages()
        if not pending:
            return False
        if not force and not is_active_notification_hour(self.active_start_hour, self.active_end_hour, self.timezone_name):
            LOGGER.info(
                "LINE notification queue has %s pending message(s), outside active hours %02d:00-%02d:00 %s",
                len(pending),
                self.active_start_hour,
                self.active_end_hour,
                self.timezone_name,
            )
            return False
        if not force and not self.queue_store.is_due(self.interval_seconds):
            LOGGER.info("LINE notification queue has %s pending message(s), waiting for interval", len(pending))
            return False

        attempted_at = now_utc_iso()
        self.queue_store.set_state("last_attempt_at", attempted_at)
        sent = self.notifier.notify_new_messages(pending)
        if not sent:
            LOGGER.warning("LINE queued notification send failed; pending messages will be retried later")
            return False

        self.queue_store.mark_notified([message.data_key for message in pending], attempted_at)
        self.queue_store.set_state("last_sent_at", attempted_at)
        LOGGER.info("LINE queued notification sent for %s message(s)", len(pending))
        return True


class SubscriberStore:
    def __init__(
        self,
        sheet_id: str,
        credentials_path: str | None = None,
        worksheet_title: str = SUBSCRIBER_WORKSHEET_TITLE,
    ) -> None:
        self.sheet_id = sheet_id.strip()
        self.credentials_path = credentials_path
        self.worksheet_title = worksheet_title

    @classmethod
    def from_env(cls) -> "SubscriberStore | None":
        sheet_id = os.getenv("LINE_SUBSCRIBERS_SHEET_ID", "").strip()
        if not sheet_id:
            return None
        return cls(sheet_id, os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or default_credentials_path())

    def active_user_ids(self) -> list[str]:
        if gspread is None:
            raise RuntimeError("gspread is not installed. Run: pip install -r requirements.txt")
        if not self.credentials_path:
            raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS is missing")
        if not self.sheet_id:
            return []

        spreadsheet = gspread.service_account(filename=self.credentials_path).open_by_key(self.sheet_id)
        try:
            worksheet = spreadsheet.worksheet(self.worksheet_title)
        except WorksheetNotFound:
            LOGGER.warning("LINE subscriber worksheet %s not found", self.worksheet_title)
            return []
        rows = worksheet.get_all_values()
        if not rows:
            return []
        headers = [normalize_header(value) for value in rows[0]]
        try:
            user_id_index = headers.index(SUBSCRIBER_USER_ID)
        except ValueError:
            LOGGER.warning("LINE subscriber worksheet is missing user_id header")
            return []
        status_index = headers.index(SUBSCRIBER_STATUS) if SUBSCRIBER_STATUS in headers else None

        user_ids: list[str] = []
        for row in rows[1:]:
            user_id = row[user_id_index].strip() if user_id_index < len(row) else ""
            if not user_id:
                continue
            status = row[status_index].strip().lower() if status_index is not None and status_index < len(row) else "active"
            if status in {"", "active"}:
                user_ids.append(user_id)
        return merge_unique_ids(user_ids)


class LineNotificationQueueStore:
    queue_headers = ["data_key", "date", "time", "company_id", "company_name", "subject", "queued_at", "notified_at", "status"]
    state_headers = ["key", "value"]

    def __init__(
        self,
        sheet_id: str,
        credentials_path: str | None = None,
        queue_worksheet_title: str = NOTIFICATION_QUEUE_WORKSHEET_TITLE,
        state_worksheet_title: str = NOTIFICATION_STATE_WORKSHEET_TITLE,
    ) -> None:
        self.sheet_id = sheet_id.strip()
        self.credentials_path = credentials_path
        self.queue_worksheet_title = queue_worksheet_title
        self.state_worksheet_title = state_worksheet_title

    @classmethod
    def from_env(cls) -> "LineNotificationQueueStore | None":
        if not parse_bool(os.getenv("LINE_NOTIFY_QUEUE_ENABLED", "true")):
            return None
        sheet_id = os.getenv("LINE_NOTIFY_QUEUE_SHEET_ID", "").strip() or os.getenv("LINE_SUBSCRIBERS_SHEET_ID", "").strip()
        if not sheet_id:
            return None
        return cls(sheet_id, os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or default_credentials_path())

    def enqueue(self, messages: list[LineMessageLike]) -> int:
        if not messages:
            return 0
        worksheet = self._worksheet(self.queue_worksheet_title, self.queue_headers)
        rows = worksheet.get_all_values()
        existing_keys = {row[0].strip() for row in rows[1:] if row and row[0].strip()}
        queued_at = now_utc_iso()
        new_rows = []
        for message in messages:
            data_key = getattr(message, "data_key", "")
            if not data_key or data_key in existing_keys:
                continue
            new_rows.append(
                [
                    data_key,
                    message.date,
                    message.time,
                    message.company_id,
                    message.company_name,
                    message.subject,
                    queued_at,
                    "",
                    "pending",
                ]
            )
            existing_keys.add(data_key)
        if new_rows:
            worksheet.append_rows(new_rows, value_input_option="USER_ENTERED")
        return len(new_rows)

    def pending_messages(self) -> list[LineMessageLike]:
        worksheet = self._worksheet(self.queue_worksheet_title, self.queue_headers)
        rows = worksheet.get_all_values()
        if len(rows) <= 1:
            return []
        headers = [normalize_header(value) for value in rows[0]]
        indexes = {header: index for index, header in enumerate(headers)}
        messages = []
        for row in rows[1:]:
            status = row_value(row, indexes.get("status")).lower()
            if status and status != "pending":
                continue
            data_key = row_value(row, indexes.get("data_key"))
            if not data_key:
                continue
            messages.append(
                QueuedLineMessage(
                    data_key=data_key,
                    date=row_value(row, indexes.get("date")),
                    time=row_value(row, indexes.get("time")),
                    company_id=row_value(row, indexes.get("company_id")),
                    company_name=row_value(row, indexes.get("company_name")),
                    subject=row_value(row, indexes.get("subject")),
                )
            )
        return messages

    def mark_notified(self, data_keys: Iterable[str], notified_at: str) -> None:
        key_set = set(data_keys)
        if not key_set:
            return
        worksheet = self._worksheet(self.queue_worksheet_title, self.queue_headers)
        rows = worksheet.get_all_values()
        if len(rows) <= 1:
            return
        headers = [normalize_header(value) for value in rows[0]]
        data_key_index = headers.index("data_key")
        notified_at_index = headers.index("notified_at")
        status_index = headers.index("status")
        for row_index, row in enumerate(rows[1:], start=2):
            if row_value(row, data_key_index) not in key_set:
                continue
            worksheet.update_cell(row_index, notified_at_index + 1, notified_at)
            worksheet.update_cell(row_index, status_index + 1, "sent")

    def is_due(self, interval_seconds: int) -> bool:
        if interval_seconds <= 0:
            return True
        last_attempt_at = self.get_state("last_attempt_at")
        if not last_attempt_at:
            return True
        last_attempt = parse_iso_datetime(last_attempt_at)
        if last_attempt is None:
            return True
        return (datetime.now(timezone.utc) - last_attempt).total_seconds() >= interval_seconds

    def get_state(self, key: str) -> str:
        worksheet = self._worksheet(self.state_worksheet_title, self.state_headers)
        rows = worksheet.get_all_values()
        for row in rows[1:]:
            if row_value(row, 0) == key:
                return row_value(row, 1)
        return ""

    def set_state(self, key: str, value: str) -> None:
        worksheet = self._worksheet(self.state_worksheet_title, self.state_headers)
        rows = worksheet.get_all_values()
        for row_index, row in enumerate(rows[1:], start=2):
            if row_value(row, 0) == key:
                worksheet.update_cell(row_index, 2, value)
                return
        worksheet.append_row([key, value], value_input_option="USER_ENTERED")

    def _worksheet(self, title: str, headers: list[str]):
        if gspread is None:
            raise RuntimeError("gspread is not installed. Run: pip install -r requirements.txt")
        if not self.credentials_path:
            raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS is missing")
        spreadsheet = gspread.service_account(filename=self.credentials_path).open_by_key(self.sheet_id)
        try:
            worksheet = spreadsheet.worksheet(title)
        except WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=title, rows=1000, cols=max(len(headers), 10))
        ensure_headers(worksheet, headers)
        return worksheet


class QueuedLineMessage:
    def __init__(self, data_key: str, date: str, time: str, company_id: str, company_name: str, subject: str) -> None:
        self.data_key = data_key
        self.date = date
        self.time = time
        self.company_id = company_id
        self.company_name = company_name
        self.subject = subject


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


def clamp_hour(value: int, default: int) -> int:
    return value if 0 <= value <= 23 else default


def is_active_notification_hour(start_hour: int, end_hour: int, timezone_name: str) -> bool:
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        LOGGER.warning("Invalid LINE notify timezone %r, using %s", timezone_name, DEFAULT_LINE_NOTIFY_TIMEZONE)
        tz = ZoneInfo(DEFAULT_LINE_NOTIFY_TIMEZONE)
    current_hour = datetime.now(tz).hour
    start = clamp_hour(start_hour, DEFAULT_LINE_NOTIFY_ACTIVE_START_HOUR)
    end = clamp_hour(end_hour, DEFAULT_LINE_NOTIFY_ACTIVE_END_HOUR)
    if start == end:
        return True
    if start < end:
        return start <= current_hour < end
    return current_hour >= start or current_hour < end


def default_credentials_path() -> str | None:
    candidate = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    return candidate.strip() if candidate and candidate.strip() else None


def normalize_header(value: str) -> str:
    return str(value or "").strip().lower()


def merge_unique_ids(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def row_value(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return str(row[index] or "").strip()


def ensure_headers(worksheet, headers: list[str]) -> None:
    first_row = worksheet.row_values(1)
    normalized = [normalize_header(value) for value in first_row]
    if normalized[: len(headers)] == headers:
        return
    worksheet.update("A1", [headers])
    try:
        worksheet.freeze(rows=1)
    except Exception:
        LOGGER.debug("Could not freeze header row for worksheet")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
