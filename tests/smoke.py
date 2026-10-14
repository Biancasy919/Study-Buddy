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

REAL_ASK_JSON = A.ask_json
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

print("question types")
db.update_plan(pid, pending=None, qtypes=["mcq", "tf"])
seen_sys = []
_old = A.ask_json


def typed_ask_json(system, user, max_tokens=0, temperature=None):
    if "write quiz questions" in system:
        seen_sys.append(system)
        return {"questions": [
            {"type": "mcq", "q": "Pick the capital of France", "options": ["Berlin", "Paris", "Rome", "Madrid"], "correct": 1,
             "answer": "Paris is the capital.", "hint": "Think of the Eiffel Tower."},
            {"type": "tf", "q": "The Earth is flat.", "tf": False, "answer": "It is a sphere.", "hint": "Think of photos from space."},
            {"type": "mcq", "q": "broken", "options": ["only", "two"], "correct": 5, "answer": "x"}]}
    return _old(system, user, max_tokens, temperature)


A.ask_json = typed_ask_json
sent.clear()
db.update_plan(pid, nq=3)
webhook(upd(940, 55, "/quiz"))
A.ask_json = _old
pend = db.PLANS[pid]["pending"]
q0, q1, q2 = pend["qs"]
check("the chosen types are passed to the AI", any("mcq, tf" in t for t in seen_sys))
check("multiple choice keeps the right answer after shuffling", q0["opts"][q0["ci"]] == "Paris" and sorted(q0["opts"]) == ["Berlin", "Madrid", "Paris", "Rome"])
check("true/false is shaped", q1["opts"] == ["True", "False"] and q1["ci"] == 1)
check("a malformed question falls back to open-ended", q2["type"] == "open" and "opts" not in q2)
check("the first question has answer buttons", any("Pick the capital" in t for c_, t in sent))
wrong = (q0["ci"] + 1) % 4
tap(941, f"q:pick:0:{wrong}", msg_id=600)
check("a wrong tap gives a hint, not the answer", any("Not quite" in t and "Eiffel" in t and "Paris is" not in t for c_, t in sent)
      and db.PLANS[pid]["pending"]["await"] == "choice")
tap(942, "q:retry", msg_id=601)
tap(943, f"q:pick:0:{q0['ci']}", msg_id=602)
check("a correct retry moves on", db.PLANS[pid]["pending"]["i"] == 1)
webhook(upd(944, 55, "false"))
check("typing the answer works too", db.PLANS[pid]["pending"]["i"] == 2 and db.PLANS[pid]["pending"]["score"] == 1.0)
db.update_plan(pid, pending=None, qtypes=["open"], nq=3)
check("old plans default to open-ended", A.clean_qtypes(None) == ["open"] and A.clean_qtypes(["tf", "zzz"]) == ["tf"])
r = c.post("/api/plans", json=dict(PLAN, qtypes=["mcq", "tf", "bogus"], topic="Chess"))
check("the website's choice is stored", db.PLANS[r.get_json()["id"]]["qtypes"] == ["mcq", "tf"])
r = c.post("/api/plans", json=dict(PLAN, topic="Chess"))
check("no choice means open-ended", db.PLANS[r.get_json()["id"]]["qtypes"] == ["open"])

print("weak questions come back")
units = db.PLANS[pid]["units"]
db.update_plan(pid, missed=[dict(topic=units[0]["title"], q="Old Q?", a="x", reviewed=0)])
check("on a first round, only earlier topics' misses return", A.pick_review(db.PLANS[pid], units[0], 0) == [])
check("on a second round, this topic's misses return too", len(A.pick_review(db.PLANS[pid], units[0], 1)) == 1)
db.update_plan(pid, missed=[])

print("quizzes follow the lesson")
u0 = db.PLANS[pid]["units"][0]
A.remember_taught(db.PLANS[pid], u0, "Jokes use puns. Say: elephant joke.", [dict(text="大象", pron="da xiang", meaning="elephant")])
A.remember_taught(db.PLANS[pid], u0, "More: homophones.", [], add=True)
t = db.PLANS[pid]["taught"][u0["title"]]
check("what was taught is remembered, and 'tell me more' adds to it", "puns" in t["text"] and "homophones" in t["text"] and t["phrases"][0]["text"] == "大象")
seen_user = []
_old2 = A.ask_json


