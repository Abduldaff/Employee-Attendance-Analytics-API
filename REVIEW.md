# REVIEW.md

Defects found in the starter `app/main.py` and how they were fixed.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Uses `punch_in.replace(hour=..)` in the datetime's own zone (UTC from Mongo), not IST; no overnight handling; no second truncation | Punch-in 00:30 IST (19:00 UTC the day before) is compared against the wrong day's shift start | Build the shift start as an IST datetime for the record's `date` (R1) and subtract |
| 2 | `compute_late_minutes` | Floors to minutes *then* tests `> 10`, so 09:40:01 gives 10 -> returns 0 | Shift 09:30, punch-in 09:40:01 must give 10, gives 0 | Compare **seconds** (`> 600`), then floor to minutes |
| 3 | `compute_work_hours` | Python `round()` is banker's rounding on binary floats; no seconds truncation | 8h 7m 30s = 8.125 h returns 8.12, spec wants 8.13 (R4) | `Decimal` arithmetic with `ROUND_HALF_UP` |
| 4 | `compute_overtime` | No 30-minute minimum (R3); uses `date_str` as the end date so overnight shifts end a day too early; naive/aware mix | Leaving 10 min late gives overtime 10 instead of 0; 22:00-06:00 shift gives huge/negative values | Shift end built as IST datetime, +1 day when overnight; return 0 below 30 |
| 5 | `/health` | Always returns ok even when MongoDB is down | Stop MongoDB, still 200 | Ping MongoDB, return 503 on failure |
| 6 | `MongoClient(...)` | Not `tz_aware`: naive datetimes, so comparisons and conversions depend on the server's local zone | Epoch conversion shifts by the server's UTC offset | `tz_aware=True`, all datetimes are UTC-aware |
| 7 | `create_employee` | check-then-insert with no unique index (race); `datetime.now()` is local naive time; returns a datetime instead of epoch ms; almost no validation | Two parallel creates both return 201; `emp_code="X"`, bad email, `shift_start == shift_end` accepted | Unique index + catch `DuplicateKeyError` -> 409; UTC ms timestamp; pydantic patterns/validators -> 422 |
| 8 | `list_employees` | `skip = page * page_size` skips the first page; `total` counts the whole collection; no sort; no page limits | Page 1 starts at the 21st employee; filter + total mismatch | `(page-1)*page_size`, `count_documents(filter)`, sort by `emp_code`, `Query(ge=1, le=100)` |
| 9 | `punch_in` | Unknown employee -> `emp` is `None` -> `TypeError` -> 500 (should be 404); `punched_at` unvalidated (seconds, floats) and `if body.punched_at` treats 0 as missing; any `status` accepted | `POST` with unknown `emp_code`; `punched_at: 1783312500` | 404 check; strict epoch-ms type with range; status limited to the three presence values |
| 10 | `punch_in` | Date comes from the server-local `ts.date()`, not IST, ignores overnight shifts (R1); seconds not truncated | 02:00 IST punch-in on a 22:00-06:00 shift gets the wrong `date` | `attendance_date()` implements R1 in IST |
| 11 | `punch_in` | check-then-insert race: parallel punch-ins both succeed | 10 parallel requests -> several 201 | Unique index `(emp_code, date)` + `DuplicateKeyError` -> 409 |
| 12 | `punch_in` response | Returns `id` (from `_id`) and raw datetimes | Response contains `id`, `punch_in` is a date string | `record_out()` strips `_id` and converts instants to epoch ms |
| 13 | `list_attendance` | Loads the whole collection and sorts/pages in Python; sorts by `date` only (no `emp_code` tie-break); no index | 100k docs: slow, memory heavy; order unstable for same-date rows | DB-side `sort/skip/limit`, `count_documents`, indexes `(date desc, emp_code)` etc. |
| 14 | `list_attendance` | No validation: `date_from > date_to`, bad dates, unknown status accepted; `page`/`page_size` unbounded; returns `_id` as `id`; legacy docs without `half_day`/`history` would break clients | `date_from=2026-07-05&date_to=2026-07-01` returns 200 | 422 for bad ranges/dates, `Status` literal, `Query` limits, `record_out()` defaults |
| 15 | startup | No indexes are created | `explain` shows COLLSCAN | `ensure_indexes()` in the lifespan hook (idempotent) |

**Looked at and judged fine:** `load_dotenv()` does not override real environment variables by default, so the
"environment wins" rule already holds. The `try`-less `insert_one` path in `create_employee` was fine once the unique index exists.
