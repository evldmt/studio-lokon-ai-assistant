import os
from dotenv import load_dotenv

load_dotenv()


def _list(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


VERIFY_TOKEN = os.environ["META_VERIFY_TOKEN"]
APP_SECRET = os.getenv("META_APP_SECRET", "")  # empty = signature check off (dev only)
IG_ACCESS_TOKEN = os.environ["IG_ACCESS_TOKEN"]
GRAPH_BASE = os.getenv("GRAPH_BASE", "https://graph.instagram.com/v21.0")
# The long-lived IG token lives 60 days. The bot extends it every N days and keeps the fresh one in the DB.
IG_TOKEN_REFRESH_DAYS = float(os.getenv("IG_TOKEN_REFRESH_DAYS", "7"))

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_MODELS", "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash-lite,gemini-flash-lite-latest"
).split(",") if m.strip()]
GEMINI_TIMEOUT_SEC = float(os.getenv("GEMINI_TIMEOUT_SEC", "12"))


def _normalize_db_url(url: str) -> str:
    # Supabase/Heroku give postgres:// or postgresql:// -> use psycopg3 driver
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


DATABASE_URL = _normalize_db_url(os.getenv("DATABASE_URL", "sqlite:///data/bot.db"))

SYSTEM_PROMPT_PATH = os.getenv("SYSTEM_PROMPT_PATH", "prompts/system_prompt.md")
DEBOUNCE_SECONDS = float(os.getenv("DEBOUNCE_SECONDS", "6"))
HANDOFF_PAUSE_HOURS = float(os.getenv("HANDOFF_PAUSE_HOURS", "12"))  # after mom wrote by hand
# After the bot itself handed a client over ([МАСТЕР]): short, because nobody is notified -
# if mom hasn't answered by then, the bot answers simple questions again
BOT_HANDOFF_PAUSE_HOURS = float(os.getenv("BOT_HANDOFF_PAUSE_HOURS", "2"))
# Mom shared a reel/post in the chat: a personal chat, or she leads this client herself
PERSONAL_PAUSE_HOURS = float(os.getenv("PERSONAL_PAUSE_HOURS", "72"))
FOLLOWUP_DELAY_HOURS = float(os.getenv("FOLLOWUP_DELAY_HOURS", "20"))
# FOLLOWUP_TEXT is not used any more: the follow-up is written in the client's language (app/lang.py)
# Echo arriving within N sec after client's message = automation (IG auto-reply / saved template), not mom
AUTO_REPLY_WINDOW_SEC = float(os.getenv("AUTO_REPLY_WINDOW_SEC", "15"))
# template_wins: template answered -> bot stays silent for this batch
# llm_only:      ignore templates, bot always answers (turn IG auto-replies OFF in this mode)
TEMPLATE_MODE = os.getenv("TEMPLATE_MODE", "llm_only")
WINDOW_HOURS = 24  # Instagram standard messaging window

# Telegram: mom gets a message when the bot hands a client over ([МАСТЕР]); admins get technical alerts.
# Several chat ids can be listed comma-separated.
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_IDS = _list("TG_CHAT_ID")
TG_ADMIN_CHAT_IDS = _list("TG_ADMIN_CHAT_ID")
HANDOFF_MARKER = "[МАСТЕР]"

# Personal contacts (Instagram-scoped ids from the logs): the bot never answers them
IGNORE_IGSIDS = set(_list("IGNORE_IGSIDS"))
