import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from . import config, db, lang, llm, meta_api
from .scheduler import schedule_followup, schedule_maintenance, scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs may carry tokens (Telegram, token refresh)
log = logging.getLogger("bot")

_user_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_last_template_at: dict[str, float] = {}  # when an IG auto-reply last answered this client

# The model sometimes writes the marker in another case or language
MARKER_RE = re.compile(r"\[\s*(?:МАСТЕР|MASTER|USTA|УСТА)\s*\]", re.I)
# The client is about to book: named a day/time or asks how to pay -> mom's notification is green
BOOKING_RE = re.compile(
    r"запис|брон|предоплат|оплат|карт[уаы]\b|реквизит|yozil|ёзил|понедельн|вторник|сред[уа]|четверг|пятниц|"
    r"суббот|воскресен|завтра|сегодня|послезавтра|dushanba|seshanba|chorshanba|payshanba|juma|shanba|"
    r"yakshanba|ertaga|bugun|душанба|сешанба|чоршанба|пайшанба|жума|шанба|якшанба|эртага|бугун|"
    r"\b\d{1,2}[:.]\d{2}\b|\b\d{1,2}\s*(?:числа|октябр|ноябр|декабр|январ)", re.I)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    scheduler.start()
    schedule_maintenance()
    log.info("Started. DB=%s, pending jobs=%d, telegram=%s, models=%s",
             db.engine.url.render_as_string(hide_password=True), len(scheduler.get_jobs()),
             "on" if config.TG_BOT_TOKEN and config.TG_CHAT_IDS else "off", ",".join(config.GEMINI_MODELS))
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Studio Lokon IG Bot", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "jobs": len(scheduler.get_jobs()),
            "telegram": bool(config.TG_BOT_TOKEN and config.TG_CHAT_IDS),
            "token_days_left": meta_api.token_days_left()}


@app.get("/privacy", response_class=HTMLResponse)
async def privacy():
    return Path("prompts/privacy.html").read_text(encoding="utf-8")


@app.get("/webhook")
async def verify_webhook(
    mode: str | None = Query(None, alias="hub.mode"),
    token: str | None = Query(None, alias="hub.verify_token"),
    challenge: str | None = Query(None, alias="hub.challenge"),
):
    if mode == "subscribe" and token == config.VERIFY_TOKEN:
        return PlainTextResponse(challenge or "")
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not meta_api.verify_signature(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=403, detail="Bad signature")

    payload = json.loads(body or b"{}")
    log.info("WEBHOOK %s", json.dumps(_masked(payload), ensure_ascii=False)[:1500])
    for ev in meta_api.parse_webhook(payload):
        if ev.client_id in config.IGNORE_IGSIDS:
            continue  # personal contact
        if ev.kind == "manual":
            background.add_task(handle_manual_reply, ev.client_id, ev.mid, ev.text, ev.att_types)
        else:
            background.add_task(handle_client_message, ev.client_id, ev.text, ev.mid, ev.media_ids)
    return {"ok": True}  # respond to Meta immediately


def _masked(payload: dict) -> dict:
    """Copy of the webhook payload for the log, with card numbers hidden in message texts."""
    try:
        p = json.loads(json.dumps(payload))
        for entry in p.get("entry", []):
            for ev in entry.get("messaging", []):
                m = ev.get("message") or {}
                if m.get("text"):
                    m["text"] = db.mask_cards(m["text"])
        return p
    except Exception:
        return payload


