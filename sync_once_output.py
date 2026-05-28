from __future__ import annotations

import logging
import json
import os
import re
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from mos_s import (
    APIError,
    Config,
    Deduper,
    GoogleSheetWriter,
    MOPSClient,
    MOPSMessage,
    MessageNormalizer,
    SHEET_HEADERS,
    SpreadsheetNotFound,
    WorksheetNotFound,
    configure_console_encoding,
    is_excluded_subject,
    parse_worksheet_date,
    prepare_credentials_from_json_secret,
)


LOGGER = logging.getLogger("mops_sync")
DEFAULT_CLOUD_MAX_RETRIES = 5


class CloudMOPSClient(MOPSClient):
    def __init__(self) -> None:
        super().__init__(max_retries=parse_int_env("MOPS_MAX_RETRIES", DEFAULT_CLOUD_MAX_RETRIES))

    def fetch_list(self) -> list[Any]:
        response = self._post_json(self.list_url, {"count": "0", "marketKind": ""})
        result = response.get("result") or {}
        data = result.get("data", response.get("data", []))
        if not isinstance(data, list):
            raise ValueError(f"MOPS list response is not a list: {type(data).__name__}")
        return data

    def fetch_detail(self, params: dict[str, Any]) -> str:
        payload = {
            "companyId": params.get("companyId"),
            "serialNumber": params.get("serialNumber"),
            "date": params.get("date"),
        }
        response = self._post_json(self.detail_url, payload)
        result = response.get("result") or {}
        data = result.get("data") or []
        if not data:
            return "No detail returned"
        last_item = data[-1]
        if isinstance(last_item, list) and len(last_item) > 9:
            return str(last_item[9] or "")
        if isinstance(last_item, dict):
            return str(last_item.get("detail") or last_item.get("content") or "")
        return str(last_item)


def parse_int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        LOGGER.warning("Invalid %s value, using %s", name, default)
        return default


def retry_operation(operation: Any, operation_name: str, max_retries: int | None = None) -> Any:
    retries = max_retries or parse_int_env("GOOGLE_API_MAX_RETRIES", DEFAULT_CLOUD_MAX_RETRIES)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return operation()
        except APIError as exc:
            last_error = exc
            if attempt == retries:
                break
            sleep_seconds = min(2 ** attempt, 30)
            LOGGER.warning("%s failed, retry %s/%s after %s seconds: %s", operation_name, attempt, retries - 1, sleep_seconds, exc)
            time.sleep(sleep_seconds)
    raise RuntimeError(f"{operation_name} failed after retries") from last_error


def create_sheet_writer(config: Config) -> GoogleSheetWriter:
    return retry_operation(
        lambda: GoogleSheetWriter(config.sheet_id, config.credentials_path, config.max_visible_days),
        "Google Sheet open spreadsheet",
    )


def write_github_output(name: str, value: Any) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as output_file:
        output_file.write(f"{name}={value}\n")


def sort_messages_newest_first(messages: Iterable[MOPSMessage]) -> list[MOPSMessage]:
    return sorted(messages, key=message_sort_key, reverse=True)


def message_sort_key(message: MOPSMessage) -> tuple[str, str, str, str]:
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


def install_sheet_writer_patch() -> None:
    def get_or_create_daily_worksheet(self: GoogleSheetWriter, worksheet_date):
        preferred_title = worksheet_date.strftime("%Y/%m/%d")
        fallback_title = worksheet_date.strftime("%Y-%m-%d")
        for title in (preferred_title, fallback_title):
            try:
                return retry_operation(lambda title=title: self.spreadsheet.worksheet(title), f"Google Sheet read worksheet {title}")
            except WorksheetNotFound:
                continue
        try:
            return retry_operation(
                lambda: self.spreadsheet.add_worksheet(title=preferred_title, rows=1000, cols=len(SHEET_HEADERS)),
                f"Google Sheet create worksheet {preferred_title}",
            )
        except (APIError, RuntimeError) as exc:
            LOGGER.warning("Could not create worksheet %s, fallback to %s: %s", preferred_title, fallback_title, exc)
            return retry_operation(
                lambda: self.spreadsheet.add_worksheet(title=fallback_title, rows=1000, cols=len(SHEET_HEADERS)),
                f"Google Sheet create worksheet {fallback_title}",
            )

    def ensure_headers(self: GoogleSheetWriter, worksheet) -> None:
        current_headers = retry_operation(lambda: worksheet.row_values(1), "Google Sheet read header")
        if current_headers == SHEET_HEADERS:
            return
        if not current_headers:
            retry_operation(lambda: worksheet.update("A1:H1", [SHEET_HEADERS]), "Google Sheet update header")
            retry_operation(lambda: worksheet.freeze(rows=1), "Google Sheet freeze header")
            return
        raise RuntimeError(f"Unexpected worksheet headers: {current_headers}")

    def daily_rows(self: GoogleSheetWriter, worksheet_date) -> list[list[str]]:
        worksheet = self._get_or_create_daily_worksheet(worksheet_date)
        return retry_operation(lambda: worksheet.get_all_values(), "Google Sheet read daily worksheet")

    def organize_daily_worksheets(self: GoogleSheetWriter) -> None:
        metadata = retry_operation(lambda: self.spreadsheet.fetch_sheet_metadata(), "Google Sheet fetch metadata")
        date_sheets = []
        for sheet in metadata.get("sheets", []):
            properties = sheet.get("properties", {})
            parsed_date = parse_worksheet_date(properties.get("title", ""))
            if parsed_date is not None:
                date_sheets.append((parsed_date, properties))
        if not date_sheets:
            return
        date_sheets.sort(key=lambda item: item[0], reverse=True)
        requests_payload = []
        for index, (_, properties) in enumerate(date_sheets):
            should_hide = index >= self.max_visible_days
            desired = {"sheetId": properties["sheetId"], "index": index, "hidden": should_hide}
            if properties.get("index") != index or properties.get("hidden", False) != should_hide:
                requests_payload.append({"updateSheetProperties": {"properties": desired, "fields": "index,hidden"}})
        if requests_payload:
            retry_operation(lambda: self.spreadsheet.batch_update({"requests": requests_payload}), "Google Sheet organize worksheets")
            LOGGER.info("Organized daily worksheets, newest first, visible days: %s", self.max_visible_days)

    def append_messages(self: GoogleSheetWriter, worksheet_date, messages: list[MOPSMessage]) -> int:
        if not messages:
            self.organize_daily_worksheets()
            return 0
        worksheet = self._get_or_create_daily_worksheet(worksheet_date)
        self._ensure_headers(worksheet)
        sorted_messages = sort_messages_newest_first(messages)
        rows = [message.to_sheet_row() for message in sorted_messages]
        retry_operation(lambda: worksheet.insert_rows(rows, row=2, value_input_option="USER_ENTERED"), "Google Sheet insert rows")
        self.organize_daily_worksheets()
        return len(messages)

    GoogleSheetWriter._get_or_create_daily_worksheet = get_or_create_daily_worksheet
    GoogleSheetWriter._ensure_headers = ensure_headers
    GoogleSheetWriter.daily_rows = daily_rows
    GoogleSheetWriter.organize_daily_worksheets = organize_daily_worksheets
    GoogleSheetWriter.append_messages = append_messages
    LOGGER.info("Installed newest-first Sheet writer patch with cloud API retries")


