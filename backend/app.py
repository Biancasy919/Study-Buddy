"""Study Buddy backend (Flask), built to run on Vercel with a Neon Postgres database.

Serverless functions run only while a request is being handled, so nothing here keeps a thread or a file between requests:
  * Telegram sends each message to /api/telegram (a webhook) and we answer it there.
  * A free pinger (or Vercel cron) calls /api/cron every minute, which sends whatever lessons and quizzes are due.
  * Plans, the text of uploaded material and everything else live in the database (db.py).
Run `python app.py` on your computer to develop: it uses a local SQLite file and polls Telegram instead of using a webhook.
"""
import hashlib
import html as _html
import json
import os
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request, send_from_directory

import biws
import db
from db import BudgetExceeded, update_plan

ROOT = Path(__file__).parent
DATA = ROOT / "data"  # only used for the Wall Street guide's cache when you run it on your own computer

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")  # any OpenAI-compatible API
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL", "")
CRON_SECRET = os.getenv("CRON_SECRET", "")
PORT = int(os.getenv("PORT", "5050"))
AUDIO_GRADING = False  # checking pronunciation from voice notes needs an audio-capable model, which this setup doesn't use
llm = bool(LLM_API_KEY)
_http = requests.Session()  # reuses connections to Telegram and to the AI

app = Flask(__name__)
BOT_USERNAME = ""
SIZES = {  # how much each session covers: syllabus granularity + lesson length
    "concept": ("ONE single concept per lesson. Split each week/topic into several small units, one concept each "
                "(typically 3-5 per week).", "150-220", 700),
    "section": ("one coherent section per lesson. Split a week into two units only if it is very heavy.", "350-450", 1500),
    "topic": ("one lesson per whole week/topic. Do NOT split weeks into several units.", "700-900", 2500),
}
DEPTHS = {
    "overview": "Keep it high-level: definitions and the big picture, minimal detail, no edge cases.",
    "standard": "Balanced: the key ideas with one concrete example.",
    "deep": "Go deep: explain the reasoning behind it, edge cases, common mistakes, and a worked example.",
}
CTX_CHARS = 40_000  # material fed to the model per lesson/quiz


# ---------------------------------------------------------------- storage (Postgres on Neon, see db.py)
def load():
    return db.PLANS


def save(plans):  # kept for old call sites; every write now goes straight to the database
    pass


def quota_hit(e):
    return isinstance(e, BudgetExceeded) or "429" in str(e)


def quota_note(e):
    return " (the AI limit for today looks reached)" if quota_hit(e) else ""


# ---------------------------------------------------------------- AI (any OpenAI-compatible API)
def ask(system: str, user: str, max_tokens=3000, temperature=None) -> str:
    if not llm:
        raise RuntimeError("No AI key set on the server (LLM_API_KEY).")
    db.budget_check()
    err = None
    for model in [m for m in (LLM_MODEL, LLM_FALLBACK_MODEL) if m]:
        for attempt in range(2):  # one quick retry for a busy moment, then on to the fallback model
            body = {"model": model, "max_tokens": max_tokens,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            if temperature is not None:
                body["temperature"] = temperature
            try:
                r = _http.post(f"{LLM_BASE_URL}/chat/completions", headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                               json=body, timeout=50)
            except requests.RequestException as e:
                err = e
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                err = RuntimeError(f"AI service busy ({r.status_code})")
                if attempt == 0:
                    time.sleep(1.2)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"AI service error {r.status_code}: {r.text[:200]}")
            j = r.json()
            text = ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            u = j.get("usage") or {}
            db.budget_record(u.get("prompt_tokens", 0) or 0, u.get("completion_tokens", 0) or 0)
            if text.strip():
                return text.strip()
            err = RuntimeError("The AI returned an empty reply.")
    raise err or RuntimeError("The AI service didn't answer.")


def ask_json(system: str, user: str, max_tokens=6000, temperature=None):
    text = ask(system + "\nReturn ONLY valid JSON, no prose, no code fences.", user, max_tokens, temperature)
    return json.loads(text[text.find("{"): text.rfind("}") + 1] if "{" in text else text)


def build_syllabus(plan, materials):
    """materials: {'outline': text|None, 'files': {name: text}}"""
    kind = plan["kind"]
    files = materials["files"]
    listing = "\n\n".join(f"=== FILE: {n} ===\n{t[:1800]}" for n, t in files.items())
    if kind == "smu":
        sys = ("You build a week-by-week study syllabus for a university course. Use the course outline to find "
               "which weeks exist and what each covers, and map each uploaded slide file to the week(s) it belongs to "
               "(by filename and content). Output JSON: {\"course\": str, \"units\": [{\"week\": int|null, "
               "\"title\": str, \"summary\": str (1-2 sentences), \"files\": [exact filenames]}]}. Unit size: " + SIZES[plan["size"]][0] + " Include weeks with no slides too, with an empty files list.")
        user = f"COURSE OUTLINE:\n{(materials['outline'] or '')[:25000]}\n\nSLIDE FILES (excerpts):\n{listing}"
    else:
        n = plan.get("lessons")
        sys = ("You cut a learning goal into an ordered series of bite-sized lessons, easiest first. Output JSON: "
               "{\"course\": str, \"units\": [{\"week\": null, \"title\": str, \"summary\": str, \"files\": "
               "[exact filenames that cover it, may be empty]}]}. Stay within the learner's scope. "
               "Unit size (unless a number of lessons is given): " + SIZES[plan["size"]][0])
        user = (f"TOPIC: {plan['topic']}\nSCOPE / NOTES FROM LEARNER: {plan.get('scope') or '(none)'}\n"
                f"NUMBER OF LESSONS: {n or 'choose a sensible number (6-15)'}\n\nUPLOADED MATERIAL (excerpts):\n"
                f"{listing or '(none: teach from your own knowledge)'}")
    if plan.get("notes"):
        sys += (" The learner left NOTES. If they limit the scope (e.g. exam revision of certain weeks or topics, 'only weeks "
                "1-6', 'skip chapter 3'), include ONLY the units in that scope and nothing outside it; otherwise cover "
                "everything. Notes about learning style do not change which units exist.")
        user += f"\n\nLEARNER NOTES: {plan['notes']}"
    if kind == "language":
        sys += (" This is a LANGUAGE course at the learner's stated level. Order lessons from most useful to least (survival "
                "phrases first). Build each lesson around speakable words/phrases and a tiny dialogue, include pronunciation "
                "(and tones, if the language has them) early, and keep to the learner's level and focus areas.")
    if plan.get("voice"):
        sys += (' Also add a top-level "language": ONLY if this is a language-learning course where '
                'pronunciation matters, the BCP-47 locale of the language being learned (e.g. "zh-CN", "de-DE", "ja-JP"); '
                'for any other subject (accounting, history, science...) set it to null.')
    data = ask_json(sys, user)
    units = data["units"]
    for u in units:
        u["files"] = [f for f in u.get("files", []) if f in files]
        w = u.get("week")
        u["week"] = w if isinstance(w, int) and 1 <= w <= 52 else None
    return data.get("course") or plan.get("topic") or "Your course", units, data.get("language")


def unit_context(plan, unit):
    if plan.get("kind") == "biws" and unit.get("biws_day"):
        return biws.source_text(unit["biws_day"])[:CTX_CHARS]
    mats = db.get_materials(plan["id"], "file") if unit.get("files") else {}
    return "\n\n".join(f"=== {n} ===\n{mats[n]}" for n in (unit.get("files") or []) if n in mats)[:CTX_CHARS]


def learner_notes(plan, weak=True):
    out = ""
    if plan.get("notes"):
        out += "\nLEARNER NOTES (their own words about how they want to learn; follow them): " + plan["notes"]
    if plan.get("prefs"):
        out += "\nLEARNER PREFERENCES (follow these): " + "; ".join(plan["prefs"])
    if weak and plan.get("weak"):
        out += "\nKNOWN WEAK POINTS (work these in where relevant): " + "; ".join(plan["weak"])
    return out


MORE = (" This is a FOLLOW-UP that goes beyond the standard lesson on this topic: do NOT repeat the basics. Add new "
        "detail, nuance, extra examples, common mistakes, and 3 short practice prompts at the end.")


STOP = set("this that with from have been will your what when which their there about into than then them they were "
           "also more most some such only other these those over under between within using used each both".split())


def _words(t):
    return {w for w in re.findall(r"[a-z]{4,}", t.lower()) if w not in STOP}


def bank_excerpt(plan, unit, kind, budget):
    """Pick the chunks of uploaded exam/practice questions that are about this unit's topic."""
    docs = db.get_materials(plan["id"], kind)
    if not docs:
        return ""
    title_w = _words(unit["title"] + " " + unit.get("summary", ""))
    topic_w = title_w | _words(unit_context(plan, unit)[:4000])
    scored = []
    for name, body in docs.items():
        for ch in re.split(r"\n(?=\[(?:Page|Slide) \d+\])", body):
            for i in range(0, len(ch), 2500):
                part = ch[i:i + 2500]
                w = _words(part)
                sc = len(w & topic_w) + 3 * len(w & title_w)
                if sc >= 3:
                    scored.append((sc, f"[{name}] {part.strip()}"))
    out, used = [], 0
    import random
    # still favour the most relevant chunks, but shuffle among them so each round draws on different exam questions
    for sc, part in sorted(scored, key=lambda x: -x[0] * random.uniform(0.5, 1.5)):
        if used + len(part) > budget:
            continue
        out.append(part)
        used += len(part)
    return "\n---\n".join(out)


BANK_RULES = (" The material includes REAL past exam questions (most important) and practice questions from this course. "
              "Base at least half of the questions on VARIANTS of the exam questions (same concept and format, different "
              "numbers, scenario or wording) and include at least one HARDER version (an extra step, a twist, or combining "
              "two ideas). Stay strictly within this topic. Never copy a question verbatim. Practice questions are a "
              "secondary source of the same kind. For calculation questions, put the worked solution in \"answer\".")


