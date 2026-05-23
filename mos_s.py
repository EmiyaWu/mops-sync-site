from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from curl_cffi import requests

try:
    import gspread
    from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound
except ImportError:  # pragma: no cover
    gspread = None
    APIError = SpreadsheetNotFound = WorksheetNotFound = Exception


LIST_URL = "https://mops.twse.com.tw/mops/api/home_page/t05sr01_1"
DETAIL_URL = "https://mops.twse.com.tw/mops/api/t05sr01_1_detail"
DEFAULT_SHEET_ID = "12nk-HoWKMWs4-M4VEmIbEuZVqygYS7AjX8hUqF7hkHs"
DEFAULT_POLL_INTERVAL_SECONDS = 180
DEFAULT_TIMEZONE = "Asia/Taipei"
DEFAULT_MAX_VISIBLE_DAYS = 7
STATE_PATH = Path("state") / "seen_messages.json"

F_DATE = "\u65e5\u671f"
F_TIME = "\u6642\u9593"
F_COMPANY_ID = "\u516c\u53f8\u4ee3\u865f"
F_COMPANY_NAME = "\u516c\u53f8\u7c21\u7a31"
F_SUBJECT = "\u4e3b\u65e8"
F_DETAIL = "\u8a73\u7d30\u5167\u5bb9"
F_KEY = "\u8cc7\u6599\u9375"
F_FETCHED_AT = "\u6293\u53d6\u6642\u9593"

SHEET_HEADERS = [F_DATE, F_TIME, F_COMPANY_ID, F_COMPANY_NAME, F_SUBJECT, F_DETAIL, F_KEY, F_FETCHED_AT]
PUBLIC_FIELDS = [F_DATE, F_TIME, F_COMPANY_ID, F_COMPANY_NAME, F_SUBJECT]

LOGGER = logging.getLogger("mops_sync")


def configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


@dataclass(frozen=True)
class Config:
    sheet_id: str
    credentials_path: str | None
    poll_interval_seconds: int
    timezone: str
    state_path: Path
    max_visible_days: int

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            sheet_id=os.getenv("MOPS_SHEET_ID", DEFAULT_SHEET_ID),
            credentials_path=os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or default_credentials_path(),
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", str(DEFAULT_POLL_INTERVAL_SECONDS))),
            timezone=os.getenv("TZ", DEFAULT_TIMEZONE),
            state_path=Path(os.getenv("MOPS_STATE_PATH", str(STATE_PATH))),
            max_visible_days=int(os.getenv("MOPS_MAX_VISIBLE_DAYS", str(DEFAULT_MAX_VISIBLE_DAYS))),
        )


@dataclass(frozen=True)
class MOPSMessage:
    date: str
    time: str
    company_id: str
    company_name: str
    subject: str
    detail: str
    data_key: str
    fetched_at: str

    def to_sheet_row(self) -> list[str]:
        return [self.date, self.time, self.company_id, self.company_name, self.subject, self.detail, self.data_key, self.fetched_at]

    def to_public_dict(self) -> dict[str, str]:
        return {
            F_DATE: self.date,
            F_TIME: self.time,
            F_COMPANY_ID: self.company_id,
            F_COMPANY_NAME: self.company_name,
            F_SUBJECT: self.subject,
        }


