"""Load your own copy of the Breaking Into Wall Street guide into the database, once.

Run it on YOUR computer. It reads the guide from your IBIG bot folder and stores it in the database that DATABASE_URL points
to (your Neon database). The guide is never put in GitHub or in the website's files, and the site only shows it to someone who
enters the private BIWS_ACCESS_CODE.

    cd backend && ../.venv/bin/python import_guide.py

It can be run again safely: anything already loaded is skipped. Only use a guide you have the right to use.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import db


def load(src, say=print):
    src = Path(src).expanduser()
    if not (src / "plan.json").exists() or not (src / "content" / "days").exists():
        raise SystemExit(f"I can't find the guide in {src} (looking for plan.json and content/days).")
    db.ensure_schema()
    plan = json.loads((src / "plan.json").read_text())
    db.guide_put("plan", plan)
    images = []
    for d in plan["days"]:
        items = json.loads((src / "content" / "days" / f"day_{d['day']:03d}.json").read_text())
        db.guide_put(f"day:{d['day']}", items)
        images += [it["path"] for it in items if it["k"] == "img"]
    say(f"Loaded the text of {len(plan['days'])} lessons. Now the {len(set(images))} figures...")
    done = skipped = size = 0
    for i, rel in enumerate(dict.fromkeys(images), 1):
        if db.guide_has_image(rel):
            skipped += 1
            continue
        data = (src / rel).read_bytes()
        db.guide_put_image(rel, data)
        done += 1
        size += len(data)
        if i % 50 == 0:
            say(f"  {i} figures checked ({done} uploaded)")
    say(f"Done. Uploaded {done} figures ({size / 1e6:.1f} MB), {skipped} were already there.")
    return len(plan["days"]), done, skipped


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=os.getenv("BIWS_DIR") or "~/ibig-study-bot", help="your IBIG bot folder")
    ap.add_argument("--yes", action="store_true", help="don't ask before loading into a local test file")
    a = ap.parse_args()
    where = "your Postgres database (DATABASE_URL)" if db.IS_PG else f"a LOCAL test file ({db.LOCAL_DB}). DATABASE_URL isn't set"
    print(f"Loading the guide from {Path(a.src).expanduser()} into {where}.")
    if not db.IS_PG and not a.yes and input("That's not your online database. Continue anyway? [y/N] ").strip().lower() != "y":
        sys.exit("Stopped. Put your Neon connection string in .env as DATABASE_URL and run it again.")
    load(a.src)
