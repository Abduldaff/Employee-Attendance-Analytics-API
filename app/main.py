from __future__ import annotations

"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded for convenience; real environment variables win)

Layout of this single file (read it top to bottom):
  1. Setup            - Mongo client, indexes (created at startup, idempotent)
  2. Time + rule helpers - R1..R5 as small pure functions (easy to unit test)
  3. Serialisation    - Mongo documents -> API JSON (epoch milliseconds, no _id)
  4. Request models   - pydantic validation (bad input => 422, never 500)
  5. Employees, punch-in/out, list, PATCH (regularize)
  6. Analytics        - MongoDB aggregation pipelines
  7. Explain endpoint - proves the queries use indexes
"""
import calendar
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, time as time_cls, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False (default): real environment variables take priority over .env
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Setup
# --------------------------------------------------------------------------- #
# tz_aware=True makes PyMongo return *aware* UTC datetimes instead of naive ones.
# Without it every comparison / conversion silently depends on the server's local zone.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]  # R6
ALL_STATUSES = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


def ensure_indexes() -> None:
    """Create every index we need. create_index is idempotent when the spec is identical."""
    # employees
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")
    employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    # attendance_logs
    # (1) the natural key: one record per employee per day. Also makes punch-in race-safe.
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date")
    # (2) list sorted by date DESC, emp_code ASC (mixed directions need this exact index) + month range scans
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp")
    # (3) status filter + the same sort
    logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp")
    # (4) punch-out: "latest record of this employee with punch_in <= t"
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Mongo may still be starting; retry for up to ~10 s so we are ready well inside the 20 s limit.
    for attempt in range(10):
        try:
            ensure_indexes()
            break
        except PyMongoError as exc:  # pragma: no cover
            log.warning("index creation failed (attempt %s): %s", attempt + 1, exc)
            time.sleep(1)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse(url="/docs")


# --------------------------------------------------------------------------- #
# 2. Time + rule helpers
# --------------------------------------------------------------------------- #
def unprocessable(loc: str, msg: str) -> HTTPException:
    """422 in FastAPI's own error shape ({"detail": [ {loc, msg, type} ]})."""
    return HTTPException(422, detail=[{"loc": ["body", loc], "msg": msg, "type": "value_error"}])


def ms_to_dt(ms: int) -> datetime:
    """epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """aware (or naive-UTC) datetime -> epoch ms. None stays None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp()) * 1000 + dt.microsecond // 1000


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_hhmm(value: str) -> time_cls:
    h, m = value.split(":")
    return time_cls(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM strings compare correctly


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_date(value: str, loc: str = "date") -> date_cls:
    if not DATE_RE.match(value):
        raise HTTPException(422, detail=[{"loc": ["query", loc], "msg": "must be YYYY-MM-DD", "type": "value_error"}])
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, detail=[{"loc": ["query", loc], "msg": "not a real calendar date", "type": "value_error"}])


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date a punch-in belongs to (overnight shifts may belong to yesterday)."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_start_dt(date_str: str, shift_start: str) -> datetime:
    d = date_cls.fromisoformat(date_str)
    return datetime.combine(d, parse_hhmm(shift_start), tzinfo=IST)


def shift_end_dt(date_str: str, shift_start: str, shift_end: str) -> datetime:
    """The shift ends on the NEXT calendar day for overnight shifts (R1)."""
    d = date_cls.fromisoformat(date_str)
    if is_overnight(shift_start, shift_end):
        d += timedelta(days=1)
    return datetime.combine(d, parse_hhmm(shift_end), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly MORE than 10:00 after shift start; value counts from shift start (floor)."""
    seconds = int((punch_in - shift_start_dt(date_str, shift_start)).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, only counted if >= 30."""
    seconds = int((punch_out - shift_end_dt(date_str, shift_start, shift_end)).total_seconds())
    minutes = seconds // 60 if seconds > 0 else 0
    return minutes if minutes >= 30 else 0


def half_up(value: float | Decimal | int, places: int) -> float:
    """Round half-UP (what the spec asks for). Python's round() is banker's rounding on binary floats."""
    if not isinstance(value, Decimal):
        # round(.., 9) first removes float noise such as 8.124999999999999 so real ties are seen as ties
        value = Decimal(str(round(float(value), 9)))
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals half-up. Both inputs already truncated to whole seconds."""
    seconds = int((punch_out - punch_in).total_seconds())
    return half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: judged on the ROUNDED work hours."""
    return work_hours is not None and work_hours < 4.5


def derive(status: str, punch_in, punch_out, date_str: str, shift_start: str, shift_end: str) -> dict:
    """All derived fields for one record, from the final times. Used by punch-out and PATCH."""
    if status not in PRESENT_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "late_minutes": compute_late_minutes(punch_in, date_str, shift_start),
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, date_str, shift_start, shift_end)
        out["half_day"] = compute_half_day(out["work_hours"])
    return out


