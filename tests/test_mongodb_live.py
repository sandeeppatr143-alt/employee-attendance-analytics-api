"""Opt-in integration tests against a real MongoDB server.

Set MONGO_TEST_URI to an isolated local/Atlas test deployment to run these tests.
They create and drop uniquely named temporary databases; do not point this at a
production deployment. The normal unit suite skips this file when the variable is
not set.
"""
from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app import main


def epoch_ms(local_iso: str) -> int:
    """Convert an ISO timestamp with offset to integer epoch milliseconds."""
    value = datetime.fromisoformat(local_iso).astimezone(timezone.utc)
    return int(value.timestamp() * 1000)


def employee_body(code: str, department: str = "Engineering", joined_on: str = "2026-07-01") -> dict[str, Any]:
    return {
        "emp_code": code,
        "name": f"Test {code}",
        "email": f"{code.lower()}@example.test",
        "department": department,
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": joined_on,
    }


def attendance_doc(
    emp_code: str,
    day: str,
    status: str,
    *,
    late: int = 0,
    overtime: int = 0,
    hours: float | None = None,
    half_day: bool = False,
) -> dict[str, Any]:
    # Timestamps are synthetic BSON datetimes; analytics intentionally use the
    # stored derived metrics, not recompute them from the timestamps.
    punch_in = datetime.fromisoformat(f"{day}T09:30:00+05:30").astimezone(timezone.utc)
    punch_out = (
        datetime.fromisoformat(f"{day}T18:30:00+05:30").astimezone(timezone.utc)
        if hours is not None
        else None
    )
    doc: dict[str, Any] = {
        "emp_code": emp_code,
        "date": day,
        "status": status,
        "punch_in": None if status in ("ABSENT", "LEAVE") else punch_in,
        "punch_out": None if status in ("ABSENT", "LEAVE") else punch_out,
        "work_hours": None if status in ("ABSENT", "LEAVE") else hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
    }
    # Omit these on purpose to exercise the documented legacy-data behavior.
    if day != "2026-07-08":
        doc["half_day"] = half_day
        doc["history"] = []
    return doc


@pytest.fixture

def live_api():
    uri = os.getenv("MONGO_TEST_URI")
    if not uri:
        pytest.skip("Set MONGO_TEST_URI to run real MongoDB integration tests")

    client = MongoClient(uri, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=2500)
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        client.close()
        pytest.skip(f"MongoDB is not reachable at MONGO_TEST_URI: {exc}")

    database_name = f"attendance_it_{uuid.uuid4().hex[:12]}"
    test_db = client[database_name]
    previous_db = main.db
    previous_index_state = main._indexes_ready
    main.db = test_db
    main._indexes_ready = False
    try:
        with TestClient(main.app) as api:
            yield api, test_db
    finally:
        try:
            client.drop_database(database_name)
        finally:
            client.close()
            main.db = previous_db
            main._indexes_ready = previous_index_state


def create_employee(api: TestClient, code: str, department: str = "Engineering", joined_on: str = "2026-07-01") -> None:
    response = api.post("/employees", json=employee_body(code, department, joined_on))
    assert response.status_code == 201, response.text


def walk_stages(value: Any) -> list[str]:
    stages: list[str] = []
    if isinstance(value, dict):
        stage = value.get("stage")
        if isinstance(stage, str):
            stages.append(stage)
        for child in value.values():
            stages.extend(walk_stages(child))
    elif isinstance(value, list):
        for child in value:
            stages.extend(walk_stages(child))
    return stages


def test_real_db_attendance_workflow_and_audit_history(live_api):
    api, test_db = live_api
    assert api.get("/health").status_code == 200
    create_employee(api, "EMP9101")

    # Derived fields supplied by a client must be ignored and recomputed.
    punched_in = api.post(
        "/attendance/punch-in",
        json={
            "emp_code": "EMP9101",
            "punched_at": epoch_ms("2026-07-06T09:40:01+05:30"),
            "late_minutes": 999,
            "work_hours": 123,
        },
    )
    assert punched_in.status_code == 201, punched_in.text
    assert punched_in.json()["late_minutes"] == 10
    assert punched_in.json()["work_hours"] is None
    assert punched_in.json()["history"] == []

    duplicate = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP9101", "punched_at": epoch_ms("2026-07-06T09:40:01+05:30")},
    )
    assert duplicate.status_code == 409, duplicate.text

    punched_out = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP9101", "punched_at": epoch_ms("2026-07-06T19:00:00+05:30")},
    )
    assert punched_out.status_code == 200, punched_out.text
    assert punched_out.json()["work_hours"] == 9.33
    assert punched_out.json()["overtime_minutes"] == 30
    assert punched_out.json()["half_day"] is False

    duplicate_out = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP9101", "punched_at": epoch_ms("2026-07-06T19:00:00+05:30")},
    )
    assert duplicate_out.status_code == 409, duplicate_out.text

    corrected = api.patch(
        "/attendance/EMP9101/2026-07-06",
        json={"status": "WFH", "reason": "Correct attendance", "regularized_by": "manager"},
    )
    assert corrected.status_code == 200, corrected.text
    history = corrected.json()["history"]
    assert len(history) == 1
    assert history[0]["by"] == "manager"
    assert history[0]["reason"] == "Correct attendance"
    assert history[0]["changes"]["status"] == {"from": "PRESENT", "to": "WFH"}
    assert isinstance(history[0]["at"], int)

    raw = test_db.attendance_logs.find_one({"emp_code": "EMP9101", "date": "2026-07-06"})
    assert raw is not None
    assert raw["history"][0]["changes"]["status"]["from"] == "PRESENT"


