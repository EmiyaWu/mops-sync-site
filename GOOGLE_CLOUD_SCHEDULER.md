# Google Cloud Scheduler for MOPS Sync

This project uses Google Cloud Scheduler as the primary 5-minute trigger for GitHub Actions.

GitHub Actions `schedule` is kept as a fallback, but it can be delayed or skipped. Google Cloud Scheduler calls the GitHub `workflow_dispatch` API directly, which is more stable for this use case.

## 1. Create a GitHub token

Create a GitHub fine-grained personal access token:

1. Open GitHub `Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens`.
2. Select repository access for `EmiyaWu/mops-sync-site` only.
3. Add repository permission:
   - `Actions`: `Read and write`
4. Generate the token and copy it once.

Do not commit this token to the repository.

## 2. Test the dispatch API locally

PowerShell:

```powershell
$env:GITHUB_DISPATCH_TOKEN="<YOUR_GITHUB_TOKEN>"

$headers = @{
  Authorization = "Bearer $env:GITHUB_DISPATCH_TOKEN"
  Accept = "application/vnd.github+json"
  "X-GitHub-Api-Version" = "2022-11-28"
}

$body = '{"ref":"main"}'

Invoke-RestMethod `
  -Uri "https://api.github.com/repos/EmiyaWu/mops-sync-site/actions/workflows/sync-and-deploy.yml/dispatches" `
  -Method Post `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
```

A successful response is usually empty. Then open GitHub Actions and confirm a new workflow run appears.

## 3. Create the Cloud Scheduler job

In Google Cloud Console:

1. Open `Cloud Scheduler`.
2. Click `Create job`.
3. Set:
   - Name: `mops-sync-github-dispatch`
   - Region: use any nearby region, for example `asia-east1`
   - Frequency: `*/5 * * * *`
   - Time zone: `Asia/Taipei`
4. Target type: `HTTP`
5. URL:

```text
https://api.github.com/repos/EmiyaWu/mops-sync-site/actions/workflows/sync-and-deploy.yml/dispatches
```

6. HTTP method: `POST`
7. Headers:

```text
Authorization: Bearer <YOUR_GITHUB_TOKEN>
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
User-Agent: google-cloud-scheduler-mops-sync
```

8. Body:

```json
{"ref":"main"}
```

9. Authentication: leave disabled. The GitHub token in the `Authorization` header is the authentication for this request.

## 4. Validate

1. Click `Force run` in Cloud Scheduler.
2. Open GitHub Actions.
3. Confirm a new `workflow_dispatch` run appears and finishes with a green check.
4. Observe 15 to 30 minutes. Runs should appear about every 5 minutes.
5. Confirm:
   - Google Sheet does not duplicate rows.
   - LINE only sends messages when new MOPS data is written.
   - The website updates normally.

## Notes

- If both GitHub `schedule` and Google Cloud Scheduler trigger at similar times, `concurrency` prevents overlapping runs.
- If duplicate workflow runs become noisy, remove the GitHub `schedule` trigger later and keep only `workflow_dispatch`.
- Keep the GitHub token private. If it is exposed, revoke it and create a new token immediately.
