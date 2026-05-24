# Free Cloud Deployment Guide

## Architecture

- Google Cloud Scheduler triggers GitHub Actions every 5 minutes through the `workflow_dispatch` API.
- GitHub Actions still keeps its own schedule as a fallback, but Google Cloud Scheduler is the primary scheduler.
- The job runs `python sync_once_output.py`, then exports the public site with `python site_export.py`.
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

Do not commit `service-account.json` to GitHub.

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
