"""Employee Attendance & Analytics API.

Run from the repository root with:
    uvicorn app.main:app --port 8000

Configuration: MONGO_URI and MONGO_DB. A local .env is loaded for convenience;
real environment variables take precedence.
"""
from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

logger = logging.getLogger(__name__)

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=UTC,
    serverSelectionTimeoutMS=2000,
    connectTimeoutMS=2000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

_indexes_ready = False


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Best-effort at boot; /health retries if MongoDB was not ready yet.
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time, validation, and serialization helpers
# --------------------------------------------------------------------------- #
def now_utc_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_utc(dt: datetime) -> datetime:
    """Normalize PyMongo or Python datetimes to aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def truncate_seconds(dt: datetime) -> datetime:
    return to_utc(dt).replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:
    # timedelta arithmetic avoids float timestamp rounding near whole seconds.
    return (EPOCH + timedelta(milliseconds=value)).replace(microsecond=0)


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    # R1 says the whole-second value is what the API returns, including legacy records.
    value = truncate_seconds(value)
    delta = to_utc(value) - EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def parse_date(value: str, label: str = "date") -> Date:
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        raise HTTPException(status_code=422, detail=f"{label} must be YYYY-MM-DD")
    try:
        parsed = Date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{label} must be a valid calendar date")
    if parsed.isoformat() != value:
        raise HTTPException(status_code=422, detail=f"{label} must be YYYY-MM-DD")
    return parsed


def parse_month(value: str) -> tuple[str, str, Date, Date]:
    if not MONTH_PATTERN.fullmatch(value or ""):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    year, month_num = map(int, value.split("-"))
    try:
        first = Date(year, month_num, 1)
        if year == 9999 and month_num == 12:
            last = Date.max
        else:
            next_first = Date(year + 1, 1, 1) if month_num == 12 else Date(year, month_num + 1, 1)
            last = next_first - timedelta(days=1)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="month must be a valid calendar month") from exc
    return first.isoformat(), last.isoformat(), first, last


def round_half_up(value: float | int | Decimal, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def working_days(start: Date, end: Date) -> int:
    """Weekdays, inclusive. Returns zero for an empty interval."""
    if end < start:
        return 0
    count = (end - start).days + 1
    full_weeks, remainder = divmod(count, 7)
    total = full_weeks * 5
    for offset in range(remainder):
        if (start + timedelta(days=full_weeks * 7 + offset)).weekday() < 5:
            total += 1
    return total


def attendance_date(punch_in: datetime, employee: dict[str, Any]) -> Date:
    local = to_utc(punch_in).astimezone(IST)
    shift_end = employee["shift_end"]
    is_overnight = shift_end <= employee["shift_start"]
    date_for_record = local.date()
    if is_overnight and local.strftime("%H:%M") < shift_end:
        date_for_record -= timedelta(days=1)
    return date_for_record


def shift_boundary(record_date: str | Date, shift_time: str, *, overnight_end: bool = False) -> datetime:
    day = Date.fromisoformat(record_date) if isinstance(record_date, str) else record_date
    if overnight_end:
        day += timedelta(days=1)
    hh, mm = map(int, shift_time.split(":"))
    return datetime.combine(day, time(hh, mm), tzinfo=IST).astimezone(UTC)


def compute_late_minutes(punch_in: datetime, shift_start: str, record_date: str | Date) -> int:
    punch = truncate_seconds(punch_in)
    shift_start_dt = shift_boundary(record_date, shift_start)
    elapsed_seconds = (punch - shift_start_dt).total_seconds()
    if elapsed_seconds > 10 * 60:
        return int(elapsed_seconds // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, shift_end: str, record_date: str | Date, employee: dict[str, Any]) -> int:
    is_overnight = employee["shift_end"] <= employee["shift_start"]
    shift_end_dt = shift_boundary(record_date, shift_end, overnight_end=is_overnight)
    elapsed_seconds = (truncate_seconds(punch_out) - shift_end_dt).total_seconds()
    if elapsed_seconds >= 30 * 60:
        return int(elapsed_seconds // 60)
    return 0


def derived_fields(
    employee: dict[str, Any],
    record_date: str,
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
) -> dict[str, Any]:
    if status not in PRESENCE_STATUSES:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if punch_in is None:
        # A caller should validate the status/punch-in relationship before here.
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    punch_in = truncate_seconds(punch_in)
    late = compute_late_minutes(punch_in, employee["shift_start"], record_date)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    punch_out = truncate_seconds(punch_out)
    hours = compute_work_hours(punch_in, punch_out)
    overtime = compute_overtime(punch_out, employee["shift_end"], record_date, employee)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": hours < 4.50,
    }


def api_history_entry(entry: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for field, pair in (entry.get("changes") or {}).items():
        if not isinstance(pair, dict):
            continue
        changes[field] = {
            "from": datetime_to_epoch_ms(pair.get("from")) if field in ("punch_in", "punch_out") else pair.get("from"),
            "to": datetime_to_epoch_ms(pair.get("to")) if field in ("punch_in", "punch_out") else pair.get("to"),
        }
    return {
        "at": datetime_to_epoch_ms(entry.get("at")),
        "by": entry.get("by", ""),
        "reason": entry.get("reason", ""),
        "changes": changes,
    }


def api_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    """Return exactly the public attendance shape, never MongoDB's internal _id."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [api_history_entry(item) for item in (doc.get("history") or [])],
    }