def month_bounds(month: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31')  (dates are plain strings, so range compares work)."""
    y, m = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"


def count_weekdays(start: date_cls, end: date_cls) -> int:
    """Mon-Fri days in [start, end] (R7). At most 31 iterations - this loops over days, not over documents."""
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


# --------------------------------------------------------------------------- #
# 3. Serialisation: Mongo doc -> API JSON
# --------------------------------------------------------------------------- #
def history_out(entry: dict) -> dict:
    changes = {}
    for field, ch in (entry.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):
            changes[field] = {"from": dt_to_ms(ch.get("from")), "to": dt_to_ms(ch.get("to"))}
        else:
            changes[field] = {"from": ch.get("from"), "to": ch.get("to")}
    return {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(d: dict) -> dict:
    """Missing history / half_day / late / overtime in legacy data are treated as empty / false / 0."""
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": dt_to_ms(d.get("punch_in")),
        "punch_out": dt_to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": [history_out(h) for h in (d.get("history") or [])],
    }


def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d["name"],
        "email": d["email"],
        "department": d["department"],
        "shift_start": d["shift_start"],
        "shift_end": d["shift_end"],
        "joined_on": d["joined_on"],
        "created_at": dt_to_ms(d.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# 4. Request models
# --------------------------------------------------------------------------- #
# strict=True: 1.5, "1783312500000" and True are rejected instead of being coerced. Range => seconds-looking values fail.
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("must be YYYY-MM-DD")
        date_cls.fromisoformat(v)  # raises ValueError for 2026-02-30
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


MonthQ = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")


# --------------------------------------------------------------------------- #
# 5. Health, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    now = datetime.now(UTC)
    doc["created_at"] = now.replace(microsecond=now.microsecond // 1000 * 1000)  # Mongo keeps ms precision
    try:
        employees.insert_one(doc)  # the UNIQUE index decides; no check-then-insert race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = employees.count_documents(q)  # total of the FILTERED set (R10)
    cur = employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


def get_employee_or_404(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    date_str = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, date_str, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): of N parallel requests exactly one insert succeeds
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    out_dt = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # most recent record whose punch_in is at or before the punch-out (works across midnight: the
    # overnight record has date = yesterday but is still the latest punch_in)
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": out_dt}}, sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = rec["punch_in"]
    if out_dt <= p_in:
        raise unprocessable("punched_at", "must be after punch-in")
    if out_dt - p_in > timedelta(hours=24):
        raise unprocessable("punched_at", "more than 24 hours after punch-in")
    fields = derive(rec["status"], p_in, out_dt, rec["date"], emp["shift_start"], emp["shift_end"])
    fields["punch_out"] = out_dt
    # Atomic compare-and-set: only succeeds while punch_out is still null. Two parallel requests => one 200, one 409.
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None}, {"$set": fields}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_query(emp_code, date_from, date_to, status, page, page_size):
    """Builds (filter, sort, skip, limit). Shared by GET /attendance and the explain endpoint."""
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    f = parse_date(date_from, "date_from") if date_from else None
    t = parse_date(date_to, "date_to") if date_to else None
    if f and t and f > t:
        raise HTTPException(422, detail=[{"loc": ["query", "date_from"], "msg": "date_from after date_to", "type": "value_error"}])
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    sort = [("date", DESCENDING), ("emp_code", ASCENDING)]
    return q, sort, (page - 1) * page_size, page_size


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q, sort, skip, limit = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    total = logs.count_documents(q)
    items = [record_out(d) for d in logs.find(q).sort(sort).skip(skip).limit(limit)]  # DB sorts + pages
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date")
    emp = get_employee_or_404(emp_code)
    sent = body.model_fields_set  # which optional fields the client actually sent

    for _attempt in range(5):  # optimistic concurrency: re-read and retry if somebody changed the record meanwhile
        rec = logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")

        new_status = body.status if "status" in sent and body.status else rec["status"]
        if new_status in ("ABSENT", "LEAVE"):
            if "punch_in" in sent or "punch_out" in sent:
                raise unprocessable("punch_in", f"{new_status} clears punch times; do not send them")
            new_in = new_out = None
        else:
            new_in = ms_to_dt(body.punch_in) if "punch_in" in sent else rec.get("punch_in")
            new_out = ms_to_dt(body.punch_out) if "punch_out" in sent else rec.get("punch_out")
            if new_in is None:
                raise unprocessable("punch_in", "a presence status requires a punch_in")
            if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
                raise unprocessable("punch_in", "punch_in must fall on the record's attendance date")
            if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
                raise unprocessable("punch_out", "must be after punch_in and within 24 hours")

        new = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
        new.update(derive(new_status, new_in, new_out, date, emp["shift_start"], emp["shift_end"]))

        old = {
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            "work_hours": rec.get("work_hours"),
            "late_minutes": rec.get("late_minutes") or 0,
            "overtime_minutes": rec.get("overtime_minutes") or 0,
            "half_day": bool(rec.get("half_day", False)),
        }
        changes = {k: {"from": old[k], "to": new[k]} for k in new if new[k] != old[k]}
        if not changes:
            raise unprocessable("body", "request changes nothing")

        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

        # The write only matches if the record is still exactly as we read it (state + history length).
        hist = rec.get("history")
        guard: dict[str, Any] = {
            "_id": rec["_id"],
            "status": old["status"],
            "punch_in": old["punch_in"],
            "punch_out": old["punch_out"],
            "history": {"$size": len(hist)} if hist is not None else {"$exists": False},
        }
        updated = logs.find_one_and_update(
            guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
        )
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "record was modified concurrently, please retry")


# --------------------------------------------------------------------------- #
# 6. Analytics (MongoDB aggregation pipelines)
# --------------------------------------------------------------------------- #
# Reusable expression fragments. They run INSIDE MongoDB on the stored values (we never recompute from punch times).
IS_PRESENT = {"$in": ["$status", PRESENT_STATUSES]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
HALF = {"$ifNull": ["$half_day", False]}
LATE = {"$ifNull": ["$late_minutes", 0]}
OVERTIME = {"$ifNull": ["$overtime_minutes", 0]}
PRESENT_VALUE = {"$cond": [IS_PRESENT, {"$cond": [HALF, 0.5, 1]}, 0]}  # present, half day = 0.5
PRESENT_WEEKDAY_VALUE = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, {"$cond": [HALF, 0.5, 1]}, 0]}  # R7
IS_LATE = {"$gt": [LATE, 0]}
HAS_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def monthly_pipeline(emp_code: str, month: str) -> list:
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},  # uses uniq_emp_date
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": PRESENT_WEEKDAY_VALUE},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
                "total_late_minutes": {"$sum": LATE},
                "total_overtime_minutes": {"$sum": OVERTIME},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = MonthQ):
    emp = get_employee_or_404(emp_code)
    start, end = month_bounds(month)
    # R7: working days run from the later of (first of month, joined_on) to month end
    first = max(date_cls.fromisoformat(start), date_cls.fromisoformat(emp["joined_on"]))
    last = date_cls.fromisoformat(end)
    working_days = count_weekdays(first, last) if first <= last else 0
    agg = next(logs.aggregate(monthly_pipeline(emp_code, month)), None) or {}
    present = agg.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": half_up(present, 2),
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": half_up(present / working_days * 100, 2) if working_days else None,
    }