def spy_ask_json(system, user, max_tokens=0, temperature=None):
    if "write quiz questions" in system:
        seen_user.append((system, user))
        return {"questions": [{"q": "Q1?", "answer": "a"}, {"q": "Q2?", "answer": "b"}, {"q": "Q3?", "answer": "c"}]}
    return _old2(system, user, max_tokens, temperature)


A.ask_json = spy_ask_json
A.make_quiz(db.PLANS[pid], u0)
A.ask_json = _old2
sys_, usr_ = seen_user[-1]
check("the quiz is told to test only what was taught", "WHAT THE LESSON ACTUALLY TAUGHT" in usr_ and "大象" in usr_ and "did not teach" in sys_)
db.update_plan(pid, taught={})

print("voice notes")
db.update_plan(pid, pending=None, voice=True, speak_target=dict(text="你好", pron="ni hao", meaning="hello"))
_sent_audio = []
_old_get, _old_ask = A._http.get, A.ask


class _Resp:
    content = b"OggS-fake-audio"


A._http.get = lambda url, **k: _Resp()
A.tg = lambda m, **k: ({"ok": True, "result": {"file_path": "voice/a.oga"}} if m == "getFile" else fake_tg(m, **k))


def ask_with_audio(system, user, max_tokens=3000, temperature=None, audio=None):
    _sent_audio.append((audio, user))
    return "I heard 你好. Nicely done. Pronunciation: 4/5"


A.ask = ask_with_audio
_old_aj = A.ask_json
_heard = {"transcript": "你好", "romanization": "ni hao"}
A.ask_json = lambda system, user, max_tokens=0, temperature=None, audio=None: (
    _sent_audio.append((audio, "blind transcription")) or _heard) if audio else _old_aj(system, user, max_tokens, temperature)
sent.clear()
webhook({"update_id": 960, "message": {"message_id": 960, "chat": {"id": 55, "type": "private"},
                                       "voice": {"file_id": "f1", "mime_type": "audio/ogg", "duration": 2, "file_size": 9000}}})
got_coaching = any("Pronunciation: 4/5" in t for c_, t in sent)
check("a voice note is sent to the AI as audio, with the phrase it was meant to say",
      any(x[0] == (b"OggS-fake-audio", "audio/ogg") and "你好" in x[1] for x in _sent_audio))
check("it is transcribed blind first (the target is not in that request)", _sent_audio[0][1] == "blind transcription")
check("how close a transcript is to the target is measured", A.how_close(_heard, dict(text="你好", pron="ni hao")) > 0.9
      and A.how_close(_heard, dict(text="谢谢你的帮助", pron="xie xie ni de bang zhu")) < 0.5)
_heard.update(transcript="谢谢", romanization="xiexie")
sent.clear()
webhook({"update_id": 965, "message": {"message_id": 965, "chat": {"id": 55, "type": "private"},
                                       "voice": {"file_id": "f5", "mime_type": "audio/ogg", "duration": 2}}})
check("a different phrase is reported honestly and rated low, without a coaching call", any("1/5" in t and "谢谢" in t for c_, t in sent))
_heard.update(transcript="你好", romanization="ni hao")
check("and the learner gets coaching back", got_coaching)
N_AUDIO = len(_sent_audio)
sent.clear()
webhook({"update_id": 961, "message": {"message_id": 961, "chat": {"id": 55, "type": "private"},
                                       "voice": {"file_id": "f2", "duration": 300}}})
check("a very long voice note is declined", any("bit long" in t for c_, t in sent) and len(_sent_audio) == N_AUDIO)
db.update_plan(pid, speak_target=None)
sent.clear()
webhook({"update_id": 962, "message": {"message_id": 962, "chat": {"id": 55, "type": "private"},
                                       "voice": {"file_id": "f3", "duration": 2}}})
check("a voice note with nothing to practise gets a gentle nudge", any("/say" in t for c_, t in sent) and len(_sent_audio) == N_AUDIO)
# inside a quiz: a speaking question is graded and counts
db.update_plan(pid, pending={"unit": 0, "qs": [dict(q="Say hello", answer="你好", speak=True, target="你好", pron="ni hao"),
                                              dict(q="Q2?", answer="b")], "i": 0, "score": 0.0},
               speak_target=dict(text="你好", pron="ni hao", meaning="Say hello"))
sent.clear()
webhook({"update_id": 963, "message": {"message_id": 963, "chat": {"id": 55, "type": "private"},
                                       "voice": {"file_id": "f4", "mime_type": "audio/ogg", "duration": 2}}})
