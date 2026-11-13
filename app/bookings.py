"""Creating a booking: validation (L2), one confirmed booking per slot (L3),
Idempotency-Key handling (L4) and per-email rate limiting (L5).

The order of checks inside create_booking matters:
  1. Idempotency replay  - a retry of a request we already answered gets the stored answer,
                           and does not count against the rate limit.
  2. Rate limit          - counts every new attempt for that email, including ones that then
                           fail validation, 404 or 409. A 429 is never stored for a key.
  3. Validation, slot lookup, insert.

Locks are always taken in the same order (idempotency key, then email, then slot),
so concurrent requests can wait on each other but never deadlock.
"""

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass, field

import asyncpg

from . import config, db
from .timeutil import format_timestamp, now_utc

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MAX_IDEMPOTENCY_KEY_LENGTH = 255


@dataclass
class Outcome:
    status_code: int
    body: dict
    headers: dict[str, str] = field(default_factory=dict)


def booking_json(row: asyncpg.Record) -> dict:
    return {
        "id": row["id"],
        "slot_id": row["slot_id"],
        "name": row["name"],
        "email": row["email"],
        "status": row["status"],
        "created_at": format_timestamp(row["created_at"]),
    }


def _error(status_code: int, message: str, headers: dict[str, str] | None = None) -> Outcome:
    return Outcome(status_code, {"error": message}, headers or {})


# ---------------------------------------------------------------- request parsing


def _parse(raw: bytes) -> tuple[object | None, str]:
    """Return (payload, fingerprint). The fingerprint decides "same body" for idempotency:
    JSON is compared by content, so key order and whitespace don't matter."""
    try:
        payload = json.loads(raw) if raw else None
    except (ValueError, UnicodeDecodeError):
        return None, hashlib.sha256(raw).hexdigest()
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return payload, hashlib.sha256(canonical.encode()).hexdigest()


def _clean(value: object) -> str | None:
    """Trimmed string, or None if the field is missing, not a string, or blank."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _validate(payload: object) -> tuple[dict | None, Outcome | None]:
    if not isinstance(payload, dict):
        return None, _error(400, "request body must be a JSON object")

    slot_id = payload.get("slot_id")
    slot_id = slot_id if isinstance(slot_id, str) and slot_id.strip() else None
    name = _clean(payload.get("name"))
    email = _clean(payload.get("email"))

    missing = [f for f, v in (("slot_id", slot_id), ("name", name), ("email", email)) if v is None]
    if missing:
        return None, _error(400, f"missing or empty field: {', '.join(missing)}")

    email = email.lower()
    if not EMAIL_RE.match(email):
        return None, _error(400, "email is not valid")

    return {"slot_id": slot_id, "name": name, "email": email}, None


# ---------------------------------------------------------------- level 3: the booking itself


async def _book(conn: asyncpg.Connection, payload: object) -> Outcome:
    fields, problem = _validate(payload)
    if problem:
        return problem

    slot = await conn.fetchrow("SELECT start_at FROM slots WHERE id = $1", fields["slot_id"])
    if slot is None:
        return _error(404, "slot not found")
    if slot["start_at"] <= now_utc():
        return _error(400, "slot has already started")

    try:
        # A savepoint, so a conflict here doesn't abort an enclosing idempotency transaction.
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO bookings (id, slot_id, name, email, status, created_at)
                VALUES ($1, $2, $3, $4, 'confirmed', clock_timestamp())
                RETURNING id, slot_id, name, email, status, created_at
                """,
                str(uuid.uuid4()), fields["slot_id"], fields["name"], fields["email"],
            )
    except asyncpg.UniqueViolationError:
        # The partial unique index allows one confirmed booking per slot. When many requests
        # race for the same slot, Postgres lets exactly one insert win and rejects the rest.
        return _error(409, "slot already booked")
    except asyncpg.ForeignKeyViolationError:
        # The slot vanished between the lookup and the insert (a concurrent /reset).
        return _error(404, "slot not found")

    return Outcome(201, booking_json(row))


# ---------------------------------------------------------------- level 5: rate limit


