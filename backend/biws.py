"""Breaking Into Wall Street (BIWS) IB Interview Guide support.

Reads the pre-extracted guide and turns a day's section into the exact Telegram messages, same as the IBIG bot: the
guide's own text word for word, with its figures.

Two places it can come from:
  * files  - your IBIG bot project on this computer (plan.json + content/days + content/img). Set BIWS_DIR if it lives elsewhere.
  * db     - a private copy in the database, loaded once with import_guide.py. This is what the deployed site uses.
"""
import html as _html
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # settings must be read before the values below are computed

BIWS_DIR = Path(os.getenv("BIWS_DIR") or "~/ibig-study-bot").expanduser()
TITLE = "Breaking Into Wall Street: IB Interview Guide"
CLOSING = ("✅ That's today's reading. Ask me anything about it, and when you feel ready, "
           "type /question and I'll test you.")
LIMIT = 3600  # Telegram's cap is 4096 characters per message

# the tutor persona and quiz/grading rules from the IBIG bot
TEACH_STYLE = (
    "You are a warm, patient investment banking tutor helping a beginner through Telegram. "
    "The student reads the Breaking Into Wall Street (BIWS) IB Interview Guide, one Key Rule per day, and asks you "
    "follow-up questions. Plain English; define jargon the first time it appears; use small, simple numbers for examples. "
    "Telegram formatting rules: no tables, no code blocks, no LaTeX, and no markdown symbols like ** or #. Short "
    "paragraphs. Bullets start with '- '. Put math on its own line, like: Revenue 100 - COGS 40 = Gross Profit 60."
)
CHAT_RULES = (
    " Answer their questions using the guide text first. If something is outside it, you may use general finance "
    "knowledge but say so. Keep answers under ~200 words unless they ask for depth. If they seem confused, try a "
    "different analogy or a simpler number example. Occasionally (not every time) remind them they can type "
    "/question when ready to be tested."
)
QUIZ_STYLE = (
    "You write practice questions for an investment banking interview prep student. Base every question strictly on "
    "the source excerpt. Questions must be answerable in 1-4 sentences typed on a phone. Order them from easiest to "
    "hardest: mostly conceptual ('explain', 'why'), at least one interview-style scenario, and one small calculation "
    "with simple numbers if the material supports it."
)

_plan = None
_avail = (0.0, False)


def _files_here() -> bool:
    return (BIWS_DIR / "plan.json").exists() and (BIWS_DIR / "content" / "days").exists()


def source() -> str:
    """'files' when your IBIG folder is on this computer, otherwise 'db'."""
    return "files" if _files_here() else "db"


def available() -> bool:
    if _files_here():
        return True
    global _avail
    import time
    import db
    if time.time() - _avail[0] > 60:  # one cheap lookup a minute is plenty
        try:
            _avail = (time.time(), db.guide_loaded())
        except Exception:
            _avail = (time.time(), False)
    return _avail[1]


def plan() -> dict:
    global _plan
    if _plan is None:
        if _files_here():
            _plan = json.loads((BIWS_DIR / "plan.json").read_text())
        else:
            import db
            _plan = db.guide_get("plan")
    return _plan


def modules() -> list[dict]:
    """Modules for the wizard, in order, with how many regular and optional days each has."""
    out = []
    for m in plan()["modules"]:
        days = [d for d in plan()["days"] if d["module"] == m["id"]]
        out.append(dict(id=m["id"], name=m["name"], blurb=m.get("blurb", ""),
                        days=sum(1 for d in days if not d["optional"]), optional=sum(1 for d in days if d["optional"])))
    return out


def build_units(module_ids: list[str], include_optional: bool, start: int = 1) -> list[dict]:
    """Plan units (one per guide day), in the guide's own order, easiest first."""
    units = []
    for d in plan()["days"]:
        if d["module"] not in module_ids or (d["optional"] and not include_optional) or d["day"] < start:
            continue
        kind = "Q&A drill" if d["kind"] == "drill" else "Lesson"
        units.append(dict(week=(d["day"] - 1) // 6 + 1, title=d["title"] + (" (optional)" if d["optional"] else ""),
                          summary=f"{d['module_name']} · {kind}", files=[], biws_day=d["day"]))
    return units


def day_info(n: int) -> dict:
    return plan()["days"][n - 1]


_days = {}


def day_items(n: int) -> list[dict]:
    if _files_here():
        return json.loads((BIWS_DIR / "content" / "days" / f"day_{n:03d}.json").read_text())
    if n not in _days:  # kept for the life of this server instance, so a lesson costs one database read
        import db
        _days[n] = db.guide_get(f"day:{n}")
    return _days[n]


def source_text(n: int) -> str:
    """Plain text of the day's reading (figures are images and not included)."""
    return "\n\n".join(("- " if i.get("bullet") else "") + i["txt"] for i in day_items(n) if i["k"] == "p")


def image_bytes(rel: str) -> tuple[bytes, str]:
    """(picture, filename) for a figure, from your files or from the database."""
    name = Path(rel).name
    if _files_here():
        return (BIWS_DIR / rel).read_bytes(), name
    import db
    data = db.guide_image(rel)
    if data is None:
        raise FileNotFoundError(rel)
    return data, name


def header_html(pos: int, total: int, n: int) -> str:
    d = day_info(n)
    tag = " (optional / advanced)" if d["optional"] else ""
    kind = "Q&A drill" if d["kind"] == "drill" else "Lesson"
    e = lambda s: _html.escape(s, quote=False)
    return f"📘 <b>Day {pos} of {total}</b> · {e(d['module_name'])}\n<b>{e(d['title'])}</b>{tag}\n<i>{kind}</i>"


def _item_html(it: dict) -> str:
    if it.get("bullet"):
        return ("      ◦ " if it.get("sub") else "• ") + it["html"]
    return it["html"]


def lesson_parts(n: int) -> list[tuple[str, str]]:
    """[("html", message_text) | ("img", relative_path)] in reading order."""
    parts, buf, size = [], [], 0

    def flush():
        nonlocal buf, size
        if buf:
            out = ""
            for k, (h, bl) in enumerate(buf):
                out += h if k == 0 else (("\n" if bl and buf[k - 1][1] else "\n\n") + h)
            parts.append(("html", out))
        buf, size = [], 0

    for it in day_items(n):
        if it["k"] == "img":
            flush()
            parts.append(("img", it["path"]))
            continue
        h = _item_html(it)
        if size + len(h) + 2 > LIMIT:
            flush()
        buf.append((h, bool(it.get("bullet"))))
        size += len(h) + 2
    flush()
    return parts
