# Studio Lokon AI Assistant

Instagram Direct assistant for a one-person hair studio in Tashkent. It answers client questions about services and prices, helps book appointments, and hands the chat over to the master when a human is needed. Running in production.

## What it does

- Receives Instagram DMs via Meta Graph API webhooks (FastAPI)
- Generates replies with Google Gemini, with model fallback and per-model timeouts
- Detects client language (RU / UZ / EN) and replies in it
- Debounces bursts of messages into one reply
- Pauses itself when the master takes over a chat, or when a chat is personal
- Sends follow-ups to clients who went quiet (APScheduler)
- Notifies the master and admin in Telegram (handoffs, token/API failures)
- Refreshes the long-lived Instagram token automatically

## Stack

Python, FastAPI, httpx, google-genai, SQLAlchemy (SQLite / PostgreSQL on Supabase), APScheduler, pytest. Deployed on Render (`render.yaml`).

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your own keys
./run.sh
pytest -q
```

Contact details and IDs in prompts and tests are placeholders.