async def handle_manual_reply(client_id: str, mid: str | None, text: str | None,
                              att_types: list[str] | None = None) -> None:
    """Echo of a message sent from the studio account: bot's own / IG automation / mom."""
    received_at = time.time()
    await asyncio.sleep(1)
    if db.is_bot_message(mid, text):
        return
    client = db.get_client(client_id)
    if config.TEMPLATE_MODE != "llm_only":
        if client and received_at - client["last_inbound_at"] < config.AUTO_REPLY_WINDOW_SEC:
            # Too fast for a human -> Instagram auto-reply / saved template
            _last_template_at[client_id] = received_at
            n = db.skip_pending(client_id)
            log.info("Template answered %s -> bot silent for %d msg(s): %r", client_id, n, text)
            return
        db.set_pause(client_id, config.HANDOFF_PAUSE_HOURS)
        log.warning("HANDOFF: bot paused for %s for %sh (echo mid=%s text=%r)",
                    client_id, config.HANDOFF_PAUSE_HOURS, mid, text)
        return

    # llm_only: only the bot and mom write in Direct; the comment -> DM automation is the one exception
    if any(t in meta_api.SHARE_TYPES for t in att_types or []):
        # Mom shared a reel/post: a personal chat, or she is leading this client herself
        db.save_studio_message(client_id, "master", text, mid)
        db.set_pause(client_id, config.PERSONAL_PAUSE_HOURS)
        log.warning("HANDOFF: mom shared a post with %s -> bot paused %sh", client_id, config.PERSONAL_PAUSE_HOURS)
        return
    if meta_api.is_ad_greeting(text):
        # Greeting of a click-to-Direct ad: may arrive just before or after the client's first message
        db.save_studio_message(client_id, "auto", text, mid)
        log.info("Ad greeting echo for %s kept as context", client_id)
        return
    recent_inbound = client and received_at - client["last_inbound_at"] <= config.WINDOW_HOURS * 3600
    if not recent_inbound and meta_api.looks_like_automation(text):
        db.save_studio_message(client_id, "auto", text, mid)
        log.info("Automation echo (comment -> DM) for %s kept as context", client_id)
        return
    db.save_studio_message(client_id, "master", text, mid)
    db.set_pause(client_id, config.HANDOFF_PAUSE_HOURS)
    log.warning("HANDOFF: bot paused for %s for %sh (echo mid=%s text=%r)",
                client_id, config.HANDOFF_PAUSE_HOURS, mid, db.mask_cards(text))


async def handle_client_message(client_id: str, text: str | None, mid: str | None,
                                media_ids: list[str] | None = None) -> None:
    if media_ids:
        text = await meta_api.enrich_text(text, media_ids)
    if not text:
        return
    saved = db.save_inbound(client_id, text, mid)
    if saved is None:
        return  # duplicate delivery from Meta
    msg_id, ts = saved
    schedule_followup(client_id, ts)

    # Debounce: wait; if a newer message arrived meanwhile, let its task do the work
    await asyncio.sleep(config.DEBOUNCE_SECONDS)
    if not db.is_latest_inbound(client_id, msg_id):
        return

    async with _user_locks[client_id]:
        pending = db.fetch_unprocessed(client_id)
        if not pending:
            return
        ids = [p[0] for p in pending]

        if db.is_paused(client_id):
            db.mark_processed(ids)  # mom handles it; keep in history
            log.info("Paused (mom) -> silent for %s", client_id)
            return

        batch_started = time.time()
        combined = "\n".join(p[1] for p in pending)
        log.info("-> Gemini for %s: %r", client_id, combined)
        reply = await llm.generate_reply(db.get_history(client_id), combined)

        if not db.is_latest_inbound(client_id, ids[-1]):
            # Client wrote again while Gemini was thinking: drop this draft, the newer task
            # (waiting on the lock) will answer all unprocessed messages in ONE reply.
            log.info("Newer message during generation -> draft dropped for %s", client_id)
            return
        if db.is_paused(client_id):  # mom answered while Gemini was thinking
            db.mark_processed(ids)
            return
        if _last_template_at.get(client_id, 0) >= batch_started - config.DEBOUNCE_SECONDS - 1:
            db.mark_processed(ids)  # IG auto-reply answered meanwhile -> stay silent
            log.info("Template answered during generation -> silent for %s", client_id)
            return

        failed = reply is None
        if failed:  # every model failed: polite fallback + mom takes over
            reply = f"{lang.fallback_text(combined)} {config.HANDOFF_MARKER}"
            await meta_api.notify_admin("llm", "Gemini не ответил ни одной моделью, клиентке ушёл запасной ответ.")
        handoff = bool(MARKER_RE.search(reply))
        reply = re.sub(r"[ \t]+\n", "\n", MARKER_RE.sub("", reply)).strip()
        out_id = db.save_outbound(client_id, reply)  # save first: protects from echo race
        try:
            out_mid = await meta_api.send_text(client_id, reply)
        except Exception:
            db.delete_message(out_id)
            log.exception("Reply failed for %s", client_id)  # stays unprocessed -> retried on next msg
            await meta_api.notify_admin("send", "Instagram не принял ответ бота. Проверь токен и логи Render.")
            return
        db.set_outbound_mid(out_id, out_mid)
        db.mark_processed(ids)
        log.info("<- Sent to %s: %r", client_id, reply)
        if handoff:
            db.set_pause(client_id, config.BOT_HANDOFF_PAUSE_HOURS)
            db.set_kv(f"handoff:{client_id}", str(time.time()))
            log.warning("HANDOFF by bot for %s -> paused %sh", client_id, config.BOT_HANDOFF_PAUSE_HOURS)
            kind = "error" if failed else ("booking" if BOOKING_RE.search(combined) else "handoff")
            await meta_api.notify_master(client_id, combined, reply, kind)
