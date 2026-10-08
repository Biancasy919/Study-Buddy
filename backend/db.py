"""Database layer.

Production (Vercel): Postgres on Neon, set by DATABASE_URL. Serverless functions forget everything between requests, so
every piece of state lives here: plans, extracted course material, Telegram update ids, per-chat locks, cached AI output,
daily usage. Local development: with no DATABASE_URL it uses a SQLite file, so you can run it on your computer with no setup.

All SQL below is written to run on both. Placeholders are %s; they are translated for SQLite.
"""
import contextlib
import json
import os
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
load_dotenv(HERE.parent / ".env")  # a single .env at the repo root works too

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
IS_PG = DATABASE_URL.startswith("postgres")
LOCAL_DB = Path(os.getenv("LOCAL_DB") or HERE / "data" / "local.db")
DAILY_BUDGET_USD = float(os.getenv("DAILY_BUDGET_USD", "5.00"))  # whole site, per day. 0 turns the cap off
PRICE_IN = float(os.getenv("PRICE_IN_PER_M", "0.30"))             # USD per million input tokens (keep at or above your model's rate)
PRICE_OUT = float(os.getenv("PRICE_OUT_PER_M", "2.50"))           # USD per million output tokens

_local = threading.local()


# ---------------------------------------------------------------- connections
def _connect():
    if IS_PG:
        import psycopg
        from psycopg.rows import dict_row
        # prepare_threshold=None: Neon's pooled endpoint (pgbouncer) doesn't support prepared statements
        return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row, connect_timeout=10, prepare_threshold=None)
    LOCAL_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(LOCAL_DB, timeout=15, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def conn():
    c = getattr(_local, "c", None)
    if c is None or getattr(c, "closed", False):
        c = _local.c = _connect()
    return c


def _sql(sql):
    return sql if IS_PG else sql.replace("%s", "?").replace(" FOR UPDATE", "")


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


def _retry(fn):
    """A serverless instance can wake up holding a connection the database already closed. Reconnect once."""
    try:
        return fn()
    except Exception as e:
        if IS_PG and e.__class__.__module__.startswith("psycopg") and "Operational" in e.__class__.__name__ or \
                IS_PG and "closed" in str(e).lower():
            _local.c = None
            return fn()
        raise


def q(sql, params=()):
    def go():
        cur = conn().execute(_sql(sql), params)
        return _rows(cur)
    return _retry(go)


def x(sql, params=()):
    def go():
        return conn().execute(_sql(sql), params).rowcount
    return _retry(go)


class _Tx:
    def __init__(self, c):
        self.c = c

    def q(self, sql, params=()):
        return _rows(self.c.execute(_sql(sql), params))

    def x(self, sql, params=()):
        return self.c.execute(_sql(sql), params).rowcount


@contextlib.contextmanager
def tx():
    """One atomic transaction. Reads that end in FOR UPDATE lock the row until it finishes."""
    c = conn()
    if IS_PG:
        with c.transaction():
            yield _Tx(c)
    else:
        c.execute("BEGIN IMMEDIATE")
        try:
            yield _Tx(c)
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


# ---------------------------------------------------------------- schema (also in schema.sql, for pasting into Neon's SQL editor)
_ready = False


def ensure_schema():
    global _ready
    if _ready:
        return
    text = "\n".join(l for l in (HERE / "schema.sql").read_text().splitlines() if not l.strip().startswith("--"))
    for stmt in [s.strip() for s in text.split(";") if s.strip()]:
        x(stmt)
    _ready = True
    if not IS_PG:
        _import_legacy()


def _import_legacy():
    """Local only: bring in plans from the old single-file version (data/plans.json and data/materials)."""
    old = HERE.parent / "data" / "plans.json"
    if not old.exists() or q("SELECT 1 AS a FROM plans LIMIT 1"):
        return
    for pid, p in json.loads(old.read_text()).items():
        PLANS[pid] = p
        mdir = HERE.parent / "data" / "materials" / pid
        if mdir.exists():
            for f in mdir.rglob("*.txt"):
                kind = f.parent.name if f.parent != mdir else ("outline" if f.name[:-4] == p.get("outline") else "file")
                put_material(pid, kind, f.name[:-4], f.read_text())


# ---------------------------------------------------------------- plans
def _fix(p):
    """Plans made before some options existed still load."""
    p.setdefault("size", "section")
    p.setdefault("depth", "standard")
    p.setdefault("nq", 3)
    if p.get("mode") == "biws":
        p["mode"] = "teach_test"
        p["size"] = {"full": "topic", "bite": "concept"}.get(p.get("format"), "section") if "format" in p else "topic"
    return p


class Plans:
    """Dict-like view of the plans table, so the app can use PLANS[pid], PLANS.get(pid), PLANS.values()."""

    def __getitem__(self, pid):
        r = q("SELECT data FROM plans WHERE id=%s", (pid,))
        if not r:
            raise KeyError(pid)
        return _fix(json.loads(r[0]["data"]))

    def get(self, pid, default=None):
        try:
            return self[pid]
        except KeyError:
            return default

    def __contains__(self, pid):
        return bool(q("SELECT 1 AS a FROM plans WHERE id=%s", (pid,)))

    def __setitem__(self, pid, plan):
        x("INSERT INTO plans (id, chat_id, data, updated) VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET "
          "chat_id=excluded.chat_id, data=excluded.data, updated=excluded.updated",
          (pid, plan.get("chat_id"), json.dumps(plan), time.time()))

    def values(self):
        return [_fix(json.loads(r["data"])) for r in q("SELECT data FROM plans")]

    def for_chat(self, chat_id):
        return [_fix(json.loads(r["data"])) for r in q("SELECT data FROM plans WHERE chat_id=%s", (chat_id,))]

    def delete(self, pid):
        x("DELETE FROM plans WHERE id=%s", (pid,))
        x("DELETE FROM materials WHERE plan_id=%s", (pid,))
        x("DELETE FROM cache WHERE key LIKE %s", (f"prep:{pid}:%",))


PLANS = Plans()


def update_plan(pid, **fields):
    """Atomically merge fields into a plan. Safe when two requests touch the same plan at once."""
    with tx() as t:
        r = t.q("SELECT data FROM plans WHERE id=%s FOR UPDATE", (pid,))
        if not r:
            return None
        p = _fix(json.loads(r[0]["data"]))
        p.update(fields)
        t.x("UPDATE plans SET chat_id=%s, data=%s, updated=%s WHERE id=%s", (p.get("chat_id"), json.dumps(p), time.time(), pid))
        return p


def claim(pid, key, value):
    """Set plan[key]=value only if it isn't already that. True for the one caller that wins (stops double deliveries)."""
    with tx() as t:
        r = t.q("SELECT data FROM plans WHERE id=%s FOR UPDATE", (pid,))
        if not r:
            return False
        p = json.loads(r[0]["data"])
        if p.get(key) == value:
            return False
        p[key] = value
        t.x("UPDATE plans SET data=%s, updated=%s WHERE id=%s", (json.dumps(p), time.time(), pid))
        return True


# ---------------------------------------------------------------- extracted course material (the browser reads the files; only text is stored)
def put_material(pid, kind, name, body):
    x("INSERT INTO materials (plan_id, kind, name, body) VALUES (%s,%s,%s,%s) ON CONFLICT (plan_id, kind, name) "
      "DO UPDATE SET body=excluded.body", (pid, kind, name, body))


def get_materials(pid, kind):
    return {r["name"]: r["body"] for r in q("SELECT name, body FROM materials WHERE plan_id=%s AND kind=%s ORDER BY name", (pid, kind))}


def material_names(pid, kind):
    return [r["name"] for r in q("SELECT name FROM materials WHERE plan_id=%s AND kind=%s ORDER BY name", (pid, kind))]


def material_totals(pid):
    r = q("SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(body)),0) AS chars FROM materials WHERE plan_id=%s", (pid,))[0]
    return int(r["n"]), int(r["chars"])


# ---------------------------------------------------------------- cache (prepared sessions, shorter guide lessons)
def cache_get(key):
    r = q("SELECT value FROM cache WHERE key=%s", (key,))
    return json.loads(r[0]["value"]) if r else None


def cache_put(key, value):
    x("INSERT INTO cache (key, value, at) VALUES (%s,%s,%s) ON CONFLICT (key) DO UPDATE SET value=excluded.value, at=excluded.at",
      (key, json.dumps(value), time.time()))


def cache_pop(key):
    v = cache_get(key)
    if v is not None:
        x("DELETE FROM cache WHERE key=%s", (key,))
    return v


def cache_clear(prefix):
    x("DELETE FROM cache WHERE key LIKE %s", (prefix + "%",))


# ---------------------------------------------------------------- Telegram retries, per-chat locks
def first_time(update_id):
    """True the first time an update is seen. Telegram re-sends an update if we're slow to answer; this drops the repeat."""
    return x("INSERT INTO updates (update_id, at) VALUES (%s,%s) ON CONFLICT (update_id) DO NOTHING", (update_id, time.time())) == 1


def prune(days=7):
    x("DELETE FROM updates WHERE at < %s", (time.time() - days * 86400,))
    x("DELETE FROM locks WHERE until < %s", (time.time(),))
    x("DELETE FROM counters WHERE until < %s", (time.time(),))


@contextlib.contextmanager
def lease(key, ttl=75, wait=30):
    """One request at a time per chat (and per plan). A second request waits, so a quiz answer never races the next question."""
    start = time.time()
    while True:
        now = time.time()
        if x("INSERT INTO locks (key, until) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET until=excluded.until "
             "WHERE locks.until < %s", (key, now + ttl, now)) == 1:
            break
        if time.time() - start > wait:
            raise TimeoutError(f"busy: {key}")
        time.sleep(0.35)
    try:
        yield
    finally:
        x("DELETE FROM locks WHERE key=%s", (key,))


# ---------------------------------------------------------------- spending cap and abuse limits
class BudgetExceeded(Exception):
    def __init__(self, user_message):
        super().__init__(user_message)
        self.user_message = user_message


def _today():
    return date.today().isoformat()


def usage_today():
    r = q("SELECT calls, cost FROM usage WHERE day=%s", (_today(),))
    return (r[0]["calls"], float(r[0]["cost"])) if r else (0, 0.0)


def budget_check():
    if DAILY_BUDGET_USD > 0 and usage_today()[1] >= DAILY_BUDGET_USD:
        raise BudgetExceeded("The study buddy has reached its daily AI limit, so it can't write or grade anything more today. "
                             "It resets tomorrow.")


def budget_record(tokens_in, tokens_out):
    cost = tokens_in / 1e6 * PRICE_IN + tokens_out / 1e6 * PRICE_OUT
    x("INSERT INTO usage (day, calls, cost) VALUES (%s,1,%s) ON CONFLICT (day) DO UPDATE SET calls=usage.calls+1, "
      "cost=usage.cost+excluded.cost", (_today(), cost))


def bump(key, limit, ttl):
    """Count an event under `key`; False once `limit` events have happened within `ttl` seconds."""
    now = time.time()
    with tx() as t:
        r = t.q("SELECT n, until FROM counters WHERE key=%s FOR UPDATE", (key,))
        if not r or r[0]["until"] < now:
            t.x("INSERT INTO counters (key, n, until) VALUES (%s,1,%s) ON CONFLICT (key) DO UPDATE SET n=1, until=excluded.until",
                (key, now + ttl))
            return True
        if r[0]["n"] >= limit:
            return False
        t.x("UPDATE counters SET n=n+1 WHERE key=%s", (key,))
        return True


# ---------------------------------------------------------------- the Wall Street guide (private copy loaded by import_guide.py)
def guide_put(key, obj):
    x("INSERT INTO guide_docs (key, body) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET body=excluded.body", (key, json.dumps(obj)))


def guide_get(key):
    r = q("SELECT body FROM guide_docs WHERE key=%s", (key,))
    return json.loads(r[0]["body"]) if r else None


def guide_has_image(path):
    return bool(q("SELECT 1 AS a FROM guide_images WHERE path=%s", (path,)))


def guide_put_image(path, data):
    x("INSERT INTO guide_images (path, data) VALUES (%s,%s) ON CONFLICT (path) DO UPDATE SET data=excluded.data", (path, data))


def guide_image(path):
    r = q("SELECT data FROM guide_images WHERE path=%s", (path,))
    return bytes(r[0]["data"]) if r else None


def guide_loaded():
    return bool(q("SELECT 1 AS a FROM guide_docs WHERE key=%s", ("plan",)))
