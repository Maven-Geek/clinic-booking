"""End-to-end checks for all five levels, including the concurrency cases.

    python scripts/check_levels.py --base http://localhost:8000 --key YOUR_KEY

WARNING: this calls POST /reset, which wipes all bookings. Run it before you submit,
never after (the brief says not to change the deployment once submitted).
"""

import argparse
import asyncio
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx

RESULTS = {"pass": 0, "fail": 0}


def check(condition: bool, label: str, detail: object = "") -> bool:
    if condition:
        RESULTS["pass"] += 1
        print(f"  PASS  {label}")
    else:
        RESULTS["fail"] += 1
        print(f"  FAIL  {label}  {detail}")
    return condition


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def unique_email(tag: str) -> str:
    return f"{tag}-{uuid.uuid4().hex[:8]}@example.com"


class Api:
    def __init__(self, base: str, key: str):
        self.client = httpx.AsyncClient(
            base_url=base.rstrip("/"), timeout=60,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=100),
        )
        self.key = key

    def h(self, extra: dict | None = None, key: str | None = "default") -> dict:
        headers = {} if key is None else {"X-API-Key": self.key if key == "default" else key}
        headers.update(extra or {})
        return headers

    async def reset(self, slots):
        return await self.client.post("/reset", json={"slots": slots}, headers=self.h())

    async def slots(self, **params):
        return await self.client.get("/slots", params=params, headers=self.h())

    async def book(self, body, idem=None):
        extra = {"Idempotency-Key": idem} if idem else None
        return await self.client.post("/bookings", json=body, headers=self.h(extra))

    async def get(self, booking_id):
        return await self.client.get(f"/bookings/{booking_id}", headers=self.h())

    async def cancel(self, booking_id):
        return await self.client.delete(f"/bookings/{booking_id}", headers=self.h())

    async def mine(self, **params):
        return await self.client.get("/bookings", params=params, headers=self.h())


def make_slots():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    future = now + timedelta(days=30)
    slots = [
        {"id": "s3", "provider": "Dr Bola", "start": iso(future + timedelta(hours=2)), "end": iso(future + timedelta(hours=2, minutes=30))},
        {"id": "s1", "provider": "Dr Ade", "start": iso(future), "end": iso(future + timedelta(minutes=30))},
        {"id": "s2", "provider": "Dr Ade", "start": iso(future + timedelta(hours=1)), "end": iso(future + timedelta(hours=1, minutes=30))},
        {"id": "past", "provider": "Dr Ade", "start": iso(now - timedelta(days=1)), "end": iso(now - timedelta(days=1) + timedelta(minutes=30))},
    ]
    for i in range(20):
        start = future + timedelta(days=1, minutes=30 * i)
        slots.append({"id": f"x{i}", "provider": "Dr Chi", "start": iso(start), "end": iso(start + timedelta(minutes=30))})
    return slots


