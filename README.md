# Employee Attendance & Analytics API

FastAPI + PyMongo service for attendance punch-in/out, audited manual corrections, and MongoDB aggregation analytics. All application code is in `app/main.py`. Requires Python 3.11+ and MongoDB 6.0+.

```bash
python -m venv .venv
# Activate the environment, then:
pip install -r requirements.txt
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
# PowerShell equivalents: $env:MONGO_URI = 'mongodb://localhost:27017'; $env:MONGO_DB = 'attendance_db'
uvicorn app.main:app --port 8000
```

Indexes are created idempotently at startup. `GET /health` returns 200 only when MongoDB is reachable and the indexes are ready. Explore the API at `http://localhost:8000/docs`; the contract reference is `openapi.yaml`. Run `pytest -q` for logic tests. Real-database workflow, concurrency, aggregation and explain-plan tests are opt-in: set `MONGO_TEST_URI` to an isolated MongoDB 6.0+ test deployment and run `pytest -q tests/test_mongodb_live.py`. The tests create and drop temporary databases, so never point `MONGO_TEST_URI` at production. Do not commit `.env` files, credentials or data dumps.
