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


def main() -> int:
    output_dir = Path(os.getenv("SITE_OUTPUT_DIR", "public"))
    sheet_id = os.environ["MOPS_SHEET_ID"]
    credentials_path = prepare_credentials()
    spreadsheet = gspread.service_account(filename=credentials_path).open_by_key(sheet_id)
    now = datetime.now(ZoneInfo(os.getenv("TZ", "Asia/Taipei")))
    rows = read_today_rows(spreadsheet, now)
    messages = rows_to_public_messages(rows)
    export_site(output_dir, messages, now)
    print(f"Exported {len(messages)} rows to {output_dir}")
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


def read_today_rows(spreadsheet, now: datetime) -> list[list[str]]:
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
        messages.append(item)
    return sorted(messages, key=message_sort_key, reverse=True)


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


def export_site(output_dir: Path, messages: list[dict[str, str]], generated_at: datetime) -> None:
    data_dir = output_dir / "data"
    assets_dir = output_dir / "assets"
    data_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "count": len(messages),
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
  <title>MOPS Material Information Dashboard</title>
  <link rel="stylesheet" href="assets/site.css">
</head>
<body>
  <main class="app-shell">
    <section class="topbar">
      <div>
        <p class="eyebrow">MOPS Live Feed</p>
        <h1>Material Information Dashboard</h1>
        <p class="subtitle">A clean public dashboard for MOPS material information, including subject and full detail content.</p>
      </div>
      <div class="metrics">
        <div class="metric"><span>Rows Today</span><strong id="totalCount">0</strong></div>
        <div class="metric wide"><span>Last Updated</span><strong id="generatedAt">-</strong></div>
      </div>
    </section>

    <section class="toolbar">
      <label>Company ID<input id="companyIdFilter" type="search" inputmode="numeric" placeholder="2330"></label>
      <label>Company<input id="companyNameFilter" type="search" placeholder="TSMC"></label>
      <label class="wide-filter">Subject or detail<input id="subjectFilter" type="search" placeholder="Search subject and detail content"></label>
      <button id="sortTimeButton" type="button">Time: Newest</button>
    </section>

    <section class="table-wrap">
      <table>
        <thead><tr><th>Time</th><th>Company ID</th><th>Company</th><th>Subject and detail</th></tr></thead>
        <tbody id="messageRows"><tr class="empty-row"><td colspan="4">Loading...</td></tr></tbody>
      </table>
    </section>
  </main>
  <script src="assets/site.js"></script>
</body>
</html>
"""


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
body { margin: 0; background: var(--bg); color: var(--ink); font-family: "Noto Sans TC", "Microsoft JhengHei", system-ui, sans-serif; }
.app-shell { width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 36px; }
.topbar { display: flex; align-items: end; justify-content: space-between; gap: 20px; padding-bottom: 18px; border-bottom: 1px solid var(--line); }
.eyebrow { margin: 0 0 6px; color: var(--accent-strong); font-size: 13px; font-weight: 700; }
h1 { margin: 0; font-size: 28px; line-height: 1.25; }
.subtitle { max-width: 720px; margin: 10px 0 0; color: var(--muted); font-size: 14px; line-height: 1.65; }
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
td.subject strong { display: block; margin-bottom: 8px; color: #111827; font-size: 15px; }
td.subject p { margin: 0; color: #3f4a59; white-space: pre-wrap; }
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

const FIELD_DATE = "\\u65e5\\u671f";
const FIELD_TIME = "\\u6642\\u9593";
const FIELD_COMPANY_ID = "\\u516c\\u53f8\\u4ee3\\u865f";
const FIELD_COMPANY_NAME = "\\u516c\\u53f8\\u7c21\\u7a31";
const FIELD_SUBJECT = "\\u4e3b\\u65e8";
const FIELD_DETAIL = "\\u8a73\\u7d30\\u5167\\u5bb9";
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
    .filter((item) => `${normalize(item[FIELD_SUBJECT])} ${normalize(item[FIELD_DETAIL])}`.includes(subjectTerm))
    .sort((a, b) => {
      const left = `${a[FIELD_DATE]} ${a[FIELD_TIME]}`;
      const right = `${b[FIELD_DATE]} ${b[FIELD_TIME]}`;
      return newestFirst ? right.localeCompare(left) : left.localeCompare(right);
    });
  totalCount.textContent = String(filtered.length);
  rowsBody.innerHTML = filtered.length
    ? filtered.map((item) => `
      <tr>
        <td data-label="Time">${escapeHtml(item[FIELD_TIME])}</td>
        <td data-label="Company ID">${escapeHtml(item[FIELD_COMPANY_ID])}</td>
        <td data-label="Company">${escapeHtml(item[FIELD_COMPANY_NAME])}</td>
        <td data-label="Subject and detail" class="subject">
          <strong>${escapeHtml(item[FIELD_SUBJECT])}</strong>
          <p>${escapeHtml(item[FIELD_DETAIL])}</p>
        </td>
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
