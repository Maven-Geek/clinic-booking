-- Applied on startup. Every statement is idempotent so it is safe to run on each boot.

CREATE TABLE IF NOT EXISTS slots (
    id         TEXT PRIMARY KEY,
    provider   TEXT NOT NULL,
    start_raw  TEXT NOT NULL,          -- echoed back exactly as it was sent to /reset
    end_raw    TEXT NOT NULL,
    start_at   TIMESTAMPTZ NOT NULL,   -- parsed, used for sorting and the "already started" check
    end_at     TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS slots_start_idx ON slots (start_at, id);
CREATE INDEX IF NOT EXISTS slots_provider_idx ON slots (provider, start_at);

CREATE TABLE IF NOT EXISTS bookings (
    seq        BIGSERIAL UNIQUE,       -- insertion order, used as the pagination cursor
    id         TEXT PRIMARY KEY,
    slot_id    TEXT NOT NULL REFERENCES slots (id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    email      TEXT NOT NULL,          -- stored trimmed and lower-cased
    status     TEXT NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
    created_at TIMESTAMPTZ NOT NULL
);

-- Level 3: the database itself guarantees at most one confirmed booking per slot.
-- Concurrent inserts for the same slot serialise on this index; all but one fail.
-- Cancelled bookings are outside the index, so cancelling frees the slot.
CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_booking_per_slot
    ON bookings (slot_id) WHERE status = 'confirmed';

CREATE INDEX IF NOT EXISTS bookings_email_seq_idx ON bookings (email, seq);

-- Level 4: one row per Idempotency-Key, holding the request fingerprint and the response we sent.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key           TEXT PRIMARY KEY,
    request_hash  TEXT NOT NULL,
    status_code   INTEGER,
    response_body JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Level 5: one row per counted booking attempt (sliding one-minute window per email).
CREATE TABLE IF NOT EXISTS booking_attempts (
    id    BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL,
    at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS booking_attempts_email_at_idx ON booking_attempts (email, at);
