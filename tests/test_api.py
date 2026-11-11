"""API tests. Run against real MongoDB with:  MONGO_URI=mongodb://localhost:27017 pytest -q
Without MONGO_URI they run on mongomock (CRUD + simple aggregations only)."""
import concurrent.futures as cf
from datetime import datetime, timezone

from tests.conftest import m, requires_real_mongo

IST = m.IST


def ms(y, mo, d, h, mi, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000


def emp(api, code="EMP0001", dept="Engineering", start="09:30", end="18:30", joined="2026-01-05", name="A"):
    r = api.post("/employees", json={"emp_code": code, "name": name, "email": f"{code}@x.com", "department": dept,
                                     "shift_start": start, "shift_end": end, "joined_on": joined})
    assert r.status_code == 201, r.text
    return r.json()


# ---------- employees ----------
def test_employee_create_and_validation(api):
    e = emp(api)
    assert "_id" not in e and isinstance(e["created_at"], int)
    assert api.post("/employees", json={**e, "email": "a@b.co", "name": "x", "joined_on": "2026-01-05"}).status_code == 409
    bad = {"emp_code": "EMP0009", "name": "x", "email": "a@b.co", "department": "d", "joined_on": "2026-01-05"}
    assert api.post("/employees", json={**bad, "emp_code": "X1"}).status_code == 422
    assert api.post("/employees", json={**bad, "joined_on": "2026-02-30"}).status_code == 422
    assert api.post("/employees", json={**bad, "shift_start": "10:00", "shift_end": "10:00"}).status_code == 422
    assert api.post("/employees", json={**bad, "email": "nope"}).status_code == 422


def test_employee_list_pagination_and_filter(api):
    for i in range(1, 6):
        emp(api, f"EMP000{i}", dept="A" if i % 2 else "B")
    r = api.get("/employees", params={"page": 1, "page_size": 2}).json()
    assert [x["emp_code"] for x in r["items"]] == ["EMP0001", "EMP0002"] and r["total"] == 5
    r = api.get("/employees", params={"department": "A", "page": 2, "page_size": 2}).json()
    assert [x["emp_code"] for x in r["items"]] == ["EMP0005"] and r["total"] == 3
    assert api.get("/employees", params={"page": 0}).status_code == 422
    assert api.get("/employees", params={"page_size": 101}).status_code == 422


# ---------- punch in / out ----------
def test_punch_in_out_flow(api):
    emp(api)
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 15, 59) + 900})
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["date"] == "2026-07-06" and j["late_minutes"] == 45 and j["punch_in"] == ms(2026, 7, 6, 10, 15, 59)
    assert j["punch_out"] is None and j["work_hours"] is None and j["history"] == [] and "id" not in j
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 11, 0)}).status_code == 409
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 19, 10)})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["overtime_minutes"] == 40 and j["half_day"] is False and j["work_hours"] == 8.9
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 20, 0)}).status_code == 409


def test_punch_in_errors(api):
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0404"}).status_code == 404
    emp(api)
    for bad in [1783312500, 1783312500000.5, "1783312500000", True]:
        assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad}).status_code == 422, bad
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404


def test_overnight_shift_across_midnight(api):
    emp(api, "EMP0005", dept="Support", start="22:00", end="06:00")
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 6, 21, 55)})
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 0
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 40)})
    assert r.status_code == 200 and r.json()["overtime_minutes"] == 40 and r.json()["work_hours"] == 8.75
    # a punch-in at 02:00 IST belongs to the shift that started the day before
    emp(api, "EMP0006", dept="Support", start="22:00", end="06:00")
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0006", "punched_at": ms(2026, 7, 8, 2, 0)})
    assert r.json()["date"] == "2026-07-07" and r.json()["late_minutes"] == 240


def test_punch_out_validation(api):
    emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 31)}).status_code == 422
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 13, 0)})
    assert r.json()["half_day"] is True and r.json()["work_hours"] == 3.5


def test_concurrent_punch_in_exactly_one_201(api):
    emp(api)
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)}
    with cf.ThreadPoolExecutor(12) as ex:
        codes = sorted(f.result().status_code for f in [ex.submit(api.post, "/attendance/punch-in", json=body) for _ in range(12)])
    assert codes.count(201) == 1 and codes.count(409) == 11


def test_concurrent_punch_out_exactly_one_200(api):
    emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 30)}
    with cf.ThreadPoolExecutor(12) as ex:
        codes = sorted(f.result().status_code for f in [ex.submit(api.post, "/attendance/punch-out", json=body) for _ in range(12)])
    assert codes.count(200) == 1 and codes.count(409) == 11


# ---------- list ----------
def seed_logs(api, n_days=5, codes=("EMP0001", "EMP0002")):
    for c in codes:
        emp(api, c)
    for c in codes:
        for d in range(1, n_days + 1):
            api.post("/attendance/punch-in", json={"emp_code": c, "punched_at": ms(2026, 7, d, 9, 30)})


