from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app import main

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def dt_ist(day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, minute, second, tzinfo=IST)


def employee(shift_start="09:30", shift_end="18:30"):
    return {"emp_code": "EMP0001", "shift_start": shift_start, "shift_end": shift_end}


def test_late_minutes_observe_grace_boundary_and_flooring():
    day = "2026-07-06"
    assert main.compute_late_minutes(dt_ist(6, 9, 40, 0), "09:30", day) == 0
    assert main.compute_late_minutes(dt_ist(6, 9, 40, 1), "09:30", day) == 10
    assert main.compute_late_minutes(dt_ist(6, 10, 15, 59), "09:30", day) == 45


def test_work_hours_uses_half_up_rounding_after_second_truncation():
    start = dt_ist(6, 9, 0, 0)
    end = start + timedelta(hours=8, minutes=7, seconds=30, microseconds=900_000)
    assert main.compute_work_hours(start, end) == 8.13


def test_overtime_has_a_thirty_minute_threshold_and_floors_minutes():
    emp = employee()
    assert main.compute_overtime(dt_ist(6, 18, 59, 59), "18:30", "2026-07-06", emp) == 0
    assert main.compute_overtime(dt_ist(6, 19, 0, 0), "18:30", "2026-07-06", emp) == 30
    assert main.compute_overtime(dt_ist(6, 19, 1, 59), "18:30", "2026-07-06", emp) == 31


def test_overnight_punch_in_before_shift_end_belongs_to_previous_day():
    emp = employee("22:00", "06:00")
    punch = dt_ist(7, 5, 59).astimezone(UTC)
    assert main.attendance_date(punch, emp) == date(2026, 7, 6)
    assert main.attendance_date(dt_ist(7, 6, 0).astimezone(UTC), emp) == date(2026, 7, 7)


def test_overnight_overtime_uses_next_calendar_day_for_shift_end():
    emp = employee("22:00", "06:00")
    assert main.compute_overtime(dt_ist(7, 6, 40), "06:00", "2026-07-06", emp) == 40


def test_epoch_milliseconds_are_truncated_to_whole_seconds():
    value = main.epoch_ms_to_datetime(1_783_312_500_999)
    assert value.microsecond == 0
    assert main.datetime_to_epoch_ms(value) % 1000 == 0


def test_working_days_are_weekdays_and_empty_ranges_are_zero():
    assert main.working_days(date(2026, 7, 1), date(2026, 7, 5)) == 3
    assert main.working_days(date(2026, 7, 6), date(2026, 7, 5)) == 0


def test_timestamp_input_is_strict_integer_epoch_milliseconds():
    assert main.PunchInIn(emp_code="EMP0001", punched_at=1_783_312_500_000).punched_at == 1_783_312_500_000
    for value in (1_783_312_500, 1_783_312_500_000.0, "1783312500000", None):
        with pytest.raises(ValidationError):
            main.PunchInIn(emp_code="EMP0001", punched_at=value)


def test_punch_in_accepts_only_presence_statuses_and_ignores_derived_fields():
    with pytest.raises(ValidationError):
        main.PunchInIn(emp_code="EMP0001", status="LEAVE")
    # Per the OpenAPI contract, computed attendance metrics from clients are ignored.
    punch = main.PunchInIn(emp_code="EMP0001", late_minutes=999, work_hours=8.0)
    assert "late_minutes" not in punch.model_dump()
    assert "work_hours" not in punch.model_dump()
    # Unknown fields are still rejected so genuine typos do not disappear silently.
    with pytest.raises(ValidationError):
        main.PunchInIn(emp_code="EMP0001", late_minuts=999)


def test_employee_validation_checks_code_calendar_date_and_overnight_shift_rules():
    valid = main.EmployeeIn(
        emp_code="EMP0001", name="Asha Rao", email="asha@example.com",
        department="Engineering", joined_on="2026-07-06",
    )
    assert valid.shift_start == "09:30"
    with pytest.raises(ValidationError):
        main.EmployeeIn(
            emp_code="EMP01", name="Asha Rao", email="asha@example.com",
            department="Engineering", joined_on="2026-07-06",
        )
    with pytest.raises(ValidationError):
        main.EmployeeIn(
            emp_code="EMP0001", name="Asha Rao", email="asha@example.com",
            department="Engineering", joined_on="2026-02-30",
        )
    # Employee creation's operation description says shift boundaries must differ.
    with pytest.raises(ValidationError):
        main.EmployeeIn(
            emp_code="EMP0001", name="Asha Rao", email="asha@example.com",
            department="Engineering", joined_on="2026-07-06", shift_start="09:30", shift_end="09:30",
        )
    # R1's <= rule remains implemented for historical/seeded records at calculation time.
    equal_boundary_record = employee("09:30", "09:30")
    assert main.attendance_date(dt_ist(7, 9, 29), equal_boundary_record) == date(2026, 7, 6)


