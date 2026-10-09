# Starter code review

| # | Where | Defect | How to notice it | Fix in this submission |
|---:|---|---|---|---|
| 1 | `compute_late_minutes` | Builds shift start on the punch-in's date without applying IST or the attendance-date rule. | Run under a non-IST host timezone or punch into an overnight shift before its `shift_end`. | Normalize instants to UTC, derive the record date in IST, and build the boundary from that date. |
| 2 | `compute_late_minutes` / timestamp handling | Instants are not truncated to whole seconds before calculations. | Send a timestamp with non-zero milliseconds and inspect the stored value and derived fields. | Truncate to seconds at the API boundary and store the truncated datetime. |
| 3 | `compute_work_hours` | Uses Python `round` (ties-to-even) rather than half-up rounding. | A duration of 8:07:30 should become 8.13 hours. | Use `Decimal` and `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | Creates shift end on the same date even for an overnight shift. | Punch out after midnight on a 22:00–06:00 shift. | Move the shift-end boundary to the next date for overnight shifts. |
| 5 | `compute_overtime` | Does not enforce the 30-minute minimum and mixes a timezone-aware shift boundary with naive punch timestamps. | Try 29:59 and 30:00 overtime, or compare aware and naive datetimes. | Use normalized instants and count whole overtime minutes only at 30 minutes or more. |
| 6 | `GET /health` | Always returns 200 without checking MongoDB. | Stop MongoDB and call the readiness route. | Ping MongoDB and return 503 if it is unavailable or required indexes are not ready. |
| 7 | `EmployeeIn` | Accepts malformed employee codes, email, date and shift strings; it permits equal shift boundaries. | Submit malformed fields or the same `shift_start` and `shift_end`. | Validate against the contract, including real calendar dates and HH:MM formats; employee creation rejects equal boundaries as its operation description requires. Time helpers still treat `shift_end <= shift_start` as overnight for existing/seeded records under R1. |
| 8 | `POST /employees` | Uses a check-then-insert duplicate check without a database uniqueness guarantee. | Send parallel create requests with one `emp_code`. | Enforce a unique index and map `DuplicateKeyError` to 409. |
| 9 | `POST /employees` | Stores local naive `created_at` and returns a datetime string rather than epoch milliseconds. | Inspect the BSON timestamp and response field type. | Store UTC whole-second datetimes and serialize timestamps to integer epoch milliseconds. |
| 10 | `GET /employees` | Uses `skip = page * page_size`, skipping the first page. | Request page 1 with a small page size. | Use `(page - 1) * page_size`. |
| 11 | `GET /employees` | Counts all employees even when a department filter is applied. | Compare `total` to a filtered result set. | Count with the same query used for the page. |
| 12 | `GET /employees` | Does not define deterministic sorting or constrain pagination. | Repeat a page query or request zero/oversized page sizes. | Sort by `emp_code` and validate `page` and `page_size`. |
| 13 | `POST /attendance/punch-in` | An unknown employee is dereferenced, causing a 500. | Punch in using an unknown `emp_code`. | Return 404 before using employee fields. |
| 14 | `PunchInIn` / punch-in timestamp | A normal optional integer allows floats/strings/seconds-looking values; timestamp conversion uses the host timezone. | Send a float, numeric string, seconds, or run on a host with a different timezone. | Strictly validate epoch milliseconds, convert explicitly to UTC, and then apply IST attendance rules. |
| 15 | `POST /attendance/punch-in` | Accepts arbitrary status strings, including `ABSENT` and `LEAVE`, although punch-in only supports presence statuses. | Submit an unsupported status. | Restrict the field to `PRESENT`, `WFH`, or `ON_DUTY`. |
| 16 | `POST /attendance/punch-in` | The existence check alone is race-prone. It also returns a Mongo `_id`-derived `id` not in the contract. | Send simultaneous requests or inspect response keys. | Use the unique `(emp_code, date)` index as the concurrency guarantee and serialize the documented response shape only. |
| 17 | `GET /attendance` | Loads all matches and sorts/slices them in Python, which scales poorly. | Run against a large attendance collection and observe memory/latency. | Filter, sort, skip and limit in MongoDB; count the filtered query separately. |
| 18 | `GET /attendance` | Sort lacks the `emp_code` tie-breaker; raw BSON datetimes and Mongo IDs leak into the response. | Query multiple records for one date and inspect fields. | Sort by date descending then employee code ascending, and serialize only contract fields. |
| 19 | `GET /attendance` | Does not validate actual calendar dates, reversed date ranges or pagination bounds. | Use an impossible date, `date_from > date_to`, or invalid page values. | Validate ISO dates and range order; constrain pagination to the contract. |
| 20 | Application setup | Creates no supporting indexes; list and analytics queries can degrade into collection scans. | Inspect plans on a large collection. | Create idempotent natural-key, date, status, department and join-date indexes at startup. |
| 21 | Missing routes | Punch-out, regularization, four analytics routes and explain are not implemented. | Call each contract path. | Implement all contract endpoints, shared aggregation builders, and execution-stats explain output. |

## Looked at and decided was not a defect

- The default punch-in status of `PRESENT` is correct.
- The late rule uses a 10-minute grace period but reports whole minutes since shift start; the grace period must not be subtracted from the reported count.
- The work-hours elapsed-seconds formula is sound; its rounding and timestamp normalization were the issues.
- `emp_code` plus attendance date is the required natural key; MongoDB `_id` must remain internal.
