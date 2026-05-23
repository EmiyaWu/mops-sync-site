from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Iterable

from mos_s import Config, GoogleSheetWriter, MOPSMessage, SpreadsheetNotFound, SyncService, configure_console_encoding, prepare_credentials_from_json_secret


LOGGER = logging.getLogger("mops_sync")


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


def install_newest_first_sheet_writer() -> None:
    def append_messages(self: GoogleSheetWriter, worksheet_date, messages: list[MOPSMessage]) -> int:
        if not messages:
            self.organize_daily_worksheets()
            return 0
        worksheet = self._get_or_create_daily_worksheet(worksheet_date)
        self._ensure_headers(worksheet)
        rows = [message.to_sheet_row() for message in sort_messages_newest_first(messages)]
        self._with_retry(lambda: worksheet.insert_rows(rows, row=2, value_input_option="USER_ENTERED"))
        self.organize_daily_worksheets()
        return len(messages)

    GoogleSheetWriter.append_messages = append_messages


def main() -> int:
    configure_console_encoding()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    prepare_credentials_from_json_secret()
    install_newest_first_sheet_writer()
    try:
        new_rows = SyncService(Config.from_env()).sync_once()
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
