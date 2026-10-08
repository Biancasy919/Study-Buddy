"""Storage layer tests. Runs against SQLite, and against a real Postgres when started with `pg`:
   .venv/bin/python tests/test_db.py        (SQLite)
   .venv/bin/python tests/test_db.py pg     (embedded Postgres, same engine as Neon)"""
import os, sys, tempfile, threading, time
from pathlib import Path
tmp = tempfile.mkdtemp()
if "pg" in sys.argv:
    import pgserver
    srv = pgserver.get_server(Path(tmp) / "pg", cleanup_mode="stop")
    os.environ["DATABASE_URL"] = srv.get_uri()
else:
    os.environ["DATABASE_URL"] = ""
    os.environ["LOCAL_DB"] = str(Path(tmp) / "t.db")
os.environ["DAILY_BUDGET_USD"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import db
OK = BAD = 0
def check(n, c):
    global OK, BAD
    print(("  ok   " if c else "  FAIL ") + n); OK += bool(c); BAD += (not c)

print("engine:", "postgres" if db.IS_PG else "sqlite")
db.ensure_schema(); db.ensure_schema()
db.PLANS["p1"] = {"id": "p1", "chat_id": 7, "course": "C", "units": [1, 2]}
check("save and load a plan", db.PLANS["p1"]["course"] == "C" and "p1" in db.PLANS and "nope" not in db.PLANS)
check("defaults filled in", db.PLANS["p1"]["nq"] == 3)
db.update_plan("p1", step=3, extra="x")
check("update merges fields", db.PLANS["p1"]["step"] == 3 and db.PLANS["p1"]["course"] == "C")
check("find by chat", [p["id"] for p in db.PLANS.for_chat(7)] == ["p1"] and db.PLANS.for_chat(8) == [])
check("update of a missing plan is harmless", db.update_plan("zzz", a=1) is None)
check("claim wins once", db.claim("p1", "last_sent", "2026-10-08") and not db.claim("p1", "last_sent", "2026-10-08") and db.claim("p1", "last_sent", "2026-10-09"))

wins = []
def racer():
    wins.append(db.claim("p1", "last_sent", "RACE"))
ts = [threading.Thread(target=racer) for _ in range(8)]
[t.start() for t in ts]; [t.join() for t in ts]
check("8 simultaneous claims: exactly one wins", sum(wins) == 1)
def inc():
    for _ in range(5):
        with db.tx() as t:
            r = t.q("SELECT data FROM plans WHERE id=%s FOR UPDATE", ("p1",))
            import json; p = json.loads(r[0]["data"]); p["n"] = p.get("n", 0) + 1
            t.x("UPDATE plans SET data=%s WHERE id=%s", (json.dumps(p), "p1"))
ts = [threading.Thread(target=inc) for _ in range(4)]
[t.start() for t in ts]; [t.join() for t in ts]
check("20 concurrent read-modify-writes lose nothing", db.PLANS["p1"]["n"] == 20)

db.put_material("p1", "file", "w1.pdf", "slide one"); db.put_material("p1", "file", "w1.pdf", "slide one v2"); db.put_material("p1", "outline", "o.pdf", "weeks")
check("materials stored and replaced", db.get_materials("p1", "file") == {"w1.pdf": "slide one v2"} and db.material_names("p1", "outline") == ["o.pdf"])
check("material totals", db.material_totals("p1") == (2, len("slide one v2") + len("weeks")))
db.cache_put("prep:p1:3", {"a": [1]}); db.cache_put("prep:p1:4", {"b": 2})
check("cache get/pop", db.cache_get("prep:p1:3") == {"a": [1]} and db.cache_pop("prep:p1:3") == {"a": [1]} and db.cache_get("prep:p1:3") is None)
check("seen updates are dropped", db.first_time(101) and not db.first_time(101) and db.first_time(102))

order = []
def worker(n):
    with db.lease("chat:1", ttl=30, wait=10):
        order.append(("in", n)); time.sleep(0.4); order.append(("out", n))
ts = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
[t.start() for t in ts]; [t.join() for t in ts]
ok = all(order[i][0] == "in" and order[i + 1][0] == "out" and order[i][1] == order[i + 1][1] for i in range(0, 6, 2))
check("lock: one at a time, nobody overlaps", ok and len(order) == 6)
with db.lease("chat:2", ttl=0.2): pass
x = db.x("INSERT INTO locks (key, until) VALUES ('chat:3', %s)", (time.time() - 5,))
with db.lease("chat:3", ttl=5, wait=1): got = True
check("an expired lock can be taken over", got)

check("rate limit counts then blocks", [db.bump("ip:1", 3, 60) for _ in range(5)] == [True, True, True, False, False])
check("different key unaffected", db.bump("ip:2", 3, 60))
db.budget_check(); db.budget_record(2_000_000, 500_000)
try:
    db.budget_check(); capped = False
except db.BudgetExceeded:
    capped = True
check("daily budget cap trips", capped and db.usage_today()[0] == 1)
db.PLANS.delete("p1")
check("deleting a plan removes its material and cache", "p1" not in db.PLANS and db.get_materials("p1", "file") == {} and db.cache_get("prep:p1:4") is None)
print(f"\n{OK} passed, {BAD} failed")
sys.exit(1 if BAD else 0)