def make_lesson(plan, unit, more=False):
    """Returns (lesson_text, phrases). phrases is [] unless the plan has voice practice on."""
    if more:
        plan = dict(plan, size="topic", depth="deep")
    ctx = unit_context(plan, unit)
    voice = ""
    if plan.get("voice"):
        voice = (" Judge from the topic whether hearing it would help. ONLY if this lesson teaches words, phrases, sounds or "
                 "tones that are meant to be spoken, add after the lesson a new line with exactly ###PHRASES then up to 5 "
                 "lines, one per key word or phrase, formatted: text in the target language's own script | pronunciation "
                 "(pinyin/romanization) | English meaning. For pure grammar, writing/characters, reading or culture "
                 "topics with nothing to pronounce, do NOT add ###PHRASES.")
    text = ask(
        f"You are a friendly tutor teaching over Telegram. Write a lesson of about {SIZES[plan['size']][1]} words in plain text "
        "(no markdown symbols like ** or #; use short paragraphs and simple dashes). Structure: what it is, why it "
        "matters, the key ideas, then a 'Remember this' line. Depth: " + DEPTHS[plan["depth"]] + " Teach from the "
        "provided material when present; do not invent facts it contradicts." + voice + (MORE if more else ""),
        f"Course: {plan['course']}\nLesson: {unit['title']}\nFocus: {unit['summary']}{learner_notes(plan)}\n\nMATERIAL:\n{ctx or '(none, use general knowledge)'}",
        SIZES[plan["size"]][2] + 400)
    phrases = []
    if "###PHRASES" in text:
        text, block = text.split("###PHRASES", 1)
        for line in block.strip().splitlines():
            parts = [x.strip() for x in line.strip("-• ").split("|")]
            if len(parts) >= 2 and parts[0]:
                phrases.append(dict(text=parts[0], pron=parts[1] if len(parts) > 2 else "",
                                    meaning=parts[-1]))
    return text.strip(), phrases[:5]


def pick_review(plan, unit, round_no):
    """Missed questions from OTHER, already-studied topics to revisit. 1 for short quizzes, up to 2 otherwise."""
    import random
    titles = [u["title"] for u in plan["units"]]
    cur = titles.index(unit["title"]) if unit["title"] in titles else 0
    ok = [m for m in (plan.get("missed") or []) if m["topic"] != unit["title"]
          and m["topic"] in titles and (titles.index(m["topic"]) < cur or round_no > 0)]
    random.shuffle(ok)
    ok.sort(key=lambda m: m.get("reviewed", 0))  # least-reviewed first
    return ok[: 1 if plan["nq"] <= 3 else 2]


def make_quiz(plan, unit, round_no=0, review=True):
    ctx = unit_context(plan, unit)
    exam, practice = bank_excerpt(plan, unit, "exam", 14000), bank_excerpt(plan, unit, "practice", 8000)
    bank = ((f"\n\nPAST EXAM QUESTIONS (highest priority):\n{exam}" if exam else "")
            + (f"\n\nPRACTICE QUESTIONS:\n{practice}" if practice else ""))
    import random
    rev = pick_review(plan, unit, round_no) if review else []
    rev_block = ""
    if rev:
        rev_block = (f"\n\nREVIEW QUESTIONS: after the {plan['nq']} questions on THIS topic, add {len(rev)} MORE question(s), each "
                     "flagged \"review\": true. Each must be a fresh variant (not a copy) that tests the same idea as one of "
                     "these questions the student previously got wrong in other topics, with \"review_of\" set to that original "
                     "question text. Written questions only, never a speaking task:\n"
                     + "\n".join(f"- topic: {m['topic']} | question: {m['q']} | model answer: {m['a']}" for m in rev))
    angle = random.choice(["definitions and classification", "calculations with fresh numbers", "real-world scenarios",
                           "spotting and fixing mistakes", "comparing two concepts", "multi-step problems that combine ideas",
                           "interpretation and judgement"])
    seen = (plan.get("asked") or {}).get(unit["title"], [])
    avoid = ("\n\nQUESTIONS ALREADY ASKED (never repeat these, and do not just change the numbers or names in them; "
             "base the new questions on DIFFERENT source questions or sub-topics):\n- " + "\n- ".join(seen[-30:])) if seen else ""
    avoid += f"\n\nFOR VARIETY, lean this round towards: {angle}."
    fresh_topic = None
    if review and not rev and plan["kind"] == "biws":  # spaced review: a fundamental question from a recent earlier day
        titles = [u["title"] for u in plan["units"]]
        cur = titles.index(unit["title"]) if unit["title"] in titles else 0
        if cur:
            ru = random.choice(plan["units"][max(0, cur - 10):cur])
            fresh_topic = ru["title"]
            rev_block = (f"\n\nREVIEW QUESTION: after the {plan['nq']} questions on THIS topic, add 1 MORE question flagged "
                         f"\"review\": true. It is a spaced-review question on the EARLIER topic '{ru['title']}': keep it "
                         "fundamental, and base it strictly on this source:\n<source>\n"
                         f"{unit_context(plan, ru)[:6000]}\n</source>")
    n_rev = len(rev) or (1 if fresh_topic else 0)
    biws_quiz = plan["kind"] == "biws"
    d = ask_json(
        (biws.QUIZ_STYLE + " Output JSON: " if biws_quiz else "You write quiz questions that test real understanding, not just recall. Output JSON: ")
        + "{\"questions\": [{\"q\": str, \"answer\": str}]} with exactly " + str(plan["nq"]) + " questions"
        + ("" if biws_quiz else ", progressing from recall to explain-why to application/scenario (repeat that pattern if there are more than 3)")
        + ". \"answer\" is the model answer with the key points a correct answer must include. Difficulty: " + DEPTHS[plan["depth"]] + (
            ' This is a language course with optional voice practice. Judge from the topic: ONLY if it teaches words, phrases, '
            'sounds or tones meant to be spoken, make the LAST question a SPEAKING task; otherwise make them all normal written '
            'questions and never set "speak". For a speaking task set "speak": true, '
            '"target": the phrase the student must say aloud in the target language script, "pron": its '
            'romanization/pinyin, and write "q" as an English prompt such as "Say \'Hello, how are you?\' in '
            'Mandarin". "answer" = the target phrase.' if plan.get("voice") and AUDIO_GRADING else "") + (BANK_RULES if bank else ""),
        f"Course: {plan['course']}\nTopic: {unit['title']} ({unit['summary']})\nThis is attempt #{round_no + 1} "
        f"at this topic; vary the questions if > 1.{learner_notes(plan)}\n\nMATERIAL:\n{ctx or '(none, use general knowledge)'}{bank}{avoid}{rev_block}", 400 + 250 * (plan["nq"] + n_rev), temperature=1.0)
    qs = [q for q in d["questions"] if not q.get("review")][:plan["nq"]]
    topic_of = {m["q"]: m["topic"] for m in rev}
    for q in [q for q in d["questions"] if q.get("review")][:n_rev]:
        q["review_topic"] = topic_of.get(q.get("review_of"), rev[0]["topic"] if rev else (fresh_topic or ""))
        q.pop("speak", None)
        qs.append(q)
    return qs


def tutor_reply(plan, unit, msg):
    ctx = unit_context(plan, unit)
    hist = "\n".join(f"{'Student' if h['r'] == 'u' else 'Tutor'}: {h['t']}" for h in (plan.get("chat") or [])[-12:])
    hist = f"\n\nCONVERSATION SO FAR:\n{hist}" if hist else ""
    if plan["kind"] == "biws":
        titles = [u["title"] for u in plan["units"]]
        pos = titles.index(unit["title"]) + 1 if unit["title"] in titles else 1
        system = (biws.TEACH_STYLE + f"\n\nThe student is on Day {pos} of {len(plan['units'])}: {unit['title']} "
                  f"({unit['summary']}). They have just read this section of the guide (its tables and screenshots are "
                  "images, not shown in the text below)." + biws.CHAT_RULES)
        user = f"<source>\n{ctx}\n</source>{hist}\n\nSTUDENT: {msg}"
    else:
        system = ("You are a patient tutor answering a student's question on Telegram. Plain text, concise (under 200 words). "
                  "Prefer the provided material. If the question is off-topic, gently steer back.")
        user = f"Course: {plan['course']}\nCurrent topic: {unit['title']}\nMATERIAL:\n{ctx}{hist}\n\nSTUDENT: {msg}"
    reply = ask(system, user, 1200)
    update_plan(plan["id"], chat=((plan.get("chat") or []) + [{"r": "u", "t": msg[:600]}, {"r": "a", "t": reply[:900]}])[-24:])
    return reply


# ---------------------------------------------------------------- telegram
_http = requests.Session()  # reuses the connection to Telegram instead of reconnecting per message


def tg(method, **params):
    r = _http.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=60)
    return r.json()


_voices = None


def tts(text, lang):
    """Text -> mp3 bytes with a free neural voice (edge-tts)."""
    import asyncio
    import edge_tts

    async def run():
        global _voices
        if _voices is None:
            _voices = await edge_tts.list_voices()
        lang_l = (lang or "en-US").lower()
        cands = ([v for v in _voices if v["Locale"].lower() == lang_l]
                 or [v for v in _voices if v["Locale"].lower().startswith(lang_l.split("-")[0])])
        if not cands:
            raise RuntimeError(f"no voice for {lang}")
        voice = next((v for v in cands if v["Gender"] == "Female"), cands[0])["ShortName"]
        out = bytearray()
        async for ch in edge_tts.Communicate(text, voice).stream():
            if ch["type"] == "audio":
                out += ch["data"]
        return bytes(out)
    return asyncio.run(run())


def send_phrase_audio(chat, plan, ph):
    try:
        data = tts(ph["text"], plan.get("language"))
        cap = f"{ph['text']}\n{ph['pron']}\n{ph['meaning']}".replace("\n\n", "\n").strip()
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendAudio",
                      data={"chat_id": chat, "caption": cap[:1000], "title": ph["text"][:60], "performer": "Tutor"},
                      files={"audio": ("phrase.mp3", data, "audio/mpeg")}, timeout=60)
    except Exception as e:
        print("tts error:", e)


def handle_voice(chat, v):
    send(chat, "Thanks for the voice note! I can't listen to voice messages yet, so I can't check pronunciation. "
               "Use /say to hear a phrase, then repeat it out loud. That still helps a lot.")


def move_on(plan, pend):
    """Leave the current question, keeping the score from the first attempt."""
    held = pend.get("held")
    pend["held"], pend["await"] = None, None
    if held:
        if held.get("reveal"):  # they're done trying, so now show the full answer
            send(plan["chat_id"], f"📖 Here's the full answer:\n{held['reveal']}")
        advance(plan, pend, held["pts"], held["answer"], held["fb"])
    else:
        advance(plan, pend, None)


def offer_choice(plan, pend):
    pend["await"] = "choice"
    update_plan(plan["id"], pending=pend)
    tg("sendMessage", chat_id=plan["chat_id"], text="Want another go? Practice attempts don't change your score.",
       reply_markup={"inline_keyboard": [[{"text": "🔁 Try again", "callback_data": "q:retry"},
                                          {"text": "➡️ Move on", "callback_data": "q:next"}]]})