class MOPSClient:
    def __init__(
        self,
        list_url: str = LIST_URL,
        detail_url: str = DETAIL_URL,
        timeout_seconds: int = 20,
        max_retries: int = 3,
        detail_delay_seconds: float = 0.1,
    ) -> None:
        self.list_url = list_url
        self.detail_url = detail_url
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.detail_delay_seconds = detail_delay_seconds
        self.headers = {
            "Content-Type": "application/json",
            "Origin": "https://mops.twse.com.tw",
            "Referer": "https://mops.twse.com.tw/mops/web/t05sr01_1",
        }

    def fetch_messages(self, fetched_at: str) -> list[MOPSMessage]:
        messages: list[MOPSMessage] = []
        for item in self.fetch_list():
            if not isinstance(item, dict):
                LOGGER.warning("Skip unexpected list item: %r", item)
                continue
            params = self._extract_detail_params(item)
            detail = self.fetch_detail(params) if params else "No detail"
            if params and self.detail_delay_seconds > 0:
                time.sleep(self.detail_delay_seconds)
            messages.append(MessageNormalizer.normalize(item, detail, params, fetched_at))
        return messages

    def fetch_list(self) -> list[Any]:
        response = self._post_json(self.list_url, {"count": "0", "marketKind": ""})
        result = response.get("result", {})
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
        data = response.get("result", {}).get("data", [])
        if not data:
            return "No detail returned"
        last_item = data[-1]
        if isinstance(last_item, list) and len(last_item) > 9:
            return str(last_item[9] or "")
        if isinstance(last_item, dict):
            return str(last_item.get("detail") or last_item.get("content") or "")
        return str(last_item)

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.post(
                    url,
                    headers=self.headers,
                    json=payload,
                    timeout=self.timeout_seconds,
                    impersonate="chrome",
                )
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last_error = exc
                if attempt == self.max_retries:
                    break
                sleep_seconds = min(2 ** attempt, 10)
                LOGGER.warning("MOPS request failed, retry %s after %s seconds: %s", attempt, sleep_seconds, exc)
                time.sleep(sleep_seconds)
        raise RuntimeError(f"MOPS request failed: {url}") from last_error

    @staticmethod
    def _extract_detail_params(item: dict[str, Any]) -> dict[str, Any]:
        url_data = item.get("url") or {}
        if not isinstance(url_data, dict):
            return {}
        params = url_data.get("parameters") or {}
        return params if isinstance(params, dict) else {}


class MessageNormalizer:
    @staticmethod
    def normalize(item: dict[str, Any], detail: str, params: dict[str, Any], fetched_at: str) -> MOPSMessage:
        date = clean_text(item.get("date"))
        item_time = clean_text(item.get("time"))
        company_id = clean_text(item.get("companyId"))
        company_name = clean_text(item.get("companyAbbreviation"))
        subject = clean_text(item.get("subject"))
        serial_number = clean_text(params.get("serialNumber"))
        param_date = clean_text(params.get("date"))
        data_key = "|".join([company_id, date or param_date, item_time, serial_number, subject])
        return MOPSMessage(date, item_time, company_id, company_name, subject, clean_text(detail), data_key, fetched_at)


