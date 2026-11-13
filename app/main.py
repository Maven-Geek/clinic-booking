import base64
import binascii
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db
from .bookings import booking_json, create_booking
from .errors import ApiError, error_response
from .security import ApiKeyMiddleware
from .timeutil import parse_timestamp

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.connect()
    yield
    await db.disconnect()


app = FastAPI(title="Clinic booking API", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

# Starlette runs the last-added middleware first: CORS wraps the key check,
# so even 401 responses carry CORS headers and browser preflights are answered.
app.add_middleware(ApiKeyMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key", "Idempotency-Key"],
    expose_headers=["Retry-After"],
)


# ---------------------------------------------------------------- every error is {"error": "..."}


@app.exception_handler(ApiError)
async def _api_error(_: Request, exc: ApiError):
    return error_response(exc.status_code, exc.message, exc.headers)


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, exc: StarletteHTTPException):
    messages = {404: "not found", 405: "method not allowed"}
    return error_response(exc.status_code, messages.get(exc.status_code, str(exc.detail)))


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError):
    return error_response(400, "invalid request")


@app.exception_handler(Exception)
async def _unexpected(_: Request, exc: Exception):
    return error_response(500, "internal server error")


async def read_json(request: Request) -> object:
    raw = await request.body()
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "request body must be valid JSON")


def slot_json(row: asyncpg.Record) -> dict:
    return {
        "id": row["id"],
        "provider": row["provider"],
        "start": row["start_raw"],
        "end": row["end_raw"],
        "available": row["available"],
    }


# ---------------------------------------------------------------- web page + health


@app.get("/", include_in_schema=False)
async def web_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", include_in_schema=False)
async def health():
    # Deliberately doesn't touch the database: an uptime pinger can keep the web service
    # awake without keeping a serverless database awake (and burning its free hours).
    return {"ok": True}


# ---------------------------------------------------------------- level 1: reset, slots


@app.post("/reset")
async def reset(request: Request):
    body = await read_json(request)
    slots = body.get("slots") if isinstance(body, dict) else None
    if not isinstance(slots, list):
        raise ApiError(400, "body must be an object with a 'slots' array")

    rows, seen = [], set()
    for i, slot in enumerate(slots):
        if not isinstance(slot, dict):
            raise ApiError(400, f"slots[{i}] must be an object")
        values = {f: slot.get(f) for f in ("id", "provider", "start", "end")}
        bad = [f for f, v in values.items() if not isinstance(v, str) or not v.strip()]
        if bad:
            raise ApiError(400, f"slots[{i}] is missing {', '.join(bad)}")
        if values["id"] in seen:
            raise ApiError(400, f"duplicate slot id: {values['id']}")
        seen.add(values["id"])
        try:
            start_at, end_at = parse_timestamp(values["start"]), parse_timestamp(values["end"])
        except ValueError:
            raise ApiError(400, f"slots[{i}] has an invalid start or end timestamp")
        rows.append((values["id"], values["provider"], values["start"], values["end"], start_at, end_at))

    async with db.pool().acquire() as conn:
        async with conn.transaction():
            # Bookings, idempotency records and rate-limit counters all describe the old slots.
            await conn.execute("TRUNCATE bookings, idempotency_keys, booking_attempts, slots")
            await conn.executemany(
                "INSERT INTO slots (id, provider, start_raw, end_raw, start_at, end_at) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                rows,
            )
    return {"ok": True, "slots": len(rows)}


@app.get("/slots")
async def list_slots(request: Request):
    provider = request.query_params.get("provider")
    sql = """
        SELECT s.id, s.provider, s.start_raw, s.end_raw,
               NOT EXISTS (
                   SELECT 1 FROM bookings b WHERE b.slot_id = s.id AND b.status = 'confirmed'
               ) AS available
        FROM slots s
        {where}
        ORDER BY s.start_at, s.id
    """
    if provider is None:
        rows = await db.pool().fetch(sql.format(where=""))
    else:
        rows = await db.pool().fetch(sql.format(where="WHERE s.provider = $1"), provider)
    return {"slots": [slot_json(r) for r in rows]}


# ---------------------------------------------------------------- bookings


@app.post("/bookings", status_code=201)
async def book(request: Request):
    outcome = await create_booking(await request.body(), request.headers.get("idempotency-key"))
    return JSONResponse(outcome.body, status_code=outcome.status_code, headers=outcome.headers)


LIMIT_RE = re.compile(r"^[0-9]{1,3}$")


def _encode_cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(f"seq:{seq}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> int:
    try:
        decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        prefix, value = decoded.split(":", 1)
        if prefix != "seq":
            raise ValueError
        return int(value)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ApiError(400, "invalid cursor")


@app.get("/bookings")
async def list_bookings(request: Request):
    """Level 4: a person's bookings, oldest first, with cursor pagination."""
    params = request.query_params
    email = (params.get("email") or "").strip().lower()
    if not email:
        raise ApiError(400, "email is required")

    raw_limit = params.get("limit")
    if raw_limit is None:
        limit = 10
    elif LIMIT_RE.match(raw_limit) and 1 <= int(raw_limit) <= 50:
        limit = int(raw_limit)
    else:
        raise ApiError(400, "limit must be an integer from 1 to 50")

    cursor = params.get("cursor")
    after = _decode_cursor(cursor) if cursor else 0

    # Fetch one extra row to know whether another page exists, so the last page
    # really ends with next_cursor: null (never a cursor pointing at an empty page).
    rows = await db.pool().fetch(
        """
        SELECT seq, id, slot_id, name, email, status, created_at
        FROM bookings WHERE email = $1 AND seq > $2
        ORDER BY seq LIMIT $3
        """,
        email, after, limit + 1,
    )
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1]["seq"]) if len(rows) > limit else None
    return {"bookings": [booking_json(r) for r in page], "next_cursor": next_cursor}


@app.get("/bookings/{booking_id}")
async def get_booking(booking_id: str):
    row = await db.pool().fetchrow(
        "SELECT id, slot_id, name, email, status, created_at FROM bookings WHERE id = $1", booking_id
    )
    if row is None:
        raise ApiError(404, "booking not found")
    return booking_json(row)


@app.delete("/bookings/{booking_id}")
async def cancel_booking(booking_id: str):
    """Level 3: cancel. The conditional UPDATE makes concurrent cancels safe: only one wins."""
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE bookings SET status = 'cancelled'
            WHERE id = $1 AND status = 'confirmed'
            RETURNING id, slot_id, name, email, status, created_at
            """,
            booking_id,
        )
        if row is not None:
            return booking_json(row)
        exists = await conn.fetchval("SELECT 1 FROM bookings WHERE id = $1", booking_id)
    if exists:
        raise ApiError(409, "booking already cancelled")
    raise ApiError(404, "booking not found")
