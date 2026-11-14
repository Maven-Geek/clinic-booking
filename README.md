# Clinic Booking API

A REST API that lets patients book clinic appointment slots, with a patient-facing web page built on top of it. Patients can browse open times, book one, get a confirmation with a booking reference, look their booking up later, and cancel it.

**Live:** [clinic-booking-1d33.onrender.com](https://clinic-booking-1d33.onrender.com) (the web page is at `/`, and the API shares the same base URL)

Built with **FastAPI** and **PostgreSQL** for Task 1 of the Koya Academy Cohort 4 assessment. All five levels of the specification are implemented.

## Features

| Level | Capability | How it's achieved |
|---|---|---|
| 1 | List slots, book one, look up a booking | Six JSON endpoints backed by Postgres, so bookings persist across requests and restarts |
| 2 | API key and input validation | Middleware rejects requests without a valid `X-API-Key`; bookings with missing or invalid details get `400`, unknown slots `404` |
| 3 | No double booking, even under concurrency; cancellation | A partial unique index lets the database itself guarantee one confirmed booking per slot |
| 4 | Safe retries and pagination | `Idempotency-Key` support backed by a transactional claim, and keyset (cursor) pagination |
| 5 | Spam protection | A sliding-window rate limit of 5 booking attempts per email per minute, with `Retry-After` |

**The web page** shows the timetable grouped by day with a doctor filter, books a slot, and displays an appointment card with the booking reference. Patients can find a booking by reference or list all bookings for their email, and cancel. Errors become plain guidance, for example "Someone else has just booked this time" for a `409`, or how many seconds to wait for a `429`.

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.12+ |
| Web framework | FastAPI on Uvicorn |
| Database | PostgreSQL (hosted on Neon) |
| Database driver | asyncpg, with raw SQL |
| Front end | One HTML page with vanilla JavaScript and CSS, served by the API |
| Hosting | Render web service |

## API

Every endpoint requires the `X-API-Key` header. Every error has the same shape: `{"error": "<message>"}`.

| Method and path | Purpose | Success | Errors |
|---|---|---|---|
| `POST /reset` | Delete all bookings and replace all slots | `200` | `400` invalid slot data |
| `GET /slots?provider=` | All slots, earliest first, with `available` | `200` | |
| `POST /bookings` | Book a slot (optional `Idempotency-Key` header) | `201` | `400`, `404`, `409`, `422`, `429` |
| `GET /bookings?email=&limit=&cursor=` | One person's bookings, oldest first, paginated | `200` | `400` invalid `limit` or cursor |
| `GET /bookings/{id}` | One booking | `200` | `404` |
| `DELETE /bookings/{id}` | Cancel a booking and free its slot | `200` | `404`, `409` already cancelled |

Example:

```http
POST /bookings
X-API-Key: <key>
Idempotency-Key: 6f1c0b9e-2d4a-4b7e-9a51-0c3f2e8d7a10
Content-Type: application/json

{"slot_id": "s1", "name": "Ada Obi", "email": "ada@example.com"}
```

```json
{
  "id": "b5f0c2d8-6c1e-4a8f-9a0b-3e2d1c4b5a69",
  "slot_id": "s1",
  "name": "Ada Obi",
  "email": "ada@example.com",
  "status": "confirmed",
  "created_at": "2026-11-14T09:15:02.481Z"
}
```

## Design decisions

### Double booking is prevented by the database

Checking whether a slot is free and then inserting leaves a race window: two simultaneous requests can both see the slot as free. Instead, the rule lives in the schema:

```sql
CREATE UNIQUE INDEX one_confirmed_booking_per_slot
    ON bookings (slot_id) WHERE status = 'confirmed';
```

When many requests race for one slot, Postgres lets exactly one insert succeed and rejects the rest with a unique violation, which the API returns as `409`. Cancelled bookings fall outside the index, so cancelling frees the slot. Cancellation itself is a single conditional `UPDATE ... WHERE status = 'confirmed'`, so simultaneous cancels produce one `200` and the rest `409`.

### Retries never create duplicates

Each `Idempotency-Key` is stored with a SHA-256 fingerprint of the request body (canonical JSON, so key order and whitespace don't matter) and the exact response that was sent. The key is claimed with `INSERT ... ON CONFLICT DO NOTHING` inside the same transaction as the booking, so a duplicate arriving at the same instant waits for the first to commit and then replays its response: same status, same booking `id`. Reusing a key with a different body returns `422`. The web page generates one key per booking attempt and reuses it when it retries after a network failure.

### Rate limiting that holds under bursts

Attempts are recorded per email in Postgres, so the limit survives restarts and would hold across multiple instances. A per-email advisory lock makes counting atomic, so a burst of 10 simultaneous attempts lets exactly 5 through. Failed attempts count, replays of an already-answered request don't, and a `429` is never stored as a key's response, so the same request can succeed once the window frees up. `Retry-After` is the whole number of seconds until the oldest counted attempt leaves the window.

### Order of checks for a booking

```mermaid
flowchart TD
    A[POST /bookings] --> B{Idempotency-Key already answered?}
    B -->|yes| R[Replay saved response, or 422 if the body differs]
    B -->|no, or no key| C{5 attempts from this email in the last 60 s?}
    C -->|yes| L[429 with Retry-After]
    C -->|no| D{Fields present and email valid?}
    D -->|no| E1[400]
    D -->|yes| F{Slot exists and hasn't started?}
    F -->|no| E2[404 or 400]
    F -->|yes| G{Insert allowed by the unique index?}
    G -->|no| E3[409]
    G -->|yes| OK[201 Created]
```

The rate limit comes before validation so that failed attempts are counted, as the specification requires.

### Keyset pagination

Bookings carry an insertion-order sequence number, and a cursor encodes "after sequence N". Unlike offset paging, results don't shift when new bookings arrive between page loads. The query fetches `limit + 1` rows to know whether another page exists, so the last page always returns `next_cursor: null`, even when it is exactly full.

### The web page never holds the API key

Code in a web page is visible to every visitor, so the page can't carry the key. It calls `/web/api/...` paths instead, which the server maps to the real endpoints for just the five operations a patient needs. They run the same code, so validation, double-booking protection, idempotency and rate limiting all still apply, and `/reset` is unreachable from the page.

## Project structure

```text
app/
  main.py        app setup, error handlers, and every endpoint
  security.py    API key middleware and the web page's restricted paths
  bookings.py    booking creation: validation, rate limit, idempotency, insert
  schema.sql     tables and indexes, applied on startup
  db.py          connection pool
  config.py      settings from environment variables
  errors.py      the {"error": ...} exception type
  timeutil.py    timestamp parsing and formatting
static/
  index.html     the patient web page
scripts/
  check_levels.py  end-to-end checks for all five levels
render.yaml      service definition for Render
```

## Running locally

Requires Python 3.12 or newer and a PostgreSQL database.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/clinic
export API_KEY=change-me
uvicorn app.main:app --reload
```

The tables are created on startup. Open `http://localhost:8000` for the web page.

| Variable | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection string | required |
| `API_KEY` | Key clients send in `X-API-Key` | required |
| `RATE_LIMIT_MAX` | Booking attempts allowed per email per window | `5` |
| `RATE_LIMIT_WINDOW` | Window length in seconds | `60` |
| `DB_POOL_MAX` | Maximum database connections | `10` |

## Testing

`scripts/check_levels.py` runs 72 end-to-end checks against a running instance, covering every level, including the concurrency cases: 40 simultaneous bookings for one slot (exactly one succeeds), 30 simultaneous retries with one idempotency key (one booking), 10 simultaneous cancels, and a burst of 10 attempts from one email (exactly 5 limited).

```bash
pip install httpx
python scripts/check_levels.py --base http://localhost:8000 --key change-me
```

The script calls `/reset`, which deletes all bookings, so point it at a test instance.

## Deployment

Runs as a Python web service on Render, connected to a Neon PostgreSQL database. Secrets are supplied as environment variables and never committed. A database-free `/health` endpoint supports uptime monitoring without waking the database.

## Known limitations and next steps

- **Patient identity isn't verified.** The page lists bookings by email without proving the visitor owns that email. A production system would add sign-in or an emailed link.
- **Idempotency records are kept indefinitely.** A scheduled cleanup would expire them after about 24 hours.
- **Schema changes run at startup.** A migration tool such as Alembic would suit a growing schema.
- **The checks are a script rather than a CI suite.** Porting them to pytest and running them on every push would catch regressions automatically.
- **Free hosting sleeps when idle**, so the first request after a quiet period can be slow.