pe = db.PLANS[pid]["pending"]
check("a spoken quiz answer is scored from the rating (4/5 = 0.8) and the quiz moves on", pe["i"] == 1 and abs(pe["score"] - 0.8) < 1e-9)
A.ask, A._http.get, A.ask_json = _old_ask, _old_get, _old_aj
A.tg = fake_tg
db.update_plan(pid, pending=None, speak_target=None, voice=False)

_ai_ask_backup = A.ask
print("garbled AI answers")
_texts = []
A.ask = lambda *a, **k: _texts.pop(0)
broken = '{"questions": [{"q": "What is \\"MOIC\\"?", "answer": "x"} {"q": "Second?", "answer": "y"}]}'
_texts[:] = [broken]
d = REAL_ASK_JSON("s", "u")
check("a missing comma in a long answer is repaired, not fatal", len(d["questions"]) == 2 and d["questions"][0]["q"] == 'What is "MOIC"?')
_texts[:] = ["not json at all", '{"ok": 1}']
check("an unreadable answer is asked for again once", REAL_ASK_JSON("s", "u") == {"ok": 1})
_texts[:] = ['[{"q": "A?", "answer": "a"}, {"q": "B?", "answer": "b"}]']
check("a bare list of questions is accepted as the questions", [q["q"] for q in REAL_ASK_JSON("s", "u")["questions"]] == ["A?", "B?"])
_texts[:] = ["nope", "still nope"]
try:
    REAL_ASK_JSON("s", "u"); raised = False
except RuntimeError as e:
    raised = "garbled" in str(e)
check("after two bad answers the learner gets a plain message, not a stack trace", raised)
A.ask = _ai_ask_backup

print("language lessons and conversation")
check("language plans default to speaking-first question types", A.clean_qtypes(None, "language") == ["talk", "say", "listen"]
      and A.clean_qtypes(["talk", "tf"], "custom") == ["tf"] and "calc" not in A.QTYPES)
q_ = A.shape_question(dict(type="talk", q="s", answer="a"))
q2 = A.shape_question(dict(type="talk", q="s", line="你好", answer="a"))
q3 = A.shape_question(dict(type="listen", audio="你好", q="What?", options=["Hello", "Bye", "Thanks", "Sorry"], correct=0, answer="x"))
q4 = A.shape_question(dict(type="say", q="Say hi", target="你好", pron="nǐ hǎo", answer="你好"))
check("question shapes: talk needs a line, listening becomes a tap question with a clip, say becomes a speaking task",
      q_["type"] == "open" and q2["type"] == "talk" and q3["type"] == "mcq" and q3["audio"] == "你好"
      and q3["opts"][q3["ci"]] == "Hello" and q4["speak"] is True)
_lesson = ("🎯 Goal: greet.\n🎧 Mini-dialogue:\nA: 你好！(nǐ hǎo) [Hello!]\n🧩 Key phrases:\n你好！(nǐ hǎo) [Hello!]\n🔍 Notice: x\n🗣 Your turn: say it\n"
           "###DIALOGUE\n你好！\n再见！\n###PHRASES\n你好！ | nǐ hǎo | Hello\n再见！ | zàijiàn | Goodbye")
_old_ask2 = A.ask
A.ask = lambda *a, **k: _lesson
lp = dict(db.PLANS[pid], kind="language", language="zh-CN", course="Mandarin")
txt, phs = A.make_lesson(lp, db.PLANS[pid]["units"][0])
A.ask = _old_ask2
check("a language lesson keeps its sections apart, plays the dialogue as one clip, and lists the key phrases",
      "\n\n🎧" in txt and "###" not in txt and phs[0].get("dialogue") and "你好" in phs[0]["text"] and len(phs) == 3 and phs[1]["pron"] == "nǐ hǎo")

# a conversation question, answered by typing and by voice
lid = c.post("/api/plans", json=dict(PLAN, kind="language", language="Mandarin", level="Beginner", duration="1m", days=[0, 2, 4])).get_json()["id"]
check("a 1-month plan on 3 days a week is 6 lessons (12 sessions), and says so",
      db.PLANS[lid]["target_lessons"] == 6 and db.PLANS[lid]["total_sessions"] == 12 and db.PLANS[lid]["duration"] == "1m"
      and db.PLANS[lid]["qtypes"] == ["talk", "say", "listen"])
check("keep-going plans have no end and start with a first batch",
      (lambda i: db.PLANS[i]["total_sessions"] is None and db.PLANS[i]["target_lessons"] is None)(
          c.post("/api/plans", json=dict(PLAN, duration="forever", topic="Finance")).get_json()["id"]))
