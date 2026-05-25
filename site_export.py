from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread


F_DATE = "\u65e5\u671f"
F_TIME = "\u6642\u9593"
F_COMPANY_ID = "\u516c\u53f8\u4ee3\u865f"
F_COMPANY_NAME = "\u516c\u53f8\u7c21\u7a31"
F_SUBJECT = "\u4e3b\u65e8"
F_DETAIL = "\u8a73\u7d30\u5167\u5bb9"
PUBLIC_FIELDS = [F_DATE, F_TIME, F_COMPANY_ID, F_COMPANY_NAME, F_SUBJECT, F_DETAIL]
EXCLUDED_SUBJECT_KEYWORDS = ("\u80a1\u6771\u5e38\u6703", "\u59d4\u54e1\u6703")


def main() -> int:
    output_dir = Path(os.getenv("SITE_OUTPUT_DIR", "public"))
    sheet_id = os.environ["MOPS_SHEET_ID"]
    credentials_path = prepare_credentials()
    spreadsheet = gspread.service_account(filename=credentials_path).open_by_key(sheet_id)
    now = datetime.now(ZoneInfo(os.getenv("TZ", "Asia/Taipei")))
    rows, source_title = read_site_rows(spreadsheet, now)
    messages = rows_to_public_messages(rows)
    export_site(output_dir, messages, now, source_title)
    write_github_output("public_count", len(messages))
    write_github_output("source_title", source_title)
    print(f"Exported {len(messages)} rows from {source_title or 'no worksheet'} to {output_dir}")
    return 0


def prepare_credentials() -> str:
    existing = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if existing:
        return existing

    secret = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    credentials_path = Path("secrets") / "google-service-account.json"
    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    credentials_path.write_text(secret, encoding="utf-8")
    return str(credentials_path)


def write_github_output(name: str, value: object) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as output_file:
        output_file.write(f"{name}={value}\n")


def read_today_rows(spreadsheet, now: datetime) -> list[list[str]]:
    rows, _ = read_site_rows(spreadsheet, now)
    return rows


def read_site_rows(spreadsheet, now: datetime) -> tuple[list[list[str]], str]:
    today_titles = [now.strftime("%Y/%m/%d"), now.strftime("%Y-%m-%d")]
    first_existing_rows: list[list[str]] = []
    first_existing_title = ""

    for title in today_titles:
        try:
            rows = spreadsheet.worksheet(title).get_all_values()
        except gspread.WorksheetNotFound:
            continue
        if not first_existing_rows:
            first_existing_rows = rows
            first_existing_title = title
        if rows_have_data(rows):
            return rows, title

    date_worksheets = []
    for worksheet in spreadsheet.worksheets():
        parsed_date = parse_worksheet_date(worksheet.title)
        if parsed_date is not None:
            date_worksheets.append((parsed_date, worksheet))
    date_worksheets.sort(key=lambda item: item[0], reverse=True)

    for _, worksheet in date_worksheets:
        if worksheet.title in today_titles:
            continue
        rows = worksheet.get_all_values()
        if rows_have_data(rows):
            return rows, worksheet.title

    return first_existing_rows, first_existing_title


def rows_have_data(rows: list[list[str]]) -> bool:
    return len(rows) > 1 and any(any(cell.strip() for cell in row) for row in rows[1:])


def parse_worksheet_date(title: str) -> datetime | None:
    match = re.fullmatch(r"(\d{4})[/-](\d{2})[/-](\d{2})", title)
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def read_exact_worksheet_rows(spreadsheet, now: datetime) -> list[list[str]]:
    for title in (now.strftime("%Y/%m/%d"), now.strftime("%Y-%m-%d")):
        try:
            return spreadsheet.worksheet(title).get_all_values()
        except gspread.WorksheetNotFound:
            continue
    return []


def rows_to_public_messages(rows: list[list[str]]) -> list[dict[str, str]]:
    if not rows:
        return []

    headers = rows[0]
    header_index = {header: index for index, header in enumerate(headers)}
    messages = []
    for row in rows[1:]:
        item = {}
        for field in PUBLIC_FIELDS:
            index = header_index.get(field)
            item[field] = row[index].strip() if index is not None and index < len(row) else ""
        if is_excluded_subject(item.get(F_SUBJECT, "")):
            continue
        messages.append(item)
    return sorted(messages, key=message_sort_key, reverse=True)


