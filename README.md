# Employee Attendance & Analytics API

A FastAPI service for employee records, attendance punch-in/out, regularization, and department/employee analytics. It
uses MongoDB for persistence; it is not a machine-learning model.

## Requirements

- Python 3.10 or newer
- MongoDB 6 or newer, running and reachable at `mongodb://localhost:27017` (the analytics use MongoDB aggregation
  operators introduced in MongoDB 5.1)

Set `MONGO_URI` and `MONGO_DB` in a local `.env` file if your MongoDB connection differs from the defaults in
[.env.example](./.env.example). Real environment variables take precedence over `.env`.

## Run on Windows (PowerShell)

From the project directory:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000 --reload
```

If Python 3.11 is not installed, use another Python 3.10+ version with `py -3.10` (or its installed version). Ensure
MongoDB is running before starting the API. Indexes are created automatically when the app starts. In another
PowerShell window, check readiness and open the interactive API documentation:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Start-Process http://127.0.0.1:8000/docs
```

The health endpoint returns `{"status":"ok"}` only when MongoDB responds.

## Run on macOS/Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

## Load the sample data (optional)

With the API stopped, run:

```powershell
.\.venv\Scripts\python.exe sample_seed.py
```

On macOS/Linux, run `python sample_seed.py` from the project directory.

This replaces all documents in the configured `employees` and `attendance_logs` collections. Do not run it against a
database containing data you need to keep. The sample records use July 2026 dates; use those dates when trying the
sample analytics.

## Use the API

In PowerShell, send JSON requests with `Invoke-RestMethod`. This example creates an employee and records a live
punch-in and punch-out; omit `punched_at` to use the server's current time:

```powershell
$base = "http://127.0.0.1:8000"
$employee = @{
    emp_code = "EMP0099"
    name = "Jordan Lee"
    email = "jordan.lee@example.com"
    department = "Engineering"
    shift_start = "09:30"
    shift_end = "18:30"
    joined_on = (Get-Date -Format "yyyy-MM-dd")
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$base/employees" -ContentType "application/json" -Body $employee

$punch = @{ emp_code = "EMP0099" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$base/attendance/punch-in" -ContentType "application/json" -Body $punch
Invoke-RestMethod -Method Post -Uri "$base/attendance/punch-out" -ContentType "application/json" -Body $punch
```

`emp_code` must be unique. For a test or backdated attendance record, supply `punched_at` as an integer Unix epoch
timestamp in **milliseconds** (not seconds). The API returns instants in the same format.

Common endpoints:

| Purpose | Method and path |
|---|---|
| Readiness | `GET /health` |
| Create/list employees | `POST /employees`, `GET /employees` |
| Punch in/out | `POST /attendance/punch-in`, `POST /attendance/punch-out` |
| Search attendance | `GET /attendance` |
| Correct a record (audited) | `PATCH /attendance/{emp_code}/{date}` |
| Employee monthly analytics | `GET /analytics/employees/{emp_code}/monthly?month=YYYY-MM` |
| Department summary | `GET /analytics/departments/summary?month=YYYY-MM` |
| Late leaderboard | `GET /analytics/leaderboard/late?month=YYYY-MM` |
| Department daily trend | `GET /analytics/departments/{department}/trend?from=YYYY-MM-DD&to=YYYY-MM-DD` |

Interactive request/response examples and full schemas are at `http://127.0.0.1:8000/docs`; the API contract is also
in [openapi.yaml](./openapi.yaml). Validation failures return 422, unknown employees/records return 404, and duplicate
employee or attendance records return 409.

## Tests

Install the test dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Run the complete suite against a dedicated MongoDB test database (the tests clear this database between cases):

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_test"
.\.venv\Scripts\python.exe -m pytest -q
```

Use a test-only database name: the test fixture drops the configured database. The suite includes analytics and
query-plan checks that require MongoDB 6+. If `MONGO_URI` is unset, the CRUD and rule tests use mongomock; the
MongoDB-specific analytics/oracle tests are skipped. `tests/test_oracle.py` checks analytics against a plain-Python
implementation using randomized data, including ties, weekends, no-log employees, and mid-month joiners.

## Implementation notes

- Attendance timestamps are stored as UTC instants; shifts and attendance dates use IST.
- Attendance rules and API schemas are documented in [openapi.yaml](./openapi.yaml); persisted document shapes are in
  [DATA_MODEL.md](./DATA_MODEL.md).
- Moving averages use unrounded daily rates; only reported values are rounded.
- Regularization retries up to five times if concurrent updates occur, then returns 409. Each successful correction
  appends an audit history entry.
