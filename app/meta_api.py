import hashlib
import hmac
import logging
import re
import time
from dataclasses import dataclass, field

import httpx

from . import config, db

log = logging.getLogger(__name__)
IG_TEXT_LIMIT = 1000


def _http(**kw) -> httpx.AsyncClient:
    """Single place to create HTTP clients (tests swap it for a mock transport)."""
    return httpx.AsyncClient(**kw)


@dataclass
class IncomingEvent:
    kind: str            # "client" (message from customer) | "manual" (echo: sent from the studio account)
    client_id: str       # always the customer's IGSID
    text: str | None
    mid: str | None
    media_ids: list[str] = field(default_factory=list)  # shared posts/reels without caption in payload
    att_types: list[str] = field(default_factory=list)  # attachment types of a studio echo


SHARE_TYPES = {"ig_post", "share", "ig_reel", "reel"}
ATTACHMENT_LABELS = {"image": "[фото]", "video": "[видео]", "audio": "[голосовое сообщение]", "file": "[файл]"}
CAPTION_LIMIT = 400


def _label(typ: str | None) -> str:
    if typ in SHARE_TYPES:
        return "[публикация]"
    return ATTACHMENT_LABELS.get(typ or "", "[вложение]")


def parse_webhook(data: dict) -> list[IncomingEvent]:
    """Normalises Meta payload. Echoes (is_echo or sender == our account) become kind='manual'."""
    events: list[IncomingEvent] = []
    if data.get("object") != "instagram":
        return events
    for entry in data.get("entry", []):
        own_id = entry.get("id")
        for ev in entry.get("messaging", []):
            msg = ev.get("message")
            if not msg or msg.get("is_deleted") or msg.get("is_unsupported"):
                continue
            sender = ev.get("sender", {}).get("id")
            recipient = ev.get("recipient", {}).get("id")
            if msg.get("is_echo") or (own_id and sender == own_id):
                types = [a.get("type") for a in msg.get("attachments") or []]
                text = msg.get("text") or (" ".join(_label(t) for t in types) if types else None)
                events.append(IncomingEvent("manual", recipient, text, msg.get("mid"), att_types=types))
                continue
            text, media_ids = describe_client_message(msg, ev)
            if sender and (text or media_ids):
                events.append(IncomingEvent("client", sender, text, msg.get("mid"), media_ids))
    return events


def clean_caption(caption: str | None) -> str:
    """Drops hashtags and extra whitespace, keeps the descriptive part of a caption."""
    if not caption:
        return ""
    caption = re.sub(r"#\S+", "", caption)
    caption = re.sub(r"\s+", " ", caption).strip()
    return caption[:CAPTION_LIMIT]


def post_context(caption: str) -> str:
    return f"[Клиентка переслала пост студии. Подпись поста: «{caption}»]"


def describe_client_message(msg: dict, ev: dict) -> tuple[str | None, list[str]]:
    """Turns text + attachments + ad referral into one text for the LLM.
    Shared posts/reels carry their caption in payload.title; if absent, media id is returned
    so the caption can be fetched from the Graph API."""
    parts: list[str] = []
    media_ids: list[str] = []
    referral = msg.get("referral") or ev.get("referral") or {}
    ads = referral.get("ads_context_data") or {}
    if ads.get("ad_title"):
        parts.append(f"[Клиентка пришла с рекламы. Текст рекламы: «{clean_caption(ads['ad_title'])}»]")
    if (msg.get("reply_to") or {}).get("story"):
        parts.append("[ответ на историю студии]")
    for att in msg.get("attachments") or []:
        typ = att.get("type")
        payload = att.get("payload") or {}
        if typ in SHARE_TYPES:
            caption = clean_caption(payload.get("title"))
            if caption:
                parts.append(post_context(caption))
            else:
                mid = payload.get("ig_post_media_id") or payload.get("reel_video_id") or payload.get("id")
                if mid:
                    media_ids.append(str(mid))
                else:
                    parts.append("[Клиентка переслала пост без подписи]")
        elif typ == "story_mention":
            parts.append("[клиентка отметила студию в своей истории]")
        else:
            parts.append(ATTACHMENT_LABELS.get(typ or "", "[вложение]"))
    if msg.get("text"):
        parts.append(msg["text"])
    return ("\n".join(parts) or None), media_ids


