from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sync_once_output
from mos_s import Config, MOPSMessage


def make_item(company_id: str, date: str, item_time: str, serial: str, subject: str) -> dict:
    return {
        "date": date,
        "time": item_time,
        "companyId": company_id,
        "companyAbbreviation": f"{company_id}\u516c\u53f8",
        "subject": subject,
        "url": {"parameters": {"companyId": company_id, "serialNumber": serial, "date": date.replace("/", "")}},
    }


def data_key(item: dict) -> str:
    params = item["url"]["parameters"]
    return "|".join([item["companyId"], item["date"], item["time"], params["serialNumber"], item["subject"]])


class FakeOptimizedMOPSClient:
    list_items: list[dict] = []
    fail_detail = False
    malformed_detail = False
    detail_calls: list[dict] = []

    def __init__(self) -> None:
        self.detail_delay_seconds = 0

    def fetch_list(self) -> list[dict]:
        return self.list_items

    def fetch_detail(self, params: dict) -> str:
        self.detail_calls.append(params)
        if self.fail_detail:
            raise RuntimeError("detail failed")
        if self.malformed_detail:
            raise AttributeError("'NoneType' object has no attribute 'get'")
        return f"detail-{params['serialNumber']}"

    @staticmethod
    def _extract_detail_params(item: dict) -> dict:
        return item.get("url", {}).get("parameters", {})


class FakeOptimizedSheetWriter:
    existing: set[str] = set()
    appended: list[MOPSMessage] = []
    organized_count = 0

    def __init__(self, sheet_id: str, credentials_path: str | None, max_visible_days: int) -> None:
        self.sheet_id = sheet_id
        self.credentials_path = credentials_path
        self.max_visible_days = max_visible_days

    def existing_keys(self, worksheet_date) -> set[str]:
        return self.existing

    def append_messages(self, worksheet_date, messages: list[MOPSMessage]) -> int:
        self.appended.extend(messages)
        return len(messages)

    def organize_daily_worksheets(self) -> None:
        type(self).organized_count += 1


class OptimizedCloudSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original_client = sync_once_output.MOPSClient
        self.original_writer = sync_once_output.GoogleSheetWriter
        sync_once_output.MOPSClient = FakeOptimizedMOPSClient
        sync_once_output.GoogleSheetWriter = FakeOptimizedSheetWriter
        FakeOptimizedMOPSClient.list_items = []
        FakeOptimizedMOPSClient.fail_detail = False
        FakeOptimizedMOPSClient.malformed_detail = False
        FakeOptimizedMOPSClient.detail_calls = []
        FakeOptimizedSheetWriter.existing = set()
        FakeOptimizedSheetWriter.appended = []
        FakeOptimizedSheetWriter.organized_count = 0

    def tearDown(self) -> None:
        sync_once_output.MOPSClient = self.original_client
        sync_once_output.GoogleSheetWriter = self.original_writer

    def make_config(self, tmpdir: str) -> Config:
        return Config(
            sheet_id="sheet-id",
            credentials_path=None,
            poll_interval_seconds=180,
            timezone="Asia/Taipei",
            state_path=Path(tmpdir) / "seen_messages.json",
            max_visible_days=7,
        )

    def test_no_new_rows_does_not_fetch_detail(self) -> None:
        item = make_item("2330", "2026/05/25", "15:00", "1", "old")
        FakeOptimizedMOPSClient.list_items = [item]
        FakeOptimizedSheetWriter.existing = {data_key(item)}

        with tempfile.TemporaryDirectory() as tmpdir:
            new_rows = sync_once_output.sync_once_optimized(self.make_config(tmpdir))

        self.assertEqual(new_rows, 0)
        self.assertEqual(FakeOptimizedMOPSClient.detail_calls, [])
        self.assertEqual(FakeOptimizedSheetWriter.appended, [])
        self.assertEqual(FakeOptimizedSheetWriter.organized_count, 1)

    def test_fetches_detail_only_for_new_rows(self) -> None:
        old_item = make_item("2330", "2026/05/25", "15:00", "1", "old")
        new_items = [make_item("2330", "2026/05/25", f"15:0{index}", str(index), f"new-{index}") for index in range(2, 5)]
        FakeOptimizedMOPSClient.list_items = [old_item, *new_items]
        FakeOptimizedSheetWriter.existing = {data_key(old_item)}

        with tempfile.TemporaryDirectory() as tmpdir:
            new_rows = sync_once_output.sync_once_optimized(self.make_config(tmpdir))

        self.assertEqual(new_rows, 3)
        self.assertEqual(len(FakeOptimizedMOPSClient.detail_calls), 3)
        self.assertEqual(len(FakeOptimizedSheetWriter.appended), 3)
        self.assertEqual([message.detail for message in FakeOptimizedSheetWriter.appended], ["detail-2", "detail-3", "detail-4"])

    def test_excluded_subjects_are_skipped_before_detail_fetch(self) -> None:
        FakeOptimizedMOPSClient.list_items = [
            make_item("2330", "2026/05/25", "15:02", "2", "\u516c\u544a\u672c\u516c\u53f8\u8463\u4e8b\u6703\u6c7a\u8b70\u53ec\u958b\u80a1\u6771\u5e38\u6703"),
            make_item("2317", "2026/05/25", "15:03", "3", "\u516c\u544a\u672c\u516c\u53f8\u5be9\u8a08\u59d4\u54e1\u6703\u59d4\u54e1\u7570\u52d5"),
            make_item("2454", "2026/05/25", "15:04", "4", "\u91cd\u5927\u8a0a\u606f"),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            new_rows = sync_once_output.sync_once_optimized(self.make_config(tmpdir))

        self.assertEqual(new_rows, 1)
        self.assertEqual(len(FakeOptimizedMOPSClient.detail_calls), 1)
        self.assertEqual(FakeOptimizedMOPSClient.detail_calls[0]["serialNumber"], "4")
        self.assertEqual([message.subject for message in FakeOptimizedSheetWriter.appended], ["\u91cd\u5927\u8a0a\u606f"])

    def test_detail_failure_does_not_append_partial_rows(self) -> None:
        FakeOptimizedMOPSClient.list_items = [make_item("2330", "2026/05/25", "15:02", "2", "new")]
        FakeOptimizedMOPSClient.fail_detail = True

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(RuntimeError):
                sync_once_output.sync_once_optimized(self.make_config(tmpdir))

        self.assertEqual(FakeOptimizedSheetWriter.appended, [])

    def test_malformed_detail_response_continues_with_empty_detail(self) -> None:
        FakeOptimizedMOPSClient.list_items = [make_item("2330", "2026/05/25", "15:02", "2", "new")]
        FakeOptimizedMOPSClient.malformed_detail = True

        with tempfile.TemporaryDirectory() as tmpdir:
            new_rows = sync_once_output.sync_once_optimized(self.make_config(tmpdir))

        self.assertEqual(new_rows, 1)
        self.assertEqual(FakeOptimizedSheetWriter.appended[0].detail, "No detail returned")


if __name__ == "__main__":
    unittest.main()
