# DECISIONS.md

1. **Indexes.** Unique `(emp_code, date)` is the natural key: it makes punch-in race-safe and serves PATCH, monthly and per-employee lists. `(date desc, emp_code)` serves the default list sort and the month range scans of the leaderboard. `(status, date desc, emp_code)` serves the status filter. `(emp_code, punch_in desc)` finds the open record at punch-out. On employees: unique `emp_code`, plus `(department, joined_on)` and `joined_on` for headcount. Rejected: a separate index on `late_minutes`; the date range already narrows the scan.
2. **Punch-in race.** Both requests build the same document and call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept only one; the other gets `DuplicateKeyError`, which I turn into 409. No read before write.
3. **Ties.** Ranking uses `$rank`, so tied employees share a rank and the next rank is skipped. The limit filters on `rank <= limit` after ranking, so everybody tied at the cutoff is returned.
4. **Headcount.** The summary pipeline starts from `employees`, not from the logs, and left-joins each employee's logs with `$lookup`. An employee with no logs gets an empty array and still adds 1 to headcount.
5. **100x data.** Pre-aggregate monthly numbers per employee into a summary collection (updated on write) instead of grouping raw logs on every request, and archive or shard old months by `date`.