def resolve(plan, pend, pts, answer, fb, ok_at=1.0, reveal=""):
    """The first attempt sets the score. If it wasn't great, offer practice; retries never change the score."""
    held = pend.get("held")
    if held:  # this was a practice attempt
        if pts >= ok_at:
            send(plan["chat_id"], "🎉 Got it! (Practice doesn't change your score.)")
            move_on(plan, pend)
        else:
            offer_choice(plan, pend)
    elif pts >= ok_at:
        advance(plan, pend, pts, answer, fb)
    else:
        pend["held"] = dict(pts=pts, answer=answer, fb=fb, reveal=reveal)
        offer_choice(plan, pend)


def arm_quiz(plan, idx, qs):
    """Start a quiz: questions are asked one at a time, each graded as it is answered."""
    pend = {"unit": idx, "qs": qs, "i": 0, "score": 0.0}
    title = plan["units"][idx]["title"]
    asked = dict(load()[plan["id"]].get("asked") or {})
    asked[title] = (asked.get(title, []) + [q["q"] for q in qs])[-30:]
    update_plan(plan["id"], pending=pend, speak_target=None, asked=asked)
    send_question(plan, pend)


def ask_with_skip(chat, text, i):
    tg("sendMessage", chat_id=chat, text=text,
       reply_markup={"inline_keyboard": [[{"text": "⏭ Skip this question", "callback_data": f"q:skip:{i}"}]]})


def do_skip(plan, pend):
    """Skipping never counts against the learner. If they had already attempted it, their first attempt stands."""
    if pend.get("held"):
        send(plan["chat_id"], "Okay, moving on.")
        move_on(plan, pend)
    else:
        send(plan["chat_id"], "Skipped. It won't count against you.")
        advance(plan, pend, None, "(skipped)")


def send_question(plan, pend):
    q, i, n = pend["qs"][pend["i"]], pend["i"], len(pend["qs"])
    if q.get("review"):
        ask_with_skip(plan["chat_id"], f"Question {i+1}/{n} 🔁 Review from earlier: {q.get('review_topic', '')}\n\n{q['q']}", i)
        update_plan(plan["id"], speak_target=None)
        return
    if q.get("speak"):
        ph = dict(text=q["target"], pron=q.get("pron", ""), meaning=q["q"])
        update_plan(plan["id"], speak_target=ph)
        ask_with_skip(plan["chat_id"], f"Question {i+1}/{n} 🎤\n\n{q['q']}\n\nListen below, then send me a voice message.", i)
        send_phrase_audio(plan["chat_id"], plan, dict(ph, meaning="Listen, then record yours"))
    else:
        update_plan(plan["id"], speak_target=None)
        ask_with_skip(plan["chat_id"], f"Question {i+1}/{n}\n\n{q['q']}", i)


def analyze(plan, pend):
    """Coach-style summary of how the learner answered; also remembers weak points for future quizzes."""
    log = "\n\n".join(f"Q: {x['q']}\nStudent: {x['a']}\nResult: {x['r']}\nFeedback given: {x['fb']}" for x in pend["log"])
    d = ask_json(
        "You are a supportive learning coach. From this quiz log, output JSON: {\"summary\": 1-2 sentences, "
        "\"strengths\": [max 2 short items], \"work_on\": [max 3 concrete, specific things to practise, with the "
        "pattern you noticed in HOW they answered, e.g. missing the 'why', too vague, mixing up terms], "
        "\"next_step\": one concrete thing to do before the next session, \"weak_topics\": [max 3 short phrases]}. "
        "Be honest, encouraging, specific; no generic advice.",
        f"Course: {plan['course']}\nTopic: {plan['units'][pend['unit']]['title']}\n\n{log}", 900)
    lines = ["📊 How you did", d.get("summary", "")]
    if d.get("strengths"):
        lines += ["", "💪 Going well:"] + [f"- {x}" for x in d["strengths"]]
    if d.get("work_on"):
        lines += ["", "🎯 Work on:"] + [f"- {x}" for x in d["work_on"]]
    if d.get("next_step"):
        lines += ["", f"👉 Next: {d['next_step']}"]
    weak = [w for w in d.get("weak_topics", []) if w]
    update_plan(plan["id"], weak=(weak + [w for w in (plan.get("weak") or []) if w not in weak])[:6])
    return "\n".join(lines)


def feedback_buttons(chat, unit_idx=0):
    tg("sendMessage", chat_id=chat, text="How were those questions?", reply_markup={"inline_keyboard": [
        [{"text": "➕ More questions on this topic", "callback_data": f"moreq:{unit_idx}"}],
        [{"text": "👍 Just right", "callback_data": "fb:ok"}, {"text": "😴 Too easy", "callback_data": "fb:easy"}],
        [{"text": "😵 Too hard", "callback_data": "fb:hard"}, {"text": "✍️ Something else…", "callback_data": "fb:other"}]]})


def merge_feedback(plan, text):
    """Fold new feedback into the plan's standing preferences. Returns a confirmation message."""
    prefs = plan.get("prefs") or []
    try:
        d = ask_json("Merge the learner's new feedback into a short list of standing preferences for their tutor bot "
                     "(max 8 items, each one short imperative sentence, e.g. 'Make questions harder'). Resolve "
                     "contradictions in favour of the new feedback. Output {\"prefs\": [...], \"reply\": one friendly "
                     "sentence saying what will change}.", f"Current preferences: {prefs}\nNew feedback: {text}", 600)
        prefs, reply = [str(p)[:200] for p in d["prefs"]][:8], d.get("reply", "Got it.")
    except Exception:
        prefs, reply = (prefs + [text[:200]])[-8:], "Got it, I'll keep that in mind from the next lesson and quiz."
    update_plan(plan["id"], prefs=prefs, awaiting_feedback=False)
    clear_prefetch(plan["id"])
    return reply


def handle_callback(cq):
    chat = cq["message"]["chat"]["id"]
    tg("answerCallbackQuery", callback_query_id=cq["id"])
    plan = plan_for_chat(chat)
    data = cq.get("data", "")
    if plan and plan["kind"] == "biws" and data.startswith(("fmt:", "figs:", "bite:")):
        parts = data.split(":")
        if parts[0] == "bite":
            send_bite(plan)
        elif parts[0] == "figs":
            send_figures(plan, int(parts[1]))
        elif parts[1] in FORMATS:
            send_guide_lesson(plan, int(parts[2]), parts[1])
        return
    if plan and data.startswith("moreq:"):
        start_extra(plan, int(data.split(":")[1]))
        return
    if plan and data.startswith("more:"):
        idx = int(data.split(":")[1])
        unit = plan["units"][idx % len(plan["units"])]
        tg("sendChatAction", chat_id=chat, action="typing")
        try:
            text, phrases = make_lesson(plan, unit, more=True)
        except Exception as e:
            send(chat, "⚠️ Couldn't write more just now" + quota_note(e) + ". Try again soon.")
            return
        send(chat, f"📚 Going deeper: {unit['title']}\n\n{text}", {"inline_keyboard": [[
            {"text": "📚 Even more", "callback_data": f"more:{idx}"}, {"text": "⚙️ Lesson settings", "callback_data": "set:menu"}]]})
        for ph in phrases:
            send_phrase_audio(chat, plan, ph)
        return
    if plan and data.startswith("set:"):
        parts = data.split(":")
        if parts[1] == "size" and parts[2] in SIZES:
            update_plan(plan["id"], size=parts[2])
        elif parts[1] == "depth" and parts[2] in DEPTHS:
            update_plan(plan["id"], depth=parts[2])
        elif parts[1] == "nq":
            update_plan(plan["id"], nq=max(2, min(15, plan["nq"] + int(parts[2]))))
        elif parts[1] == "fmt":
            pass
        elif parts[1] == "noop":
            return
        if parts[1] != "menu":
            clear_prefetch(plan["id"])
        text, markup = settings_view(load()[plan["id"]])
        if parts[1] == "menu":
            tg("sendMessage", chat_id=chat, text=text, reply_markup=markup)
        else:
            tg("editMessageText", chat_id=chat, message_id=cq["message"]["message_id"], text=text, reply_markup=markup)
        return
    if plan and data.startswith("q:skip:"):
        pend = plan.get("pending")
        if pend and str(pend["i"]) == data.split(":")[2]:  # ignore buttons from earlier questions
            do_skip(plan, pend)
        return
    if plan and data.startswith("q:"):
        pend = plan.get("pending")
        if not pend or pend.get("await") != "choice":
            return  # stale button
        if data == "q:next":
            move_on(plan, pend)
        else:
            pend["await"] = "retry"
            update_plan(plan["id"], pending=pend)
            q = pend["qs"][pend["i"]]
            if q.get("speak"):
                send(chat, "🔁 Practice round: listen again, then send a new voice message.")
                ph = dict(text=q["target"], pron=q.get("pron", ""), meaning=q["q"])
                update_plan(plan["id"], speak_target=ph)
                send_phrase_audio(chat, plan, dict(ph, meaning="Listen, then record yours"))
            else:
                send(chat, "🔁 Have another go. Just send your new answer (practice only, your score stays).")
        return
    if not plan or not data.startswith("fb:"):
        return
    kind = data[3:]
    if kind == "ok":
        send(chat, "Great, I'll keep it like this. 🙌 (/feedback any time to change things.)")
    elif kind == "other":
        update_plan(plan["id"], awaiting_feedback=True)
        send(chat, "Tell me what you'd like different, in your own words. e.g. 'more real-life scenarios', "
                   "'shorter explanations', 'test me on why, not just what'. (/cancel to skip)")
    else:
        send(chat, merge_feedback(plan, {"easy": "The questions are too easy; make them harder and less obvious.",
                                         "hard": "The questions are too hard; make them easier, with clearer wording."}[kind]))


def update_missed(plan, unit_idx, q, pts):
    """Remember questions the learner got wrong (first attempt) so later quizzes can revisit them."""
    if q.get("speak"):
        return
    missed = list(load()[plan["id"]].get("missed") or [])
    if q.get("review") and q.get("review_of"):
        hit = next((m for m in missed if m["q"] == q.get("review_of")), None)
        if hit and pts >= 1:
            missed.remove(hit)
        elif hit:
            hit["reviewed"] = hit.get("reviewed", 0) + 1
    elif pts < 1:
        if not any(m["q"] == q["q"] for m in missed):
            missed.append(dict(topic=q.get("review_topic") if q.get("review") else plan["units"][unit_idx]["title"],
                               q=q["q"], a=q["answer"], reviewed=0))
    else:
        missed = [m for m in missed if m["q"] != q["q"]]
    update_plan(plan["id"], missed=missed[-40:])


