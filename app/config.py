import os


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is not set")
    return value


DATABASE_URL = _required("DATABASE_URL")
API_KEY = _required("API_KEY")

# Level 5 limits: at most RATE_LIMIT_MAX attempts per email in any RATE_LIMIT_WINDOW seconds.
RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_MAX", "5"))
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))

DB_POOL_MIN = int(os.environ.get("DB_POOL_MIN", "0"))
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "10"))
