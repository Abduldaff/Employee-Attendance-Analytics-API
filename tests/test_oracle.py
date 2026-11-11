"""Cross-check the aggregation pipelines against a slow, obvious pure-Python implementation of the spec.
Needs a REAL MongoDB:   MONGO_URI=mongodb://localhost:27017 python -m pytest tests/test_oracle.py -q
Random data includes ties, weekend records, no-log employees, mid-month joiners, half days, LEAVE/ABSENT, legacy docs."""
import random
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from tests.conftest import m, requires_real_mongo

PRESENT = {"PRESENT", "WFH", "ON_DUTY"}
MONTH = "2026-07"


def build_dataset(seed=7, n_emp=60):
    rnd = random.Random(seed)
    depts = ["Eng", "Sales", "Support", "HR"]
    emps, rows = [], []
    for i in range(1, n_emp + 1):
        joined = rnd.choice(["2026-01-05", "2026-06-15", "2026-07-08", "2026-07-20", "2026-08-03"])
        emps.append({"emp_code": f"EMP{i:04d}", "name": f"N{i}", "email": f"e{i}@x.com", "department": rnd.choice(depts),
                     "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined,
                     "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})
    for e in emps[: int(n_emp * 0.8)]:                   # the last 20% have NO logs at all
        d = date(2026, 6, 25)
        while d <= date(2026, 8, 5):
            if rnd.random() < 0.8:
                st = rnd.choice(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"])
                doc = {"emp_code": e["emp_code"], "date": d.isoformat(), "status": st, "punch_in": None, "punch_out": None,
                       "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
                if st in PRESENT:
                    late = rnd.choice([0, 0, 0, 15, 30, 45, 45, 60])
                    pin = datetime(d.year, d.month, d.day, 9, 30, tzinfo=m.IST) + timedelta(minutes=late)
                    hrs = rnd.choice([3.5, 4.49, 4.5, 8, 9.12, 9.5])
                    doc.update(punch_in=pin.astimezone(timezone.utc), late_minutes=late if late > 10 else 0)
                    if rnd.random() < 0.9:
                        doc.update(punch_out=(pin + timedelta(hours=hrs)).astimezone(timezone.utc), work_hours=hrs,
                                   half_day=hrs < 4.5, overtime_minutes=rnd.choice([0, 0, 30, 45]))
                    if rnd.random() < 0.1:               # legacy document: fields missing entirely
                        doc.pop("half_day"); doc.pop("history")
                rows.append(doc)
            d += timedelta(days=1)
    return emps, rows


def r(x, p):
    return m.half_up(x, p)


def oracle_summary(emps, rows, month, dept=None):
    start, end = m.month_bounds(month)
    by = defaultdict(list)
    for x in rows:
        if start <= x["date"] <= end:
            by[x["emp_code"]].append(x)
    out = {}
    for e in emps:
        if e["joined_on"] > end or (dept and e["department"] != dept):
            continue
        o = out.setdefault(e["department"], dict(headcount=0, pd=0.0, wh=[], lc=0, tl=0, leave=0, od=0))
        o["headcount"] += 1
        for x in by[e["emp_code"]]:
            wd = date.fromisoformat(x["date"]).weekday() < 5
            if x["status"] in PRESENT and wd:
                o["pd"] += 0.5 if x.get("half_day") else 1
            if x["status"] in PRESENT and x.get("work_hours") is not None:
                o["wh"].append(x["work_hours"])
            if x["late_minutes"] > 0:
                o["lc"] += 1; o["tl"] += x["late_minutes"]
            o["leave"] += x["status"] == "LEAVE"
            o["od"] += x["status"] == "ON_DUTY"
    return [dict(department=k, headcount=v["headcount"], present_days=r(v["pd"], 2),
                 avg_work_hours=r(sum(v["wh"]) / len(v["wh"]), 2) if v["wh"] else None, late_count=v["lc"],
                 total_late_minutes=v["tl"], leave_count=v["leave"], on_duty_count=v["od"]) for k, v in sorted(out.items())]


def oracle_leaderboard(emps, rows, month, limit, dept=None):
    start, end = m.month_bounds(month)
    info = {e["emp_code"]: e for e in emps}
    tot, cnt = defaultdict(int), defaultdict(int)
    for x in rows:
        if start <= x["date"] <= end and x["late_minutes"] > 0 and x["emp_code"] in info:
            if dept and info[x["emp_code"]]["department"] != dept:
                continue
            tot[x["emp_code"]] += x["late_minutes"]; cnt[x["emp_code"]] += 1
    ordered = sorted(tot, key=lambda c: (-tot[c], c))
    res = []
    for c in ordered:
        rank = 1 + sum(1 for o in tot if tot[o] > tot[c])
        if rank <= limit:
            res.append(dict(rank=rank, emp_code=c, name=info[c]["name"], department=info[c]["department"],
                            total_late_minutes=tot[c], late_count=cnt[c]))
    return res


def oracle_trend(emps, rows, dept, f, t):
    codes = {e["emp_code"] for e in emps if e["department"] == dept}
    pc, lc = defaultdict(float), defaultdict(int)
    for x in rows:
        if x["emp_code"] in codes and f <= date.fromisoformat(x["date"]) <= t:
            if x["status"] in PRESENT:
                pc[x["date"]] += 0.5 if x.get("half_day") else 1
            if x["late_minutes"] > 0:
                lc[x["date"]] += 1
    items, rates, d = [], [], f
    while d <= t:
        ds = d.isoformat()
        hc = sum(1 for e in emps if e["department"] == dept and e["joined_on"] <= ds)
        wd = d.weekday() < 5
        rate = pc[ds] / hc if wd and hc > 0 else None
        rates.append(rate)
        win = [x for x in rates[-7:] if x is not None]
        items.append(dict(date=ds, is_working_day=wd, headcount=hc, present_count=r(pc[ds], 2), late_count=lc[ds],
                          attendance_rate=None if rate is None else r(rate, 4),
                          moving_avg_7d=r(sum(win) / len(win), 4) if win else None))
        d += timedelta(days=1)
    return items


def load(api):
    emps, rows = build_dataset()
    m.employees.insert_many([dict(e) for e in emps])
    m.logs.insert_many([dict(x) for x in rows])
    return emps, rows


@requires_real_mongo
def test_department_summary_matches_oracle(api):
    emps, rows = load(api)
    for month in ("2026-06", "2026-07", "2026-08", "2026-05"):
        got = api.get("/analytics/departments/summary", params={"month": month}).json()["items"]
        assert got == oracle_summary(emps, rows, month), month
    got = api.get("/analytics/departments/summary", params={"month": MONTH, "department": "Eng"}).json()["items"]
    assert got == oracle_summary(emps, rows, MONTH, "Eng")


@requires_real_mongo
def test_leaderboard_matches_oracle(api):
    emps, rows = load(api)
    for limit in (1, 3, 10, 50):
        for dept in (None, "Eng", "HR"):
            params = {"month": MONTH, "limit": limit, **({"department": dept} if dept else {})}
            assert api.get("/analytics/leaderboard/late", params=params).json()["items"] == oracle_leaderboard(emps, rows, MONTH, limit, dept)


@requires_real_mongo
def test_trend_matches_oracle(api):
    emps, rows = load(api)
    for dept in ("Eng", "Sales"):
        for f, t in [(date(2026, 7, 1), date(2026, 7, 31)), (date(2026, 6, 20), date(2026, 8, 10)), (date(2026, 7, 8), date(2026, 7, 8))]:
            got = api.get(f"/analytics/departments/{dept}/trend", params={"from": f.isoformat(), "to": t.isoformat()}).json()["items"]
            assert got == oracle_trend(emps, rows, dept, f, t), (dept, f, t)


@requires_real_mongo
def test_monthly_matches_oracle(api):
    emps, rows = load(api)
    start, end = m.month_bounds(MONTH)
    for e in emps[:25]:
        mine = [x for x in rows if x["emp_code"] == e["emp_code"] and start <= x["date"] <= end]
        first = max(date.fromisoformat(start), date.fromisoformat(e["joined_on"]))
        wd = m.count_weekdays(first, date.fromisoformat(end)) if first <= date.fromisoformat(end) else 0
        pd = sum((0.5 if x.get("half_day") else 1) for x in mine if x["status"] in PRESENT and date.fromisoformat(x["date"]).weekday() < 5)
        got = api.get(f"/analytics/employees/{e['emp_code']}/monthly", params={"month": MONTH}).json()
        assert got["working_days"] == wd and got["present_days"] == pd
        assert got["leave_days"] == sum(x["status"] == "LEAVE" for x in mine)
        assert got["total_late_minutes"] == sum(x["late_minutes"] for x in mine if x["late_minutes"] > 0)
        assert got["attendance_pct"] == (r(pd / wd * 100, 2) if wd else None)