def advance(plan, pend, points, answer="", fb=""):
    q = pend["qs"][pend["i"]]
    if points is not None:
        update_missed(plan, pend["unit"], q, points)
    pend.setdefault("log", []).append(dict(q=q["q"], a=answer or "(no answer)", fb=fb,
                                           r="not graded" if points is None else f"{points:g}/1"))
    if points is None:  # couldn't be graded (AI unavailable): doesn't count for or against
        pend["ungraded"] = pend.get("ungraded", 0) + 1
    else:
        pend["score"] += points
    pend["i"] += 1
    pend["tries"] = 0
    if pend["i"] >= len(pend["qs"]):
        n = len(pend["qs"]) - pend.get("ungraded", 0)
        note = f" ({pend['ungraded']} not graded)" if pend.get("ungraded") else ""
        send(plan["chat_id"], f"🏁 Quiz done! Score: {pend['score']:g}/{n}{note}" if n else
             "🏁 Quiz done! I couldn't grade any of it this time, but you got the practice in.")
        hist = (plan.get("history") or []) + [dict(topic=plan["units"][pend["unit"]]["title"], score=pend["score"], n=n,
                                                   date=datetime.now().strftime("%d %b"))]
        update_plan(plan["id"], history=hist[-30:])
        if n:
            try:
                send(plan["chat_id"], analyze(plan, pend))
            except Exception as e:
                print("analysis error:", e)
        feedback_buttons(plan["chat_id"], pend["unit"])
        nxt = load()[plan["id"]]
        clear_prefetch(plan["id"])  # the next session is rebuilt with today's mistakes in mind
        update_plan(plan["id"], pending=None, speak_target=None)
    else:
        update_plan(plan["id"], pending=pend)
        send_question(plan, pend)


def grade_one(plan, unit, q, reply, tries=0):
    """Returns (points, feedback, reveal). Feedback coaches the method without giving the answer away; the full answer
    is kept in reveal and only shown once the learner moves on."""
    d = ask_json(
        "You grade one quiz answer kindly but honestly. Output JSON: {\"verdict\": \"correct\"|\"partly\"|\"not_yet\"|\"not_attempt\", "
        "\"feedback\": str, \"reveal\": str}. Accept correct answers in different words; do not demand exact wording, but "
        "require the key idea(s). If the message is NOT an attempt (a question about the question, a request to repeat or "
        "clarify), use verdict \"not_attempt\" and put a short helpful clarification in feedback WITHOUT revealing the "
        "answer. If correct: feedback is one short line of praise. If NOT fully correct: "
        "feedback (plain text, under 70 words) must COACH, not tell: say what part is right, then explain the method or "
        "steps to work it out and where their reasoning went off, WITHOUT stating the final answer, final number or the "
        "missing key fact, so they can try again. "
        + ("This is their retry number " + str(tries) + ": give a more concrete hint than before (e.g. name the formula or "
           "the next step), still without the final answer. " if tries else "")
        + "reveal: the complete model answer with the worked solution (shown only if they give up).",
        f"Topic: {unit['title']}\nQuestion: {q['q']}\nModel answer: {q['answer']}\n\nSTUDENT ANSWER:\n{reply}", 700)
    if d.get("verdict") == "not_attempt":
        return -1, d.get("feedback", ""), ""
    return {"correct": 1.0, "partly": 0.5}.get(d.get("verdict"), 0.0), d.get("feedback", ""), d.get("reveal") or q["answer"]


def start_extra(plan, idx, n=None):
    """Extra practice on one topic. Doesn't move the schedule forward."""
    if plan.get("pending"):
        send(plan["chat_id"], "Let's finish the current quiz first (or skip questions to leave it).")
        return
    unit = plan["units"][idx % len(plan["units"])]
    tg("sendChatAction", chat_id=plan["chat_id"], action="typing")
    p2 = dict(plan, nq=n or plan["nq"])
    try:
        qs = make_quiz(p2, unit, round_no=len((plan.get("asked") or {}).get(unit["title"], [])) // max(1, p2["nq"]), review=False)
    except Exception as e:
        send(plan["chat_id"], "⚠️ Couldn't write more questions just now" + quota_note(e) + ". Try again soon.")
        return
    send(plan["chat_id"], f"📝 More practice: {unit['title']}\n\n{len(qs)} fresh questions, one at a time.")
    arm_quiz(plan, idx % len(plan["units"]), qs)


def current_idx(plan):
    return max(0, (plan["step"] - 1) // (2 if plan["mode"] == "teach_test" else 1)) % len(plan["units"])


def send_html(chat, text):
    r = tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview=True)
    if not r.get("ok"):  # malformed markup: fall back to plain text
        send(chat, re.sub(r"<[^>]+>", "", _html.unescape(text)))


def send_photo(chat, rel):
    data, name = biws.image_bytes(rel)
    for method, field in (("sendPhoto", "photo"), ("sendDocument", "document")):  # very wide/tall images go as files
        r = _http.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data={"chat_id": chat},
                       files={field: (name, data)}, timeout=90).json()
        if r.get("ok"):
            return


FORMATS = {"full": "📖 Word for word", "section": "📄 Condensed lesson", "bite": "🍪 Bite-size", "summary": "📝 Summary",
           "key": "🔑 Key points"}
SIZE_TO_FORMAT = {"concept": "bite", "section": "section", "topic": "full"}


def fmt_for(plan):
    return SIZE_TO_FORMAT.get(plan.get("size"), "full")
FORMAT_PROMPTS = {
    "summary": ("Write a SUMMARY of this section of the guide, about 250 words: what it is, why it matters, the key ideas, "
                "and one simple worked number example if the section has one. Keep the guide's own terminology. Short "
                "paragraphs, plain text."),
    "key": ("Write a KEY POINTS cheat-sheet for this section: 6 to 10 bullets (each starts with '- ') covering the key "
            "terms, formulas and rules to remember, then a line 'How interviewers ask this:' followed by 2 or 3 likely "
            "interview questions, each with a one-line answer."),
}


def guide_version(day, fmt, depth="standard"):
    """A shorter version of a guide day, written once from the guide's text and cached on disk."""
    depth = depth if fmt in ("bite", "section") else "x"
    key = f"guide:{day}:{fmt}:{depth}"
    hit = db.cache_get(key)
    if hit is not None:
        return hit
    src = f"Lesson: {biws.day_info(day)['title']}\n<source>\n{biws.source_text(day)[:CTX_CHARS]}\n</source>"
    if fmt == "bite":
        out = ask_json(biws.TEACH_STYLE + " Split this section of the guide into 3 to 6 BITES, in order. Each bite teaches ONE "
                       "idea in 80 to 150 words, with a short title. Depth: " + DEPTHS[depth] + " Output JSON: "
                       "{\"bites\": [{\"title\": str, \"text\": str}]}.", src, 3000)["bites"][:6]
    elif fmt == "section":
        out = ask(biws.TEACH_STYLE + " Write a condensed LESSON on this section of the guide, about 350 to 450 words: what it "
                  "is, why it matters, the key ideas (with the guide's own simple example if it has one), then a 'Remember "
                  "this' line. Depth: " + DEPTHS[depth], src, 1500)
    else:
        out = ask(biws.TEACH_STYLE + " " + FORMAT_PROMPTS[fmt], src, 1500)
    db.cache_put(key, out)
    return out


def warm_guide(plan, idx):
    """Write the next shorter lesson ahead of time so it arrives instantly."""
    fmt = fmt_for(plan)
    if fmt != "full" and 0 <= idx < len(plan["units"]):
        try:
            guide_version(plan["units"][idx]["biws_day"], fmt, plan.get("depth", "standard"))
        except Exception as e:
            print("warm failed:", e)


def format_buttons(idx, cur, day):
    others = [k for k in FORMATS if k != cur]
    rows = [[{"text": FORMATS[k], "callback_data": f"fmt:{k}:{idx}"} for k in others[i:i + 2]] for i in range(0, len(others), 2)]
    if cur != "full" and biws.day_info(day)["n_images"]:
        rows.append([{"text": "🖼 Show the guide's figures", "callback_data": f"figs:{idx}"}])
    return {"inline_keyboard": rows}


def send_figures(plan, idx):
    chat = plan["chat_id"]
    imgs = [v for k, v in biws.lesson_parts(plan["units"][idx]["biws_day"]) if k == "img"]
    for rel in imgs:
        tg("sendChatAction", chat_id=chat, action="upload_photo")
        send_photo(chat, rel)
    if not imgs:
        send(chat, "This lesson has no figures.")


def send_bite(plan):
    b = load()[plan["id"]].get("bites")
    if not b:
        return
    chat, parts, i = plan["chat_id"], b["parts"], b["i"]
    send(chat, f"🍪 Bite {i + 1}/{len(parts)}: {parts[i]['title']}\n\n{parts[i]['text']}",
         {"inline_keyboard": [[{"text": f"Next bite ➡️ ({i + 2}/{len(parts)})", "callback_data": "bite:next"}]]}
         if i + 1 < len(parts) else None)
    if i + 1 < len(parts):
        update_plan(plan["id"], bites=dict(b, i=i + 1))
    else:
        update_plan(plan["id"], bites=None)
        send(chat, biws.CLOSING, format_buttons(b["idx"], "bite", plan["units"][b["idx"]]["biws_day"]))


def send_guide_lesson(plan, idx, fmt=None):
    unit, n, chat = plan["units"][idx], len(plan["units"]), plan["chat_id"]
    day = unit["biws_day"]
    fmt = fmt or fmt_for(plan)
    update_plan(plan["id"], bites=None)
    version = None
    if fmt != "full":
        try:
            tg("sendChatAction", chat_id=chat, action="typing")
            version = guide_version(day, fmt, plan.get("depth", "standard"))
        except Exception as e:
            send(chat, f"⚠️ I couldn't write the {FORMATS[fmt].split(' ', 1)[1].lower()} version just now"
                       f"{quota_note(e)}, so here's the full guide text.")
            fmt = "full"
    send_html(chat, biws.header_html(idx + 1, n, day))
    if fmt == "full":
        for kind, val in biws.lesson_parts(day):
            if kind == "html":
                send_html(chat, val)
            else:
                tg("sendChatAction", chat_id=chat, action="upload_photo")
                send_photo(chat, val)
            time.sleep(0.8)
        send(chat, biws.CLOSING, format_buttons(idx, "full", day))
    elif fmt == "bite":
        update_plan(plan["id"], bites={"idx": idx, "parts": version, "i": 0})
        send_bite(plan)
    else:
        send(chat, f"{FORMATS[fmt]}\n\n{version}")
        send(chat, biws.CLOSING, format_buttons(idx, fmt, day))


