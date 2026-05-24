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
- `LINE_CHANNEL_ACCESS_TOKEN`: LINE Messaging API channel access token
- `LINE_NOTIFY_ENABLED`: set to `true` to send LINE notifications
- `LINE_NOTIFY_MAX_INDIVIDUAL`: maximum individual messages to send per sync, for example `10`

Do not commit `service-account.json` to GitHub.

## LINE Broadcast Notifications

GitHub Actions uses `LINE_NOTIFY_MODE=broadcast`, so every user who added the LINE Official Account as a friend can receive new MOPS notifications.

`LINE_TARGET_IDS` is no longer required for cloud notifications unless you later change the workflow back to push mode.

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
