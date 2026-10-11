# DECISIONS.md

1. **Indexes.**
   - `employees`: `{ emp_code: 1 }` (unique) ensures business key uniqueness and powers quick employee lookups.
   - `attendance_logs`: `{ emp_code: 1, date: 1 }` (unique compound) guarantees atomic single punch-in per day per employee and powers record retrieval.
   - `attendance_logs`: `{ date: 1, department: 1, emp_code: 1 }` and `{ emp_code: 1, date: 1, status: 1 }` ensure index-covered scans for monthly employee analytics, department summaries, and leaderboard pipelines.
   - *Rejected*: Considered indexing individual `punched_at` or `status` standalone, but rejected because queries always filter along with `emp_code` or `date` compound prefixes.

2. **Punch-in race.**
   Both concurrent requests query employee existence and compute the target IST date. Both attempt an atomic insertion into `attendance_logs`. Due to the unique compound index on `{ emp_code: 1, date: 1 }`, the database accepts exactly the first request (returning 201 Created) while the second immediately triggers a `DuplicateKeyError`, caught and converted into a 409 Conflict.

3. **Ties.**
   Using MongoDB's `$rank` window function (`dense: false`), tied employees share the identical rank with the next rank skipped (e.g., 1, 2, 2, 4). When tied at the cutoff boundary, sorting deterministically breaks ties by `emp_code ASC`, returning rows up to `limit` without corrupting rank numbers.

4. **Headcount.**
   The aggregation begins from the `employees` collection filtered by `joined_on <= period_end`. It then performs a `$lookup` onto `attendance_logs` matching the specified month/range. Unmatched employees are preserved, guaranteeing an accurate headcount that includes active staff having zero attendance logs.

5. **One thing you would change for 100x data.**
   Pre-aggregate or roll up daily attendance summaries into a dedicated monthly cache collection via asynchronous change streams or background workers, rather than calculating full-table dynamic aggregation pipelines across multi-million log documents on demand.