class Deduper:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self.seen_keys = self._load()

    def filter_new(self, messages: Iterable[MOPSMessage], extra_seen_keys: Iterable[str] = ()) -> list[MOPSMessage]:
        seen = self.seen_keys | set(extra_seen_keys)
        new_messages: list[MOPSMessage] = []
        for message in messages:
            if message.data_key in seen:
                continue
            seen.add(message.data_key)
            new_messages.append(message)
        return new_messages

    def mark_seen(self, messages: Iterable[MOPSMessage]) -> None:
        self.seen_keys.update(message.data_key for message in messages)
        self._save()

    def _load(self) -> set[str]:
        if not self.state_path.exists():
            return set()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            LOGGER.warning("Could not read state file, continuing empty: %s", exc)
            return set()
        keys = data.get("seen_keys", []) if isinstance(data, dict) else []
        return {str(key) for key in keys}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated_at": datetime.now().isoformat(timespec="seconds"), "seen_keys": sorted(self.seen_keys)}
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class GoogleSheetWriter:
    def __init__(self, sheet_id: str, credentials_path: str | None, max_visible_days: int = DEFAULT_MAX_VISIBLE_DAYS) -> None:
        if gspread is None:
            raise RuntimeError("gspread is not installed. Run: pip install -r requirements.txt")
        if not credentials_path:
            raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS is missing")
        self.sheet_id = sheet_id
        self.max_visible_days = max_visible_days
        self.client = gspread.service_account(filename=credentials_path)
        self.spreadsheet = self.client.open_by_key(sheet_id)

    def validate(self) -> None:
        self.spreadsheet.fetch_sheet_metadata()
        self.organize_daily_worksheets()

    def append_messages(self, worksheet_date: datetime, messages: list[MOPSMessage]) -> int:
        if not messages:
            self.organize_daily_worksheets()
            return 0
        worksheet = self._get_or_create_daily_worksheet(worksheet_date)
        self._ensure_headers(worksheet)
        self._with_retry(lambda: worksheet.append_rows([message.to_sheet_row() for message in messages], value_input_option="USER_ENTERED"))
        self.organize_daily_worksheets()
        return len(messages)

    def existing_keys(self, worksheet_date: datetime) -> set[str]:
        rows = self.daily_rows(worksheet_date)
        if not rows:
            return set()
        try:
            key_index = rows[0].index(F_KEY)
        except ValueError:
            return set()
        return {row[key_index] for row in rows[1:] if len(row) > key_index and row[key_index]}

    def daily_rows(self, worksheet_date: datetime) -> list[list[str]]:
        try:
            return self._get_or_create_daily_worksheet(worksheet_date).get_all_values()
        except Exception as exc:
            LOGGER.warning("Could not read daily worksheet: %s", exc)
            return []

    def organize_daily_worksheets(self) -> None:
        metadata = self.spreadsheet.fetch_sheet_metadata()
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
            self._with_retry(lambda: self.spreadsheet.batch_update({"requests": requests_payload}))
            LOGGER.info("Organized daily worksheets, newest first, visible days: %s", self.max_visible_days)

    def _get_or_create_daily_worksheet(self, worksheet_date: datetime):
        preferred_title = worksheet_date.strftime("%Y/%m/%d")
        fallback_title = worksheet_date.strftime("%Y-%m-%d")
        for title in (preferred_title, fallback_title):
            try:
                return self.spreadsheet.worksheet(title)
            except WorksheetNotFound:
                continue
        try:
            return self.spreadsheet.add_worksheet(title=preferred_title, rows=1000, cols=len(SHEET_HEADERS))
        except APIError as exc:
            LOGGER.warning("Could not create worksheet %s, fallback to %s: %s", preferred_title, fallback_title, exc)
            return self.spreadsheet.add_worksheet(title=fallback_title, rows=1000, cols=len(SHEET_HEADERS))

    @staticmethod
    def _ensure_headers(worksheet: Any) -> None:
        current_headers = worksheet.row_values(1)
        if current_headers == SHEET_HEADERS:
            return
        if not current_headers:
            worksheet.update("A1:H1", [SHEET_HEADERS])
            worksheet.freeze(rows=1)
            return
        raise RuntimeError(f"Unexpected worksheet headers: {current_headers}")

    @staticmethod
    def _with_retry(operation: Any, max_retries: int = 3) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                return operation()
            except APIError as exc:
                last_error = exc
                if attempt == max_retries:
                    break
                sleep_seconds = min(2 ** attempt, 10)
                LOGGER.warning("Google Sheet API failed, retry %s after %s seconds: %s", attempt, sleep_seconds, exc)
                time.sleep(sleep_seconds)
        raise RuntimeError("Google Sheet API failed after retries") from last_error


