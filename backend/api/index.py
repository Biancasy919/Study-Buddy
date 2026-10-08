# Vercel runs this file as the backend's one function. Every request (the website's API, Telegram's webhook, the cron ping) comes through here.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402,F401  (Vercel looks for a WSGI object named `app`)