async def level1(api: Api):
    print("\nLevel 1: reset, list slots, book, look up")
    slots = make_slots()
    r = await api.reset(slots)
    check(r.status_code == 200, "POST /reset returns 200", r.text)

    r = await api.slots()
    data = r.json()["slots"]
    check(r.status_code == 200 and len(data) == len(slots), "GET /slots lists every slot, including past ones", len(data))
    starts = [s["start"] for s in data]
    check(starts == sorted(starts), "slots are sorted earliest start first")
    check(data[0]["id"] == "past", "the past slot comes first")
    s1 = next(s for s in data if s["id"] == "s1")
    sent = next(s for s in slots if s["id"] == "s1")
    check(s1 == {**sent, "available": True}, "slot fields echo exactly what was sent, plus available", s1)

    r = await api.slots(provider="Dr Ade")
    check(sorted(s["id"] for s in r.json()["slots"]) == ["past", "s1", "s2"], "?provider= filters by exact match", r.text)
    r = await api.slots(provider="dr ade")
    check(r.json()["slots"] == [], "provider match is exact (case-sensitive)")

    email = unique_email("ada")
    r = await api.book({"slot_id": "s1", "name": "Ada Obi", "email": email})
    b = r.json()
    check(r.status_code == 201, "POST /bookings returns 201", r.text)
    check(b.get("status") == "confirmed" and b.get("slot_id") == "s1" and b.get("id"), "booking is confirmed with an id", b)
    check(bool(re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$", b.get("created_at", ""))), "created_at is an ISO timestamp", b.get("created_at"))

    r = await api.get(b["id"])
    check(r.status_code == 200 and r.json() == b, "GET /bookings/{id} returns the booking", r.text)
    r = await api.get("does-not-exist")
    check(r.status_code == 404 and "error" in r.json(), "unknown booking id returns 404 {error}", r.text)

    r = await api.slots()
    check(next(s for s in r.json()["slots"] if s["id"] == "s1")["available"] is False, "booked slot shows available: false")


async def level2(api: Api):
    print("\nLevel 2: API key and validation")
    await api.reset(make_slots())
    c = api.client
    for method, path in [("GET", "/slots"), ("POST", "/bookings"), ("GET", "/bookings/x"), ("DELETE", "/bookings/x"),
                         ("GET", "/bookings?email=a@b.co"), ("POST", "/reset")]:
        r = await c.request(method, path, json={} if method == "POST" else None)
        check(r.status_code == 401 and "error" in r.json(), f"{method} {path} without key returns 401", r.status_code)
    r = await c.get("/slots", headers={"X-API-Key": "wrong"})
    check(r.status_code == 401, "wrong key returns 401", r.status_code)

    good = {"slot_id": "s1", "name": "Ada Obi", "email": unique_email("v")}
    cases = [
        ("missing slot_id", {k: v for k, v in good.items() if k != "slot_id"}),
        ("missing name", {k: v for k, v in good.items() if k != "name"}),
        ("missing email", {k: v for k, v in good.items() if k != "email"}),
        ("empty name", {**good, "name": "   "}),
        ("empty slot_id", {**good, "slot_id": ""}),
        ("invalid email", {**good, "email": "not-an-email"}),
        ("email without domain dot", {**good, "email": "ada@example"}),
        ("non-string name", {**good, "name": 42}),
    ]
    for label, body in cases:
        body = {**body}
        if "email" in body and isinstance(body["email"], str) and "@example.com" in body["email"]:
            body["email"] = unique_email("v")  # fresh email so the rate limit never interferes
        r = await api.book(body)
        check(r.status_code == 400 and "error" in r.json(), f"{label} returns 400", (r.status_code, r.text))

    r = await c.post("/bookings", content=b"{not json", headers=api.h({"Content-Type": "application/json"}))
    check(r.status_code == 400, "malformed JSON returns 400", r.status_code)

    r = await api.book({**good, "slot_id": "nope", "email": unique_email("v")})
    check(r.status_code == 404 and r.json().get("error"), "unknown slot returns 404", r.text)
    r = await api.book({**good, "slot_id": "past", "email": unique_email("v")})
    check(r.status_code == 400, "slot that already started returns 400", r.text)

    raw_email = "  Ada.OBI+" + uuid.uuid4().hex[:6] + "@Example.COM  "
    r = await api.book({"slot_id": "s2", "name": "  Ada Obi  ", "email": raw_email})
    b = r.json()
    check(r.status_code == 201 and b["name"] == "Ada Obi" and b["email"] == raw_email.strip().lower(),
          "name and email are trimmed, email stored lower-case", b)


async def level3(api: Api):
    print("\nLevel 3: no double booking, even concurrently; cancellation")
    await api.reset(make_slots())
    r = await api.book({"slot_id": "s1", "name": "A", "email": unique_email("a")})
    first = r.json()
    r = await api.book({"slot_id": "s1", "name": "B", "email": unique_email("b")})
    check(r.status_code == 409 and "error" in r.json(), "booking a booked slot returns 409", r.text)

    n = 40
    rs = await asyncio.gather(*[api.book({"slot_id": "s2", "name": f"P{i}", "email": unique_email(f"race{i}")}) for i in range(n)])
    codes = [x.status_code for x in rs]
    check(codes.count(201) == 1 and codes.count(409) == n - 1, f"{n} simultaneous requests: exactly one 201, rest 409",
          {c: codes.count(c) for c in set(codes)})

    r = await api.cancel(first["id"])
    check(r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["id"] == first["id"], "DELETE cancels the booking", r.text)
    r = await api.get(first["id"])
    check(r.json()["status"] == "cancelled", "GET shows the booking as cancelled")
    r = await api.slots()
    check(next(s for s in r.json()["slots"] if s["id"] == "s1")["available"] is True, "cancelling frees the slot")
    r = await api.cancel(first["id"])
    check(r.status_code == 409, "cancelling twice returns 409", r.text)
    r = await api.cancel("does-not-exist")
    check(r.status_code == 404, "cancelling an unknown booking returns 404", r.text)
    r = await api.book({"slot_id": "s1", "name": "C", "email": unique_email("c")})
    check(r.status_code == 201, "a freed slot can be booked again", r.text)

    again = r.json()
    rs = await asyncio.gather(*[api.cancel(again["id"]) for _ in range(10)])
    codes = [x.status_code for x in rs]
    check(codes.count(200) == 1 and codes.count(409) == 9, "10 simultaneous cancels: one 200, rest 409", codes)


async def level4(api: Api):
    print("\nLevel 4: idempotency and pagination")
    await api.reset(make_slots())
    key = str(uuid.uuid4())
    body = {"slot_id": "s1", "name": "Ada", "email": unique_email("idem")}
    r1 = await api.book(body, idem=key)
    r2 = await api.book(body, idem=key)
    check(r1.status_code == 201 and r2.status_code == 201 and r1.json()["id"] == r2.json()["id"],
          "same key + same body: same status and same booking id", (r1.text, r2.text))
    r = await api.mine(email=body["email"])
    check(len(r.json()["bookings"]) == 1, "the retry did not create a second booking", r.text)
    r = await api.book({**body, "name": "Someone Else"}, idem=key)
    check(r.status_code == 422 and "error" in r.json(), "same key + different body returns 422", r.text)
    r = await api.book({"email": body["email"], "name": "Ada", "slot_id": "s1"}, idem=key)
    check(r.status_code == 201 and r.json()["id"] == r1.json()["id"], "key order in the JSON doesn't make it a different body", r.text)

    key = str(uuid.uuid4())
    body = {"slot_id": "s2", "name": "Bo", "email": unique_email("idemrace")}
    await asyncio.gather(*[api.client.get("/") for _ in range(30)])  # open connections so the copies truly race
    rs = await asyncio.gather(*[api.book(body, idem=key) for _ in range(30)])
    ids = {x.json().get("id") for x in rs}
    codes = {x.status_code for x in rs}
    check(codes == {201} and len(ids) == 1, "30 simultaneous retries with one key: all 201, one booking id", (codes, ids))
    r = await api.mine(email=body["email"])
    check(len(r.json()["bookings"]) == 1, "...and only one booking exists", r.text)
    codes = [(await api.book({**body, "slot_id": "missing"})).status_code for _ in range(5)]
    check(codes == [404, 404, 404, 404, 429], "...and the key counted as one rate-limit attempt, not 30", codes)

    key = str(uuid.uuid4())
    body = {"slot_id": "s1", "name": "Late", "email": unique_email("idem409")}
    r1 = await api.book(body, idem=key)
    r2 = await api.book(body, idem=key)
    check(r1.status_code == 409 and r2.status_code == 409, "a replayed error returns the same error", (r1.status_code, r2.status_code))

    print("  -- pagination")
    email = unique_email("Pager").replace("pager", "Pager")
    created = []
    for i in range(5):
        r = await api.book({"slot_id": f"x{i}", "name": f"P{i}", "email": email})
        created.append(r.json()["id"])
    await api.book({"slot_id": "x10", "name": "Other", "email": unique_email("other")})

    pages, cursor = [], None
    for _ in range(5):
        params = {"email": email.upper(), "limit": 2}
        if cursor:
            params["cursor"] = cursor
        r = await api.mine(**params)
        if not check(r.status_code == 200, f"page {len(pages) + 1} returns 200", r.text):
            break
        data = r.json()
        pages.append([b["id"] for b in data["bookings"]])
        cursor = data["next_cursor"]
        if cursor is None:
            break
    check([len(p) for p in pages] == [2, 2, 1], "limit=2 over 5 bookings gives pages of 2, 2, 1", pages)
    check([i for p in pages for i in p] == created, "pages are oldest first, no gaps or repeats")
    check(cursor is None, "last page has next_cursor: null")

    r = await api.mine(email=email, limit=5)
    check(len(r.json()["bookings"]) == 5 and r.json()["next_cursor"] is None, "exactly a full last page still ends with next_cursor: null", r.text)
    r = await api.mine(email=email)
    check(len(r.json()["bookings"]) == 5, "default limit (10) returns all 5")
    for bad in ["0", "51", "abc", "-1", "2.5"]:
        r = await api.mine(email=email, limit=bad)
        check(r.status_code == 400 and "error" in r.json(), f"limit={bad} returns 400", r.status_code)
    r = await api.mine(email=unique_email("nobody"))
    check(r.json() == {"bookings": [], "next_cursor": None}, "unknown email returns an empty page", r.text)
    await api.cancel(created[0])
    r = await api.mine(email=email, limit=1)
    check(r.json()["bookings"][0]["status"] == "cancelled", "cancelled bookings are still listed")


async def level5(api: Api):
    print("\nLevel 5: rate limit per email")
    await api.reset(make_slots())
    email = unique_email("spam")
    codes = []
    for i in range(5):
        slot = f"x{i}" if i % 2 == 0 else "missing-slot"   # mix of successes and failures
        r = await api.book({"slot_id": slot, "name": "Spam", "email": email})
        codes.append(r.status_code)
    check(429 not in codes, "first 5 attempts (some failing) are not limited", codes)
    r = await api.book({"slot_id": "x9", "name": "Spam", "email": email.upper()})
    ra = r.headers.get("Retry-After", "")
    check(r.status_code == 429 and "error" in r.json(), "6th attempt returns 429 (email case-insensitive)", r.text)
    check(ra.isdigit() and 1 <= int(ra) <= 60, "Retry-After is whole seconds", ra)
    r = await api.book({"slot_id": "x9", "name": "Fine", "email": unique_email("other")})
    check(r.status_code == 201, "a different email is not affected", r.text)

    email = unique_email("burst")
    rs = await asyncio.gather(*[api.book({"slot_id": f"x{10 + i}", "name": "B", "email": email}) for i in range(10)])
    codes = [x.status_code for x in rs]
    check(codes.count(429) == 5, "10 simultaneous attempts: exactly 5 limited", codes)


async def web_bridge(api: Api):
    print("\nWeb page bridge")
    c = api.client
    r = await c.get("/")
    check(r.status_code == 200 and "<html" in r.text.lower(), "GET / serves the web page")
    r = await c.get("/web/api/slots")
    check(r.status_code == 200 and "slots" in r.json(), "page can list slots without shipping the key")
    r = await c.post("/web/api/reset", json={"slots": []})
    check(r.status_code == 404, "/reset is not reachable through the page bridge", r.status_code)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--levels", default="1,2,3,4,5,web")
    args = parser.parse_args()
    api = Api(args.base, args.key)
    runners = {"1": level1, "2": level2, "3": level3, "4": level4, "5": level5, "web": web_bridge}
    try:
        for name in args.levels.split(","):
            await runners[name.strip()](api)
    finally:
        await api.client.aclose()
    print(f"\n{RESULTS['pass']} passed, {RESULTS['fail']} failed")
    sys.exit(1 if RESULTS["fail"] else 0)


if __name__ == "__main__":
    asyncio.run(main())