class SiteExporter:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    def export(self, messages: list[MOPSMessage], generated_at: datetime) -> None:
        public_messages = [message.to_public_dict() for message in self._sort_messages(messages)]
        payload = {
            "generated_at": generated_at.isoformat(timespec="seconds"),
            "timezone": generated_at.tzname(),
            "count": len(public_messages),
            "fields": PUBLIC_FIELDS,
            "messages": public_messages,
        }
        data_dir = self.output_dir / "data"
        assets_dir = self.output_dir / "assets"
        data_dir.mkdir(parents=True, exist_ok=True)
        assets_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.output_dir / "index.html").write_text(self._render_html(payload), encoding="utf-8")
        (assets_dir / "site.css").write_text(SITE_CSS, encoding="utf-8")
        (assets_dir / "site.js").write_text(SITE_JS, encoding="utf-8")
        LOGGER.info("Exported public site to %s with %s rows", self.output_dir, len(public_messages))

    @staticmethod
    def _sort_messages(messages: list[MOPSMessage]) -> list[MOPSMessage]:
        return sorted(messages, key=lambda message: (message.date, message.time, message.company_id, message.subject), reverse=True)

    @staticmethod
    def _render_html(payload: dict[str, Any]) -> str:
        rows = "\n".join(
            "<tr>"
            f"<td data-label=\"Time\">{html.escape(item[F_TIME])}</td>"
            f"<td data-label=\"Company ID\">{html.escape(item[F_COMPANY_ID])}</td>"
            f"<td data-label=\"Company\">{html.escape(item[F_COMPANY_NAME])}</td>"
            f"<td data-label=\"Subject\" class=\"subject\">{html.escape(item[F_SUBJECT])}</td>"
            "</tr>"
            for item in payload["messages"]
        )
        if not rows:
            rows = '<tr class="empty-row"><td colspan="4">No public rows yet.</td></tr>'
        generated_at = html.escape(str(payload["generated_at"]))
        count = html.escape(str(payload["count"]))
        return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MOPS Material Information</title>
  <link rel="stylesheet" href="assets/site.css">
</head>
<body>
  <main class="app-shell">
    <section class="topbar" aria-label="Summary">
      <div>
        <p class="eyebrow">MOPS Live Feed</p>
        <h1>Material Information Dashboard</h1>
      </div>
      <div class="metrics" aria-label="Update information">
        <div class="metric"><span>Rows Today</span><strong id="totalCount">{count}</strong></div>
        <div class="metric wide"><span>Last Updated</span><strong id="generatedAt">{generated_at}</strong></div>
      </div>
    </section>
    <section class="toolbar" aria-label="Filters">
      <label>Company ID<input id="companyIdFilter" type="search" inputmode="numeric" placeholder="2330"></label>
      <label>Company<input id="companyNameFilter" type="search" placeholder="TSMC"></label>
      <label class="wide-filter">Subject<input id="subjectFilter" type="search" placeholder="Search subject"></label>
      <button id="sortTimeButton" type="button">Time: Newest</button>
    </section>
    <section class="table-wrap" aria-label="Messages">
      <table>
        <thead><tr><th>Time</th><th>Company ID</th><th>Company</th><th>Subject</th></tr></thead>
        <tbody id="messageRows">{rows}</tbody>
      </table>
    </section>
  </main>
  <script src="assets/site.js"></script>