def summary_pipeline(month: str, department: Optional[str]) -> list:
    """Starts from EMPLOYEES (so people with zero logs still count) and left-joins their logs for the month."""
    start, end = month_bounds(month)
    match: dict[str, Any] = {"joined_on": {"$lte": end}}  # R9: joined on or before month end
    if department:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {"$sum": PRESENT_WEEKDAY_VALUE},
                            "wh_sum": {"$sum": {"$cond": [HAS_HOURS, "$work_hours", 0]}},
                            "wh_cnt": {"$sum": {"$cond": [HAS_HOURS, 1, 0]}},
                            "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
                            "total_late_minutes": {"$sum": {"$cond": [IS_LATE, LATE, 0]}},
                            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                        }
                    },
                ],
                "as": "agg",  # [] for an employee with no logs, otherwise one element
            }
        },
        {"$addFields": {"a": {"$ifNull": [{"$arrayElemAt": ["$agg", 0]}, {}]}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},  # every matched employee, with or without logs
                "present_days": {"$sum": {"$ifNull": ["$a.present_days", 0]}},
                "wh_sum": {"$sum": {"$ifNull": ["$a.wh_sum", 0]}},
                "wh_cnt": {"$sum": {"$ifNull": ["$a.wh_cnt", 0]}},  # average over RECORDS, not per-employee averages
                "late_count": {"$sum": {"$ifNull": ["$a.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$a.total_late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$a.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$a.on_duty_count", 0]}},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {"$cond": [{"$gt": ["$wh_cnt", 0]}, {"$divide": ["$wh_sum", "$wh_cnt"]}, None]},
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = MonthQ, department: Optional[str] = None):
    items = []
    for row in employees.aggregate(summary_pipeline(month, department)):
        row["present_days"] = half_up(row["present_days"], 2)
        row["avg_work_hours"] = None if row["avg_work_hours"] is None else half_up(row["avg_work_hours"], 2)
        items.append(row)
    return {"month": month, "items": items}


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},  # uses date_desc_emp
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # drops logs whose employee does not exist
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  # rank INSIDE the department
    pipeline += [
        # $rank = standard competition ranking: ties share a rank and the next rank is skipped (1,2,2,4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = MonthQ, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    return {"month": month, "items": list(logs.aggregate(leaderboard_pipeline(month, limit, department)))}


def trend_inputs(department: str) -> tuple[list[str], list[str]]:
    """(emp_codes, sorted joined_on strings) of the department. 404 if it has no employees."""
    docs = list(employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not docs:
        raise HTTPException(404, "unknown department")
    return [d["emp_code"] for d in docs], sorted(d["joined_on"] for d in docs)


def trend_range(from_: str, to: str) -> tuple[date_cls, date_cls]:
    f, t = parse_date(from_, "from"), parse_date(to, "to")
    if t < f:
        raise HTTPException(422, detail=[{"loc": ["query", "to"], "msg": "to is before from", "type": "value_error"}])
    if (t - f).days + 1 > 92:
        raise HTTPException(422, detail=[{"loc": ["query", "to"], "msg": "range exceeds 92 days", "type": "value_error"}])
    return f, t


def trend_pipeline(codes: list[str], joined: list[str], f: date_cls, t: date_cls) -> list:
    from_dt = datetime(f.year, f.month, f.day, tzinfo=UTC)
    to_dt = datetime(t.year, t.month, t.day, tzinfo=UTC)
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": f.isoformat(), "$lte": t.isoformat()}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": PRESENT_VALUE}, "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}}}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # gap filling: one document per day. Upper bound of explicit bounds is exclusive, so go one day past `to`
        # and trim afterwards (works whether the bound is treated as inclusive or exclusive).
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt + timedelta(days=1)]}}},
        {"$match": {"d": {"$lte": to_dt}}},
        {
            "$set": {
                "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$set": {
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
                # R9 headcount per day: how many of the department's join dates are <= this day
                "headcount": {"$size": {"$filter": {"input": {"$literal": joined}, "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}},
            }
        },
        {
            "$set": {
                "rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        # window = this row + up to 6 previous rows INSIDE the range; $avg ignores null rates, and is null if all are null
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {"moving": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "rate": 1,
                "moving": 1,
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query()):
    f, t = trend_range(from_, to)
    codes, joined = trend_inputs(department)
    items = []
    for row in logs.aggregate(trend_pipeline(codes, joined, f, t)):
        items.append(
            {
                "date": row["date"],
                "is_working_day": row["is_working_day"],
                "headcount": row["headcount"],
                "present_count": half_up(row["present_count"], 2),
                "late_count": row["late_count"],
                "attendance_rate": None if row["rate"] is None else half_up(row["rate"], 4),
                "moving_avg_7d": None if row["moving"] is None else half_up(row["moving"], 4),
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 7. Explain endpoint
# --------------------------------------------------------------------------- #
def need(value, name: str):
    if value is None or value == "":
        raise HTTPException(422, detail=[{"loc": ["query", name], "msg": "field required", "type": "missing"}])
    return value


def run_explain(command: dict) -> dict:
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result))  # BSON types (dates, ObjectId, Int64) -> plain JSON


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Every branch builds the query with the SAME builder function the real endpoint uses.
    if endpoint == "attendance_list":
        q, sort, skip, lim = attendance_query(emp_code, date_from, date_to, status, page, page_size)
        coll, cmd = "attendance_logs", {"find": "attendance_logs", "filter": q, "sort": dict(sort), "skip": skip, "limit": lim}
    elif endpoint == "employee_monthly":
        pipeline = monthly_pipeline(need(emp_code, "emp_code"), need(month, "month"))
        coll, cmd = "attendance_logs", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
    elif endpoint == "department_summary":
        pipeline = summary_pipeline(need(month, "month"), department)
        coll, cmd = "employees", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
    elif endpoint == "late_leaderboard":
        pipeline = leaderboard_pipeline(need(month, "month"), limit, department)
        coll, cmd = "attendance_logs", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
    else:  # department_trend
        dept, f_s, t_s = need(department, "department"), need(from_, "from"), need(to, "to")
        f, t = trend_range(f_s, t_s)
        codes, joined = trend_inputs(dept)
        pipeline = trend_pipeline(codes, joined, f, t)
        coll, cmd = "attendance_logs", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
    return {"endpoint": endpoint, "collection": coll, "explain": run_explain(cmd)}