class _RateLimited(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after


def _too_many(retry_after: int) -> Outcome:
    return _error(429, "too many booking attempts for this email, try again later",
                  {"Retry-After": str(retry_after)})


async def _register_attempt(conn: asyncpg.Connection, email: str) -> int | None:
    """Count one attempt for this email. Returns None if allowed, or the Retry-After seconds.
    Must run inside a transaction: the per-email lock is held until that transaction ends."""
    # Serialise attempts for the same email so 6 simultaneous requests can't all squeeze in.
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "rate:" + email)
    now = await conn.fetchval("SELECT clock_timestamp()")
    await conn.execute(
        "DELETE FROM booking_attempts WHERE email = $1 AND at <= $2::timestamptz - $3 * interval '1 second'",
        email, now, float(config.RATE_LIMIT_WINDOW),
    )
    recent = await conn.fetch("SELECT at FROM booking_attempts WHERE email = $1 ORDER BY at", email)
    if len(recent) >= config.RATE_LIMIT_MAX:
        # Rejected attempts aren't recorded, so the window frees up as soon as the oldest
        # counted attempt turns a minute old.
        frees_at = recent[len(recent) - config.RATE_LIMIT_MAX]["at"]
        wait = (frees_at - now).total_seconds() + config.RATE_LIMIT_WINDOW
        return max(1, math.ceil(wait))
    await conn.execute("INSERT INTO booking_attempts (email, at) VALUES ($1, $2)", email, now)
    return None


# ---------------------------------------------------------------- level 4: idempotency


async def _stored_response(conn: asyncpg.Connection, key: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        "SELECT request_hash, status_code, response_body FROM idempotency_keys WHERE key = $1", key
    )


def _replay(stored: asyncpg.Record, fingerprint: str) -> Outcome:
    if stored["request_hash"] != fingerprint:
        return _error(422, "Idempotency-Key was already used with a different request body")
    if stored["status_code"] is None:  # defensive; responses are saved in the same transaction
        return _error(409, "a request with this Idempotency-Key is still in progress")
    return Outcome(stored["status_code"], json.loads(stored["response_body"]))


# ---------------------------------------------------------------- entry point


async def create_booking(raw_body: bytes, idempotency_key: str | None) -> Outcome:
    payload, fingerprint = _parse(raw_body)
    key = (idempotency_key or "").strip() or None
    if key is not None and len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        return _error(400, f"Idempotency-Key must be at most {MAX_IDEMPOTENCY_KEY_LENGTH} characters")

    # Rate limiting is per email, when the body has one to count against.
    email = _clean(payload.get("email")) if isinstance(payload, dict) else None
    email = email.lower() if email else None

    async with db.pool().acquire() as conn:
        # No Idempotency-Key: count the attempt (committed on its own), then book.
        if key is None:
            if email is not None:
                async with conn.transaction():
                    retry_after = await _register_attempt(conn, email)
                if retry_after is not None:
                    return _too_many(retry_after)
            return await _book(conn, payload)

        # Fast path: a retry of a request we've already answered is replayed and not counted.
        stored = await _stored_response(conn, key)
        if stored is not None:
            return _replay(stored, fingerprint)

        # Claim the key, count the attempt and book, all in one transaction. A concurrent
        # request with the same key blocks on the claim until this commits, then finds the saved
        # response and replays it. So one key gives one booking and is counted once, however
        # many copies of the request arrive together.
        try:
            async with conn.transaction():
                claimed = await conn.fetchval(
                    """
                    INSERT INTO idempotency_keys (key, request_hash) VALUES ($1, $2)
                    ON CONFLICT (key) DO NOTHING RETURNING key
                    """,
                    key, fingerprint,
                )
                if claimed is None:
                    return _replay(await _stored_response(conn, key), fingerprint)

                if email is not None:
                    retry_after = await _register_attempt(conn, email)
                    if retry_after is not None:
                        # Roll back the claim: a 429 is never saved as the key's answer,
                        # so the same request can be retried once the window frees up.
                        raise _RateLimited(retry_after)

                outcome = await _book(conn, payload)
                await conn.execute(
                    "UPDATE idempotency_keys SET status_code = $2, response_body = $3::jsonb WHERE key = $1",
                    key, outcome.status_code, json.dumps(outcome.body),
                )
                return outcome
        except _RateLimited as limited:
            return _too_many(limited.retry_after)
