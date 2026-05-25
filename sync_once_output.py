from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import mos_s
from line_notify import LineNotifier
from mos_s import (
    Config,
    Deduper,
    GoogleSheetWriter,
    MOPSClient,
    MOPSMessage,
    MessageNormalizer,
    SpreadsheetNotFound,
    configure_console_encoding,
    prepare_credentials_from_json_secret,
)


LOGGER = logging.getLogger("mops_sync")


class NoopNotifier:
    def notify_new_messages(self, messages: list[MOPSMessage]) -> None:
        return


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
    notifier = LineNotifier.from_env()
    if hasattr(mos_s, "LineNotifier"):
        mos_s.LineNotifier.from_env = classmethod(lambda cls: NoopNotifier())

    def append_messages(self: GoogleSheetWriter, worksheet_date, messages: list[MOPSMessage]) -> int:
        if not messages:
            self.organize_daily_worksheets()
            return 0
        worksheet = self._get_or_create_daily_worksheet(worksheet_date)
        self._ensure_headers(worksheet)
        sorted_messages = sort_messages_newest_first(messages)
        rows = [message.to_sheet_row() for message in sorted_messages]
        self._with_retry(lambda: worksheet.insert_rows(rows, row=2, value_input_option="USER_ENTERED"))
        self.organize_daily_worksheets()
        if notifier is not None:
            try:
                notifier.notify_new_messages(sorted_messages)
            except Exception as exc:
                LOGGER.warning("LINE notification failed. Sheet sync remains complete: %s", exc)
        return len(messages)

    GoogleSheetWriter.append_messages = append_messages
    LOGGER.info("Installed newest-first Sheet writer patch with line_notify notifier")


def sync_once_optimized(config: Config) -> int:
    client = MOPSClient()
    writer = GoogleSheetWriter(config.sheet_id, config.credentials_path, config.max_visible_days)
    deduper = Deduper(config.state_path)
    now = datetime.now(ZoneInfo(config.timezone))
    fetched_at = now.isoformat(timespec="seconds")

    list_items = client.fetch_list()
    existing_keys = writer.existing_keys(now)
    candidates: list[tuple[MOPSMessage, dict[str, Any]]] = []
    for item in list_items:
        if not isinstance(item, dict):
            LOGGER.warning("Skip unexpected list item: %r", item)
            continue
        params = client._extract_detail_params(item)
        candidate = MessageNormalizer.normalize(item, "", params, fetched_at)
        candidates.append((candidate, params))

    candidate_messages = [candidate for candidate, _ in candidates]
    new_candidates = deduper.filter_new(candidate_messages, existing_keys)
    new_keys = {message.data_key for message in new_candidates}
    if not new_candidates:
        writer.organize_daily_worksheets()
        LOGGER.info(
            "No new rows. fetched_list=%s new_candidates=0 detail_fetched=0 appended=0 skipped=%s",
            len(candidate_messages),
            len(candidate_messages),
        )
        return 0

    detail_fetched = 0
    new_messages: list[MOPSMessage] = []
    for candidate, params in candidates:
        if candidate.data_key not in new_keys:
            continue
        detail = client.fetch_detail(params) if params else "No detail"
        detail_fetched += 1 if params else 0
        if params and client.detail_delay_seconds > 0:
            time.sleep(client.detail_delay_seconds)
        new_messages.append(replace(candidate, detail=str(detail or "").strip()))

    appended_count = writer.append_messages(now, new_messages)
    deduper.mark_seen(new_messages)
    LOGGER.info(
        "Sync complete. fetched_list=%s new_candidates=%s detail_fetched=%s appended=%s skipped=%s",
        len(candidate_messages),
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
