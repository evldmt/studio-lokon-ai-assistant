import logging
import time
from datetime import datetime, timedelta, timezone

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import config, db, lang, meta_api

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(
    jobstores={"default": SQLAlchemyJobStore(engine=db.engine)},
    job_defaults={"coalesce": True, "misfire_grace_time": 3 * 3600},
    timezone=timezone.utc,
)


def schedule_followup(user_id: str, inbound_ts: float) -> None:
    run_at = datetime.fromtimestamp(inbound_ts + config.FOLLOWUP_DELAY_HOURS * 3600, tz=timezone.utc)
    scheduler.add_job(
        send_followup, trigger="date", run_date=run_at,
        id=f"followup:{user_id}", args=[user_id, inbound_ts], replace_existing=True,
    )


def followup_block_reason(user_id: str, c: dict, inbound_ts: float) -> str | None:
    """Why a follow-up must not be sent now, or None. The follow-up only nudges a client who went
    silent after the bot's answer; if she is waiting for mom, or mom is talking to her, we stay quiet."""
    now = time.time()
    if c["is_paused_until"] > now:
        return "human handoff active"
    if now - inbound_ts > (config.WINDOW_HOURS - 0.5) * 3600:
        return "24h window closed"
    last = db.last_message(user_id)
    if not last or last["role"] != "assistant":
        return f"last message is from {last['role'] if last else 'nobody'}"
    if float(db.get_kv(f"handoff:{user_id}") or 0) >= inbound_ts:
        return "client is waiting for the master"
    if lang.is_refusal(" ".join(db.recent_user_texts(user_id, 1))):
        return "client declined"
    return None


async def send_followup(user_id: str, inbound_ts: float) -> None:
    c = db.get_client(user_id)
    if not c or c["followup_sent"]:
        return
    # Postgres returns last_inbound_at rounded to ~15 significant digits: never compare the floats exactly
    if abs(c["last_inbound_at"] - inbound_ts) > 1:
        log.info("Follow-up skipped for %s: client wrote again", user_id)
        return
    reason = followup_block_reason(user_id, c, inbound_ts)
    if reason:
        log.info("Follow-up skipped for %s: %s", user_id, reason)
        return
    text = lang.followup_text(" ".join(db.recent_user_texts(user_id)))
    try:
        out_id = db.save_outbound(user_id, text)
        mid = await meta_api.send_text(user_id, text)
        db.set_outbound_mid(out_id, mid)
        db.mark_followup_sent(user_id)
        log.info("Follow-up sent to %s: %r", user_id, text)
    except Exception:
        log.exception("Follow-up failed for %s", user_id)


def schedule_maintenance() -> None:
    """Daily check that keeps the Instagram token alive (first check 2 minutes after start)."""
    scheduler.add_job(
        refresh_ig_token, trigger="interval", hours=24, id="ig_token_refresh", replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
    )


async def refresh_ig_token() -> None:
    last = float(db.get_kv("ig_token_refreshed_at") or 0)
    if time.time() - last < config.IG_TOKEN_REFRESH_DAYS * 86400:
        return
    if await meta_api.refresh_access_token() is None:
        await meta_api.notify_admin(
            "token", "не удалось продлить токен Instagram. Без продления бот перестанет отвечать, "
                     "проверь логи Render.")
