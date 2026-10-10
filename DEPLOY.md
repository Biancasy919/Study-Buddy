# Deploying Study Buddy (Vercel + Neon)

This follows the AI Lodge deployment workshop: **a frontend project and a backend project on Vercel, a Neon Postgres database, a
Telegram bot, and an OpenAI-compatible AI key.** Plan on about 30 minutes.

```
 browser ──► Frontend (Vercel, Vite) ──► Backend (Vercel, Flask) ──► Neon (Postgres)
 Telegram ──► /api/telegram (webhook) ──┘         ▲  └──► AI API
 a free pinger, every minute ──► /api/cron ───────┘
```

Why it differs from running on your computer: a serverless host only runs code while a request is being handled. So Telegram
*sends* each message to the backend (a webhook), a pinger *calls* the backend every minute to send scheduled lessons, and
everything that has to be remembered lives in Neon.

## What you need
GitHub account · Vercel account · Neon account · Telegram · the AI Lodge API key (from the AI Lodge API Key bot) · a free
account at cron-job.org (for the scheduler)

---

## Step 0. Put the code on GitHub
The workshop needs the repository to be **public**, so check nothing secret is in it. `.gitignore` already keeps out `.env`,
databases and your course files. In Terminal, inside the `study-buddy` folder:

```bash
git init
git add .
git status        # read this list: there must be NO .env, no .db files and nothing from data/
git commit -m "Study Buddy"
```
Create a public repository on github.com (no README), then run the two commands GitHub shows you (`git remote add origin ...`
and `git push -u origin main`).