def test_attendance_filter_shared_by_list_and_explain_selects_index_and_validates_ranges():
    query, hint = main.build_attendance_filter(
        emp_code="EMP0001", date_from="2026-07-01", date_to="2026-07-31", status="PRESENT"
    )
    assert query == {
        "emp_code": "EMP0001",
        "date": {"$gte": "2026-07-01", "$lte": "2026-07-31"},
        "status": "PRESENT",
    }
    assert hint == "uq_attendance_emp_date"
    assert main.build_attendance_filter(status="LEAVE")[1] == "ix_attendance_status_date_employee"
    assert main.build_attendance_filter()[1] == "ix_attendance_date_employee"
    with pytest.raises(main.HTTPException) as exc:
        main.build_attendance_filter(date_from="2026-07-31", date_to="2026-07-01")
    assert exc.value.status_code == 422


def test_aggregation_builders_keep_required_mongodb_stages():
    monthly = main.build_employee_monthly_pipeline("EMP0001", "2026-07-01", "2026-07-31")
    summary = main.build_department_summary_pipeline("2026-07-01", "2026-07-31")
    leaderboard = main.build_leaderboard_pipeline("2026-07-01", "2026-07-31", 10)
    trend = main.build_trend_pipeline("Engineering", date(2026, 7, 1), date(2026, 7, 7))
    assert monthly[0]["$match"]["emp_code"] == "EMP0001"
    assert any("$lookup" in stage for stage in summary)
    assert any("$rank" in str(stage) for stage in leaderboard)
    assert trend[0] == {"$match": {"department": "Engineering"}}
    assert trend[1] == {"$limit": 1}
    assert any("$densify" in stage for stage in trend)
    assert any("$setWindowFields" in stage for stage in trend)


def test_regularization_rejects_explicit_null_and_ignores_derived_fields():
    with pytest.raises(ValidationError):
        main.RegularizeIn(reason="Fix punch", regularized_by="manager", punch_in=None)
    correction = main.RegularizeIn(reason="Fix punch", regularized_by="manager", work_hours=8.0)
    assert "work_hours" not in correction.model_dump()


def test_all_contract_routes_are_registered_and_optional_timestamps_are_non_nullable_in_schema():
    schema = main.app.openapi()
    expected = {
        ("get", "/health"),
        ("post", "/employees"),
        ("get", "/employees"),
        ("post", "/attendance/punch-in"),
        ("post", "/attendance/punch-out"),
        ("get", "/attendance"),
        ("patch", "/attendance/{emp_code}/{date}"),
        ("get", "/analytics/employees/{emp_code}/monthly"),
        ("get", "/analytics/departments/summary"),
        ("get", "/analytics/leaderboard/late"),
        ("get", "/analytics/departments/{department}/trend"),
        ("get", "/admin/explain/{endpoint}"),
    }
    actual = {(method, path) for path, methods in schema["paths"].items() for method in methods}
    assert expected == actual
    assert "503" in schema["paths"]["/health"]["get"]["responses"]
    assert "409" in schema["paths"]["/employees"]["post"]["responses"]
    assert {"404", "409"} <= set(schema["paths"]["/attendance/punch-in"]["post"]["responses"])
    assert {"404", "409"} <= set(schema["paths"]["/attendance/punch-out"]["post"]["responses"])
    assert {"404", "409"} <= set(schema["paths"]["/attendance/{emp_code}/{date}"]["patch"]["responses"])
    assert "404" in schema["paths"]["/analytics/employees/{emp_code}/monthly"]["get"]["responses"]
    assert "404" in schema["paths"]["/analytics/departments/{department}/trend"]["get"]["responses"]
    punch_schema = schema["components"]["schemas"]["PunchInIn"]["properties"]["punched_at"]
    assert punch_schema.get("type") == "integer"
    assert "anyOf" not in punch_schema


def test_optional_request_fields_can_be_omitted_but_not_set_to_null():
    assert main.PunchInIn(emp_code="EMP0001").punched_at is None
    assert main.PunchOutIn(emp_code="EMP0001").punched_at is None
    correction = main.RegularizeIn(reason="Corrected punch", regularized_by="manager")
    assert correction.status is None
    assert correction.model_fields_set == {"reason", "regularized_by"}