def test_list_attendance_sort_filter_page(api):
    seed_logs(api)
    r = api.get("/attendance", params={"page_size": 3}).json()
    assert r["total"] == 10
    assert [(x["date"], x["emp_code"]) for x in r["items"]] == [("2026-07-05", "EMP0001"), ("2026-07-05", "EMP0002"), ("2026-07-04", "EMP0001")]
    r = api.get("/attendance", params={"emp_code": "EMP0002", "date_from": "2026-07-02", "date_to": "2026-07-03"}).json()
    assert r["total"] == 2 and [x["date"] for x in r["items"]] == ["2026-07-03", "2026-07-02"]
    assert api.get("/attendance", params={"date_from": "2026-07-05", "date_to": "2026-07-01"}).status_code == 422
    assert api.get("/attendance", params={"status": "NOPE"}).status_code == 422
    assert api.get("/attendance", params={"date_from": "07/01/2026"}).status_code == 422


# ---------- regularize ----------
def test_regularize_flow_and_history(api):
    emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 5)})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 18, 30)})
    body = {"punch_in": ms(2026, 7, 7, 9, 28), "reason": "biometric glitch", "regularized_by": "hr.admin"}
    r = api.patch("/attendance/EMP0001/2026-07-07", json=body)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["late_minutes"] == 0 and j["work_hours"] == 9.03 and len(j["history"]) == 1
    ch = j["history"][0]["changes"]
    assert set(ch) == {"punch_in", "late_minutes", "work_hours"}          # only fields that actually changed
    assert ch["punch_in"] == {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)}
    assert ch["late_minutes"] == {"from": 35, "to": 0}
    # same request again changes nothing -> 422
    assert api.patch("/attendance/EMP0001/2026-07-07", json=body).status_code == 422
    # ABSENT clears the punch times
    r = api.patch("/attendance/EMP0001/2026-07-07", json={"status": "ABSENT", "reason": "marked absent", "regularized_by": "hr"})
    j = r.json()
    assert r.status_code == 200 and j["punch_in"] is None and j["work_hours"] is None and len(j["history"]) == 2
    # ... and supplying punch times with ABSENT is a 422; PRESENT without any punch_in is a 422 too
    assert api.patch("/attendance/EMP0001/2026-07-07", json={"status": "LEAVE", "punch_in": ms(2026, 7, 7, 9, 0), "reason": "12345", "regularized_by": "hr"}).status_code == 422
    assert api.patch("/attendance/EMP0001/2026-07-07", json={"status": "PRESENT", "reason": "12345", "regularized_by": "hr"}).status_code == 422


def test_regularize_errors(api):
    emp(api)
    ok = {"status": "WFH", "reason": "12345", "regularized_by": "hr"}
    assert api.patch("/attendance/EMP0404/2026-07-07", json=ok).status_code == 404
    assert api.patch("/attendance/EMP0001/2026-07-07", json=ok).status_code == 404
    assert api.patch("/attendance/EMP0001/2026-13-07", json=ok).status_code == 422
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 30)})
    assert api.patch("/attendance/EMP0001/2026-07-07", json={**ok, "punch_in": ms(2026, 7, 8, 9, 30)}).status_code == 422  # other day
    assert api.patch("/attendance/EMP0001/2026-07-07", json={**ok, "punch_out": ms(2026, 7, 7, 9, 0)}).status_code == 422   # before in
    assert api.patch("/attendance/EMP0001/2026-07-07", json={**ok, "reason": "abc"}).status_code == 422
    assert api.patch("/attendance/EMP0001/2026-07-07", json={**ok, "work_hours": 99}).status_code == 200            # derived field ignored


def test_concurrent_regularize_keeps_every_history_entry(api):
    emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 30)})
    bodies = [{"punch_out": ms(2026, 7, 7, 14 + i % 4, i), "reason": f"edit number {i}", "regularized_by": "hr"} for i in range(1, 9)]
    with cf.ThreadPoolExecutor(8) as ex:
        results = [f.result() for f in [ex.submit(api.patch, "/attendance/EMP0001/2026-07-07", json=b) for b in bodies]]
    ok = [r for r in results if r.status_code == 200]
    assert all(r.status_code in (200, 409, 422) for r in results)
    final = api.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert len(final["history"]) == len(ok) >= 1             # nothing silently lost


# ---------- analytics that mongomock can run ----------
@requires_real_mongo
def test_monthly_summary(api):
    emp(api, joined="2026-07-13")          # mid-month joiner: working days start on the 13th (Mon)
    for d, (h, mi) in {13: (9, 30), 14: (10, 0), 15: (9, 30)}.items():
        api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, d, h, mi)})
        api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, d, 19, 0) if d == 13 else ms(2026, 7, d, 13, 0)})
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 18, 9, 30)})   # Saturday
    j = api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert j["working_days"] == 15 and j["present_days"] == 2.0     # 1 + 0.5 + 0.5 ; Saturday not counted
    assert j["late_count"] == 1 and j["total_late_minutes"] == 30 and j["total_overtime_minutes"] == 30
    assert api.get("/analytics/employees/NOPE/monthly", params={"month": "2026-07"}).status_code == 404
    assert api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-13"}).status_code == 422
    assert api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-06"}).json()["attendance_pct"] is None