def new_messages_output_path(config: Config) -> Path:
    return Path(os.getenv("MOPS_NEW_MESSAGES_OUTPUT", str(config.state_path.parent / "new_messages.json")))


def write_new_messages_payload(messages: list[MOPSMessage], generated_at: datetime, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "count": len(messages),
        "messages": [
            {
                "date": message.date,
                "time": message.time,
                "company_id": message.company_id,
                "company_name": message.company_name,
                "subject": message.subject,
            }
            for message in sort_messages_newest_first(messages)
        ],
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def sync_once_optimized(config: Config) -> int:
    client = CloudMOPSClient()
    writer = create_sheet_writer(config)
    deduper = Deduper(config.state_path)
    now = datetime.now(ZoneInfo(config.timezone))
    fetched_at = now.isoformat(timespec="seconds")
    output_path = new_messages_output_path(config)

    list_items = client.fetch_list()
    existing_keys = writer.existing_keys(now)
    candidates: list[tuple[MOPSMessage, dict[str, Any]]] = []
    excluded_count = 0
    for item in list_items:
        if not isinstance(item, dict):
            LOGGER.warning("Skip unexpected list item: %r", item)
            continue
        subject = str(item.get("subject") or "").strip()
        if is_excluded_subject(subject):
            excluded_count += 1
            LOGGER.debug("Skip excluded MOPS subject: %s", subject)
            continue
        params = client._extract_detail_params(item)
        candidate = MessageNormalizer.normalize(item, "", params, fetched_at)
        candidates.append((candidate, params))

    candidate_messages = [candidate for candidate, _ in candidates]
    new_candidates = deduper.filter_new(candidate_messages, existing_keys)
    new_keys = {message.data_key for message in new_candidates}
    if not new_candidates:
        writer.organize_daily_worksheets()
        write_new_messages_payload([], now, output_path)
        LOGGER.info(
            "No new rows. fetched_list=%s excluded=%s new_candidates=0 detail_fetched=0 appended=0 skipped=%s",
            len(list_items),
            excluded_count,
            len(candidate_messages),
        )
        return 0

    detail_fetched = 0
    new_messages: list[MOPSMessage] = []
    for candidate, params in candidates:
        if candidate.data_key not in new_keys:
            continue
        try:
            detail = client.fetch_detail(params) if params else "No detail"
        except AttributeError as exc:
            LOGGER.warning("MOPS detail response was malformed, continuing without detail for %s: %s", candidate.data_key, exc)
            detail = "No detail returned"
        detail_fetched += 1 if params else 0
        if params and client.detail_delay_seconds > 0:
            time.sleep(client.detail_delay_seconds)
        new_messages.append(replace(candidate, detail=str(detail or "").strip()))

    appended_count = writer.append_messages(now, new_messages)
    deduper.mark_seen(new_messages)
    write_new_messages_payload(new_messages, now, output_path)
    LOGGER.info(
        "Sync complete. fetched_list=%s excluded=%s new_candidates=%s detail_fetched=%s appended=%s skipped=%s",
        len(list_items),
        excluded_count,
        len(new_candidates),
        detail_fetched,
        appended_count,
        len(candidate_messages) - appended_count,
    )
    return appended_count


def main() -> int:
    configure_console_encoding()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    prepare_credentials_from_json_secret()
    install_sheet_writer_patch()
    try:
        new_rows = sync_once_optimized(Config.from_env())
    except (SpreadsheetNotFound, FileNotFoundError, RuntimeError, ValueError) as exc:
        LOGGER.error("Execution failed: %s", exc)
        return 1

    has_new_rows = str(new_rows > 0).lower()
    write_github_output("new_rows", new_rows)
    write_github_output("has_new_rows", has_new_rows)
    LOGGER.info("GitHub Actions output: new_rows=%s has_new_rows=%s", new_rows, has_new_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
