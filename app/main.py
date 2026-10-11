import os
import calendar
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def init_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("department", ASCENDING)])


def round_half_up(value: float, decimals: int = 2) -> float:
    d = Decimal(str(value))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def epoch_to_sec_dt(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(epoch_ms // 1000, tz=timezone.utc)


def parse_hh_mm(val: str) -> time:
    h, m = map(int, val.split(":"))
    return time(hour=h, minute=m)


def resolve_shift_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    local_dt = punch_in_dt.astimezone(IST)
    start_t = parse_hh_mm(shift_start)
    end_t = parse_hh_mm(shift_end)

    if end_t <= start_t:
        if local_dt.time() < end_t:
            return (local_dt.date() - timedelta(days=1)).isoformat()
    return local_dt.date().isoformat()


def build_shift_boundaries(record_date: str, shift_start: str, shift_end: str):
    base = datetime.fromisoformat(record_date).date()
    start_t = parse_hh_mm(shift_start)
    end_t = parse_hh_mm(shift_end)

    start_dt = datetime.combine(base, start_t, tzinfo=IST)
    if end_t <= start_t:
        end_dt = datetime.combine(base + timedelta(days=1), end_t, tzinfo=IST)
    else:
        end_dt = datetime.combine(base, end_t, tzinfo=IST)
    return start_dt, end_dt


def evaluate_late_minutes(punch_in_dt: datetime, shift_start_dt: datetime) -> int:
    delay_sec = (punch_in_dt - shift_start_dt).total_seconds()
    if delay_sec > 600:
        return int(delay_sec // 60)
    return 0


def evaluate_overtime(punch_out_dt: datetime, shift_end_dt: datetime) -> int:
    surplus_sec = (punch_out_dt - shift_end_dt).total_seconds()
    if surplus_sec >= 1800:
        return int(surplus_sec // 60)
    return 0


def evaluate_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    duration_sec = (punch_out_dt - punch_in_dt).total_seconds()
    return round_half_up(duration_sec / 3600.0, 2)


class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, v):
        if v is not None and not isinstance(v, int):
            raise ValueError("punched_at must be integer milliseconds")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, v):
        if v is not None and not isinstance(v, int):
            raise ValueError("punched_at must be integer milliseconds")
        return v


class CorrectionIn(BaseModel):
    reason: str
    author: str
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_instants(cls, v):
        if v is not None and not isinstance(v, int):
            raise ValueError("must be integer milliseconds")
        return v


def serialize_attendance(doc: dict) -> dict:
    history_payload = []
    for h in doc.get("history", []):
        history_payload.append({
            "timestamp": to_epoch_ms(h.get("timestamp")),
            "author": h.get("author"),
            "reason": h.get("reason"),
            "changes": h.get("changes", {}),
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_payload,
    }


def compute_working_days(joined_on_str: str, year: int, month: int) -> int:
    _, days_in_month = calendar.monthrange(year, month)
    m_start = datetime(year, month, 1).date()
    m_end = datetime(year, month, days_in_month).date()

    join_date = datetime.fromisoformat(joined_on_str).date()
    if join_date > m_end:
        return 0

    effective_start = max(m_start, join_date)
    count = 0
    curr = effective_start
    while curr <= m_end:
        if curr.weekday() < 5:
            count += 1
        curr += timedelta(days=1)
    return count


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MongoDB service unavailable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = to_epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    filter_q = {}
    if department:
        filter_q["department"] = department

    total = db.employees.count_documents(filter_q)
    skip = (page - 1) * page_size
    cursor = (
        db.employees.find(filter_q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )

    items = list(cursor)
    for row in items:
        if isinstance(row.get("created_at"), datetime):
            row["created_at"] = to_epoch_ms(row["created_at"])

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee record not found")

    if body.status not in ["PRESENT", "WFH", "ON_DUTY"]:
        raise HTTPException(422, "Invalid punch-in status supplied")

    raw_dt = (
        epoch_to_sec_dt(body.punched_at)
        if body.punched_at
        else datetime.now(timezone.utc).replace(microsecond=0)
    )
    assigned_date = resolve_shift_date(
        raw_dt, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    )

    s_start_dt, _ = build_shift_boundaries(
        assigned_date, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    )
    late_mins = evaluate_late_minutes(raw_dt, s_start_dt)

    record = {
        "emp_code": body.emp_code,
        "date": assigned_date,
        "status": body.status,
        "punch_in": raw_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(409, "Attendance already recorded for this calendar date")

    return serialize_attendance(record)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee record not found")

    raw_dt = (
        epoch_to_sec_dt(body.punched_at)
        if body.punched_at
        else datetime.now(timezone.utc).replace(microsecond=0)
    )
    local_day = raw_dt.astimezone(IST).date()
    eligible_dates = [local_day.isoformat(), (local_day - timedelta(days=1)).isoformat()]

    match_doc = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "date": {"$in": eligible_dates},
            "punch_in": {"$ne": None},
            "punch_out": None,
        },
        sort=[("date", DESCENDING)],
    )

    if not match_doc:
        raise HTTPException(404, "No active open punch-in session found")

    punch_in_dt = match_doc["punch_in"]
    if raw_dt < punch_in_dt:
        raise HTTPException(422, "punch_out cannot occur before punch_in")

    _, s_end_dt = build_shift_boundaries(
        match_doc["date"], emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    )

    w_hours = evaluate_work_hours(punch_in_dt, raw_dt)
    ot_mins = evaluate_overtime(raw_dt, s_end_dt)
    is_half = w_hours < 4.50

    db.attendance_logs.update_one(
        {"_id": match_doc["_id"]},
        {
            "$set": {
                "punch_out": raw_dt,
                "work_hours": w_hours,
                "overtime_minutes": ot_mins,
                "half_day": is_half,
            }
        },
    )

    fresh = db.attendance_logs.find_one({"_id": match_doc["_id"]})
    return serialize_attendance(fresh)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize(emp_code: str, date: str, body: CorrectionIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee record not found")

    target = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not target:
        raise HTTPException(404, "Attendance record not found for date")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    diff = {}

    target_status = body.status if body.status is not None else target["status"]
    target_in = (
        epoch_to_sec_dt(body.punch_in)
        if body.punch_in is not None
        else target.get("punch_in")
    )
    target_out = (
        epoch_to_sec_dt(body.punch_out)
        if body.punch_out is not None
        else target.get("punch_out")
    )

    if target_status in ["ABSENT", "LEAVE"]:
        target_in = None
        target_out = None

    if body.status is not None and body.status != target["status"]:
        diff["status"] = {"before": target["status"], "after": target_status}

    if body.punch_in is not None:
        p_in_old = to_epoch_ms(target.get("punch_in"))
        p_in_new = to_epoch_ms(target_in)
        if p_in_old != p_in_new:
            diff["punch_in"] = {"before": p_in_old, "after": p_in_new}

    if body.punch_out is not None:
        p_out_old = to_epoch_ms(target.get("punch_out"))
        p_out_new = to_epoch_ms(target_out)
        if p_out_old != p_out_new:
            diff["punch_out"] = {"before": p_out_old, "after": p_out_new}

    s_start_dt, s_end_dt = build_shift_boundaries(
        date, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    )

    if target_in:
        late_mins = evaluate_late_minutes(target_in, s_start_dt)
    else:
        late_mins = 0

    if target_in and target_out:
        if target_out < target_in:
            raise HTTPException(422, "punch_out cannot precede punch_in")
        w_hours = evaluate_work_hours(target_in, target_out)
        ot_mins = evaluate_overtime(target_out, s_end_dt)
        is_half = w_hours < 4.50
    else:
        w_hours = None
        ot_mins = 0
        is_half = False

    history_item = {
        "timestamp": now_utc,
        "author": body.author,
        "reason": body.reason,
        "changes": diff,
    }

    db.attendance_logs.update_one(
        {"_id": target["_id"]},
        {
            "$set": {
                "status": target_status,
                "punch_in": target_in,
                "punch_out": target_out,
                "late_minutes": late_mins,
                "work_hours": w_hours,
                "overtime_minutes": ot_mins,
                "half_day": is_half,
            },
            "$push": {"history": history_item},
        },
    )

    updated = db.attendance_logs.find_one({"_id": target["_id"]})
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query_builder = {}
    if emp_code:
        query_builder["emp_code"] = emp_code
    if date_from or date_to:
        query_builder["date"] = {}
        if date_from:
            query_builder["date"]["$gte"] = date_from
        if date_to:
            query_builder["date"]["$lte"] = date_to
    if status:
        query_builder["status"] = status

    total = db.attendance_logs.count_documents(query_builder)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(query_builder)
        .sort("date", DESCENDING)
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_attendance(row) for row in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., regex=r"^\d{4}-\d{2}$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee record not found")

    y, m = map(int, month.split("-"))
    working_days = compute_working_days(emp["joined_on"], y, m)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
        {
            "$project": {
                "date": 1,
                "status": 1,
                "work_hours": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
                "weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$weekday", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
            }
        },
    ]

    agg = list(db.attendance_logs.aggregate(pipeline))
    if agg:
        data = agg[0]
        p_days = float(data["present_days"])
        rate = round_half_up(p_days / working_days, 4) if working_days > 0 else 0.0
        return {
            "emp_code": emp_code,
            "month": month,
            "working_days": working_days,
            "present_days": round_half_up(p_days, 2),
            "attendance_rate": rate,
            "total_work_hours": round_half_up(data["total_work_hours"], 2),
            "total_late_minutes": int(data["total_late_minutes"]),
            "total_overtime_minutes": int(data["total_overtime_minutes"]),
        }

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": 0.0,
        "attendance_rate": 0.0,
        "total_work_hours": 0.0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
    }


@app.get("/analytics/departments/summary")
def departments_summary(month: str = Query(..., regex=r"^\d{4}-\d{2}$")):
    y, m = map(int, month.split("-"))
    _, end_day = calendar.monthrange(y, m)
    cutoff_date = f"{month}-{end_day:02d}"

    pipeline = [
        {"$match": {"joined_on": {"$lte": cutoff_date}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$regexMatch": {"input": "$date", "regex": f"^{month}"}},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "date": 1,
                            "status": 1,
                            "half_day": 1,
                            "work_hours": 1,
                            "weekday": {
                                "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$unwind": "$department"},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "records": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on",
                        "logs": "$logs",
                    }
                },
            }
        },
    ]

    results = list(db.employees.aggregate(pipeline))
    output = []

    for dept in results:
        dept_name = dept["_id"]
        hc = dept["headcount"]
        expected_working = sum(
            compute_working_days(item["joined_on"], y, m) for item in dept["records"]
        )

        sum_present = 0.0
        sum_hours = 0.0
        work_entries = 0

        for staff in dept["records"]:
            for entry in staff["logs"]:
                if entry.get("work_hours") is not None:
                    sum_hours += entry["work_hours"]
                    work_entries += 1
                if entry.get("status") in ["PRESENT", "WFH", "ON_DUTY"] and entry.get("weekday") in [2, 3, 4, 5, 6]:
                    sum_present += 0.5 if entry.get("half_day") else 1.0

        daily_avg = (
            round_half_up(sum_hours / work_entries, 2) if work_entries > 0 else 0.0
        )
        rate = (
            round_half_up(sum_present / expected_working, 4)
            if expected_working > 0
            else 0.0
        )

        output.append({
            "department": dept_name,
            "headcount": hc,
            "attendance_rate": rate,
            "avg_daily_work_hours": daily_avg,
        })

    output.sort(key=lambda x: x["department"])
    return {"departments": output}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., regex=r"^\d{4}-\d{2}$"),
    limit: int = Query(10, ge=1, le=100),
):
    pipeline = [
        {"$match": {"date": {"$regex": f"^{month}"}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "info",
            }
        },
        {"$unwind": "$info"},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$sort": {"rank": 1, "_id": 1}},
        {"$limit": limit},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$info.name",
                "department": "$info.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    start_date: str = Query(..., regex=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: str = Query(..., regex=r"^\d{4}-\d{2}-\d{2}$"),
):
    codes = [
        e["emp_code"]
        for e in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})
    ]

    start_d = datetime.fromisoformat(start_date).date()
    finish_d = datetime.fromisoformat(end_date).date()

    calendar_sequence = []
    pointer = start_d
    while pointer <= finish_d:
        calendar_sequence.append(pointer.isoformat())
        pointer += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": codes},
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "presence": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
            }
        },
    ]

    records = list(db.attendance_logs.aggregate(pipeline))
    log_lookup = {r["_id"]: float(r["presence"]) for r in records}

    series = []
    window = []
    for day_str in calendar_sequence:
        val = log_lookup.get(day_str, 0.0)
        window.append(val)
        if len(window) > 7:
            window.pop(0)

        rolling = round_half_up(sum(window) / len(window), 2)
        series.append({
            "date": day_str,
            "present_count": val,
            "moving_avg_7d": rolling,
        })

    return {"department": department, "series": series}


@app.get("/admin/explain/{endpoint}")
def explain_query(endpoint: str):
    if endpoint == "attendance_list":
        return db.attendance_logs.find({}).sort("date", DESCENDING).limit(20).explain()
    elif endpoint == "employee_monthly":
        return db.command(
            "aggregate",
            "attendance_logs",
            pipeline=[{"$match": {"emp_code": "EMP0001", "date": {"$regex": "^2026-10"}}}],
            explain=True,
        )
    elif endpoint == "late_leaderboard":
        return db.command(
            "aggregate",
            "attendance_logs",
            pipeline=[{"$match": {"date": {"$regex": "^2026-10"}}}],
            explain=True,
        )
    elif endpoint == "department_summary":
        return db.command(
            "aggregate",
            "employees",
            pipeline=[{"$match": {"joined_on": {"$lte": "2026-10-31"}}}],
            explain=True,
        )
    elif endpoint == "department_trend":
        return db.command(
            "aggregate",
            "attendance_logs",
            pipeline=[{"$match": {"date": {"$gte": "2026-10-01", "$lte": "2026-10-15"}}}],
            explain=True,
        )
    raise HTTPException(404, "Unknown explain endpoint requested")
