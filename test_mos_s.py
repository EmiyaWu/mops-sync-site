from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mos_s import (
    Config,
    Deduper,
    F_COMPANY_NAME,
    F_DATE,
    F_DETAIL,
    F_FETCHED_AT,
    F_KEY,
    F_SUBJECT,
    F_TIME,
    GoogleSheetWriter,
    MOPSMessage,
    MessageNormalizer,
    SiteExporter,
    SyncService,
    parse_worksheet_date,
)


COMPANY_NAME = "\u53f0\u7a4d\u96fb"
SUBJECT = "\u91cd\u5927\u8a0a\u606f"
DETAIL = "\u9019\u662f\u4e0d\u80fd\u516c\u958b\u7684\u8a73\u7d30\u5167\u5bb9"


class FakeMOPSClient:
    def __init__(self, messages: list[MOPSMessage]) -> None:
        self.messages = messages

    def fetch_messages(self, fetched_at: str) -> list[MOPSMessage]:
        return self.messages


class FakeGoogleSheetWriter:
    def __init__(self, existing_keys: set[str] | None = None) -> None:
        self._existing_keys = existing_keys or set()
        self.appended: list[MOPSMessage] = []
        self.titles: list[str] = []
        self.organized_count = 0

    def existing_keys(self, worksheet_date: datetime) -> set[str]:
        self.titles.append(worksheet_date.strftime("%Y/%m/%d"))
        return self._existing_keys

    def append_messages(self, worksheet_date: datetime, messages: list[MOPSMessage]) -> int:
        self.appended.extend(messages)
        self._existing_keys.update(message.data_key for message in messages)
        return len(messages)

    def organize_daily_worksheets(self) -> None:
        self.organized_count += 1


class FakeWorksheet:
    def __init__(self) -> None:
        self.inserted_rows: list[list[str]] = []
        self.insert_row_number: int | None = None

    def row_values(self, row: int) -> list[str]:
        return []

    def update(self, range_name: str, values: list[list[str]]) -> None:
        return None

    def freeze(self, rows: int) -> None:
        return None

    def insert_rows(self, rows: list[list[str]], row: int, value_input_option: str) -> None:
        self.inserted_rows = rows
        self.insert_row_number = row


def make_message(data_key: str, item_time: str = "09:01:02") -> MOPSMessage:
    return MOPSMessage(
        date="2026/05/23",
        time=item_time,
        company_id="2330",
        company_name=COMPANY_NAME,
        subject=SUBJECT,
        detail=DETAIL,
        data_key=data_key,
        fetched_at="2026-05-23T09:01:05+08:00",
    )


class MessageNormalizerTest(unittest.TestCase):
    def test_normalize_builds_stable_data_key(self) -> None:
        item = {
            "date": "2026/05/23",
            "time": "09:01:02",
            "companyId": "2330",
            "companyAbbreviation": COMPANY_NAME,
            "subject": SUBJECT,
        }
        params = {"serialNumber": "1", "date": "20260523"}

        message = MessageNormalizer.normalize(item, "\u8a73\u7d30\u5167\u5bb9", params, "2026-05-23T09:01:05+08:00")

        self.assertEqual(message.company_id, "2330")
        self.assertEqual(message.detail, "\u8a73\u7d30\u5167\u5bb9")
        self.assertEqual(message.data_key, f"2330|2026/05/23|09:01:02|1|{SUBJECT}")

    def test_normalize_handles_missing_fields(self) -> None:
        message = MessageNormalizer.normalize({}, "", {}, "2026-05-23T09:01:05+08:00")

        self.assertEqual(message.to_sheet_row(), ["", "", "", "", "", "", "||||", "2026-05-23T09:01:05+08:00"])


class DeduperTest(unittest.TestCase):
    def test_filters_duplicate_keys_and_persists_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "seen_messages.json"
            deduper = Deduper(state_path)
            first = make_message("key-1")
            duplicate = make_message("key-1")
            second = make_message("key-2")

            new_messages = deduper.filter_new([first, duplicate, second])
            deduper.mark_seen(new_messages)
            reloaded = Deduper(state_path)

            self.assertEqual([message.data_key for message in new_messages], ["key-1", "key-2"])
            self.assertEqual(reloaded.filter_new([first, second]), [])


