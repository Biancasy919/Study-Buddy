# Study Buddy

A website that sets up a personal study bot in Telegram. Tell it what you're learning (an SMU course from your slides and past papers, a
language, or any topic), choose how and when you want to study, and the bot sends lessons and quizzes on your schedule, grades your
answers, coaches you and revisits what you got wrong.

Built to deploy on **Vercel** (website + backend) with a **Neon** Postgres database. See **DEPLOY.md** for the step-by-step.

```
frontend/   the website (Vite). Reads uploaded PDFs/PPTX in the browser and sends only their text
backend/    Flask API + the Telegram bot (webhook) + the scheduler (/api/cron)
  app.py      everything the bot and API do
  db.py       Postgres (Neon) or a local SQLite file
  schema.sql  the tables: paste into Neon's SQL editor
  biws.py     optional: the Breaking Into Wall Street guide (your own copy, from files or a private database table)
  import_guide.py  loads your own copy of that guide into your database, once
tests/      backend tests (SQLite or a real Postgres)
```

## Run it on your computer
```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
cp backend/.env.example .env           # fill in the Telegram token and the AI key
cd backend && ../.venv/bin/python app.py    # http://localhost:5050
```
Locally the bot polls Telegram instead of using a webhook, and a local file replaces Neon. Use a separate bot from the deployed one.

## Tests
```bash
.venv/bin/python tests/smoke.py        # API, webhook, scheduler, AI call and daily cap (stubbed, free)
.venv/bin/python tests/smoke.py pg     # the same against a real embedded Postgres (pip install pgserver)
.venv/bin/python tests/test_db.py pg   # the database layer, including simultaneous requests
.venv/bin/python tests/test_guide.py   # the private guide: loading, the access code, sending a lesson with its figure
```

`versions/` (not committed) holds saved earlier versions: the single-computer version and the multi-user version with sign-in.
