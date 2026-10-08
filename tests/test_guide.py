"""The Wall Street guide stored privately in the database and shown only with the access code.
   .venv/bin/python tests/test_guide.py        (or: ... pg)"""
import json, os, sys, tempfile
from pathlib import Path
tmp = Path(tempfile.mkdtemp())
if "pg" in sys.argv:
    import pgserver
    os.environ["DATABASE_URL"] = pgserver.get_server(tmp / "pg", cleanup_mode="stop").get_uri()
else:
    os.environ["DATABASE_URL"] = ""; os.environ["LOCAL_DB"] = str(tmp / "t.db")
# a tiny pretend guide on disk, for the loader to read
src = tmp / "ibig"; (src / "content" / "days").mkdir(parents=True); (src / "content" / "img").mkdir(parents=True)
PNG = b"\x89PNG\r\n\x1a\n" + os.urandom(300)
(src / "content" / "img" / "a.png").write_bytes(PNG)
days = [{"day": 1, "module": "core", "module_no": 1, "module_name": "Core Concepts", "title": "Key Rule #1: Money", "kind": "lesson",
         "optional": False, "file": "x", "pages": [1, 2], "n_images": 1, "n_paras": 2},
        {"day": 2, "module": "core", "module_no": 1, "module_name": "Core Concepts", "title": "Q&A drill", "kind": "drill",
         "optional": True, "file": "x", "pages": [2, 3], "n_images": 0, "n_paras": 1}]
(src / "plan.json").write_text(json.dumps({"modules": [{"id": "core", "name": "Core Concepts", "blurb": "Basics"}], "days": days}))
items1 = [{"k": "p", "html": "<b>Key Rule #1</b>", "txt": "Key Rule #1", "head": True, "bullet": False, "sub": False},
          {"k": "p", "html": "Money today is worth more.", "txt": "Money today is worth more.", "head": False, "bullet": False, "sub": False},
          {"k": "img", "path": "content/img/a.png"}]
(src / "content" / "days" / "day_001.json").write_text(json.dumps(items1))
(src / "content" / "days" / "day_002.json").write_text(json.dumps([items1[1]]))
os.environ.update(TELEGRAM_BOT_TOKEN="1:x", LLM_API_KEY="k", CRON_SECRET="cs", BIWS_DIR=str(tmp / "not-here"), BIWS_ACCESS_CODE="open-sesame",
                  TELEGRAM_BOT_USERNAME="TestBot")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import app as A, biws, db, import_guide  # noqa: E402
OK = BAD = 0
def check(n, c):
    global OK, BAD
    print(("  ok   " if c else "  FAIL ") + n); OK += bool(c); BAD += (not c)

print("engine:", "postgres" if db.IS_PG else "sqlite")
db.ensure_schema()
check("nothing is available before the guide is loaded", not biws.available())
import_guide.load(src, say=lambda *a: None)
n1 = import_guide.load(src, say=lambda *a: None)
check("loading twice skips what's already there", n1 == (2, 0, 1))
biws._avail = (0.0, False)
check("the site now has the guide (source: database)", biws.available() and biws.source() == "db")
check("lessons and figures come back exactly", biws.day_items(1) == items1 and db.guide_image("content/img/a.png") == PNG)
check("lesson text and figure order are intact", [k for k, _ in biws.lesson_parts(1)] == ["html", "img"])

c = A.app.test_client()
r = c.get("/api/biws").get_json()
check("without the code, nothing about the guide is revealed", r == {"available": False, "locked": True})
r = c.get("/api/biws", headers={"X-Biws-Code": "wrong"}).get_json()
check("a wrong code is treated the same", r["available"] is False and "modules" not in r)
r = c.get("/api/biws", headers={"X-Biws-Code": "open-sesame"}).get_json()
check("the right code unlocks it", r["available"] and r["modules"][0]["id"] == "core")
body = {"kind": "biws", "modules": ["core"], "mode": "teach_test", "size": "topic", "depth": "standard", "nq": 3, "days": [0, 1, 2, 3, 4, 5, 6], "time": "17:00", "tz": "UTC"}
check("a guide plan can't be made without the code", c.post("/api/plans", json=body).status_code == 403)
check("or with a wrong one", c.post("/api/plans", json=body, headers={"X-Biws-Code": "nope"}).status_code == 403)
r = c.post("/api/plans", json=body, headers={"X-Biws-Code": "open-sesame"})
pid = r.get_json()["id"]
check("with the code it works", r.status_code == 200 and db.PLANS[pid]["status"] == "ready")
check("guessing the code is rate limited", 429 in [c.get("/api/biws", headers={"X-Biws-Code": f"g{i}"}, environ_base={"REMOTE_ADDR": "5.5.5.5"}).status_code for i in range(14)])

sent, photos = [], []
A.tg = lambda m, **k: sent.append((m, k.get("text", ""))) or {"ok": True}
class R:
    def json(s): return {"ok": True}
A._http.post = lambda url, **k: photos.append((url.rsplit("/", 1)[1], k["files"])) or R()
A.time.sleep = lambda s: None
u = {"update_id": 1, "message": {"chat": {"id": 42, "type": "private"}, "text": f"/start {pid}"}}
c.post("/api/telegram", json=u, headers={"X-Telegram-Bot-Api-Secret-Token": A.webhook_secret()})
texts = " ".join(t for m, t in sent)
check("Day 1 is sent word for word", "Money today is worth more" in texts and "Day 1 of" in texts)
check("with its figure, straight from the database", photos and photos[0][0] == "sendPhoto" and photos[0][1]["photo"][1] == PNG)
A.ask = lambda *a, **k: "A short version."
check("shorter versions are cached in the database", A.guide_version(1, "section", "standard") == "A short version." and db.cache_get("guide:1:section:standard") == "A short version.")
check("no guide files are needed on the server", not (Path(A.__file__).parent / "content").exists())
print(f"\n{OK} passed, {BAD} failed"); sys.exit(1 if BAD else 0)