_PRICE_RE = re.compile(r"\d[\d\s.,]{2,}\d\s*(?:сум|сўм|so'm|som|uzs)", re.I)


def looks_like_automation(text: str | None) -> bool:
    """Instagram's comment -> DM automation (RU/UZ/EN) sends a 💛 price list; mom writes short messages."""
    if not text:
        return False
    low = text.lower()
    if "💛" in text or "спасибо за интерес" in low or "thank you for your interest" in low or "qiziqish" in low:
        return True
    bullets = sum(1 for line in text.splitlines() if line.strip().startswith("•"))
    return bullets >= 3 or len(_PRICE_RE.findall(text)) >= 3 or is_ad_greeting(text)


# Greetings of the click-to-Direct ad templates (Ads Manager -> "Переписки"). If Meta echoes one into the
# chat, it is automation, not mom: keep it as context and don't pause. Keep in sync with AD_TEMPLATE.md.
AD_GREETINGS = (
    "здравствуйте! чем мы можем вам помочь",
    "здравствуйте! афролоконы ариэль — от 1 300 000",
    "здравствуйте! детские косички, работа + материал",
)


def is_ad_greeting(text: str | None) -> bool:
    low = " ".join((text or "").lower().split())
    return any(low.startswith(g) for g in AD_GREETINGS)


# ---------- access token ----------
_token_cache: dict = {"value": None, "at": 0.0}


def _env_fingerprint() -> str:
    return hashlib.sha256(config.IG_ACCESS_TOKEN.encode()).hexdigest()[:16]


def access_token() -> str:
    """Refreshed token from the DB; the env token if nothing was refreshed yet or env was changed by hand."""
    now = time.time()
    if _token_cache["value"] and now - _token_cache["at"] < 600:
        return _token_cache["value"]
    token = config.IG_ACCESS_TOKEN
    try:
        stored = db.get_kv("ig_token")
        if stored and db.get_kv("ig_token_env") == _env_fingerprint():
            token = stored
    except Exception:
        log.exception("Token lookup failed, using env token")
    _token_cache.update(value=token, at=now)
    return token


def _drop_refreshed_token() -> None:
    db.set_kv("ig_token", "")
    _token_cache.update(value=None, at=0.0)


