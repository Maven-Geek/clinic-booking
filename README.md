# Clinic booking API

Koya Cohort 4 assessment, Task 1. A FastAPI + Postgres API that lets patients book appointment slots, and a web page that uses it. All five levels are implemented.

| | |
|---|---|
| API base URL | `https://YOUR-SERVICE.onrender.com` |
| Web page | `https://YOUR-SERVICE.onrender.com/` |
| Stack | Python 3.12, FastAPI, asyncpg, Postgres (Neon), hosted on Render |

## How each level works

**Level 1: slots and bookings** (`app/main.py`)
`POST /reset` truncates everything and inserts the new slots in one transaction. Slot `start` and `end` are stored twice: the original string (returned exactly as sent) and a parsed `timestamptz` (used for sorting and the "already started" check). `available` means the slot has no confirmed booking.

**Level 2: API key and validation** (`app/security.py`, `app/bookings.py`)
A middleware rejects any request without the right `X-API-Key` (compared in constant time) before routing, so every endpoint returns `401` consistently. Bookings are validated in order: missing or blank fields, email format, slot exists (`404`), slot not started (`400`). `name` and `email` are trimmed and the email is lower-cased before saving.

**Level 3: no double booking** (`app/schema.sql`)
The guarantee lives in the database, not in application code:

```sql
CREATE UNIQUE INDEX one_confirmed_booking_per_slot ON bookings (slot_id) WHERE status = 'confirmed';
```

When many requests race for one slot, Postgres lets exactly one insert succeed; the rest raise a unique violation, which becomes `409`. A check-then-insert in Python would have a race window; this doesn't. Cancelled bookings fall outside the index, so cancelling frees the slot. Cancel is a single conditional `UPDATE ... WHERE status = 'confirmed'`, so simultaneous cancels give one `200` and the rest `409`.

**Level 4: idempotency and pagination** (`app/bookings.py`, `app/main.py`)
Each `Idempotency-Key` gets a row holding a SHA-256 fingerprint of the request body (canonical JSON, so key order doesn't matter) and the response that was sent. The key is claimed with `INSERT ... ON CONFLICT DO NOTHING` inside the same transaction as the booking. A duplicate request arriving at the same moment blocks on that row until the first commits, then replays the saved response: same status, same booking id, never a second booking. A different body with the same key gets `422`.
`GET /bookings` uses keyset pagination on an insertion-order column. It fetches `limit + 1` rows to know if another page exists, so the last page always has `next_cursor: null`, even when it's exactly full.

**Level 5: rate limiting** (`app/bookings.py`)
A sliding one-minute window per email, stored in Postgres so it holds across restarts and multiple instances. A per-email advisory lock makes the count-and-record step atomic, so 10 simultaneous attempts give exactly 5 through and 5 `429`s. Failed attempts count. `Retry-After` is the whole seconds until the oldest counted attempt leaves the window.

How levels 4 and 5 interact:
- A replay of an already-answered key returns the saved response and isn't counted, since it isn't a new attempt.
- Simultaneous copies of one keyed request count once.
- A `429` is never saved as a key's response, so the same request can succeed after the window frees up.

## The web page

`static/index.html`, served at `/`. Patients pick a time, enter their name and email, and get an appointment card with their booking reference. They can look a booking up by reference or list every booking for their email (with "Show more" pagination), and cancel.

The page never sees the API key. It calls `/web/api/...` on the same server, and the middleware maps those calls to the real endpoints as trusted requests. Only the endpoints a patient needs are mapped; `/reset` is not reachable that way. Each booking attempt sends a fresh `Idempotency-Key`, and network retries reuse it, so a flaky connection can't create a duplicate.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/clinic
export API_KEY=change-me
uvicorn app.main:app --reload
```

Tables are created on startup.

## Test every level

`scripts/check_levels.py` runs 72 checks across all five levels, including the race conditions (40 simultaneous bookings for one slot, 30 simultaneous retries with one key, a burst of 10 attempts from one email).

```bash
pip install httpx
python scripts/check_levels.py --base http://localhost:8000 --key change-me
```

It calls `/reset`, which wipes all bookings, so only run it against the live deployment before submitting.

## Deploy (free: Neon + Render)

1. **Database.** Create a project at [neon.tech](https://neon.tech). Copy the connection string from the dashboard (the one starting `postgresql://`).
2. **Key.** Generate an API key: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. **Repo.** Push this folder to GitHub.
4. **Web service.** On [render.com](https://render.com), choose New, then Blueprint, and select the repo. Render reads `render.yaml` and asks for `DATABASE_URL` and `API_KEY`. (Or create a Web Service by hand with build command `pip install -r requirements.txt` and start command `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.)
5. **Keep it awake.** Render's free plan sleeps after 15 idle minutes and takes up to a minute to wake, which could time out the graders' checks. Point a free uptime monitor (UptimeRobot or cron-job.org) at `https://YOUR-SERVICE.onrender.com/health` every 10 minutes. `/health` doesn't touch the database, so Neon can still sleep between real requests.
6. **Verify.** Run the check script against the live URL, then load the web page once.

## Assumptions

- `available` on a slot means "has no confirmed booking". Past slots are listed (as `/reset` allows them) but booking one returns `400`; the web page shows them as started.
- `/reset` also clears idempotency keys and rate-limit counters, since they describe bookings that no longer exist.
- Bookings for an email include cancelled ones.
- Timestamps in responses (`created_at`) are UTC ISO 8601 with milliseconds and a `Z` suffix, matching the spec's example.
