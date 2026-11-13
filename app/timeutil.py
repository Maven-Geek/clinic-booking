from datetime import datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO 8601 timestamp. A value without an offset is taken to be UTC."""
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_timestamp(value: datetime) -> str:
    """2026-10-29T22:54:52.530Z: UTC, millisecond precision, Z suffix (same shape as the spec)."""
    utc = value.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"