async def refresh_access_token() -> int | None:
    """Extends the long-lived token by ~60 days. Returns days of validity, or None on failure."""
    base = re.sub(r"/v\d+(\.\d+)?/?$", "", config.GRAPH_BASE)
    try:
        async with _http(timeout=15) as client:
            r = await client.get(f"{base}/refresh_access_token",
                                 params={"grant_type": "ig_refresh_token", "access_token": access_token()})
        if r.status_code >= 400:
            log.error("IG token refresh failed %s: %s", r.status_code, r.text[:300])
            return None
        data = r.json()
        new, expires_in = data.get("access_token"), float(data.get("expires_in") or 0)
        if not new:
            log.error("IG token refresh: no token in response")
            return None
        db.set_kv("ig_token", new)
        db.set_kv("ig_token_env", _env_fingerprint())
        db.set_kv("ig_token_refreshed_at", str(time.time()))
        db.set_kv("ig_token_expires_at", str(time.time() + expires_in))
        _token_cache.update(value=new, at=time.time())
        days = int(expires_in // 86400)
        log.info("IG token refreshed, valid ~%d days", days)
        return days
    except Exception as e:
        log.error("IG token refresh error: %s", type(e).__name__)
        return None


def token_days_left() -> int | None:
    try:
        exp = db.get_kv("ig_token_expires_at")
        return int((float(exp) - time.time()) // 86400) if exp else None
    except Exception:
        return None


def _auth() -> dict:
    return {"Authorization": f"Bearer {access_token()}"}


# ---------- Graph API ----------
async def fetch_caption(media_id: str) -> str:
    """Caption of the studio's own post/reel. Empty string on any error."""
    try:
        async with _http(timeout=10) as client:
            r = await client.get(f"{config.GRAPH_BASE}/{media_id}", params={"fields": "caption"}, headers=_auth())
        if r.status_code >= 400:
            log.warning("Caption fetch failed %s for %s: %s", r.status_code, media_id, r.text[:200])
            return ""
        return clean_caption(r.json().get("caption"))
    except Exception:
        log.exception("Caption fetch error for %s", media_id)
        return ""


async def enrich_text(text: str | None, media_ids: list[str]) -> str | None:
    """Adds captions of shared posts that came without payload.title."""
    extra = []
    for mid in media_ids:
        cap = await fetch_caption(mid)
        extra.append(post_context(cap) if cap else "[Клиентка переслала пост, подпись не удалось получить]")
    return "\n".join(extra + ([text] if text else [])) or None


def _is_token_error(r: httpx.Response) -> bool:
    try:
        return r.status_code in (400, 401) and (r.json().get("error") or {}).get("code") == 190
    except Exception:
        return False


async def send_text(recipient_id: str, text: str) -> str | None:
    """Sends DM, returns Meta message_id (used to recognise our own echoes)."""
    payload = {"recipient": {"id": recipient_id}, "message": {"text": text[:IG_TEXT_LIMIT]}}
    for attempt in range(2):
        token = access_token()
        async with _http(timeout=15) as client:
            r = await client.post(f"{config.GRAPH_BASE}/me/messages", json=payload,
                                  headers={"Authorization": f"Bearer {token}"})
        if r.status_code < 400:
            return r.json().get("message_id")
        if attempt == 0 and _is_token_error(r) and token != config.IG_ACCESS_TOKEN:
            log.error("Refreshed IG token rejected, falling back to the env token")
            _drop_refreshed_token()
            continue
        log.error("Meta send failed %s: %s", r.status_code, r.text)
        r.raise_for_status()
    return None


async def fetch_username(igsid: str) -> str | None:
    try:
        async with _http(timeout=6) as client:
            r = await client.get(f"{config.GRAPH_BASE}/{igsid}", params={"fields": "username"}, headers=_auth())
        return r.json().get("username") if r.status_code < 400 else None
    except Exception:
        return None


# ---------- Telegram ----------
async def _tg_send(chat_ids: list[str], text: str) -> bool:
    """httpx request logging is off (see main.py): the bot token is part of the URL."""
    if not (config.TG_BOT_TOKEN and chat_ids):
        return False
    ok = False
    async with _http(timeout=10) as client:
        for cid in chat_ids:
            try:
                r = await client.post(f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}/sendMessage",
                                      json={"chat_id": cid, "text": text, "disable_web_page_preview": True})
                if r.status_code >= 400:
                    log.error("Telegram notify failed %s: %s", r.status_code, r.text[:200])
                else:
                    ok = True
            except Exception as e:
                log.error("Telegram notify error: %s", type(e).__name__)
    return ok


NOTIFY_TITLES = {
    "booking": "🟢 Клиентка хочет записаться",
    "handoff": "🔴 Клиентке нужен ответ мастера",
    "error": "🟠 Бот не смог ответить",
}


async def notify_master(client_id: str, client_text: str, bot_reply: str = "", kind: str = "handoff") -> None:
    """Telegram message to mom: a client needs her personally. No-op if not configured."""
    if not (config.TG_BOT_TOKEN and config.TG_CHAT_IDS):
        log.warning("HANDOFF for %s (Telegram notify not configured)", client_id)
        return
    username = await fetch_username(client_id)
    lines = [f"{NOTIFY_TITLES.get(kind, NOTIFY_TITLES['handoff'])} (директ Instagram)"]
    if username:
        lines.append(f"@{username} — открыть чат: https://ig.me/m/{username}")
    lines.append(f"\nКлиентка: {client_text[:600]}")
    if bot_reply:
        lines.append(f"\nБот ответил: {bot_reply[:400]}")
    lines.append("\nБот в этом чате замолчал — ответьте, пожалуйста, сами.")
    if await _tg_send(config.TG_CHAT_IDS, "\n".join(lines)):
        log.info("Mom notified in Telegram about %s (%s)", client_id, kind)


_admin_last: dict[str, float] = {}


async def notify_admin(key: str, text: str) -> None:
    """Technical alert for Vadim, at most once an hour per kind of problem."""
    log.error("ALERT [%s]: %s", key, text)
    now = time.time()
    if now - _admin_last.get(key, 0) < 3600:
        return
    _admin_last[key] = now
    await _tg_send(config.TG_ADMIN_CHAT_IDS, f"⚠️ studio-lokon-bot: {text}")


def verify_signature(body: bytes, header: str | None) -> bool:
    if not config.APP_SECRET:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(config.APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))
