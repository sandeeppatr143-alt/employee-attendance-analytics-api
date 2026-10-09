# Design decisions

1. **Indexes.** Unique `emp_code` and `(emp_code, date)` indexes enforce natural keys. `(date, emp_code)` supports attendance listing; status/date and department/employee-code indexes support filtered lists. Join-date indexes serve headcount; employee/punch-in finds the latest eligible punch.

2. **Simultaneous punch-ins.** Both may pass the pre-check, but the unique `(emp_code, date)` index allows one insert. MongoDB rejects the other; the API maps duplicate-key errors to 409. The pre-check is not the lock.

3. **Ties.** `$rank` uses total late minutes only, so ties share a rank and the next rank is skipped. Filtering `rank <= limit` after ranking returns everyone tied at the cutoff; `emp_code` makes order deterministic.

4. **Headcount.** Summary starts from employees who joined by month-end and looks up their logs. Employees without logs count; future joiners do not. Average hours use sum/count across eligible records, not per-employee averages.

5. **100x scale.** I would benchmark latency and explain plans, then consider daily rollups for dashboards. Raw logs and correction history remain authoritative; rollups are checked against source records.