def is_excluded_subject(subject: str) -> bool:
    normalized = str(subject or "").strip()
    return any(keyword in normalized for keyword in EXCLUDED_SUBJECT_KEYWORDS)


def message_sort_key(item: dict[str, str]) -> tuple[str, str, str, str]:
    return (normalize_date_for_sort(item.get(F_DATE, "")), normalize_time_for_sort(item.get(F_TIME, "")), item.get(F_COMPANY_ID, ""), item.get(F_SUBJECT, ""))


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


def export_site(output_dir: Path, messages: list[dict[str, str]], generated_at: datetime, source_title: str = "") -> None:
    data_dir = output_dir / "data"
    assets_dir = output_dir / "assets"
    data_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "count": len(messages),
        "source_title": source_title,
        "fields": PUBLIC_FIELDS,
        "messages": messages,
    }
    (data_dir / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (assets_dir / "site.css").write_text(SITE_CSS, encoding="utf-8")
    (assets_dir / "site.js").write_text(SITE_JS, encoding="utf-8")


INDEX_HTML = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>\u516c\u958b\u8cc7\u8a0a\u89c0\u6e2c\u7ad9\u91cd\u5927\u8a0a\u606f</title>
  <link rel="stylesheet" href="assets/site.css">
</head>
<body>
  <main class="app-shell">
    <section class="topbar">
      <div class="brand-block">
        <div class="brand-row">
          <span class="brand-mark">M</span>
          <p class="eyebrow">\u5373\u6642\u91cd\u8a0a\u770b\u677f</p>
        </div>
        <h1>\u516c\u958b\u8cc7\u8a0a\u89c0\u6e2c\u7ad9\u91cd\u5927\u8a0a\u606f</h1>
        <p class="subtitle">\u81ea\u52d5\u540c\u6b65\u7576\u65e5\u4e0a\u5e02\u6ac3\u516c\u53f8\u91cd\u5927\u8a0a\u606f\uff0c\u986f\u793a\u516c\u53f8\u3001\u4e3b\u65e8\u8207\u8a73\u7d30\u5167\u5bb9\u3002</p>
      </div>
      <div class="metrics">
        <div class="metric"><span>\u986f\u793a\u7b46\u6578</span><strong id="totalCount">0</strong></div>
        <div class="metric wide"><span>\u6700\u5f8c\u66f4\u65b0</span><strong id="generatedAt">-</strong></div>
      </div>
    </section>

    <section class="toolbar-panel">
      <div class="toolbar">
        <label>\u516c\u53f8\u4ee3\u865f<input id="companyIdFilter" type="search" inputmode="numeric" placeholder="2330"></label>
        <label>\u516c\u53f8\u7c21\u7a31<input id="companyNameFilter" type="search" placeholder="\u53f0\u7a4d\u96fb"></label>
        <label class="wide-filter">\u95dc\u9375\u5b57<input id="subjectFilter" type="search" placeholder="\u641c\u5c0b\u4e3b\u65e8\u6216\u8a73\u7d30\u5167\u5bb9"></label>
        <button id="sortTimeButton" type="button">\u6700\u65b0\u5728\u524d</button>
      </div>
      <div class="status-line">
        <span id="resultStatus">\u8cc7\u6599\u8f09\u5165\u4e2d</span>
        <span>\u4f86\u6e90\uff1aMOPS \u516c\u958b\u8cc7\u8a0a\u89c0\u6e2c\u7ad9</span>
      </div>
    </section>

    <section class="table-wrap">
      <table>
        <thead><tr><th>\u6642\u9593</th><th>\u516c\u53f8\u4ee3\u865f</th><th>\u516c\u53f8\u7c21\u7a31</th><th>\u4e3b\u65e8\u8207\u8a73\u7d30\u5167\u5bb9</th></tr></thead>
        <tbody id="messageRows"><tr class="empty-row"><td colspan="4">\u8cc7\u6599\u8f09\u5165\u4e2d...</td></tr></tbody>
      </table>
    </section>
  </main>
  <script src="assets/site.js"></script>
</body>
</html>
"""


SITE_CSS = """
:root {
  --bg: #f3f5f4;
  --surface: #eef3f1;
  --panel: #ffffff;
  --panel-soft: #f8faf8;
  --ink: #14201f;
  --muted: #65716f;
  --line: #d6dfdc;
  --line-soft: #edf2f0;
  --accent: #08726f;
  --accent-strong: #045350;
  --accent-soft: #e3f2ef;
  --gold: #9a6a18;
  --gold-soft: #fff3d8;
  --danger: #9f3a31;
  --shadow: 0 18px 42px rgba(20, 32, 31, 0.08);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  background:
    linear-gradient(180deg, #e9f0ee 0, #f6f7f5 280px, var(--bg) 100%);
  color: var(--ink);
  font-family: "Noto Sans TC", "Microsoft JhengHei", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
}
.app-shell { width: min(1280px, calc(100% - 36px)); margin: 0 auto; padding: 30px 0 42px; }
.topbar {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 24px;
  align-items: end;
  padding: 20px 22px;
  background: rgba(255, 255, 255, 0.82);
  border: 1px solid rgba(214, 223, 220, 0.9);
  border-radius: 8px;
  box-shadow: var(--shadow);
}
.brand-block { min-width: 0; }
.brand-row { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.brand-mark {
  display: inline-grid;
  place-items: center;
  width: 30px;
  height: 30px;
  border-radius: 8px;
  color: #fff;
  background: linear-gradient(135deg, var(--accent), #0e8f78);
  font-size: 14px;
  font-weight: 900;
}
.eyebrow {
  margin: 0;
  color: var(--accent-strong);
  font-size: 13px;
  font-weight: 800;
}
h1 { margin: 0; font-size: 31px; line-height: 1.24; letter-spacing: 0; }
.subtitle { max-width: 780px; margin: 10px 0 0; color: var(--muted); font-size: 14px; line-height: 1.72; }
.metrics { display: grid; grid-template-columns: 118px minmax(238px, auto); gap: 10px; }
.metric {
  min-height: 76px;
  padding: 13px 15px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: 0 10px 26px rgba(20, 32, 31, 0.045);
}
.metric span { display: block; color: var(--muted); font-size: 12px; margin-bottom: 8px; font-weight: 700; }
.metric strong { display: block; font-size: 18px; line-height: 1.35; }
.toolbar-panel {
  margin: 16px 0 12px;
  padding: 14px;
  background: rgba(255, 255, 255, 0.9);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: 0 12px 30px rgba(20, 32, 31, 0.055);
}
.toolbar {
  display: grid;
  grid-template-columns: 164px 188px minmax(280px, 1fr) 136px;
  gap: 12px;
  align-items: end;
}
label { color: var(--muted); font-size: 12px; font-weight: 800; }
input, button { width: 100%; min-height: 43px; margin-top: 7px; border-radius: 8px; font: inherit; }
input {
  padding: 9px 12px;
  background: #fbfcfb;
  color: var(--ink);
  border: 1px solid var(--line);
  outline: none;
}
input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(0, 111, 114, 0.12); }
button {
  cursor: pointer;
  color: #fff;
  background: var(--accent);
  border: 1px solid var(--accent);
  font-weight: 800;
}
button:hover { background: var(--accent-strong); border-color: var(--accent-strong); }
.status-line {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--line-soft);
  color: var(--muted);
  font-size: 13px;
}
.status-line span:first-child {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--gold);
  font-weight: 800;
}
.status-line span:first-child::before {
  content: "";
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: var(--gold);
  box-shadow: 0 0 0 4px var(--gold-soft);
}
.table-wrap {
  overflow: auto;
  max-height: calc(100vh - 260px);
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: var(--shadow);
}
table { width: 100%; border-collapse: separate; border-spacing: 0; }
th, td { padding: 13px 15px; border-bottom: 1px solid var(--line-soft); text-align: left; vertical-align: top; }
th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: #edf3f1;
  color: #344441;
  font-size: 13px;
  white-space: nowrap;
}
td { font-size: 14px; background: #fff; }
tbody tr:hover td { background: var(--panel-soft); }
td:nth-child(1) { width: 96px; color: var(--accent-strong); font-weight: 800; white-space: nowrap; }
td:nth-child(2) { width: 116px; font-variant-numeric: tabular-nums; }
td:nth-child(3) { width: 136px; font-weight: 700; }
td.subject { line-height: 1.6; min-width: 0; }
td.subject strong {
  display: inline-block;
  margin-bottom: 7px;
  color: #111827;
  font-size: 15px;
  border-bottom: 2px solid rgba(8, 114, 111, 0.18);
  overflow-wrap: anywhere;
}
.detail-content { min-width: 0; max-width: 100%; color: #43514f; }
.detail-text {
  margin: 0;
  color: #43514f;
  line-height: 1.75;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
}
.detail-table {
  max-width: 100%;
  margin: 10px 0;
  padding: 10px 12px;
  overflow-x: auto;
  background: #f7faf9;
  border: 1px solid var(--line-soft);
  border-radius: 8px;
  -webkit-overflow-scrolling: touch;
}
.detail-table pre {
  min-width: max-content;
  margin: 0;
  color: #22302d;
  font: 12px/1.55 "Cascadia Mono", "Consolas", "Menlo", monospace;
  white-space: pre;
}
.empty-row td { padding: 30px 14px; color: var(--muted); text-align: center; }
@media (max-width: 760px) {
  .app-shell { width: min(100% - 20px, 1280px); padding-top: 16px; }
  .topbar, .metrics, .toolbar { display: grid; grid-template-columns: 1fr; }
  .topbar { padding: 16px; gap: 16px; }
  h1 { font-size: 24px; }
  .toolbar-panel { padding: 12px; }
  .status-line { display: grid; }
  .table-wrap { max-height: none; overflow: visible; background: transparent; border: 0; box-shadow: none; }
  table, thead, tbody, tr, th, td { display: block; }
  thead { display: none; }
  tr {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px 12px;
    margin-bottom: 10px;
    padding: 13px 14px;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
    box-shadow: 0 10px 24px rgba(20, 32, 31, 0.055);
  }
  td {
    display: block;
    width: auto !important;
    min-width: 0;
    padding: 0;
    border-bottom: 0;
    background: transparent;
  }
  td::before {
    content: attr(data-label);
    display: block;
    margin-bottom: 4px;
    color: var(--muted);
    font-size: 13px;
    font-weight: 800;
  }
  td:nth-child(1), td:nth-child(2), td:nth-child(3) { width: auto; white-space: normal; }
  td.subject {
    grid-column: 1 / -1;
    margin-top: 4px;
    padding-top: 12px;
    border-top: 1px solid var(--line-soft);
  }
  td.subject strong {
    display: block;
    margin-bottom: 10px;
    padding-bottom: 8px;
    font-size: 18px;
    line-height: 1.6;
  }
  .detail-text { font-size: 16px; line-height: 1.85; }
  .detail-table { margin: 12px 0; padding: 9px 10px; }
  .detail-table pre { font-size: 11px; line-height: 1.6; }
}
"""


SITE_JS = """
const rowsBody = document.querySelector("#messageRows");
const totalCount = document.querySelector("#totalCount");
const companyIdFilter = document.querySelector("#companyIdFilter");
const companyNameFilter = document.querySelector("#companyNameFilter");
const subjectFilter = document.querySelector("#subjectFilter");
const sortTimeButton = document.querySelector("#sortTimeButton");

const FIELD_DATE = "\\u65e5\\u671f";
const FIELD_TIME = "\\u6642\\u9593";
const FIELD_COMPANY_ID = "\\u516c\\u53f8\\u4ee3\\u865f";
const FIELD_COMPANY_NAME = "\\u516c\\u53f8\\u7c21\\u7a31";
const FIELD_SUBJECT = "\\u4e3b\\u65e8";
const FIELD_DETAIL = "\\u8a73\\u7d30\\u5167\\u5bb9";
let messages = [];
let newestFirst = true;

function normalize(value) { return String(value || "").trim().toLowerCase(); }
function sortDate(value) {
  const digits = String(value || "").replace(/\\D/g, "");
  return digits.length === 8 ? digits : String(value || "");
}
function sortTime(value) {
  const parts = String(value || "").match(/\\d+/g) || [];
  const h = String(Number(parts[0] || 0)).padStart(2, "0");
  const m = String(Number(parts[1] || 0)).padStart(2, "0");
  const s = String(Number(parts[2] || 0)).padStart(2, "0");
  return `${h}:${m}:${s}`;
}
function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
function looksLikeTableLine(line) {
  const trimmed = String(line || "").trim();
  if (!trimmed) return false;
  return trimmed.length >= 12 && (/={4,}/.test(trimmed) || /\t/.test(line) || / {2,}/.test(line));
}
function renderTextBlock(lines) {
  return lines.length ? `<div class="detail-text">${escapeHtml(lines.join("\\n"))}</div>` : "";
}
function renderTableBlock(lines) {
  return lines.length ? `<div class="detail-table"><pre>${escapeHtml(lines.join("\\n"))}</pre></div>` : "";
}
function formatDetail(value) {
  const lines = String(value || "").replace(/\\r\\n/g, "\\n").replace(/\\r/g, "\\n").split("\\n");
  const parts = [];
  let block = [];
  let tableMode = false;
  function flush() {
    if (!block.length) return;
    parts.push(tableMode ? renderTableBlock(block) : renderTextBlock(block));
    block = [];
  }
  for (const line of lines) {
    const nextTableMode = looksLikeTableLine(line);
    if (block.length && nextTableMode !== tableMode) flush();
    tableMode = nextTableMode;
    block.push(line);
  }
  flush();
  return parts.join("");
}
function render() {
  const idTerm = normalize(companyIdFilter.value);
  const nameTerm = normalize(companyNameFilter.value);
  const subjectTerm = normalize(subjectFilter.value);
  const filtered = messages
    .filter((item) => normalize(item[FIELD_COMPANY_ID]).includes(idTerm))
    .filter((item) => normalize(item[FIELD_COMPANY_NAME]).includes(nameTerm))
    .filter((item) => `${normalize(item[FIELD_SUBJECT])} ${normalize(item[FIELD_DETAIL])}`.includes(subjectTerm))
    .sort((a, b) => {
      const leftKey = `${sortDate(a[FIELD_DATE])} ${sortTime(a[FIELD_TIME])}`;
      const rightKey = `${sortDate(b[FIELD_DATE])} ${sortTime(b[FIELD_TIME])}`;
      return newestFirst ? rightKey.localeCompare(leftKey) : leftKey.localeCompare(rightKey);
    });
  totalCount.textContent = String(filtered.length);
  const sourceTitle = window.siteSourceTitle || "\\u6700\\u65b0\\u8cc7\\u6599";
  document.querySelector("#resultStatus").textContent = `\\u76ee\\u524d\\u986f\\u793a ${filtered.length} \\u7b46\\uff0f${sourceTitle} \\u5171 ${messages.length} \\u7b46`;
  rowsBody.innerHTML = filtered.length
    ? filtered.map((item) => `
      <tr>
        <td data-label="\\u6642\\u9593">${escapeHtml(item[FIELD_TIME])}</td>
        <td data-label="\\u516c\\u53f8\\u4ee3\\u865f">${escapeHtml(item[FIELD_COMPANY_ID])}</td>
        <td data-label="\\u516c\\u53f8\\u7c21\\u7a31">${escapeHtml(item[FIELD_COMPANY_NAME])}</td>
        <td data-label="\\u4e3b\\u65e8\\u8207\\u8a73\\u7d30\\u5167\\u5bb9" class="subject">
          <strong>${escapeHtml(item[FIELD_SUBJECT])}</strong>
          <div class="detail-content">${formatDetail(item[FIELD_DETAIL])}</div>
        </td>
      </tr>
    `).join("")
    : '<tr class="empty-row"><td colspan="4">\\u6c92\\u6709\\u7b26\\u5408\\u689d\\u4ef6\\u7684\\u8cc7\\u6599</td></tr>';
}
async function loadData() {
  const response = await fetch("data/latest.json", { cache: "no-store" });
  const data = await response.json();
  messages = data.messages || [];
  window.siteSourceTitle = data.source_title || "";
  document.querySelector("#generatedAt").textContent = data.generated_at ? new Date(data.generated_at).toLocaleString("zh-TW", { hour12: false }) : "-";
  render();
}
[companyIdFilter, companyNameFilter, subjectFilter].forEach((input) => input.addEventListener("input", render));
sortTimeButton.addEventListener("click", () => {
  newestFirst = !newestFirst;
  sortTimeButton.textContent = newestFirst ? "\\u6700\\u65b0\\u5728\\u524d" : "\\u6700\\u820a\\u5728\\u524d";
  render();
});
loadData().catch(() => {
  document.querySelector("#resultStatus").textContent = "\\u8cc7\\u6599\\u8f09\\u5165\\u5931\\u6557";
  rowsBody.innerHTML = '<tr class="empty-row"><td colspan="4">\\u8cc7\\u6599\\u8f09\\u5165\\u5931\\u6557\\uff0c\\u8acb\\u7a0d\\u5f8c\\u91cd\\u65b0\\u6574\\u7406</td></tr>';
});
"""


if __name__ == "__main__":
    raise SystemExit(main())