big = c.post("/api/plans", json=dict(PLAN, duration="3m", days=[0, 1, 2, 3, 4], topic="Finance")).get_json()["id"]
check("a long plan writes a first batch and the rest as you go", db.PLANS[big]["target_lessons"] == 32 and db.PLANS[big]["lessons"] == 12
      and db.PLANS[big]["total_sessions"] == 64)
UN = [dict(week=None, title=f"Lesson {i}", summary="s", files=[]) for i in range(1, 4)]
cu = c.post("/api/plans", json=dict(PLAN, duration="custom", duration_days=10, days=[0, 2, 4], topic="Cooking")).get_json()["id"]
check("a custom length (10 days, 3 days a week) is about 4 sessions, 2 lessons", db.PLANS[cu]["target_lessons"] == 2
      and db.PLANS[cu]["total_sessions"] == 4 and db.PLANS[cu]["duration_days"] == 10 and "for 10 days" in A.describe_schedule(db.PLANS[cu]))
check("a custom length needs a number", c.post("/api/plans", json=dict(PLAN, duration="custom", topic="Cooking")).status_code == 400)
db.update_plan(lid, units=UN, status="ready", chat_id=88, language="zh-CN", voice=True, course="Mandarin", step=0, last_sent=None)
sent.clear()
A.send_voice_clip = lambda chat, plan, text, caption="": sent.append(("clip", text))
TALK = dict(type="talk", q="A classmate greets you. Greet them back.", line="你好！", pron="nǐ hǎo", meaning="Hello", answer="你好 | nǐ hǎo | Hello")
db.update_plan(lid, pending={"unit": 0, "qs": [TALK, dict(q="Q2?", answer="b")], "i": 0, "score": 0.0})
A.send_question(db.PLANS[lid], db.PLANS[lid]["pending"])
check("a conversation question does not play what the other person says (it would give the words away)",
      ("clip", "你好！") not in sent and any("Reply out loud" in str(t) for c_, t in sent))
webhook({"update_id": 969, "callback_query": {"id": "h1", "data": "q:hear:0", "message": {"message_id": 969, "chat": {"id": 88, "type": "private"}}}})
check("but they can ask to hear it", ("clip", "你好！") in sent)
_old_aj3 = A.ask_json
A.ask_json = lambda system, user, max_tokens=0, temperature=None, audio=None: (
    {"transcript": "你好", "romanization": "nǐ hǎo"} if audio else {"rating": 4, "feedback": "Nice reply.", "natural": "你好 | nǐ hǎo | Hello"})
sent.clear()
webhook(upd(970, 88, "你好"))
pe = db.PLANS[lid]["pending"]
check("a typed reply to a conversation question is coached and scored (4/5 = 0.8)", pe["i"] == 1 and abs(pe["score"] - 0.8) < 1e-9
      and any("natural way to say it" in t for c_, t in sent))
db.update_plan(lid, pending={"unit": 0, "qs": [TALK, dict(q="Q2?", answer="b")], "i": 0, "score": 0.0})
A._http.get = lambda url, **k: type("R", (), {"content": b"aud"})()
A.tg = lambda m, **k: ({"ok": True, "result": {"file_path": "v/a.oga"}} if m == "getFile" else fake_tg(m, **k))
sent.clear()
webhook({"update_id": 971, "message": {"message_id": 971, "chat": {"id": 88, "type": "private"},
                                       "voice": {"file_id": "x", "mime_type": "audio/ogg", "duration": 3}}})
check("a spoken reply is transcribed, shown back, coached and scored", db.PLANS[lid]["pending"]["i"] == 1
      and any("I heard" in t and "你好" in t for c_, t in sent))
A.ask_json = _old_aj3
A.tg = fake_tg
db.update_plan(lid, pending=None)

print("duration: keep going, or finish")
_titles = iter(range(100, 200))
A.ask_json = lambda system, user, max_tokens=0, temperature=None, audio=None: {"units": [dict(title=f"New {next(_titles)}", summary="s") for _ in range(10)]}
db.update_plan(lid, units=UN, target_lessons=6, total_sessions=12, duration="1m", step=3, finished=False)
A.maybe_extend(db.PLANS[lid])
check("when the lessons run low, the next ones are written (only up to the plan's length)", len(db.PLANS[lid]["units"]) == 6)
db.update_plan(lid, units=UN, duration="forever", target_lessons=None, total_sessions=None, step=5)
A.maybe_extend(db.PLANS[lid])
check("with no end date, ten more are added each time", len(db.PLANS[lid]["units"]) == 13)
db.update_plan(lid, units=UN, duration="1m", target_lessons=6, total_sessions=12, step=12, finished=False)
sent.clear()
A.deliver_next(db.PLANS[lid])
check("at the end of the chosen length the learner is congratulated and offered more", db.PLANS[lid]["finished"]
      and any("finished your plan" in t for c_, t in sent) and db.PLANS[lid]["step"] == 12)
