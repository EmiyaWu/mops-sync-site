MOPS Sync Runner

How to use:
1. Double-click validate_mops_sync.bat to check MOPS and Google Sheet access.
2. If validation succeeds, double-click run_mops_sync.bat to start syncing.
3. Keep the window open. The runner checks for new rows every 3 minutes.
4. To stop syncing, close the window or press Ctrl+C.

Notes:
- service-account.json is a write credential for Google Sheet. Do not publish it.
- Daily worksheets are sorted newest first.
- Only the latest 7 daily worksheets stay visible; older daily worksheets are hidden.