class SiteExporterTest(unittest.TestCase):
    def test_exports_public_site_with_detail_but_without_private_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "public"
            SiteExporter(output_dir).export([make_message("private-key-1")], datetime(2026, 5, 23, 9, 5, tzinfo=ZoneInfo("Asia/Taipei")))

            data = json.loads((output_dir / "data" / "latest.json").read_text(encoding="utf-8"))
            page = (output_dir / "index.html").read_text(encoding="utf-8")

            self.assertTrue((output_dir / "assets" / "site.css").exists())
            self.assertTrue((output_dir / "assets" / "site.js").exists())
            self.assertEqual(data["fields"], [F_DATE, F_TIME, "\u516c\u53f8\u4ee3\u865f", F_COMPANY_NAME, F_SUBJECT, F_DETAIL])
            self.assertEqual(data["messages"][0][F_COMPANY_NAME], COMPANY_NAME)
            self.assertEqual(data["messages"][0][F_DETAIL], DETAIL)
            serialized = json.dumps(data, ensure_ascii=False)
            self.assertNotIn(F_KEY, serialized)
            self.assertNotIn(F_FETCHED_AT, serialized)
            self.assertNotIn("private-key-1", serialized)
            self.assertIn(DETAIL, page)

    def test_exports_newest_time_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "public"
            older = make_message("older", "9:01")
            newer = make_message("newer", "15:02:03")

            SiteExporter(output_dir).export([older, newer], datetime(2026, 5, 23, 15, 5, tzinfo=ZoneInfo("Asia/Taipei")))

            data = json.loads((output_dir / "data" / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual([message[F_TIME] for message in data["messages"]], ["15:02:03", "9:01"])


class GoogleSheetWriterTest(unittest.TestCase):
    def test_inserts_new_rows_below_header_newest_first(self) -> None:
        worksheet = FakeWorksheet()
        writer = GoogleSheetWriter.__new__(GoogleSheetWriter)
        writer.max_visible_days = 7
        writer._get_or_create_daily_worksheet = lambda worksheet_date: worksheet
        writer.organize_daily_worksheets = lambda: None

        count = writer.append_messages(datetime(2026, 5, 23), [make_message("older", "9:01"), make_message("newer", "15:02:03")])

        self.assertEqual(count, 2)
        self.assertEqual(worksheet.insert_row_number, 2)
        self.assertEqual([row[1] for row in worksheet.inserted_rows], ["15:02:03", "9:01"])


class WorksheetDateTest(unittest.TestCase):
    def test_parse_worksheet_date_accepts_slash_and_dash(self) -> None:
        self.assertEqual(parse_worksheet_date("2026/05/23"), datetime(2026, 5, 23))
        self.assertEqual(parse_worksheet_date("2026-05-23"), datetime(2026, 5, 23))

    def test_parse_worksheet_date_ignores_non_daily_sheets(self) -> None:
        self.assertIsNone(parse_worksheet_date("\u8a2d\u5b9a"))
        self.assertIsNone(parse_worksheet_date("2026/99/99"))


class SyncServiceTest(unittest.TestCase):
    def make_config(self, tmpdir: str) -> Config:
        return Config(
            sheet_id="sheet-id",
            credentials_path=None,
            poll_interval_seconds=180,
            timezone="Asia/Taipei",
            state_path=Path(tmpdir) / "seen_messages.json",
            max_visible_days=7,
        )

    def test_sync_once_appends_only_new_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = FakeGoogleSheetWriter(existing_keys={"key-1"})
            service = SyncService(self.make_config(tmpdir), client=FakeMOPSClient([make_message("key-1"), make_message("key-2")]), writer=writer)

            appended_count = service.sync_once()

            self.assertEqual(appended_count, 1)
            self.assertEqual([message.data_key for message in writer.appended], ["key-2"])

    def test_sync_once_does_not_write_when_no_new_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = FakeGoogleSheetWriter(existing_keys={"key-1"})
            service = SyncService(self.make_config(tmpdir), client=FakeMOPSClient([make_message("key-1")]), writer=writer)

            appended_count = service.sync_once()

            self.assertEqual(appended_count, 0)
            self.assertEqual(writer.appended, [])
            self.assertEqual(writer.organized_count, 1)

    def test_sync_once_exports_site_even_when_no_new_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = FakeGoogleSheetWriter(existing_keys={"key-1"})
            service = SyncService(self.make_config(tmpdir), client=FakeMOPSClient([make_message("key-1")]), writer=writer)

            service.sync_once(Path(tmpdir) / "public")

            self.assertTrue((Path(tmpdir) / "public" / "data" / "latest.json").exists())

    def test_sync_uses_taipei_daily_worksheet_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = FakeGoogleSheetWriter()
            service = SyncService(self.make_config(tmpdir), client=FakeMOPSClient([make_message("key-1")]), writer=writer)

            service.sync_once()

            today = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y/%m/%d")
            self.assertEqual(writer.titles[0], today)


if __name__ == "__main__":
    unittest.main()