</body>
</html>
"""


class SyncService:
    def __init__(
        self,
        config: Config,
        client: MOPSClient | None = None,
        writer: GoogleSheetWriter | None = None,
        site_exporter: SiteExporter | None = None,
    ) -> None:
        self.config = config
        self.client = client or MOPSClient()
        self.writer = writer or GoogleSheetWriter(config.sheet_id, config.credentials_path, config.max_visible_days)
        self.site_exporter = site_exporter
        self.deduper = Deduper(config.state_path)
        self.tz = ZoneInfo(config.timezone)

    def sync_once(self, export_site_path: Path | None = None) -> int:
        now = datetime.now(self.tz)
        fetched_at = now.isoformat(timespec="seconds")
        messages = self.client.fetch_messages(fetched_at)
        existing_keys = self.writer.existing_keys(now)
        new_messages = self.deduper.filter_new(messages, existing_keys)
        if not new_messages:
            self.writer.organize_daily_worksheets()
            LOGGER.info("No new rows. fetched=%s skipped=%s", len(messages), len(messages))
        else:
            appended_count = self.writer.append_messages(now, new_messages)
            self.deduper.mark_seen(new_messages)
            LOGGER.info("Sync complete. fetched=%s appended=%s skipped=%s", len(messages), appended_count, len(messages) - appended_count)
        if export_site_path:
            exporter = self.site_exporter or SiteExporter(export_site_path)
            exporter.export(messages, now)
        return len(new_messages)

    def run_forever(self, export_site_path: Path | None = None) -> None:
        LOGGER.info("Starting scheduler every %s seconds", self.config.poll_interval_seconds)
        while True:
            started_at = time.monotonic()
            try:
                self.sync_once(export_site_path)
            except Exception:
                LOGGER.exception("Sync failed. Existing data is preserved.")
            elapsed = time.monotonic() - started_at
            sleep_seconds = max(self.config.poll_interval_seconds - elapsed, 0)
            next_run = datetime.now(self.tz).timestamp() + sleep_seconds
            LOGGER.info("Next run: %s", datetime.fromtimestamp(next_run, self.tz).isoformat(timespec="seconds"))
            time.sleep(sleep_seconds)

    def validate(self) -> None:
        self.client.fetch_list()
        self.writer.validate()


def parse_worksheet_date(title: str) -> datetime | None:
    match = re.fullmatch(r"(\d{4})[/-](\d{2})[/-](\d{2})", title)
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def default_credentials_path() -> str | None:
    candidates = [
        Path.cwd() / "service-account.json",
        Path(sys.executable).resolve().parent / "service-account.json",
        Path(__file__).resolve().parent / "service-account.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def prepare_credentials_from_json_secret() -> None:
    secret = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not secret or os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        return
    credentials_path = Path("secrets") / "google-service-account.json"
    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    credentials_path.write_text(secret, encoding="utf-8")
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(credentials_path)


def clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync MOPS material information to Google Sheet and optionally export a public static site")
    parser.add_argument("command", choices=["run", "once", "validate"], help="run forever, run once, or validate connectivity")
    parser.add_argument("--export-site", type=Path, help="Export public static site to this directory, for example: public")
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logs")
    return parser


def main() -> int:
    configure_console_encoding()
    prepare_credentials_from_json_secret()
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(args.verbose)
    config = Config.from_env()
    try:
        service = SyncService(config)
        if args.command == "run":
            service.run_forever(args.export_site)
        elif args.command == "once":
            service.sync_once(args.export_site)
        elif args.command == "validate":
            service.validate()
            LOGGER.info("Validation complete")
    except (SpreadsheetNotFound, FileNotFoundError, RuntimeError, ValueError) as exc:
        LOGGER.error("Execution failed: %s", exc)
        return 1
    except KeyboardInterrupt:
        LOGGER.info("Stopped")
        return 0
    return 0


SITE_CSS = """
:root {
  --bg: #f4f6f8;
  --panel: #ffffff;
  --ink: #1b2430;
  --muted: #657181;
  --line: #d8dee6;
  --accent: #007a78;
  --accent-strong: #005c5a;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: "Noto Sans TC", "Microsoft JhengHei", system-ui, sans-serif;
}
.app-shell { width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 36px; }
.topbar { display: flex; align-items: end; justify-content: space-between; gap: 20px; padding-bottom: 18px; border-bottom: 1px solid var(--line); }
.eyebrow { margin: 0 0 6px; color: var(--accent-strong); font-size: 13px; font-weight: 700; }
h1 { margin: 0; font-size: 28px; line-height: 1.25; }
.metrics { display: grid; grid-template-columns: minmax(96px, auto) minmax(220px, auto); gap: 10px; }
.metric { min-height: 68px; padding: 12px 14px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
.metric span { display: block; color: var(--muted); font-size: 12px; margin-bottom: 6px; }
.metric strong { display: block; font-size: 16px; line-height: 1.35; }
.toolbar { display: grid; grid-template-columns: 160px 180px minmax(240px, 1fr) 128px; gap: 12px; align-items: end; margin: 20px 0 14px; }
label { color: var(--muted); font-size: 12px; font-weight: 700; }
input, button { width: 100%; min-height: 40px; margin-top: 6px; border: 1px solid var(--line); border-radius: 8px; font: inherit; }
input { padding: 8px 10px; background: #fff; }
button { cursor: pointer; color: #fff; background: var(--accent); font-weight: 700; }
button:hover { background: var(--accent-strong); }
.table-wrap { overflow: hidden; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 12px 14px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
th { position: sticky; top: 0; z-index: 1; background: #eef3f6; color: #334155; font-size: 13px; }
td { font-size: 14px; }
td.subject { line-height: 1.55; }
.empty-row td { padding: 28px 14px; color: var(--muted); text-align: center; }
@media (max-width: 760px) {
  .app-shell { width: min(100% - 20px, 1180px); padding-top: 18px; }
  .topbar, .metrics, .toolbar { display: grid; grid-template-columns: 1fr; }
  h1 { font-size: 22px; }
  table, thead, tbody, tr, th, td { display: block; }
  thead { display: none; }
  tr { padding: 12px 14px; border-bottom: 1px solid var(--line); }
  td { display: grid; grid-template-columns: 92px 1fr; gap: 10px; padding: 5px 0; border-bottom: 0; }
  td::before { content: attr(data-label); color: var(--muted); font-weight: 700; }
}
"""

SITE_JS = """
const rowsBody = document.querySelector("#messageRows");
const totalCount = document.querySelector("#totalCount");
const companyIdFilter = document.querySelector("#companyIdFilter");
const companyNameFilter = document.querySelector("#companyNameFilter");
const subjectFilter = document.querySelector("#subjectFilter");
const sortTimeButton = document.querySelector("#sortTimeButton");

const FIELD_TIME = "\\u6642\\u9593";
const FIELD_COMPANY_ID = "\\u516c\\u53f8\\u4ee3\\u865f";
const FIELD_COMPANY_NAME = "\\u516c\\u53f8\\u7c21\\u7a31";
const FIELD_SUBJECT = "\\u4e3b\\u65e8";
let messages = [];
let newestFirst = true;

function normalize(value) { return String(value || "").trim().toLowerCase(); }
function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
function render() {
  const idTerm = normalize(companyIdFilter.value);
  const nameTerm = normalize(companyNameFilter.value);
  const subjectTerm = normalize(subjectFilter.value);
  const filtered = messages
    .filter((item) => normalize(item[FIELD_COMPANY_ID]).includes(idTerm))
    .filter((item) => normalize(item[FIELD_COMPANY_NAME]).includes(nameTerm))
    .filter((item) => normalize(item[FIELD_SUBJECT]).includes(subjectTerm))
    .sort((a, b) => {
      const left = `${a["\\u65e5\\u671f"]} ${a[FIELD_TIME]}`;
      const right = `${b["\\u65e5\\u671f"]} ${b[FIELD_TIME]}`;
      return newestFirst ? right.localeCompare(left) : left.localeCompare(right);
    });
  totalCount.textContent = String(filtered.length);
  rowsBody.innerHTML = filtered.length
    ? filtered.map((item) => `
      <tr>
        <td data-label="Time">${escapeHtml(item[FIELD_TIME])}</td>
        <td data-label="Company ID">${escapeHtml(item[FIELD_COMPANY_ID])}</td>
        <td data-label="Company">${escapeHtml(item[FIELD_COMPANY_NAME])}</td>
        <td data-label="Subject" class="subject">${escapeHtml(item[FIELD_SUBJECT])}</td>
      </tr>
    `).join("")
    : '<tr class="empty-row"><td colspan="4">No matching rows.</td></tr>';
}
async function loadData() {
  const response = await fetch("data/latest.json", { cache: "no-store" });
  const data = await response.json();
  messages = data.messages || [];
  document.querySelector("#generatedAt").textContent = data.generated_at || "";
  render();
}
[companyIdFilter, companyNameFilter, subjectFilter].forEach((input) => input.addEventListener("input", render));
sortTimeButton.addEventListener("click", () => {
  newestFirst = !newestFirst;
  sortTimeButton.textContent = newestFirst ? "Time: Newest" : "Time: Oldest";
  render();
});
loadData().catch(() => {
  rowsBody.innerHTML = '<tr class="empty-row"><td colspan="4">Failed to load data.</td></tr>';
});
"""


if __name__ == "__main__":
    raise SystemExit(main())