def api_employee(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc["created_at"]),
    }


def explain_json_value(value: Any) -> Any:
    """Convert explain output to standard JSON values without exposing ObjectIds."""
    if isinstance(value, datetime):
        return to_utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {str(key): explain_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [explain_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Int64 and other BSON numeric wrappers are convertible to standard ints.
    try:
        if hasattr(value, "__int__"):
            return int(value)
    except (TypeError, ValueError, OverflowError):
        pass
    return str(value)


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def ignore_client_supplied_derived_fields(cls, value: Any) -> Any:
        """Ignore read-only attendance metrics while still rejecting other typos.

        The API contract explicitly says derived fields are computed by the server
        and ignored when clients send them. Pydantic's default ``extra='ignore'``
        would also silently ignore misspelled fields, so remove only the known
        derived fields and keep ``extra='forbid'`` for everything else.
        """
        if not isinstance(value, dict):
            return value
        cleaned = dict(value)
        for field in ("work_hours", "late_minutes", "overtime_minutes", "half_day"):
            cleaned.pop(field, None)
        return cleaned


class EmployeeIn(APIModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        if not DATE_PATTERN.fullmatch(value):
            raise ValueError("joined_on must be YYYY-MM-DD")
        try:
            Date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("joined_on must be a valid calendar date") from exc
        return value

    @model_validator(mode="after")
    def shift_times_must_differ(self) -> "EmployeeIn":
        # The /employees contract explicitly disallows equal shift boundaries.
        # Calculation helpers still use <= to correctly interpret any pre-existing
        # or seeded overnight records that have equal boundaries.
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(APIModel):
    emp_code: str
    # Default None permits omission while the non-nullable annotation keeps the
    # request schema aligned with the contract; explicit null is rejected below.
    punched_at: EpochMillis = None  # type: ignore[assignment]
    status: PresenceStatus = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def reject_explicit_null_timestamp(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("punched_at must be epoch milliseconds when supplied")
        return value


class PunchOutIn(APIModel):
    emp_code: str
    punched_at: EpochMillis = None  # type: ignore[assignment]

    @field_validator("punched_at", mode="before")
    @classmethod
    def reject_explicit_null_timestamp(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("punched_at must be epoch milliseconds when supplied")
        return value


class RegularizeIn(APIModel):
    # Omitted fields are represented by default None but explicit null is invalid.
    status: Status = None  # type: ignore[assignment]
    punch_in: EpochMillis = None  # type: ignore[assignment]
    punch_out: EpochMillis = None  # type: ignore[assignment]
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status", mode="before")
    @classmethod
    def reject_explicit_null_status(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("status must be a valid status value when supplied")
        return value

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def reject_explicit_null_times(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("punch times must be epoch milliseconds, not null")
        return value


# --------------------------------------------------------------------------- #
# MongoDB indexes
# --------------------------------------------------------------------------- #
def create_indexes() -> None:
    """Create idempotent indexes. A transient DB outage should not stop the app booting."""
    global _indexes_ready
    try:
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_emp_code")
        db.employees.create_index(
            [("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined"
        )
        db.employees.create_index(
            [("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_emp_code"
        )
        db.employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined")

        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date"
        )
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee"
        )
        db.attendance_logs.create_index(
            [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_status_date_employee"
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_employee_punchin"
        )
        _indexes_ready = True
    except PyMongoError:
        logger.exception("Could not create MongoDB indexes at startup; health check will report not-ready")


# --------------------------------------------------------------------------- #
# Shared aggregation builders (also used by the explain endpoint)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, first: str, last: str) -> list[dict[str, Any]]:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$set": {
                "_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                "_late": {"$ifNull": ["$late_minutes", 0]},
                "_overtime": {"$ifNull": ["$overtime_minutes", 0]},
                "_half_day": {"$ifNull": ["$half_day", False]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$in": ["$_weekday", [2, 3, 4, 5, 6]]}]},
                            {"$cond": ["$_half_day", 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$_late"},
                "total_overtime_minutes": {"$sum": "$_overtime"},
            }
        },
    ]


def build_department_summary_pipeline(first: str, last: str, department: Optional[str] = None) -> list[dict[str, Any]]:
    employee_match: dict[str, Any] = {"joined_on": {"$lte": last}}
    if department is not None:
        employee_match["department"] = department
    default_stats = {
        "present_days": 0,
        "hours_sum": 0,
        "hours_count": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "leave_count": 0,
        "on_duty_count": 0,
    }
    log_pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "$expr": {
                    "$and": [
                        {"$eq": ["$emp_code", "$$employee_code"]},
                        {"$gte": ["$date", first]},
                        {"$lte": ["$date", last]},
                    ]
                }
            }
        },
        {
            "$set": {
                "_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                "_late": {"$ifNull": ["$late_minutes", 0]},
                "_work_hours": "$work_hours",
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$in": ["$_weekday", [2, 3, 4, 5, 6]]}]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "hours_sum": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$ne": [{"$ifNull": ["$_work_hours", None]}, None]}]},
                            "$_work_hours",
                            0,
                        ]
                    }
                },
                "hours_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$ne": [{"$ifNull": ["$_work_hours", None]}, None]}]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$_late"},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }
        },
    ]
    return [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "let": {"employee_code": "$emp_code"}, "pipeline": log_pipeline, "as": "_month_stats"}},
        {
            "$set": {
                "_stats": {"$ifNull": [{"$arrayElemAt": ["$_month_stats", 0]}, default_stats]}
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$_stats.present_days"},
                "hours_sum": {"$sum": "$_stats.hours_sum"},
                "hours_count": {"$sum": "$_stats.hours_count"},
                "late_count": {"$sum": "$_stats.late_count"},
                "total_late_minutes": {"$sum": "$_stats.total_late_minutes"},
                "leave_count": {"$sum": "$_stats.leave_count"},
                "on_duty_count": {"$sum": "$_stats.on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]}, {"$divide": ["$hours_sum", "$hours_count"]}, None]},
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str] = None) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "_employee"}},
        {"$unwind": "$_employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"_employee.department": department}})
    pipeline.extend(
        [
            # Rank on totals only: tied totals share the same rank.
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$_employee.name",
                    "department": "$_employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
    )
    return pipeline


def build_trend_pipeline(department: str, first_day: Date, last_day: Date) -> list[dict[str, Any]]:
    start_dt = datetime.combine(first_day, time.min, tzinfo=UTC)
    exclusive_end_dt = datetime.combine(last_day + timedelta(days=1), time.min, tzinfo=UTC)
    return [
        # Use an indexed employee query to seed one row, then densify the date axis.
        # The endpoint verifies the department exists before executing this pipeline.
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "_seed": {"$literal": 1}}},
        {"$set": {"day": start_dt}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, exclusive_end_dt]}}},
        {
            "$set": {
                "date_str": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}},
                "_dow": {"$dayOfWeek": "$day"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"date_str": "$date_str", "target_department": {"$literal": department}},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$eq": ["$department", "$$target_department"]}, {"$lte": ["$joined_on", "$$date_str"]}]}}},
                    {"$count": "n"},
                ],
                "as": "_headcount_rows",
            }
        },
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$_headcount_rows.n", 0]}, 0]}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"date_str": "$date_str"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$date_str"]}}},
                    {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "_employee"}},
                    {"$unwind": "$_employee"},
                    {"$match": {"_employee.department": department}},
                    {
                        "$group": {
                            "_id": None,
                            # Count half-day units as integers to avoid binary rounding in rate calculation.
                            "present_units": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                        {"$cond": [{"$ifNull": ["$half_day", False]}, 1, 2]},
                                        0,
                                    ]
                                }
                            },
                            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                        }
                    },
                ],
                "as": "_daily_stats",
            }
        },
        {
            "$set": {
                "present_units": {"$ifNull": [{"$arrayElemAt": ["$_daily_stats.present_units", 0]}, 0]},
                "present_count": {"$divide": [{"$ifNull": [{"$arrayElemAt": ["$_daily_stats.present_units", 0]}, 0]}, 2]},
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$_daily_stats.late_count", 0]}, 0]},
                "is_working_day": {"$and": [{"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]},
            }
        },
        {
            "$set": {
                # Convert attendance to 1/10000 units using integer arithmetic, implementing half-up rounding.
                "attendance_rate_units": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$floor": {"$divide": [{"$add": [{"$multiply": ["$present_units", 10000]}, "$headcount"]}, {"$multiply": [2, "$headcount"]}]}},
                        None,
                    ]
                }
            }
        },
        {"$set": {"attendance_rate": {"$cond": [{"$ne": ["$attendance_rate_units", None]}, {"$divide": ["$attendance_rate_units", 10000]}, None]}}},
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "_rate_sum": {"$sum": "$attendance_rate_units", "window": {"documents": [-6, "current"]}},
                    "_rate_count": {
                        "$sum": {"$cond": [{"$ne": ["$attendance_rate_units", None]}, 1, 0]},
                        "window": {"documents": [-6, "current"]},
                    },
                },
            }
        },
        {
            "$set": {
                # The integer formula is half-up rounding of the mean of the rounded daily rates.
                "moving_avg_7d": {
                    "$cond": [
                        {"$gt": ["$_rate_count", 0]},
                        {"$divide": [{"$floor": {"$divide": [{"$add": [{"$multiply": [2, "$_rate_sum"]}, "$_rate_count"]}, {"$multiply": [2, "$_rate_count"]}]}}, 10000]},
                        None,
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


def run_collection_explain(
    collection_name: str,
    pipeline: list[dict[str, Any]],
    hint: Optional[str] = None,
    *,
    allow_disk_use: bool = False,
) -> dict[str, Any]:
    command: dict[str, Any] = {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}}
    if hint is not None:
        command["hint"] = hint
    if allow_disk_use:
        command["allowDiskUse"] = True
    return db.command("explain", command, verbosity="executionStats")


def validate_trend_range(from_value: str, to_value: str) -> tuple[Date, Date]:
    start = parse_date(from_value, "from")
    end = parse_date(to_value, "to")
    if end < start:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range must not exceed 92 days")
    return start, end


def build_attendance_filter(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
) -> tuple[dict[str, Any], str]:
    """Build the exact list filter and a supporting index hint for list + explain."""
    if date_from is not None:
        parse_date(date_from, "date_from")
    if date_to is not None:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")

    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        date_filter: dict[str, str] = {}
        if date_from:
            date_filter["$gte"] = date_from
        if date_to:
            date_filter["$lte"] = date_to
        query["date"] = date_filter
    if status is not None:
        query["status"] = status

    # The hint is also passed by the explain route, so plan inspection mirrors
    # the real list query rather than relying on a different planner decision.
    if emp_code is not None:
        hint = "uq_attendance_emp_date"
    elif status is not None:
        hint = "ix_attendance_status_date_employee"
    else:
        hint = "ix_attendance_date_employee"
    return query, hint


# --------------------------------------------------------------------------- #
# System and employee endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", responses={503: {"description": "MongoDB unavailable or indexes not ready"}})
def health() -> dict[str, str]:
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB is not available")
    # If MongoDB came up after app boot, retry the idempotent index setup before reporting ready.
    if not _indexes_ready:
        create_indexes()
    if not _indexes_ready:
        raise HTTPException(status_code=503, detail="MongoDB indexes are not ready")
    return {"status": "ok"}


@app.post("/employees", status_code=201, responses={409: {"description": "Duplicate employee code"}})
def create_employee(body: EmployeeIn) -> dict[str, Any]:
    created_at = now_utc_seconds()
    doc = body.model_dump()
    doc["created_at"] = created_at
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return api_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query: dict[str, Any] = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query).sort([("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [api_employee(item) for item in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance endpoints
# --------------------------------------------------------------------------- #
@app.post(
    "/attendance/punch-in",
    status_code=201,
    responses={404: {"description": "Employee not found"}, 409: {"description": "Already punched in for this date"}},
)
def punch_in(body: PunchInIn) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = truncate_seconds(epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc_seconds())
    record_date = attendance_date(punched_at, employee).isoformat()
    key = {"emp_code": body.emp_code, "date": record_date}
    # The lookup gives a friendlier message; the unique compound index is the concurrency guarantee.
    if db.attendance_logs.find_one(key, {"_id": 1}) is not None:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    computed = derived_fields(employee, record_date, body.status, punched_at, None)
    doc = {
        **key,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **computed,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return api_attendance(doc)


@app.post(
    "/attendance/punch-out",
    responses={404: {"description": "Employee or eligible punch-in not found"}, 409: {"description": "Already punched out"}},
)
def punch_out(body: PunchOutIn) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = truncate_seconds(epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc_seconds())
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found at or before punched_at")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="that record is already punched out")

    punch_in_time = truncate_seconds(record["punch_in"])
    elapsed_seconds = (punched_at - punch_in_time).total_seconds()
    if elapsed_seconds <= 0:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(status_code=422, detail="punch-out must be within 24 hours of punch-in")

    computed = derived_fields(employee, record["date"], record["status"], punch_in_time, punched_at)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punched_at, **computed}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="that record is already punched out")
    return api_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query, index_hint = build_attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .hint(index_hint)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [api_attendance(item) for item in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch(
    "/attendance/{emp_code}/{date}",
    responses={404: {"description": "Employee or attendance record not found"}, 409: {"description": "Record changed concurrently"}},
)
def regularize_attendance(
    emp_code: str,
    date: str = Path(pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeIn = ...,
) -> dict[str, Any]:
    parse_date(date, "date")
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if current is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    old_status = current["status"]
    old_in = truncate_seconds(current["punch_in"]) if current.get("punch_in") is not None else None
    old_out = truncate_seconds(current["punch_out"]) if current.get("punch_out") is not None else None
    new_status = body.status if body.status is not None else old_status
    new_in = epoch_ms_to_datetime(body.punch_in) if "punch_in" in body.model_fields_set else old_in
    new_out = epoch_ms_to_datetime(body.punch_out) if "punch_out" in body.model_fields_set else old_out

    if new_status in ("ABSENT", "LEAVE") and ("punch_in" in body.model_fields_set or "punch_out" in body.model_fields_set):
        raise HTTPException(status_code=422, detail="punch times cannot be supplied with ABSENT or LEAVE")
    if new_status in ("ABSENT", "LEAVE"):
        new_in = None
        new_out = None
    else:
        if new_in is None:
            raise HTTPException(status_code=422, detail="presence status requires a punch_in")
        if attendance_date(new_in, employee).isoformat() != date:
            raise HTTPException(status_code=422, detail="punch_in must belong to this attendance date")
        if new_out is not None:
            seconds = (new_out - new_in).total_seconds()
            if seconds <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if seconds > 24 * 60 * 60:
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    new_in = truncate_seconds(new_in) if new_in is not None else None
    new_out = truncate_seconds(new_out) if new_out is not None else None
    if new_status in PRESENCE_STATUSES and new_in is None:
        raise HTTPException(status_code=422, detail="presence status requires a punch_in")
    if new_status in ("ABSENT", "LEAVE") and (new_in is not None or new_out is not None):
        raise HTTPException(status_code=422, detail="ABSENT and LEAVE require null punch times")
    if new_out is not None:
        if new_in is None:
            raise HTTPException(status_code=422, detail="punch_out requires punch_in")
        seconds = (new_out - new_in).total_seconds()
        if seconds <= 0 or seconds > 24 * 60 * 60:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    calculated = derived_fields(employee, date, new_status, new_in, new_out)
    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **calculated,
    }
    old_values = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": current.get("work_hours"),
        "late_minutes": int(current.get("late_minutes") or 0),
        "overtime_minutes": int(current.get("overtime_minutes") or 0),
        "half_day": bool(current.get("half_day", False)),
    }
    changes: dict[str, dict[str, Any]] = {}
    for key, new_value in new_values.items():
        old_value = old_values.get(key)
        if key in ("punch_in", "punch_out"):
            if old_value is not None:
                old_value = truncate_seconds(old_value)
            if new_value is not None:
                new_value = truncate_seconds(new_value)
        if old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(status_code=422, detail="request does not change the record")

    correction_at = now_utc_seconds()
    history_entry = {"at": correction_at, "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic compare-and-set: a concurrent editor that observed the same old record loses with 409
    # rather than overwriting another correction or its audit entry.
    cas_filter: dict[str, Any] = {"_id": current["_id"]}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"):
        if field in current:
            cas_filter[field] = current[field]
        else:
            cas_filter[field] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        cas_filter,
        {"$set": new_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record changed concurrently; retry the correction")
    return api_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get(
    "/analytics/employees/{emp_code}/monthly",
    responses={404: {"description": "Employee not found"}},
)
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": emp_code}, {"emp_code": 1, "joined_on": 1})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    first, last, month_start, month_end = parse_month(month)
    result = next(
        iter(db.attendance_logs.aggregate(build_employee_monthly_pipeline(emp_code, first, last), hint="uq_attendance_emp_date")),
        None,
    )
    stats = result or {
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
    }
    employed_start = max(month_start, Date.fromisoformat(employee["joined_on"]))
    days = working_days(employed_start, month_end)
    present = round_half_up(stats.get("present_days", 0), 2)
    attendance_pct = round_half_up(Decimal(str(present)) / Decimal(days) * 100, 2) if days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": present,
        "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
) -> dict[str, Any]:
    first, last, _month_start, _month_end = parse_month(month)
    pipeline = build_department_summary_pipeline(first, last, department)
    summary_hint = "ix_employee_joined" if department is None else "ix_employee_department_joined"
    rows = list(db.employees.aggregate(pipeline, allowDiskUse=True, hint=summary_hint))
    items = []
    for row in rows:
        items.append(
            {
                "department": row["department"],
                "headcount": int(row["headcount"]),
                "present_days": round_half_up(row.get("present_days", 0), 2),
                "avg_work_hours": round_half_up(row["avg_work_hours"], 2) if row.get("avg_work_hours") is not None else None,
                "late_count": int(row.get("late_count", 0)),
                "total_late_minutes": int(row.get("total_late_minutes", 0)),
                "leave_count": int(row.get("leave_count", 0)),
                "on_duty_count": int(row.get("on_duty_count", 0)),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
) -> dict[str, Any]:
    first, last, _month_start, _month_end = parse_month(month)
    pipeline = build_leaderboard_pipeline(first, last, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True, hint="ix_attendance_date_employee"))
    for item in items:
        item["rank"] = int(item["rank"])
        item["total_late_minutes"] = int(item["total_late_minutes"])
        item["late_count"] = int(item["late_count"])
    return {"month": month, "items": items}


@app.get(
    "/analytics/departments/{department}/trend",
    responses={404: {"description": "Department not found"}},
)
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> dict[str, Any]:
    first_day, last_day = validate_trend_range(from_date, to_date)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="department not found")
    pipeline = build_trend_pipeline(department, first_day, last_day)
    rows = list(
        db.employees.aggregate(
            pipeline, allowDiskUse=True, hint="ix_employee_department_emp_code"
        )
    )
    items = []
    for row in rows:
        rate = row.get("attendance_rate")
        moving = row.get("moving_avg_7d")
        items.append(
            {
                "date": row["date"],
                "is_working_day": bool(row["is_working_day"]),
                "headcount": int(row["headcount"]),
                "present_count": round_half_up(row.get("present_count", 0), 2),
                "late_count": int(row.get("late_count", 0)),
                "attendance_rate": round_half_up(rate, 4) if rate is not None else None,
                "moving_avg_7d": round_half_up(moving, 4) if moving is not None else None,
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, alias="date_from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, alias="date_to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> dict[str, Any]:
    if endpoint == "attendance_list":
        query, index_hint = build_attendance_filter(emp_code, date_from, date_to, status)
        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
                "hint": index_hint,
            },
            verbosity="executionStats",
        )
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        first, last, _start, _end = parse_month(month)
        explain = run_collection_explain(
            "attendance_logs", build_employee_monthly_pipeline(emp_code, first, last), "uq_attendance_emp_date"
        )
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        first, last, _start, _end = parse_month(month)
        summary_hint = "ix_employee_joined" if department is None else "ix_employee_department_joined"
        explain = run_collection_explain(
            "employees",
            build_department_summary_pipeline(first, last, department),
            summary_hint,
            allow_disk_use=True,
        )
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        first, last, _start, _end = parse_month(month)
        explain = run_collection_explain(
            "attendance_logs",
            build_leaderboard_pipeline(first, last, limit, department),
            "ix_attendance_date_employee",
            allow_disk_use=True,
        )
        collection = "attendance_logs"
    else:
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from and to are required for department_trend")
        first_day, last_day = validate_trend_range(from_date, to_date)
        explain = run_collection_explain(
            "employees",
            build_trend_pipeline(department, first_day, last_day),
            "ix_employee_department_emp_code",
            allow_disk_use=True,
        )
        collection = "employees"
    return {"endpoint": endpoint, "collection": collection, "explain": explain_json_value(explain)}