## Step 1. Create the database (Neon)
1. Neon → **New project** → Region **Singapore** → Postgres on → **Create project** → **Go to project**.
2. **SQL Editor** → paste the contents of `backend/schema.sql` → **Run**. (The backend also creates these tables on its own if
   they're missing, so this is a safety net.)
3. **Connect** → turn on **Connection pooling** → copy the snippet. That string is your `DATABASE_URL`. Save it.

## Step 2. Create the Telegram bot
Telegram → **@BotFather** → `/newbot` → pick a name and a username ending in `bot` → copy the token. That's your `TELEGRAM_BOT_TOKEN`.
Optional: `/setuserpic` with `frontend/public/mascot.png`.
*Use a new bot for the deployed site, not the one you test with on your computer (a bot can't use polling and a webhook at once).*

## Step 3. Deploy the backend
Vercel → **Add New Project** → pick your repo → name it `study-buddy-backend` (any `*-backend` name) →
**Root Directory: `backend/`** → Framework preset **Other** → add these Environment Variables → **Deploy**.

| Key | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from BotFather |
| `DATABASE_URL` | the Neon string (pooled) |
| `LLM_BASE_URL` | `https://api.apiyi.com/v1` |
| `LLM_MODEL` | `gemini-3.1-flash-lite` |
| `LLM_API_KEY` | from the AI Lodge API Key bot |
| `CRON_SECRET` | any long random text you make up (you'll use it twice below) |
| `FRONTEND_URL` | add this in Step 4 |
| `DAILY_BUDGET_USD` | optional. `5` by default; once the day's estimated AI cost reaches it, the bot pauses AI for the day |

When it finishes you get an address like `https://study-buddy-backend.vercel.app`. Open `…/api/health`. You should see
`"ok": true`, `"ai": true`, `"bot": true`, `"cron": true`, `"database": "postgres"`.

## Step 4. Deploy the frontend
Vercel → **Add New Project** → the same repo → name it `study-buddy-frontend` → **Root Directory: `frontend/`** (Vercel detects Vite) →
Environment Variable `VITE_BACKEND_URL` = your backend address from Step 3 (no trailing slash) → **Deploy**.

Now go back to the **backend** project → Settings → Environment Variables → set `FRONTEND_URL` to the frontend's address
(e.g. `https://study-buddy-frontend.vercel.app`) → **Redeploy** the backend. (This tells the backend which website may talk to it.)

## Step 5. Connect Telegram to the backend (once)
Open this in your browser, with your own address and `CRON_SECRET`:

`https://study-buddy-backend.vercel.app/api/setup?key=YOUR_CRON_SECRET`

It should show `"ok": true` and the webhook address. From now on Telegram sends every message there.

## Step 6. Start the scheduler (cron-job.org, free)
Vercel's free plan only allows a cron job once a day, which is too slow for "send my lesson at 5:00 pm". So a free pinger calls the backend every minute:
1. cron-job.org → **Create cronjob**.
2. URL: `https://study-buddy-backend.vercel.app/api/cron`
3. Schedule: **every 1 minute**.
4. Advanced → Headers → add `X-Cron-Key` with your `CRON_SECRET` as the value.
5. Save. Its history should show `200` and JSON like `{"due":0,"sent":0,"prepared":0,...}`.

*(On Vercel Pro you can skip this: add `"crons": [{"path": "/api/cron", "schedule": "* * * * *"}]` to `backend/vercel.json`.
Vercel sends your `CRON_SECRET` automatically.)*

## Step 7. Try it
Open the frontend → make a plan (upload slides if you like) → **Open Telegram** → press **Start** → try `/quiz`.
To test the scheduler, make a plan for today and set the time two minutes from now.

**Your app is live.**

---

## Optional: the Wall Street guide, just for you
This puts **your own copy** of the guide into your **private Neon database** (not GitHub, not the website's files), and shows it only
to someone who types a private access code. The guide is copyrighted by Breaking Into Wall Street: check its terms of use, keep the
code to yourself, and don't share the site's Wall Street option with other people.

1. Pick an access code (any long text) and add it to the **backend** project on Vercel as `BIWS_ACCESS_CODE`. Redeploy the backend.
2. On your Mac, put your Neon connection string in the `.env` file at the top of the project as `DATABASE_URL=...`, then load the guide (about 90 MB, a couple of minutes):
   ```bash
   cd backend && ../.venv/bin/python import_guide.py
   ```
   It reads `~/ibig-study-bot` and tells you it's loading into your Postgres database. It's safe to run again.
   **Afterwards, remove `DATABASE_URL` from that `.env`** (or keep it only while you're importing), so running the app on your computer doesn't touch the online database.
3. On the website, press **Get started**, then **Have an access code?**, and enter it. The Breaking Into Wall Street option appears on the list. The code is remembered only until you close the tab.

Neon's free plan holds 0.5 GB, and the guide takes about 93 MB. To take it down later: delete the `BIWS_ACCESS_CODE` variable (it locks immediately), or run `DROP TABLE guide_docs, guide_images;` in Neon's SQL Editor.

## Making changes later
Change the code (or ask Claude to), then:
```bash
.venv/bin/python tests/smoke.py        # backend checks (also: tests/smoke.py pg, tests/test_db.py)
git add . && git commit -m "what changed" && git push
```
Vercel redeploys the frontend and the backend by itself. Plans live in Neon, so updates never touch them.

## What's different from running it on your computer
- **Uploads are read in your browser.** A PDF or PowerPoint is turned into text on the page, and only that text is sent (Vercel
  refuses requests over 4.5 MB). Scanned PDFs have no text to read, so they're refused with a clear message.
- **Time limit:** each request can run for up to 60 seconds. Building a plan from a big course normally takes 10 to 30 seconds.
- **Breaking Into Wall Street is hidden online** until you do the optional step below. The guide is copyrighted, so it is never in GitHub
  and never shown without a private access code. It always works if you run the app on your own computer.
- **Voice notes are listened to.** In language plans the bot plays a phrase, you send a voice note, and an audio-capable AI model (`LLM_AUDIO_MODEL`, default `gemini-3.5-flash-lite`; fallback `LLM_AUDIO_FALLBACK`) writes down what it heard and coaches your pronunciation. It needs an AI key that can use a Gemini model.
- **Sessions are prepared 10 minutes ahead** by the scheduler, so scheduled lessons still arrive quickly.

## Keeping it safe and affordable
- There is **no login**: anyone with the website address can build plans, and that uses your AI key. Two guards are built in:
  a **daily budget** (`DAILY_BUDGET_USD`, an estimate from token counts, so keep it comfortably below what you can afford)
  and **20 new plans per visitor per day**. Share the address with people you trust. If it ever needs tighter control, ask Claude to add invite codes.
- A plan's secret is its random address, so only someone with that link can see or edit that plan.
- **Rotate secrets** if they ever leak: BotFather `/revoke` for the bot token, a new key from the AI Lodge bot, a new `CRON_SECRET`.
  The Telegram token you used in earlier testing was pasted into a chat, so regenerate it.
- Students' course text is stored in Neon (not the files). Delete a plan and its text goes too.

## If something doesn't work
| Problem | Check |
|---|---|
| The bot doesn't answer | Open `/api/setup?key=…` again and look at `info` (`last_error_message`). Check the backend's **Logs** in Vercel. |
| "Conflict: terminated by other getUpdates" | Something is polling with the same bot token, for example the app running on your computer. Stop it, or use a different bot locally. |
| The website says it can't reach the server | `VITE_BACKEND_URL` is wrong or missing (rebuild the frontend after changing it), or `FRONTEND_URL` on the backend doesn't match the website's address. |
| Scheduled lessons don't arrive | cron-job.org history: is it `200`? Is the header `X-Cron-Key` right? Is the plan's time zone what you expect? |
| Building a plan fails or takes too long | Try fewer or smaller files, or fewer lessons. Check the Vercel logs for the AI's error. |
| `/api/health` says `"ai": false` | `LLM_API_KEY` isn't set on the backend project. After adding variables, redeploy. |
| "daily AI limit" message | The day's estimated spend hit `DAILY_BUDGET_USD`. Raise it, or wait until tomorrow. |

## Running it on your computer
```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
cp backend/.env.example .env           # fill in the token and the AI key; leave DATABASE_URL empty to use a local file
cd backend && ../.venv/bin/python app.py    # http://localhost:5050 (website and bot, with polling instead of a webhook)
```
