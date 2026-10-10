# Manual findings (observed by hand in the Immich web UI, v3.3.1)

These were found by hand before ShutterStorm existed. The simulator reproduces them, and ShutterStorm's
automated runs confirmed M-01, M-02 and M-03 through the API (see [FINDINGS_REPORT.md](FINDINGS_REPORT.md)).

### M-01: ML hung (`docker pause immich_machine_learning`)
Smart search spinner hangs indefinitely with no error. Timeline works. After unpause the stuck request ends in an
error; new searches work instantly.

### M-02: ML down (`docker stop immich_machine_learning`)
Immediate toast "Failed to search smart (Immich Server Error)" plus a misleading "No results, try a synonym" page.
Timeline works. Recovers instantly after start.

### M-03: Redis hung (`docker pause immich_redis`)
Web upload reaches 100% but the dialog stays at "Processed 0/1, Uploaded 0" with no error. After refresh the asset
exists with an "Error loading image" placeholder (no thumbnail); the original opens. After unpause the thumbnail is
generated immediately. No data loss.

### Pattern
**A hung dependency is handled much worse than a down one** (no timeouts surfaced to users).
