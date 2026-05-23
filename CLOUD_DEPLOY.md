# Free Cloud Deployment Guide

## Architecture

- GitHub Actions runs every 5 minutes.
- The job runs `python mos_s.py once --export-site public`.
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

## Public Website Fields

The website exposes only:

- Date
- Time
- Company ID
- Company abbreviation
- Subject

The website does not expose:

- Detail content
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
