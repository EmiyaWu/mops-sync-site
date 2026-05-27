# Free Cloud Deployment Guide

## Architecture

- Google Cloud Scheduler triggers GitHub Actions every 5 minutes through the `workflow_dispatch` API.
- GitHub Actions still keeps its own schedule as a fallback, but Google Cloud Scheduler is the primary scheduler.
- The job always runs `python sync_once_output.py` to check MOPS and update Google Sheet.
- The public site is exported and deployed only when new MOPS rows are written, unless `force_deploy` is enabled manually.
- Google Sheet keeps the full internal dataset.
- `public/` contains only the semi-public static website.
- Cloudflare Pages deploys the static website.

## GitHub Actions Secrets

In the GitHub repository, open `Settings -> Secrets and variables -> Actions`, then add:

- `GOOGLE_SERVICE_ACCOUNT_JSON`: the full service account JSON file content
- `MOPS_SHEET_ID`: the Google Sheet ID
- `CLOUDFLARE_API_TOKEN`: Cloudflare Pages deploy token
- `CLOUDFLARE_ACCOUNT_ID`: Cloudflare account ID
- `CLOUDFLARE_PROJECT_NAME`: Cloudflare Pages project name
- `TELEGRAM_BOT_TOKEN`: Telegram Bot token from BotFather
- `TELEGRAM_CHAT_IDS`: Telegram chat IDs or channel usernames, separated by commas
- `TELEGRAM_NOTIFY_ENABLED`: set to `true` to send Telegram notifications
- `TELEGRAM_NOTIFY_MAX_ITEMS`: optional maximum rows shown in one Telegram summary, for example `10`

Do not commit `service-account.json` to GitHub.

## Telegram Notifications

GitHub Actions sends a single Telegram summary message after new MOPS rows are successfully written to Google Sheet.

Example:

```text
目前有 7 筆新的重大即時訊息!

1. 公司名:XXX
主旨:XXX

2. 公司名:YYY
主旨:YYY

查看網站:https://mops-sync-site.pages.dev/
```

Use one or more chat IDs in `TELEGRAM_CHAT_IDS`. A public channel can use a username such as `@your_channel_name`; private groups and channels usually need the numeric chat ID.

## Google Cloud Scheduler

Use `GOOGLE_CLOUD_SCHEDULER.md` to create the external 5-minute scheduler.

The scheduler calls:

```text
POST https://api.github.com/repos/EmiyaWu/mops-sync-site/actions/workflows/sync-and-deploy.yml/dispatches
```

with this JSON body:

```json
{"ref":"main"}
```

This intentionally does not pass `force_deploy`, so routine 5-minute checks do not redeploy Cloudflare Pages when there are no new rows.

To manually rebuild the website without new rows, open GitHub Actions, click `Run workflow`, and enable `force_deploy`.

## Public Website Fields

The website exposes only:

- Date
- Time
- Company ID
- Company abbreviation
- Subject
- Detail content

The website does not expose:

- Data key
- Fetched-at timestamp

## Local Preview

```powershell
python mos_s.py once --export-site public
python -m http.server 8000 -d public
```

Open:

```text
http://localhost:8000
```