def test_real_db_simultaneous_punches_have_exactly_one_winner(live_api):
    api, _ = live_api
    def concurrent_post(path: str, payload: dict[str, Any], barrier: threading.Barrier) -> int:
        barrier.wait(timeout=10)
        return api.post(path, json=payload).status_code

    workers = 8
    # The employee natural key is also enforced in MongoDB, not by a racy pre-check.
    create_payload = employee_body("EMP9102")
    barrier = threading.Barrier(workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        statuses = list(pool.map(lambda _: concurrent_post("/employees", create_payload, barrier), range(workers)))
    assert statuses.count(201) == 1, statuses
    assert statuses.count(409) == workers - 1, statuses

    start_at = epoch_ms("2026-07-06T09:30:00+05:30")
    punch_in_payload = {"emp_code": "EMP9102", "punched_at": start_at}
    barrier = threading.Barrier(workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        statuses = list(pool.map(lambda _: concurrent_post("/attendance/punch-in", punch_in_payload, barrier), range(workers)))
    assert statuses.count(201) == 1, statuses
    assert statuses.count(409) == workers - 1, statuses

    punch_out_payload = {"emp_code": "EMP9102", "punched_at": epoch_ms("2026-07-06T18:30:00+05:30")}
    barrier = threading.Barrier(workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        statuses = list(pool.map(lambda _: concurrent_post("/attendance/punch-out", punch_out_payload, barrier), range(workers)))
    assert statuses.count(200) == 1, statuses
    assert statuses.count(409) == workers - 1, statuses


def test_real_db_missing_resources_validation_and_filtered_pagination(live_api):
    api, _ = live_api
    missing_punch_in = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP9999", "punched_at": epoch_ms("2026-07-06T09:30:00+05:30")},
    )
    assert missing_punch_in.status_code == 404
    missing_punch_out = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP9999", "punched_at": epoch_ms("2026-07-06T18:30:00+05:30")},
    )
    assert missing_punch_out.status_code == 404

    create_employee(api, "EMP9151", department="Engineering")
    create_employee(api, "EMP9152", department="Operations")
    bad_timestamp = api.post("/attendance/punch-in", json={"emp_code": "EMP9151", "punched_at": 1783312500})
    assert bad_timestamp.status_code == 422

    for code, day in (("EMP9151", "2026-07-06"), ("EMP9152", "2026-07-07")):
        response = api.post(
            "/attendance/punch-in",
            json={"emp_code": code, "punched_at": epoch_ms(f"{day}T09:30:00+05:30")},
        )
        assert response.status_code == 201, response.text

    page = api.get("/attendance?status=PRESENT&page=1&page_size=1")
    assert page.status_code == 200, page.text
    assert page.json()["total"] == 2
    assert len(page.json()["items"]) == 1
    assert page.json()["items"][0]["emp_code"] == "EMP9152"  # newest date first

    employees_page = api.get("/employees?department=Engineering&page=1&page_size=1")
    assert employees_page.status_code == 200, employees_page.text
    assert employees_page.json()["total"] == 1
    assert [item["emp_code"] for item in employees_page.json()["items"]] == ["EMP9151"]


def test_real_db_analytics_cover_mid_month_joiner_weekend_and_no_log_employee(live_api):
    api, test_db = live_api
    create_employee(api, "EMP9201", joined_on="2026-07-01")
    create_employee(api, "EMP9202", joined_on="2026-07-08")
    create_employee(api, "EMP9203", joined_on="2026-07-01")  # intentionally no logs
    create_employee(api, "EMP9204", department="Operations", joined_on="2026-07-01")
    test_db.attendance_logs.insert_many(
        [
            attendance_doc("EMP9201", "2026-07-04", "WFH", late=5, overtime=15, hours=7.0),
            attendance_doc("EMP9201", "2026-07-06", "PRESENT", late=10, overtime=0, hours=8.0),
            attendance_doc("EMP9201", "2026-07-07", "PRESENT", late=0, overtime=30, hours=4.49, half_day=True),
            attendance_doc("EMP9201", "2026-07-08", "LEAVE", late=0, overtime=0, hours=None),
        ]
    )

    monthly_response = api.get("/analytics/employees/EMP9201/monthly?month=2026-07")
    assert monthly_response.status_code == 200, monthly_response.text
    monthly = monthly_response.json()
    assert monthly["working_days"] == 23
    assert monthly["present_days"] == 1.5
    assert monthly["leave_days"] == 1
    assert monthly["late_count"] == 2
    assert monthly["total_late_minutes"] == 15
    assert monthly["total_overtime_minutes"] == 45
    assert monthly["attendance_pct"] == 6.52

    summary_response = api.get("/analytics/departments/summary?month=2026-07")
    assert summary_response.status_code == 200, summary_response.text
    summary = {row["department"]: row for row in summary_response.json()["items"]}
    assert summary["Engineering"]["headcount"] == 3  # includes EMP9203 with no logs
    assert summary["Engineering"]["present_days"] == 1.5
    assert summary["Engineering"]["avg_work_hours"] == 6.5
    assert summary["Engineering"]["late_count"] == 2
    assert summary["Engineering"]["total_late_minutes"] == 15
    assert summary["Engineering"]["leave_count"] == 1
    assert summary["Operations"]["headcount"] == 1

    trend_response = api.get(
        "/analytics/departments/Engineering/trend?from=2026-07-04&to=2026-07-10"
    )
    assert trend_response.status_code == 200, trend_response.text
    trend = trend_response.json()["items"]
    assert [row["date"] for row in trend] == [
        "2026-07-04", "2026-07-05", "2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10"
    ]
    assert trend[0]["present_count"] == 1.0  # weekends still count presence
    assert trend[0]["attendance_rate"] is None
    assert trend[2]["headcount"] == 2
    assert trend[2]["attendance_rate"] == 0.5
    assert trend[3]["attendance_rate"] == 0.25
    assert trend[4]["headcount"] == 3  # mid-month joiner starts counting on joined_on
    assert trend[4]["attendance_rate"] == 0.0
    assert trend[-1]["moving_avg_7d"] == 0.15


def test_real_db_leaderboard_ties_at_cutoff_are_included_and_rank_within_department(live_api):
    api, test_db = live_api
    create_employee(api, "EMP9301")
    create_employee(api, "EMP9302")
    create_employee(api, "EMP9303")
    create_employee(api, "EMP9304", department="Operations")
    test_db.attendance_logs.insert_many(
        [
            attendance_doc("EMP9301", "2026-07-06", "PRESENT", late=60),
            attendance_doc("EMP9302", "2026-07-06", "PRESENT", late=60),
            attendance_doc("EMP9303", "2026-07-06", "PRESENT", late=30),
            attendance_doc("EMP9304", "2026-07-06", "PRESENT", late=120),
        ]
    )
    response = api.get("/analytics/leaderboard/late?month=2026-07&limit=1&department=Engineering")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["emp_code"] for item in items] == ["EMP9301", "EMP9302"]
    assert [item["rank"] for item in items] == [1, 1]


def test_real_db_explain_endpoints_use_indexes_and_do_not_collection_scan(live_api):
    api, test_db = live_api
    create_employee(api, "EMP9401")
    test_db.attendance_logs.insert_one(
        attendance_doc("EMP9401", "2026-10-08", "PRESENT", late=20, hours=8.0)
    )
    urls = [
        "/admin/explain/attendance_list?emp_code=EMP9401&date_from=2026-10-01&date_to=2026-10-31",
        "/admin/explain/employee_monthly?emp_code=EMP9401&month=2026-10",
        "/admin/explain/department_summary?month=2026-10&department=Engineering",
        "/admin/explain/late_leaderboard?month=2026-10&department=Engineering",
        "/admin/explain/department_trend?department=Engineering&from=2026-10-01&to=2026-10-07",
    ]
    for url in urls:
        response = api.get(url)
        assert response.status_code == 200, f"{url}: {response.status_code} {response.text}"
        body = response.json()
        assert {"endpoint", "collection", "explain"} <= body.keys()
        stages = walk_stages(body["explain"])
        assert "IXSCAN" in stages, f"No IXSCAN in {url}; stages={stages}"
        assert "COLLSCAN" not in stages, f"Unexpected COLLSCAN in {url}; stages={stages}"
