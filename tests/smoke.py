"""Backend tests for the serverless setup. Telegram and the AI are stubbed, so nothing is sent and nothing costs money.
   .venv/bin/python tests/smoke.py        (SQLite)
   .venv/bin/python tests/smoke.py pg     (a real embedded Postgres, the same engine as Neon)"""
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

tmp = tempfile.mkdtemp()
if "pg" in sys.argv:
    import pgserver
    os.environ["DATABASE_URL"] = pgserver.get_server(Path(tmp) / "pg", cleanup_mode="stop").get_uri()
else:
    os.environ["DATABASE_URL"] = ""
    os.environ["LOCAL_DB"] = str(Path(tmp) / "t.db")
os.environ["NO_LEGACY_IMPORT"] = "1"
os.environ.update(TELEGRAM_BOT_TOKEN="123:abc", LLM_API_KEY="k", LLM_BASE_URL="https://llm.example/v1", LLM_MODEL="m1",
                  LLM_FALLBACK_MODEL="m2", CRON_SECRET="cs", FRONTEND_URL="https://front.example", TELEGRAM_BOT_USERNAME="TestBot",
                  DAILY_BUDGET_USD="1", BIWS_DIR=str(Path(tmp) / "none"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import app as A  # noqa: E402
import db  # noqa: E402

REAL_ASK = A.ask  # the stubs below replace it; the AI-call tests put it back

OK = BAD = 0


def check(name, cond):
    global OK, BAD
    print(("  ok   " if cond else "  FAIL ") + name)
    OK += bool(cond)
    BAD += not cond


sent = []


def fake_tg(method, **k):
    if method == "sendMessage":
        sent.append((k.get("chat_id"), k.get("text", "")))
    elif method in ("setWebhook", "deleteWebhook", "getWebhookInfo"):
        sent.append((method, k))
    return {"ok": True, "result": {"url": "x", "username": "TestBot"}}


A.tg = fake_tg
A.send_phrase_audio = lambda *a, **k: None
real_sleep = time.sleep
A.time.sleep = lambda s: None  # skip the app's own short pauses
ai_calls = []
SYLLABUS = {"course": "Chess openings", "units": [{"week": None, "title": "The Italian", "summary": "s", "files": []},
                                                  {"week": None, "title": "The Sicilian", "summary": "s2", "files": []}], "language": None}


def fake_ask_json(system, user, max_tokens=0, temperature=None):
    ai_calls.append("json")
    if "grade one quiz answer" in system:
        return {"verdict": "correct", "feedback": "good", "reveal": "x"}
    if "interview prep" in system or "write quiz questions" in system:
        return {"questions": [{"q": f"Question {len(ai_calls)}?", "answer": "a"}, {"q": "Q2?", "answer": "b"}]}
    if "learning coach" in system:
        return {"summary": "s", "strengths": [], "work_on": [], "next_step": "", "weak_topics": []}
    return SYLLABUS


A.ask_json = fake_ask_json
A.ask = lambda *a, **k: ai_calls.append("ask") or "A lesson."
c = A.app.test_client()
H = {"Origin": "https://front.example"}


def post(url, body, ip="1.1.1.1"):
    return c.post(url, json=body, headers={**H, "X-Forwarded-For": ip})


PLAN = {"kind": "custom", "topic": "Chess openings", "mode": "teach_test", "size": "section", "depth": "standard", "nq": 3,
        "days": [0, 1, 2, 3, 4, 5, 6], "time": "18:00", "tz": "UTC"}


def upd(uid, chat, text, typ="private"):
    return {"update_id": uid, "message": {"message_id": uid, "chat": {"id": chat, "type": typ}, "text": text}}


def webhook(u, secret=None):
    return c.post("/api/telegram", json=u, headers={"X-Telegram-Bot-Api-Secret-Token": secret or A.webhook_secret()})


print("engine:", "postgres" if db.IS_PG else "sqlite")
print("basics and cross-site access")
r = c.get("/api/health")
check("health reports the setup", r.status_code == 200 and r.get_json()["ai"] and r.get_json()["cron"])
r = c.options("/api/plans", headers={"Origin": "https://front.example", "Access-Control-Request-Method": "POST"})
check("the website's address is allowed to call the API", r.status_code == 204 and r.headers["Access-Control-Allow-Origin"] == "https://front.example")
r = c.options("/api/plans", headers={"Origin": "https://evil.example"})
check("another website is not", "Access-Control-Allow-Origin" not in r.headers)

print("building a plan: choices, uploads, build")
r = post("/api/plans", PLAN)
pid = r.get_json()["id"]
check("plan is created", r.status_code == 200 and db.PLANS[pid]["status"] == "new" and db.PLANS[pid]["chat_id"] is None)
check("bad choices rejected", post("/api/plans", {**PLAN, "days": []}).status_code == 400 and post("/api/plans", {**PLAN, "kind": "x"}).status_code == 400)
r = post(f"/api/plans/{pid}/build", {})
check("custom plan builds with no uploads", r.get_json()["status"] == "ready" and len(db.PLANS[pid]["units"]) == 2)
check("the build is skipped if it's already done", post(f"/api/plans/{pid}/build", {}).get_json()["status"] == "ready")

r = post("/api/plans", {**PLAN, "kind": "smu"}, ip="2.2.2.2")
spid = r.get_json()["id"]
r = post(f"/api/plans/{spid}/build", {}, ip="2.2.2.2")
check("SMU plan without uploads gives a clear error", r.get_json()["status"] == "error" and "outline" in r.get_json()["error"])
body = lambda kind, name, text: {"kind": kind, "name": name, "text": text}
check("slides accepted", post(f"/api/plans/{spid}/material", body("file", "Week 1.pdf", "[Page 1]\nCosts " * 20)).status_code == 200)
check("outline accepted", post(f"/api/plans/{spid}/material", body("outline", "outline.pdf", "Week 1 costs")).status_code == 200)
check("outline is replaced, not added", post(f"/api/plans/{spid}/material", body("outline", "outline2.pdf", "Week 1 and 2")).status_code == 200 and db.material_names(spid, "outline") == ["outline2.pdf"])
r = post(f"/api/plans/{spid}/material", body("exam", "scan.pdf", "tiny"))
check("a scanned paper (no text) is refused with a helpful message", r.status_code == 400 and "scanned" in r.get_json()["error"])
check("too much text in one file refused", post(f"/api/plans/{spid}/material", body("file", "big.txt", "x" * (A.MAX_TEXT + 1))).status_code == 400)
check("exam paper accepted", post(f"/api/plans/{spid}/material", body("exam", "midterm.pdf", "[Page 1]\nQ1. Compute break-even. " * 5)).status_code == 200)
check("unknown kind refused", post(f"/api/plans/{spid}/material", body("virus", "a", "text text")).status_code == 400)
post(f"/api/plans/{spid}/build", {}, ip="2.2.2.2")
sp = db.PLANS[spid]
check("SMU plan builds from the stored text", sp["status"] == "ready" and sp["banks"] == {"exam": ["midterm.pdf"]} and sp["outline"] == "outline2.pdf")
SYLLABUS["units"][0]["files"] = ["Week 1.pdf"]
post(f"/api/plans/{spid}/build", {}, ip="2.2.2.2")
c2 = A.unit_context(db.PLANS[spid], {"files": ["Week 1.pdf"]})
check("lessons read their material from the database", "Costs" in c2)
SYLLABUS["units"][0]["files"] = []
check("plan page shows a Telegram link with the bot's name", c.get(f"/api/plans/{pid}", headers=H).get_json()["link"] == f"https://t.me/TestBot?start={pid}")
check("editing topics works and keeps files valid", post(f"/api/plans/{pid}/material", body("file", "n.txt", "notes notes")).status_code == 200 and
      c.put(f"/api/plans/{pid}/units", json={"units": [{"title": "A", "week": 1, "files": ["n.txt", "nope.txt"]}]}, headers=H).status_code == 200
      and db.PLANS[pid]["units"][0]["files"] == ["n.txt"])
db.update_plan(pid, units=SYLLABUS["units"])
check("one visitor can only create so many plans a day", [post("/api/plans", PLAN, ip="9.9.9.9").status_code for _ in range(22)].count(429) >= 2)

print("telegram webhook")
check("wrong secret is refused", webhook(upd(1, 55, "/start"), secret="nope").status_code == 403)
sent.clear()
check("a stranger's /start with no plan gets a friendly reply", webhook(upd(2, 55, "/start")).status_code == 200 and any("Create your study plan" in t for c_, t in sent if c_ == 55))
sent.clear()
webhook(upd(3, 55, f"/start {pid}"))
check("/start <plan> links the chat to the plan", db.PLANS[pid]["chat_id"] == 55 and any("Connected" in t for c_, t in sent))
sent.clear()
webhook(upd(3, 55, f"/start {pid}"))
check("Telegram re-sending the same update does nothing", sent == [])
sent.clear()
webhook(upd(4, 77, "/start", typ="group"))
check("group chats are ignored", sent == [])

print("a conversation across separate requests")
sent.clear()
webhook(upd(10, 55, "/quiz"))
check("the quiz starts", any("Question" in t for c_, t in sent) and db.PLANS[pid]["pending"] is not None)
n_before = len(sent)
webhook(upd(11, 55, "my answer"))
check("the answer is graded from state kept in the database", any("good" in t for c_, t in sent[n_before:]))
sent.clear()
webhook(upd(12, 55, "/progress"))
check("commands work", len(sent) == 1)

print("buttons")
db.update_plan(pid, pending=None)


def tap(uid, data, msg_id=500):
    return webhook({"update_id": uid, "callback_query": {"id": f"cb{uid}", "data": data,
                    "message": {"message_id": msg_id, "chat": {"id": 55, "type": "private"}}}})


sent.clear()
tap(930, "quiz:0")
check("the Quiz button on a lesson starts a quiz", any("Quick quiz" in t for c_, t in sent) and db.PLANS[pid]["pending"] is not None)
db.update_plan(pid, pending=None)
sent.clear()
tap(931, "quiz:0", msg_id=501)
tap(932, "quiz:0", msg_id=501)  # the same button tapped twice
check("a button tapped twice is handled once", sum("Quick quiz" in t for c_, t in sent) == 1)
db.update_plan(pid, pending=None)
check("lessons carry a Quiz button", any(b["callback_data"].startswith("quiz:") for row in A.lesson_buttons(0)["inline_keyboard"] for b in row))

print("two messages at once")
order = []
orig = A.handle_update


def slow(u):
    order.append(("in", u["update_id"]))
    real_sleep(0.3)
    orig(u)
    order.append(("out", u["update_id"]))


A.handle_update = slow
ts = [threading.Thread(target=lambda i=i: webhook(upd(20 + i, 55, "/plan"))) for i in range(3)]
[t.start() for t in ts]
[t.join() for t in ts]
A.handle_update = orig
check("one chat is handled one message at a time", all(order[i][0] == "in" and order[i + 1] == ("out", order[i][1]) for i in range(0, 6, 2)))

print("the scheduler (cron)")
check("cron needs the secret", c.get("/api/cron").status_code == 403 and c.get("/api/cron?key=wrong").status_code == 403)


def schedule(minutes_from_now, tz="UTC"):
    t = datetime.now(ZoneInfo(tz)) + timedelta(minutes=minutes_from_now)
    return dict(days=list(range(7)), time=t.strftime("%H:%M"), tz=tz)


db.update_plan(pid, pending=None, step=0, last_sent=None, schedule=schedule(-3))
sent.clear()
r = c.get("/api/cron?key=cs").get_json()
check("a due session is sent", r["sent"] == 1 and any("Lesson 1" in t for c_, t in sent) and db.PLANS[pid]["step"] == 1)
sent.clear()
r = c.get("/api/cron", headers={"Authorization": "Bearer cs"}).get_json()
check("and not sent twice in a day (Vercel-style auth header works too)", r["sent"] == 0 and sent == [])
db.update_plan(pid, last_sent=None, schedule=schedule(-1))
res = []
ts = [threading.Thread(target=lambda: res.append(c.get("/api/cron?key=cs").get_json()["sent"])) for _ in range(4)]
[t.start() for t in ts]
[t.join() for t in ts]
check("four pingers at the same moment: exactly one sends", sum(res) == 1)

print("writing sessions ahead of time")
db.update_plan(pid, last_sent=None, schedule=schedule(+6), step=1)
ai_calls.clear()
r = c.get("/api/cron?key=cs").get_json()
check("shortly before a session it's prepared", r["prepared"] == 1 and db.cache_get(f"prep:{pid}:1") is not None)
ai_calls.clear()
db.update_plan(pid, schedule=schedule(-1))
sent.clear()
c.get("/api/cron?key=cs")
check("and sent from the prepared copy without asking the AI again", any("Quiz" in t for c_, t in sent) and "json" not in ai_calls[:1] and db.cache_get(f"prep:{pid}:1") is None)

print("the AI call and the daily cap")
calls = []
class R:
    def __init__(s, code, body): s.status_code, s._b, s.text = code, body, json.dumps(body)
    def json(s): return s._b
script = []
def fake_post(url, **k):
    calls.append((url, k["json"]["model"]))
    return script.pop(0)
A.ask = REAL_ASK
A._http.post = fake_post
ok_body = {"choices": [{"message": {"content": " hello "}}], "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}
script[:] = [R(200, ok_body)]
check("a normal reply is returned", A.ask("s", "u") == "hello" and calls[-1] == ("https://llm.example/v1/chat/completions", "m1"))
script[:] = [R(429, {}), R(200, ok_body)]
check("a busy moment is retried once", A.ask("s", "u") == "hello")
script[:] = [R(503, {}), R(503, {}), R(200, ok_body)]
calls.clear()
check("then the fallback model takes over", A.ask("s", "u") == "hello" and calls[-1][1] == "m2")
script[:] = [R(401, {})]
try:
    A.ask("s", "u"); bad = False
except RuntimeError as e:
    bad = "401" in str(e)
check("a rejected key is reported clearly", bad)
db.budget_record(4_000_000, 1_000_000)  # about $3.7 at the default prices, over the $1 cap
try:
    A.ask("s", "u"); capped = False
except db.BudgetExceeded:
    capped = True
check("the daily cap stops AI calls", capped)
db.update_plan(pid, pending=None)
sent.clear()
webhook(upd(30, 55, "explain this"))
check("and the learner is told so, kindly", any("daily AI limit" in t for c_, t in sent))

print("setup")
check("setup needs the secret", c.get("/api/setup").status_code == 403)
sent.clear()
r = c.get("/api/setup?key=cs", headers={"Host": "my-backend.vercel.app"})
hook = next(k for m, k in sent if m == "setWebhook")
check("setup points Telegram at this server with a secret", hook["url"] == "https://my-backend.vercel.app/api/telegram" and hook["secret_token"] == A.webhook_secret())

print(f"\n{OK} passed, {BAD} failed")
sys.exit(1 if BAD else 0)