_due = c.get("/api/cron?key=cs").get_json()
check("a finished plan is not sent anything more", not any(chat == 88 and "Lesson" in str(t) for chat, t in sent[1:]))
def ext_tap(uid, data):
    return webhook({"update_id": uid, "callback_query": {"id": f"e{uid}", "data": data,
                    "message": {"message_id": uid, "chat": {"id": 88, "type": "private"}}}})
ext_tap(980, "ext:1m")
pl = db.PLANS[lid]
check("'1 more month' adds sessions and carries on", not pl["finished"] and pl["total_sessions"] == 12 + 12 and pl["target_lessons"] == 6 + 6)
db.update_plan(lid, finished=True)
ext_tap(981, "ext:forever")
pl = db.PLANS[lid]
check("'keep going' removes the end date", not pl["finished"] and pl["total_sessions"] is None and pl["duration"] == "forever")
db.update_plan(lid, finished=True, paused=False)
ext_tap(982, "ext:stop")
check("'that's enough' pauses the plan", db.PLANS[lid]["paused"] is True)
A.ask_json = _old_aj3
db.update_plan(lid, paused=False)

print("voices")
_calls = []
def _edge_fail(text, lang):
    _calls.append("edge"); raise RuntimeError("blocked")
def _google_ok(text, lang):
    _calls.append("google"); return b"x" * 900
_e0, _g0 = A._edge_tts, A._google_tts
A._edge_tts, A._google_tts = _edge_fail, _google_ok
check("if the first voice fails, the backup is used", A.tts("你好", "zh-CN") == b"x" * 900 and _calls == ["edge", "google"])
_calls.clear()
check("the same clip is not made twice", A.tts("你好", "zh-CN") == b"x" * 900 and _calls == [])
check("and a voice that failed lately is not tried first next time", A.tts("再见", "zh-CN") == b"x" * 900 and _calls == ["google"])
A._edge_tts, A._google_tts = _e0, _g0
pb = A.practice_buttons(db.PLANS[pid])["inline_keyboard"]
check("after practising a phrase there is still a way to the quiz", any(b["callback_data"].startswith("quiz:") for row in pb for b in row)
      and any(b["callback_data"] == "say:again" for row in pb for b in row))

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
print("a failed scheduled session is retried quietly")
db.update_plan(pid, pending=None, step=0, last_sent=None, schedule=schedule(-3))
_fail = {"n": 0}
_old_get_prepared = A.get_prepared


def failing_prepared(plan, step):
    _fail["n"] += 1
    raise RuntimeError("boom")


A.get_prepared = failing_prepared
sent.clear()
c.get("/api/cron?key=cs")
check("the first failure says nothing to the learner and frees the slot", not any("couldn't prepare" in t for c_, t in sent)
      and db.PLANS[pid]["last_sent"] is None and db.PLANS[pid]["step"] == 0)
c.get("/api/cron?key=cs")
c.get("/api/cron?key=cs")
check("after the retries it tells the learner once, plainly", sum("couldn't prepare" in t for c_, t in sent) == 1
      and "boom" not in " ".join(t for c_, t in sent))
A.get_prepared = _old_get_prepared
db.update_plan(pid, last_sent=None, step=0)
sent.clear()
c.get("/api/cron?key=cs")
check("and once the AI is back the session is sent", any("Lesson 1" in t for c_, t in sent))

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
import requests as _rq


class Slow:
    status_code = 200

    def json(self):
        raise _rq.Timeout("slow")


def post_slow_then_ok(url, **k):
    calls.append((url, k["json"]["model"], k["timeout"]))
    r = script.pop(0)
    if r == "slow":
        raise _rq.Timeout("too slow")
    return r


A._http.post = post_slow_then_ok
script[:] = ["slow", R(200, ok_body)]
calls.clear()
check("a slow model is dropped for the fallback straight away (no second wait)", A.ask("s", "u", 700) == "hello"
      and [c_[1] for c_ in calls] == ["m1", "m2"] and calls[0][2] < 20)
A._http.post = fake_post
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