@requires_real_mongo
def test_department_summary_counts_people_without_logs(api):
    emp(api, "EMP0001", "Eng"); emp(api, "EMP0002", "Eng"); emp(api, "EMP0003", "Eng", joined="2026-08-01")
    emp(api, "EMP0004", "Ops", joined="2026-09-01")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 17, 30)})
    j = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert [i["department"] for i in j["items"]] == ["Eng"]          # Ops headcount is 0 in July -> omitted
    e = j["items"][0]
    assert e["headcount"] == 2 and e["present_days"] == 1.0 and e["avg_work_hours"] == 8.0
    j = api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Nope"}).json()
    assert j["items"] == []


@requires_real_mongo
def test_leaderboard_ties_and_limit(api):
    for i, (late, dept) in enumerate([(50, "A"), (40, "A"), (40, "B"), (30, "B"), (20, "A")], start=1):
        emp(api, f"EMP000{i}", dept, name=f"N{i}")
        h, mi = divmod(9 * 60 + 30 + late, 60)
        api.post("/attendance/punch-in", json={"emp_code": f"EMP000{i}", "punched_at": ms(2026, 7, 6, h, mi)})
    emp(api, "EMP0006")                                              # no lateness -> not listed
    j = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()
    assert [(r["rank"], r["emp_code"]) for r in j["items"]] == [(1, "EMP0001"), (2, "EMP0002"), (2, "EMP0003")]
    j = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 50}).json()
    assert [r["rank"] for r in j["items"]] == [1, 2, 2, 4, 5]
    j = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "B"}).json()
    assert [(r["rank"], r["emp_code"]) for r in j["items"]] == [(1, "EMP0003"), (2, "EMP0004")]


@requires_real_mongo
def test_trend_gap_filling_and_moving_average(api):
    emp(api, "EMP0001", "Eng"); emp(api, "EMP0002", "Eng", joined="2026-07-08")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})   # Mon
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 11, 11, 0)})  # Sat, late
    j = api.get("/analytics/departments/Eng/trend", params={"from": "2026-07-06", "to": "2026-07-12"}).json()["items"]
    assert [r["date"] for r in j] == [f"2026-07-{d:02d}" for d in range(6, 13)]                        # 7 rows, weekends included
    assert j[0] == {"date": "2026-07-06", "is_working_day": True, "headcount": 1, "present_count": 1.0, "late_count": 0, "attendance_rate": 1.0, "moving_avg_7d": 1.0}
    assert j[1]["attendance_rate"] == 0.0 and j[1]["moving_avg_7d"] == 0.5                              # Tue: no records
    assert j[2]["headcount"] == 2 and j[2]["attendance_rate"] == 0.0                                    # Wed: EMP0002 joined
    sat = j[5]
    assert sat["is_working_day"] is False and sat["attendance_rate"] is None and sat["present_count"] == 1.0 and sat["late_count"] == 1
    assert j[6]["moving_avg_7d"] == round((1 + 0 + 0 + 0 + 0) / 5, 4)                                   # null rates skipped
    assert api.get("/analytics/departments/Nope/trend", params={"from": "2026-07-06", "to": "2026-07-12"}).status_code == 404
    assert api.get("/analytics/departments/Eng/trend", params={"from": "2026-07-12", "to": "2026-07-06"}).status_code == 422
    assert api.get("/analytics/departments/Eng/trend", params={"from": "2026-01-01", "to": "2026-06-01"}).status_code == 422
    assert api.get("/analytics/departments/Eng/trend", params={"from": "2026-07-06"}).status_code == 422
    # empty range of logs still yields one row per day
    k = api.get("/analytics/departments/Eng/trend", params={"from": "2026-03-02", "to": "2026-03-08"}).json()["items"]
    assert len(k) == 7


@requires_real_mongo
def test_explain_uses_indexes_only(api):
    seed_logs(api)
    cases = {
        "attendance_list": {},
        "employee_monthly": {"emp_code": "EMP0001", "month": "2026-07"},
        "department_summary": {"month": "2026-07"},
        "late_leaderboard": {"month": "2026-07"},
        "department_trend": {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"},
    }
    for name, params in cases.items():
        r = api.get(f"/admin/explain/{name}", params=params)
        assert r.status_code == 200, (name, r.text)
        text = r.text
        assert "IXSCAN" in text and "COLLSCAN" not in text, name
    assert api.get("/admin/explain/department_trend", params={"department": "x"}).status_code == 422
    assert api.get("/admin/explain/bogus").status_code == 422


def test_health(api):
    assert api.get("/health").json() == {"status": "ok"}


def test_home_redirects_to_api_docs(api):
    response = api.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/docs"