def send(chat_id, text, markup=None):
    text = text or "(empty)"
    while text:  # Telegram caps messages at 4096 chars
        cut = text[:4000]
        if len(text) > 4000 and "\n" in cut:
            cut = cut[: cut.rfind("\n")]
        text = text[len(cut):].lstrip("\n")
        extra = {"reply_markup": markup} if markup and not text else {}  # buttons go on the last chunk
        tg("sendMessage", chat_id=chat_id, text=cut, **extra)


def plans_for_chat(chat_id):
    """All plans linked to this chat, in the order they were linked."""
    return sorted(load().for_chat(chat_id), key=lambda p: p.get("linked_at", 0))


def plan_for_chat(chat_id):
    """The plan this chat is currently 'on': whichever was last linked, switched to, or sent a session."""
    ps = plans_for_chat(chat_id)
    return max(ps, key=lambda p: p.get("last_active", 0)) if ps else None


def touch(pid):
    update_plan(pid, last_active=time.time())


def step_info(plan, step):
    n = len(plan["units"])
    if plan["mode"] == "teach_test":
        return (step // 2) % n, step % 2 == 1, step // (2 * n)
    return step % n, True, step // n


def _build(plan, step):
    idx, is_quiz, round_no = step_info(plan, step)
    unit = plan["units"][idx]
    return ("quiz", make_quiz(plan, unit, round_no)) if is_quiz else ("lesson",) + make_lesson(plan, unit)


def prefetch(plan, step):
    """Write a session ahead of time so it can be sent the moment it's due. Called by the cron tick shortly before a session."""
    if plan.get("kind") == "biws":
        return  # the guide's lessons need no AI, and shorter versions are written (then cached) when first asked for
    key = f"prep:{plan['id']}:{step}"
    if db.cache_get(key) is not None:
        return
    try:
        db.cache_put(key, list(_build(plan, step)))
    except Exception as e:
        print("prefetch failed:", e, flush=True)


def get_prepared(plan, step):
    val = db.cache_pop(f"prep:{plan['id']}:{step}")
    return val if val else _build(plan, step)


def clear_prefetch(pid):  # preferences and plan edits make prepared content stale
    db.cache_clear(f"prep:{pid}:")


def lesson_buttons(idx):
    return {"inline_keyboard": [[{"text": "📚 Tell me more", "callback_data": f"more:{idx}"},
                                 {"text": "⚙️ Lesson settings", "callback_data": "set:menu"}]]}


SIZE_LABELS = {"concept": "One concept", "section": "A section", "topic": "Whole topic"}
DEPTH_LABELS = {"overview": "Overview", "standard": "Standard", "deep": "Deep dive"}


def settings_view(plan):
    ck = lambda cur, k: "✓ " if cur == k else ""
    labels = {"concept": "Bite-size", "section": "Condensed", "topic": "Word for word"} if plan["kind"] == "biws" else SIZE_LABELS
    rows = [[{"text": ck(plan["size"], k) + v, "callback_data": f"set:size:{k}"} for k, v in labels.items()],
            [{"text": ck(plan["depth"], k) + v, "callback_data": f"set:depth:{k}"} for k, v in DEPTH_LABELS.items()],
            [{"text": "➖", "callback_data": "set:nq:-1"}, {"text": f"{plan['nq']} questions", "callback_data": "set:noop"},
             {"text": "➕", "callback_data": "set:nq:1"}]]
    return (f"⚙️ Settings for {plan['course']}\n\nHow much per lesson (top row), how deep (middle), "
            f"questions per quiz (bottom). Changes apply from the next lesson or quiz."), {"inline_keyboard": rows}


def deliver_next(plan):
    """Send the next scheduled item (lesson or quiz) and advance the plan."""
    units = plan["units"]
    if not units:
        return
    step = plan["step"]
    n = len(units)
    if plan["kind"] == "biws" and plan["mode"] == "teach_test" and step >= 2 * n:
        send(plan["chat_id"], "🎉 You've finished every lesson and quiz in your plan! /plan to look back, /goto 1 to start "
                              "again, or /review to revisit your weakest topic.")
        return
    if plan["mode"] == "teach_test":
        idx, is_quiz = (step // 2) % n, step % 2 == 1
        round_no = step // (2 * n)
    else:
        idx, is_quiz, round_no = step % n, True, step // n
    unit = units[idx]
    chat = plan["chat_id"]
    touch(plan["id"])  # replies to this message belong to this plan
    label = f"Week {unit['week']}: " if unit.get("week") and plan["kind"] == "smu" else ""
    try:
        tg("sendChatAction", chat_id=chat, action="typing")
        if plan["kind"] == "biws" and not is_quiz:  # the guide's own lesson (or a shorter version of it)
            send_guide_lesson(plan, idx)
            update_plan(plan["id"], pending=None, speak_target=None, chat=[], step=step + 1)
            return
        got = get_prepared(plan, step)
        if not is_quiz:
            _, text, phrases = got
            send(chat, f"📖 {plan['course']}\nLesson {idx+1}/{n}: {label}{unit['title']}\n\n{text}\n\n"
                       f"Your quiz on this comes next session. Ask me anything meanwhile, or /quiz to be tested now.",
                 lesson_buttons(idx))
            for ph in phrases:
                send_phrase_audio(chat, plan, ph)
            if phrases:
                update_plan(plan["id"], phrases={**(load()[plan["id"]].get("phrases") or {}), unit["title"]: phrases})
            update_plan(plan["id"], pending=None)
        else:
            qs = got[1]
            send(chat, f"📝 {plan['course']}\nQuiz: {label}{unit['title']}\n\n{len(qs)} questions, one at a time"
                       f"{' (' + str(sum(1 for q in qs if q.get('review'))) + ' review from earlier topics)' if any(q.get('review') for q in qs) else ''}. "
                       f"Answer each as it comes, or skip.")
            arm_quiz(plan, idx, qs)
    except Exception as e:  # don't advance on failure; scheduler retries next tick via last_sent
        send(chat, f"⚠️ Couldn't prepare this session ({e}). I'll try again next time.")
        return
    update_plan(plan["id"], step=step + 1)


BIWS_HELP = (
    "📌 COMMANDS\n\n"
    "How it works\n"
    "On your schedule I send a lesson from your Breaking Into Wall Street guide (word for word, condensed or bite-size, "
    "as you chose), then a quiz on it. You can also type /question any time to be tested.\n\n"
    "Learn\n/today - resend today's reading\n/next - jump to the next lesson now\n/back - the previous lesson\n"
    "/goto 12 - jump to lesson 12\n(just type any question to chat with me about today's topic)\n\n"
    "Get tested\n/question - practice questions on today's topic (plus a review question from earlier)\n"
    "/skip - skip the current question\n/done - stop the quiz\n/review - quiz me on my weakest topic\n"
    "/more - extra questions on this topic\n\n"
    "Plan and progress\n/plan - the roadmap and where you are\n/schedule - the next lessons and their dates\n"
    "/progress - your scores\n\n"
    "Busy? Change the schedule\n/resched - skip the next lesson (everything shifts)\n/resched undo - take back the skip\n"
    "/pause and /resume\n/time 7:30am - change the delivery time\n/days mon-sat - change the days\n"
    "/timezone Asia/Manila - set your timezone\n"
    "/settings - lesson size (word for word, condensed, bite-size), depth, questions per quiz\n/feedback - tell me how to adjust"
)

GENERIC_HELP = (
    "📌 COMMANDS\n\n/next - the next session now\n/quiz - test me on the current topic\n/more - extra questions on this topic\n"
    "/skip - skip a question (or tap Skip)\n/done - stop the quiz\n/review - quiz me on my weakest topic\n"
    "/progress - your scores\n/plan - the roadmap\n/schedule - upcoming sessions\n/settings - lesson size, depth, questions\n"
    "/feedback - tell me how to adjust\n/plans and /switch 2 - if you're learning several things\n"
    "/pause and /resume\n/resched - skip the next session\n/time 7:30am - change the time\n/days mon-sat - change the days\n"
    "/timezone Asia/Manila - set your timezone"
)


def biws_welcome(p):
    chat = p["chat_id"]
    send(chat, f"👋 Hi! I'm your IB interview tutor, built from your Breaking Into Wall Street guide.\n\n{BIWS_HELP}")
    if p["step"] == 0:
        send(chat, f"Let's begin. {describe_schedule(p)}\nHere's Day 1:")
        update_plan(p["id"], last_sent=datetime.now(ZoneInfo(p["schedule"]["tz"])).date().isoformat())
        deliver_next(load()[p["id"]])
    else:
        send(chat, f"Welcome back. You're on lesson {current_idx(p) + 1} of {len(p['units'])}. /today resends it.")


def parse_clock(t):
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t.strip().lower())
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None


def parse_days(t):
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    t = t.lower().replace("weekdays", "mon-fri").replace("daily", "mon-sun").replace("everyday", "mon-sun")
    out = set()
    for tok in re.split(r"[,\s]+", t.strip()):
        if not tok:
            continue
        if "-" in tok:
            a, b = (x[:3] for x in tok.split("-", 1))
            if a in names and b in names:
                i, j = names.index(a), names.index(b)
                out |= set(range(i, j + 1)) if i <= j else set(range(i, 7)) | set(range(0, j + 1))
                continue
            return None
        if tok[:3] not in names:
            return None
        out.add(names.index(tok[:3]))
    return sorted(out) or None


def session_label(plan, step):
    n = len(plan["units"])
    if plan["kind"] == "biws" and plan["mode"] == "teach_test" and step >= 2 * n:
        return None
    idx, is_quiz, _ = step_info(plan, step)
    return f"{'Quiz' if is_quiz else 'Lesson'}: {plan['units'][idx]['title']}"


def upcoming(plan, count=7):
    s, now = plan["schedule"], datetime.now(ZoneInfo(plan["schedule"]["tz"]))
    h, m = map(int, s["time"].split(":"))
    out, step = [], plan["step"]
    for off in range(0, 60):
        d = now + timedelta(days=off)
        slot = d.replace(hour=h, minute=m, second=0, microsecond=0)
        if d.weekday() not in s["days"] or (off == 0 and (slot < now or plan.get("last_sent") == now.date().isoformat())):
            continue
        label = session_label(plan, step)
        if label is None:
            break
        out.append(f"{slot.strftime('%a %d %b')} {s['time']}: {label}")
        step += 1
        if len(out) >= count:
            break
    return out


def plan_overview(plan):
    units, step = plan["units"], plan["step"]
    if plan["kind"] == "biws":
        mods = {}
        for i, u in enumerate(units):
            mods.setdefault(u["summary"].split(" · ")[0], []).append(i)
        reached = min(step // 2 if plan["mode"] == "teach_test" else step, len(units))
        lines = [f"🗺 {plan['course']}\n{len(units)} lessons. You've reached {reached}."]
        for name, idxs in mods.items():
            done = sum(1 for i in idxs if i < reached)
            lines.append(f"{'✅' if done == len(idxs) else '▶️' if done else '⬜'} {name}: {done}/{len(idxs)}")
        return "\n".join(lines)
    cur = max(0, (step - 1) // (2 if plan["mode"] == "teach_test" else 1)) % len(units)
    return f"🗺 {plan['course']}\n" + "\n".join(f"{'▶️' if i == cur else '  '} {i + 1}. {u['title']}" for i, u in enumerate(units[:40]))


def handle_message(msg):
    chat = msg["chat"]["id"]
    if msg.get("voice") or msg.get("audio"):
        handle_voice(chat, msg.get("voice") or msg.get("audio"))
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    if text.startswith("/start"):
        code = text.split(maxsplit=1)[1].strip() if " " in text else ""
        plans = load()
        if code in plans:
            update_plan(code, chat_id=chat, linked_at=time.time(), last_active=time.time())
            p = plans[code]
            if p["kind"] == "biws":
                biws_welcome(p)
                return
            send(chat, f"✅ Connected! I'm your tutor for: {p['course']}.\n\n{describe_schedule(p)}\n\n"
                       f"Commands: /next (next session now), /quiz (test me on the current topic), skip (or tap Skip), /more (extra questions on the current topic), /settings, /questions, /progress, /feedback, /status, "
                       f"/pause, /resume{', /say (practise speaking)' if p.get('voice') else ''}. Learning several things? /plans lists them, /switch 2 changes which "
                       f"one these commands use.")
        else:
            send(chat, "Hi! Create your study plan on the website first, then tap the 'Open Telegram' button it gives you.")
        return
    plan = plan_for_chat(chat)
    if not plan:
        send(chat, "I don't have a plan linked to this chat yet. Set one up on the website first.")
        return
    cmd = text.split()[0].lower()
    if plan.get("pending") and text.lower() != "/next" and re.fullmatch(
            r"/?(skip( it| this| that| question)?|pass|next|next question)", text.lower().strip(" .!")):
        do_skip(plan, plan["pending"])
        return
    if cmd == "/cancel":
        update_plan(plan["id"], awaiting_feedback=False)
        send(chat, "Okay, cancelled.")
        return
    if plan.get("awaiting_feedback") and not text.startswith("/"):
        send(chat, merge_feedback(plan, text))
        return
    if cmd == "/feedback":
        if text.lower().endswith("reset"):
            update_plan(plan["id"], prefs=[], weak=[], awaiting_feedback=False)
            send(chat, "Reset. I've forgotten your preferences and weak spots.")
            return
        update_plan(plan["id"], awaiting_feedback=True)
        cur = ("\nCurrent preferences:\n- " + "\n- ".join(plan["prefs"])) if plan.get("prefs") else ""
        send(chat, "What would you like different? Tell me anything about the questions, lessons or how I teach, e.g. "
                   "'harder questions', 'more real-life examples', 'shorter lessons', 'ask me why more'. "
                   f"(/cancel to skip, '/feedback reset' to clear){cur}")
    elif cmd == "/help":
        send(chat, BIWS_HELP if plan["kind"] == "biws" else GENERIC_HELP)
    elif cmd == "/done":
        if plan.get("pending"):
            update_plan(plan["id"], pending=None, speak_target=None)
            send(chat, "Quiz stopped (no score recorded). Back to chat mode.")
        else:
            send(chat, "No quiz running.")
    elif cmd in ("/today", "/back", "/goto") and plan["kind"] == "biws":
        n, cur = len(plan["units"]), current_idx(plan)
        arg = text.split()[1] if len(text.split()) > 1 else ""
        if cmd == "/today":
            target = cur
        elif cmd == "/back":
            if cur <= 0:
                send(chat, "You're on the first lesson.")
                return
            target = cur - 1
        elif arg.isdigit() and 1 <= int(arg) <= n:
            target = int(arg) - 1
        else:
            send(chat, f"Use /goto with a lesson number from 1 to {n}, e.g. /goto 12.")
            return
        touch(plan["id"])
        send_guide_lesson(plan, target)
        # after re-reading lesson N, the next scheduled session is its quiz (or, quiz-only, that topic's quiz)
        update_plan(plan["id"], step=(2 * target + 1) if plan["mode"] == "teach_test" else target, pending=None, chat=[])
    elif cmd == "/plan":
        send(chat, plan_overview(plan))
    elif cmd == "/schedule":
        up = upcoming(plan)
        send(chat, "📅 Coming up\n" + "\n".join(up) if up else "Nothing scheduled. You've reached the end of the plan.")
    elif cmd == "/review":
        hist = [h for h in (plan.get("history") or []) if h.get("n")]
        if plan.get("pending"):
            send(chat, "Finish or /done the current quiz first.")
        elif not hist:
            send(chat, "Nothing to review yet. Take a /question quiz first.")
        else:
            worst = min(hist, key=lambda h: h["score"] / h["n"])
            idx = next((i for i, u in enumerate(plan["units"]) if u["title"] == worst["topic"]), 0)
            send(chat, f"Weakest topic so far: {worst['topic']} ({worst['score'] / worst['n']:.0%}).")
            tg("sendChatAction", chat_id=chat, action="typing")
            qs = make_quiz(plan, plan["units"][idx], review=False)
            send(chat, f"📝 Review quiz: {plan['units'][idx]['title']}\n\n{len(qs)} questions, one at a time.")
            arm_quiz(plan, idx, qs)
    elif cmd == "/time":
        t = parse_clock(" ".join(text.split()[1:]))
        if t:
            sch = dict(plan["schedule"], time=t)
            update_plan(plan["id"], schedule=sch)
            send(chat, "⏰ Done. " + describe_schedule(dict(plan, schedule=sch)))
        else:
            send(chat, "Tell me a time, e.g. /time 7:30am or /time 17:00")
    elif cmd == "/timezone":
        try:
            tz = text.split()[1]
            ZoneInfo(tz)
            sch = dict(plan["schedule"], tz=tz)
            update_plan(plan["id"], schedule=sch)
            send(chat, "🌏 Done. " + describe_schedule(dict(plan, schedule=sch)))
        except Exception:
            send(chat, "Use a timezone name like /timezone Asia/Manila or /timezone America/New_York")
    elif cmd == "/days":
        d = parse_days(" ".join(text.split()[1:]))
        if d:
            sch = dict(plan["schedule"], days=d)
            update_plan(plan["id"], schedule=sch)
            send(chat, "📆 Done. " + describe_schedule(dict(plan, schedule=sch)))
        else:
            send(chat, "Tell me the days, e.g. /days mon-sat or /days mon wed fri")
    elif cmd == "/resched":
        today = datetime.now(ZoneInfo(plan["schedule"]["tz"])).date().isoformat()
        if text.lower().endswith("undo"):
            update_plan(plan["id"], last_sent=None)
            send(chat, "Skip taken back.")
        else:
            update_plan(plan["id"], last_sent=today)
            up = upcoming(dict(plan, last_sent=today), 1)
            send(chat, "⏭ Skipped today. Next up: " + (up[0] if up else "nothing left"))
    elif cmd == "/more":
        arg = text.split()[1] if len(text.split()) > 1 else ""
        start_extra(plan, current_idx(plan), int(arg) if arg.isdigit() and 1 <= int(arg) <= 15 else None)
    elif cmd == "/format":
        text_, markup = settings_view(plan)
        tg("sendMessage", chat_id=chat, text=text_, reply_markup=markup)
    elif cmd == "/settings":
        text_, markup = settings_view(plan)
        tg("sendMessage", chat_id=chat, text=text_, reply_markup=markup)
    elif cmd == "/questions":
        arg = text.split()[1] if len(text.split()) > 1 else ""
        if arg.isdigit() and 2 <= int(arg) <= 15:
            update_plan(plan["id"], nq=int(arg))
            send(chat, f"Done. Quizzes will have {arg} questions from now on.")
        else:
            send(chat, f"Quizzes currently have {plan['nq']} questions. Set a new number between 2 and 15, "
                       f"e.g. /questions 8")
    elif cmd == "/progress":
        h = plan.get("history") or []
        lines = [f"- {x['date']}: {x['topic']}: {x['score']:g}/{x['n']}" for x in h[-8:]] or ["No quizzes yet."]
        weak = ("\n\n🎯 Still working on: " + "; ".join(plan["weak"])) if plan.get("weak") else ""
        send(chat, f"📈 {plan['course']}\n" + "\n".join(lines) + weak)
    elif cmd == "/plans" or cmd == "/switch":
        mine = plans_for_chat(chat)
        arg = text.split()[1] if len(text.split()) > 1 else ""
        if cmd == "/switch" and arg.isdigit() and 1 <= int(arg) <= len(mine):
            plan = mine[int(arg) - 1]
            touch(plan["id"])
            send(chat, f"Switched to: {plan['course']}\n{describe_schedule(plan)}")
            return
        cur = plan["id"]
        lines = [f"{'▶️' if p['id'] == cur else '  '} {i}. {p['course']}{' (paused)' if p.get('paused') else ''}"
                 for i, p in enumerate(mine, 1)]
        send(chat, "Your plans:\n" + "\n".join(lines) + "\n\nUse /switch <number> to change which one /next, /quiz, "
                   "/status and your replies apply to. /pause and /resume apply to the one marked ▶️.")
    elif cmd == "/say":
        if not plan.get("voice"):
            send(chat, "Voice practice isn't switched on for this plan.")
            return
        import random
        pool = [ph for u in plan["units"][: current_idx(plan) + 1] for ph in (plan.get("phrases") or {}).get(u["title"], [])]
        if not pool:
            send(chat, "No phrases yet. Get a lesson first with /next.")
            return
        ph = random.choice(pool)
        send(chat, f"🎧 Listen, then say it out loud:\n{ph['text']}  {ph['pron']}\n({ph['meaning']})\n\n"
                   "(I can't check pronunciation yet, but repeating it aloud still helps.)")
        send_phrase_audio(chat, plan, ph)
    elif cmd == "/pause":
        update_plan(plan["id"], paused=True); send(chat, "⏸ Paused. /resume to continue.")
    elif cmd == "/resume":
        update_plan(plan["id"], paused=False); send(chat, "▶️ Resumed.\n" + describe_schedule(plan))
    elif cmd == "/status":
        send(chat, f"{plan['course']}\n{describe_schedule(plan)}\nProgress: session {plan['step']} done "
                   f"({len(plan['units'])} topics).")
    elif cmd == "/next":
        deliver_next(plan)
    elif cmd in ("/quiz", "/question"):
        touch(plan["id"])
        units = plan["units"]
        last = max(0, (plan["step"] - 1) // (2 if plan["mode"] == "teach_test" else 1))
        unit = units[last % len(units)]
        qs = make_quiz(plan, unit)
        send(chat, f"📝 Quick quiz: {unit['title']}\n\n{len(qs)} questions, one at a time. /skip to skip one.")
        arm_quiz(plan, last % len(units), qs)
    elif cmd == "/skip" and plan.get("pending"):
        do_skip(plan, plan["pending"])
    elif plan.get("pending"):
        pend = plan["pending"]
        q = pend["qs"][pend["i"]]
        if q.get("speak"):
            send(chat, "This one needs a 🎤 voice message. Hold the mic button to record, or /skip.")
            return
        try:
            tries = pend.get("tries", 0)
            pts, fb, reveal = grade_one(plan, plan["units"][pend["unit"]], q, text, tries)
            if pts < 0:  # they asked about the question rather than answering it
                send(chat, fb + "\n\n(Send your answer when ready, or skip.)")
                return
            pend["tries"] = tries + 1
        except Exception as e:
            print("grading error:", e)
            send(chat, f"⚠️ I couldn't grade that one{quota_note(e)}, "
                       f"so we'll move on. A good answer: {q['answer']}")
            move_on(plan, pend)
            return
        send(chat, ("✅ " if pts == 1 else "🟡 " if pts else "❌ ") + fb)
        resolve(plan, pend, pts, text, fb, reveal=reveal)
    else:
        unit = plan["units"][max(0, plan["step"] - 1) // (2 if plan["mode"] == "teach_test" else 1) % len(plan["units"])]
        send(chat, tutor_reply(plan, unit, text))


def handle_update(u):
    """Handle one Telegram update (a message or a button tap)."""
    obj = u.get("callback_query") or u.get("message")
    chat = (obj["message"] if "callback_query" in u else obj)["chat"]["id"]
    try:
        if "callback_query" in u:
            handle_callback(u["callback_query"])
        else:
            if not (obj.get("text") or "").startswith("/skip"):
                tg("sendChatAction", chat_id=chat, action="typing")  # shows "typing…" while the AI works
            handle_message(obj)
    except Exception as e:
        print("handler error:", e, flush=True)
        try:
            send(chat, e.user_message if isinstance(e, BudgetExceeded) else
                 "⚠️ Something went wrong on my side" + quota_note(e) + ". Please try again.")
        except Exception:
            pass


def process_update(u):
    """Entry point for the webhook (and for local polling): drop repeats, and handle one chat's messages one at a time."""
    uid = u.get("update_id")
    if uid is not None and not db.first_time(uid):
        return  # Telegram re-sent an update we've already dealt with
    obj = u.get("callback_query") or u.get("message")
    if not obj:
        return
    msg = obj["message"] if "callback_query" in u else obj
    if msg.get("chat", {}).get("type") not in (None, "private"):
        return  # one-to-one chats only
    chat = msg["chat"]["id"]
    try:
        with db.lease(f"chat:{chat}"):
            handle_update(u)
    except TimeoutError:
        print("chat busy, dropped update", uid, flush=True)


# ---------------------------------------------------------------- scheduler
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def describe_schedule(p):
    s = p["schedule"]
    what = "a lesson, then a quiz on it, alternating" if p["mode"] == "teach_test" else "a quiz"
    return f"Schedule: {what} every {', '.join(DAYS[d] for d in sorted(s['days']))} at {s['time']} ({s['tz']})."


def cron_tick(start_by=25.0):
    """Send whatever is due right now. Called every minute by the pinger (or Vercel cron).

    Each run has a time limit, so it only STARTS new work during the first `start_by` seconds. A plan that has to wait is
    simply picked up by the next tick (a session stays due for 3 hours)."""
    t0 = time.time()
    db.ensure_schema()
    db.prune()
    due, soon = [], []
    for p in load().values():
        try:
            if not p.get("chat_id") or p.get("paused") or p["status"] != "ready" or not p["units"]:
                continue
            s = p["schedule"]
            now = datetime.now(ZoneInfo(s["tz"]))
            today = now.date().isoformat()
            h, m = map(int, s["time"].split(":"))
            at = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now.weekday() not in s["days"] or p.get("last_sent") == today:
                continue
            if at <= now < at + timedelta(hours=3):
                due.append((p["id"], today))
            elif at - timedelta(minutes=10) <= now < at:
                soon.append(p)
        except Exception as e:
            print("cron: skipping", p.get("id"), e, flush=True)

    def deliver(pid, today):
        if time.time() - t0 > start_by or not db.claim(pid, "last_sent", today):  # claim = only one run sends it
            return 0
        try:
            with db.lease(f"chat:{load()[pid]['chat_id']}", ttl=90, wait=20):
                deliver_next(load()[pid])
            return 1
        except Exception as e:
            print("cron: delivery failed", pid, e, flush=True)
            return 0

    def prepare(p):
        if time.time() - t0 <= start_by:
            prefetch(p, p["step"])
            return 1
        return 0

    with ThreadPoolExecutor(max_workers=4) as ex:
        sent = sum(ex.map(lambda a: deliver(*a), due))
        prepared = sum(ex.map(prepare, soon))
    return {"due": len(due), "sent": sent, "prepared": prepared, "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- web API
ALLOWED_ORIGINS = {o.strip().rstrip("/") for o in (os.getenv("FRONTEND_URL", "") + ",http://localhost:5173,http://localhost:5050,"
                                                    "http://127.0.0.1:5050").split(",") if o.strip()}
FRONTEND_DIR = ROOT.parent / "frontend"  # present when you run everything on your own computer


@app.before_request
def _before():
    if request.method == "OPTIONS":  # the browser's permission check before a cross-site request
        return "", 204
    if request.path.startswith("/api/"):
        try:
            db.ensure_schema()
        except Exception as e:
            print("database unavailable:", e, flush=True)
            return jsonify(error="The database isn't reachable right now. Please try again in a moment."), 503


@app.after_request
def _after(resp):
    origin = request.headers.get("Origin", "")
    if origin and (origin.rstrip("/") in ALLOWED_ORIGINS or "*" in ALLOWED_ORIGINS):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Cron-Key, X-Biws-Code"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


BIWS_ACCESS_CODE = os.getenv("BIWS_ACCESS_CODE", "")


def biws_ok():
    """The guide is copyrighted. On your own computer it's yours to use. Online it only opens with the private access code."""
    if not biws.available():
        return False
    if biws.source() == "files":
        return True
    given = request.headers.get("X-Biws-Code", "")
    return bool(BIWS_ACCESS_CODE and given) and secrets.compare_digest(given, BIWS_ACCESS_CODE)


def webhook_secret():
    return os.getenv("TELEGRAM_WEBHOOK_SECRET") or hashlib.sha256(("webhook:" + TOKEN).encode()).hexdigest()[:40]


def bot_username():
    global BOT_USERNAME
    if not BOT_USERNAME:
        BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "")
    if not BOT_USERNAME and TOKEN:
        try:
            BOT_USERNAME = tg("getMe")["result"]["username"]
        except Exception:
            pass
    return BOT_USERNAME


def client_ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")


def secret_ok():
    if not CRON_SECRET:
        return False
    given = request.headers.get("X-Cron-Key") or request.args.get("key") or request.headers.get("Authorization", "").removeprefix("Bearer ")
    return secrets.compare_digest(given, CRON_SECRET)


@app.post("/api/telegram")
def telegram_webhook():
    if not secrets.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), webhook_secret()):
        return "forbidden", 403
    try:
        process_update(request.get_json(silent=True) or {})
    except Exception as e:  # always answer 200, or Telegram keeps re-sending the same update
        print("update failed:", e, flush=True)
    return "ok"


@app.route("/api/cron", methods=["GET", "POST"])
def cron():
    if not CRON_SECRET:
        return jsonify(error="CRON_SECRET isn't set on the server."), 503
    if not secret_ok():
        return jsonify(error="Not allowed."), 403
    return jsonify(cron_tick())


@app.route("/api/setup", methods=["GET", "POST"])
def setup():
    """Point Telegram at this server. Open it once after deploying: /api/setup?key=YOUR_CRON_SECRET"""
    if not secret_ok():
        return jsonify(error="Not allowed. Open /api/setup?key=YOUR_CRON_SECRET (the CRON_SECRET you set)."), 403
    if request.args.get("remove"):
        return jsonify(tg("deleteWebhook"))
    url = f"https://{request.host}/api/telegram"
    r = tg("setWebhook", url=url, secret_token=webhook_secret(), allowed_updates=["message", "callback_query"])
    return jsonify(webhook=url, telegram=r, info=tg("getWebhookInfo").get("result"), bot=bot_username())


@app.get("/api/health")
def health():
    db.q("SELECT 1 AS ok")
    return jsonify(ok=True, ai=llm, bot=bool(TOKEN), cron=bool(CRON_SECRET), database="postgres" if db.IS_PG else "sqlite")


@app.post("/api/plans")
def create_plan():
    """Step 1 of building a plan: the choices. Uploads and the build come next (see add_material and build)."""
    f = request.get_json(silent=True) or {}
    if not db.bump(f"create:{client_ip()}", 20, 86400):
        return jsonify(error="That's a lot of plans from one place today. Please try again tomorrow."), 429
    kind = f.get("kind")
    if kind not in ("smu", "custom", "language", "biws"):
        return jsonify(error="Pick what you want to learn."), 400
    try:
        days = f.get("days", [])
        days = sorted({int(d) for d in (days.split(",") if isinstance(days, str) else days) if str(d) != ""})
        ZoneInfo(f.get("tz", ""))
        datetime.strptime(f.get("time", ""), "%H:%M")
    except Exception:
        return jsonify(error="Please choose valid days, a time and a timezone."), 400
    if not days or any(not 0 <= d <= 6 for d in days):
        return jsonify(error="Pick at least one day."), 400
    str_ = lambda k, n: str(f.get(k) or "").strip()[:n]
    if kind == "biws":
        if not biws_ok():
            return jsonify(error="That option isn't available."), 403
        ids = [m for m in (f.get("modules") or []) if m in {x["id"] for x in biws.modules()}]
        try:
            start, nq = max(1, int(f.get("start") or 1)), int(f.get("nq", 3))
        except ValueError:
            return jsonify(error="Check the start day and number of questions."), 400
        mode, size, depth = f.get("mode", "teach_test"), f.get("size", "topic"), f.get("depth", "standard")
        if mode not in ("teach_test", "test_only") or size not in SIZES or depth not in DEPTHS:
            return jsonify(error="Choose how you want to study."), 400
        units = biws.build_units(ids, bool(f.get("optional")), start)
        if not units:
            return jsonify(error="Pick at least one module (and check the start day)."), 400
        if not 2 <= nq <= 15:
            return jsonify(error="Questions per quiz must be between 2 and 15."), 400
        pid = secrets.token_hex(5)
        load()[pid] = dict(id=pid, kind="biws", topic=biws.TITLE, scope="", notes=str_("notes", 1500), lessons=None,
                           mode=mode, size=size, depth=depth, nq=nq, voice=False, language=None, speak_target=None,
                           phrases={}, missed=[], prefs=[], weak=[], history=[], awaiting_feedback=False, course=biws.TITLE,
                           units=units, status="ready", step=0, chat_id=None, paused=False, pending=None, last_sent=None,
                           schedule=dict(days=days, time=f["time"], tz=f["tz"]))
        return jsonify(id=pid)
    size, depth = f.get("size", "section"), f.get("depth", "standard")
    if size not in SIZES or depth not in DEPTHS:
        return jsonify(error="Choose a lesson size and depth."), 400
    try:
        nq = int(f.get("nq", 5))
    except (ValueError, TypeError):
        nq = 5
    if not 2 <= nq <= 15:
        return jsonify(error="Questions per quiz must be between 2 and 15."), 400
    mode = f.get("mode")
    if mode not in ("teach_test", "test_only"):
        return jsonify(error="Choose how you want to study."), 400
    topic, scope = str_("topic", 200), str_("scope", 3000)
    if kind == "language":
        lang, level = str_("language", 60), str_("level", 40) or "Beginner"
        if not lang:
            return jsonify(error="Which language do you want to learn?"), 400
        topic = f"{lang} ({level})"
        scope = f"Level: {level}. Focus areas: {str_('focus', 200) or 'Everyday conversation'}. {scope}".strip()
    elif kind == "custom" and not topic:
        return jsonify(error="Tell me what you want to learn."), 400
    try:
        lessons = int(f.get("lessons") or 0) or None
    except (ValueError, TypeError):
        lessons = None
    pid = secrets.token_hex(5)
    load()[pid] = dict(id=pid, kind=kind, topic=topic, scope=scope, notes=str_("notes", 1500), lessons=lessons, mode=mode,
                       size=size, depth=depth, nq=nq, voice=True, language=None, speak_target=None, phrases={}, missed=[],
                       prefs=[], weak=[], history=[], awaiting_feedback=False, course="", units=[], status="new", step=0,
                       chat_id=None, paused=False, pending=None, last_sent=None,
                       schedule=dict(days=days, time=f["time"], tz=f["tz"]))
    return jsonify(id=pid)


MAX_FILES, MAX_TEXT, MAX_TOTAL = 40, 1_200_000, 8_000_000


@app.post("/api/plans/<pid>/material")
def add_material(pid):
    """Step 2: the browser reads each uploaded file and sends just its text, one file per request."""
    p = load().get(pid)
    if not p or p["kind"] == "biws":
        return jsonify(error="Not found"), 404
    j = request.get_json(silent=True) or {}
    kind, name, text = j.get("kind"), Path(str(j.get("name") or "")).name[:150], str(j.get("text") or "")
    if kind not in ("file", "outline", "exam", "practice") or not name:
        return jsonify(error="Unknown file type."), 400
    if kind in ("exam", "practice") and len(text.strip()) < 50:
        return jsonify(error=f"{name}: I couldn't read any text in it (is it a scanned image?). "
                             "Try a text-based PDF, or paste the questions into a .txt file."), 400
    if not text.strip():
        return jsonify(error=f"{name}: I couldn't find any text in it."), 400
    if len(text) > MAX_TEXT:
        return jsonify(error=f"{name} has too much text for one file. Please split it into smaller files."), 400
    count, chars = db.material_totals(pid)
    if count >= MAX_FILES or chars + len(text) > MAX_TOTAL:
        return jsonify(error="That's more material than one plan can hold. Try fewer or smaller files."), 400
    if kind == "outline":  # only one outline per plan
        db.x("DELETE FROM materials WHERE plan_id=%s AND kind='outline'", (pid,))
    db.put_material(pid, kind, name, text)
    return jsonify(ok=True)


def build_plan(pid):
    """Step 3: turn the course material (or the topic) into a syllabus."""
    plan = load()[pid]
    mats = db.get_materials(pid, "file")
    outline_docs = db.get_materials(pid, "outline")
    outline_name, outline = next(iter(outline_docs.items()), (None, None))
    if plan["kind"] == "smu" and (not outline or not mats):
        raise ValueError("Please upload both your course outline and at least one slide file.")
    course, units, language = build_syllabus(plan, {"outline": outline, "files": mats})
    update_plan(pid, course=course, units=units, status="ready", error=None, outline=outline_name, language=language,
                banks={k: db.material_names(pid, k) for k in ("exam", "practice") if db.material_names(pid, k)},
                voice=bool(language))  # audio examples only for real language courses


@app.post("/api/plans/<pid>/build")
def build(pid):
    p = load().get(pid)
    if not p:
        return jsonify(error="Not found"), 404
    if p["kind"] == "biws" or p["status"] == "ready":
        return jsonify(status="ready")
    if p["status"] == "processing" and time.time() - p.get("build_started", 0) < 150:
        return jsonify(status="processing"), 202
    update_plan(pid, status="processing", build_started=time.time(), error=None)
    try:
        build_plan(pid)
    except Exception as e:
        msg = e.user_message if isinstance(e, BudgetExceeded) else str(e)
        print("build failed:", pid, e, flush=True)
        update_plan(pid, status="error", error=msg)
    return jsonify(status=load()[pid]["status"], error=load()[pid].get("error"))


def material_files(p):
    return db.material_names(p["id"], "file")


@app.put("/api/plans/<pid>/units")
def edit_units(pid):
    p = load().get(pid)
    if not p:
        return jsonify(error="Not found"), 404
    raw = (request.get_json(silent=True) or {}).get("units")
    if not isinstance(raw, list) or not raw or len(raw) > 200:
        return jsonify(error="A plan needs between 1 and 200 topics."), 400
    avail, units = set(material_files(p)), []
    for u in raw:
        title = str(u.get("title", "")).strip()[:200]
        if not title:
            return jsonify(error="Every topic needs a title."), 400
        try:
            week = int(u["week"]) if u.get("week") not in (None, "") else None
            week = week if week and 1 <= week <= 52 else None
        except (ValueError, TypeError):
            week = None
        unit = dict(week=week, title=title, summary=str(u.get("summary", "")).strip()[:600],
                    files=[f for f in u.get("files", []) if f in avail])
        if p["kind"] == "biws":  # each topic must stay tied to its day of the guide
            try:
                unit["biws_day"] = int(u["biws_day"])
                biws.day_info(unit["biws_day"])
            except (KeyError, ValueError, TypeError, IndexError):
                return jsonify(error="Every topic in this plan comes from the guide, so new topics can't be added."), 400
        units.append(unit)
    update_plan(pid, units=units, pending=None)
    clear_prefetch(pid)  # indexes may have shifted, so drop any open quiz
    return jsonify(ok=True)


@app.get("/api/biws")
def biws_info():
    if request.headers.get("X-Biws-Code") and not db.bump(f"biwscode:{client_ip()}", 10, 3600):
        return jsonify(error="Too many tries. Please wait a while."), 429  # stops anyone guessing the access code
    if biws_ok():
        return jsonify(available=True, title=biws.TITLE, modules=biws.modules(), total=len(biws.plan()["days"]))
    # not unlocked: only say that a code exists, never anything about the guide itself
    return jsonify(available=False, locked=bool(biws.available() and biws.source() == "db" and BIWS_ACCESS_CODE))


@app.get("/api/plans/<pid>")
def get_plan(pid):
    p = load().get(pid)
    if not p:
        return jsonify(error="Not found"), 404
    status, error = p["status"], p.get("error")
    if status == "processing" and time.time() - p.get("build_started", 0) > 150:  # the build function was cut off
        status, error = "error", "That took too long. Please press the button to try again."
    return jsonify(status=status, kind=p["kind"], error=error, course=p["course"], units=p["units"], files=material_files(p),
                   banks=p.get("banks") or {}, linked=bool(p.get("chat_id")), schedule=describe_schedule(p),
                   link=f"https://t.me/{bot_username()}?start={pid}" if bot_username() else None)


# ---------------------------------------------------------------- running on your own computer
if FRONTEND_DIR.exists():  # serve the website too, so one command runs everything locally
    @app.get("/")
    def index():
        return send_from_directory(FRONTEND_DIR, "index.html")

    @app.get("/src/<path:p>")
    def frontend_src(p):
        return send_from_directory(FRONTEND_DIR / "src", p)

    @app.get("/<path:p>")
    def frontend_public(p):
        return send_from_directory(FRONTEND_DIR / "public", p)


def run_local():
    """Local development: Telegram can't reach your computer, so poll for messages and tick the scheduler ourselves."""
    import threading
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first.")
    me = tg("getMe")
    if not me.get("ok"):
        raise SystemExit(f"Telegram rejected the token: {me}")
    tg("deleteWebhook")  # a bot can use polling or a webhook, not both. Use a separate bot for local testing.
    db.ensure_schema()
    print(f"Bot: @{bot_username()}  |  http://localhost:{PORT}  |  database: {'postgres' if db.IS_PG else db.LOCAL_DB}", flush=True)
    if not llm:
        print("WARNING: LLM_API_KEY isn't set, so plans can't be built.", flush=True)
    pool = ThreadPoolExecutor(max_workers=6)

    def poll():
        offset = 0
        while True:
            try:
                r = _http.post(f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                               json={"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]}, timeout=45).json()
                for u in r.get("result", []):
                    offset = u["update_id"] + 1
                    pool.submit(lambda u=u: process_update(u))
            except Exception as e:
                print("poll error:", e, flush=True)
                time.sleep(5)

    def tick():
        while True:
            try:
                cron_tick(start_by=600)
            except Exception as e:
                print("tick error:", e, flush=True)
            time.sleep(20)

    threading.Thread(target=poll, daemon=True).start()
    threading.Thread(target=tick, daemon=True).start()
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=PORT)


if __name__ == "__main__":
    run_local()
