"""SQLAlchemy layer. Works on SQLite locally and PostgreSQL (Supabase) by changing DATABASE_URL."""
import os
import re
import time
from contextlib import contextmanager

from sqlalchemy import Boolean, Float, Integer, String, Text, create_engine, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from . import config

UNPROCESSED, PROCESSED = "unprocessed", "processed"
# Message roles: user = client, assistant = bot, master = mom wrote by hand, auto = Instagram automation
STUDIO_ROLES = ("master", "auto")

_CARD_RE = re.compile(r"(?<!\d)(?:\d{4}[ \-]?){3}\d{4}(?!\d)")  # 16 digits, as cards are written


def mask_cards(text: str | None) -> str | None:
    """Card numbers never go to the DB, the logs or the LLM (prices and phone numbers don't match)."""
    return _CARD_RE.sub("[номер карты]", text) if text else text


class Base(DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = "clients"
    ig_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_inbound_at: Mapped[float] = mapped_column(Float, default=0)
    followup_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    is_paused_until: Mapped[float] = mapped_column(Float, default=0)  # epoch seconds


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ig_user_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | master | auto
    text: Mapped[str] = mapped_column(Text)
    mid: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=PROCESSED, index=True)
    created_at: Mapped[float] = mapped_column(Float)


class KV(Base):
    """Small key-value store: refreshed IG token, per-client handoff time."""
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[float] = mapped_column(Float)


_is_sqlite = config.DATABASE_URL.startswith("sqlite")
engine = create_engine(
    config.DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db() -> None:
    if _is_sqlite:
        path = config.DATABASE_URL.replace("sqlite:///", "", 1)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    Base.metadata.create_all(engine)  # creates only missing tables (kv); existing ones are untouched


def _get_or_create_client(s, user_id: str) -> Client:
    c = s.get(Client, user_id)
    if c is None:
        c = Client(ig_user_id=user_id, last_inbound_at=0, followup_sent=False, is_paused_until=0)
        s.add(c)
    return c


# ---------- inbound ----------
def save_inbound(user_id: str, text: str, mid: str | None) -> tuple[int, float] | None:
    """Saves message as unprocessed. Returns (message_id, ts) or None on duplicate (Meta retry)."""
    now = time.time()
    try:
        with session() as s:
            m = Message(ig_user_id=user_id, role="user", text=mask_cards(text), mid=mid, status=UNPROCESSED,
                        created_at=now)
            s.add(m)
            s.flush()
            c = _get_or_create_client(s, user_id)
            c.last_inbound_at = now
            c.followup_sent = False
            return m.id, now
    except IntegrityError:
        return None


def is_latest_inbound(user_id: str, message_id: int) -> bool:
    with session() as s:
        last = s.scalar(select(func.max(Message.id)).where(Message.ig_user_id == user_id, Message.role == "user"))
        return last == message_id


def fetch_unprocessed(user_id: str) -> list[tuple[int, str]]:
    with session() as s:
        rows = s.execute(
            select(Message.id, Message.text)
            .where(Message.ig_user_id == user_id, Message.status == UNPROCESSED)
            .order_by(Message.id)
        ).all()
        return [(r.id, r.text) for r in rows]


def mark_processed(ids: list[int]) -> None:
    if not ids:
        return
    with session() as s:
        s.execute(update(Message).where(Message.id.in_(ids)).values(status=PROCESSED))


def get_history(user_id: str, limit: int = 20) -> list[dict]:
    """Processed dialogue incl. mom's own messages (pending client messages are passed separately, glued)."""
    with session() as s:
        rows = s.execute(
            select(Message.role, Message.text)
            .where(Message.ig_user_id == user_id, Message.status == PROCESSED)
            .order_by(Message.id.desc())
            .limit(limit)
        ).all()
    return [{"role": r.role, "content": r.text} for r in reversed(rows)]


def last_message(user_id: str) -> dict | None:
    with session() as s:
        r = s.execute(select(Message.role, Message.text, Message.created_at)
                      .where(Message.ig_user_id == user_id).order_by(Message.id.desc()).limit(1)).first()
    return {"role": r.role, "text": r.text, "created_at": r.created_at} if r else None


def recent_user_texts(user_id: str, n: int = 3) -> list[str]:
    with session() as s:
        rows = s.execute(select(Message.text).where(Message.ig_user_id == user_id, Message.role == "user")
                         .order_by(Message.id.desc()).limit(n)).all()
    return [r.text for r in reversed(rows)]


# ---------- outbound ----------
def save_outbound(user_id: str, text: str, mid: str | None = None) -> int:
    """Call BEFORE sending, so the echo from Meta is always recognised as the bot's own message."""
    with session() as s:
        m = Message(ig_user_id=user_id, role="assistant", text=text, mid=mid, status=PROCESSED, created_at=time.time())
        s.add(m)
        s.flush()
        return m.id


def save_studio_message(user_id: str, role: str, text: str | None, mid: str | None) -> None:
    """Mom's manual message or an Instagram automation, kept so the bot knows what was already said."""
    if not text:
        return
    try:
        with session() as s:
            s.add(Message(ig_user_id=user_id, role=role, text=mask_cards(text), mid=mid, status=PROCESSED,
                          created_at=time.time()))
    except IntegrityError:
        pass  # Meta delivered the same echo twice


def set_outbound_mid(message_id: int, mid: str | None) -> None:
    if not mid:
        return
    try:
        with session() as s:
            s.execute(update(Message).where(Message.id == message_id).values(mid=mid))
    except IntegrityError:
        pass


def delete_message(message_id: int) -> None:
    with session() as s:
        m = s.get(Message, message_id)
        if m:
            s.delete(m)


def is_bot_message(mid: str | None, text: str | None, window_sec: int = 600) -> bool:
    """True if an echo belongs to a message the bot itself sent (not a manual reply)."""
    with session() as s:
        if mid and s.scalar(select(Message.id).where(Message.mid == mid, Message.role == "assistant")):
            return True
        if text:  # bot rows are saved before sending, so text match is reliable
            text = text.strip()
            return bool(s.scalar(
                select(Message.id).where(
                    Message.role == "assistant", Message.text == text,
                    Message.created_at > time.time() - window_sec,
                )
            ))
    return False


# ---------- handoff ----------
def set_pause(user_id: str, hours: float) -> None:
    with session() as s:
        c = _get_or_create_client(s, user_id)
        c.is_paused_until = max(c.is_paused_until or 0, time.time() + hours * 3600)


def is_paused(user_id: str) -> bool:
    with session() as s:
        c = s.get(Client, user_id)
        return bool(c and c.is_paused_until > time.time())


# ---------- follow-up ----------
def get_client(user_id: str) -> dict | None:
    with session() as s:
        c = s.get(Client, user_id)
        if not c:
            return None
        return {"last_inbound_at": c.last_inbound_at, "followup_sent": c.followup_sent,
                "is_paused_until": c.is_paused_until}


def mark_followup_sent(user_id: str) -> None:
    with session() as s:
        _get_or_create_client(s, user_id).followup_sent = True


def skip_pending(user_id: str) -> int:
    """Marks all unprocessed client messages as processed (bot won't answer them)."""
    with session() as s:
        r = s.execute(update(Message).where(Message.ig_user_id == user_id, Message.status == UNPROCESSED)
                      .values(status=PROCESSED))
        return r.rowcount


# ---------- key-value ----------
def get_kv(key: str) -> str | None:
    with session() as s:
        row = s.get(KV, key)
        return row.value if row else None


def set_kv(key: str, value: str) -> None:
    with session() as s:
        row = s.get(KV, key)
        if row:
            row.value, row.updated_at = value, time.time()
        else:
            s.add(KV(key=key, value=value, updated_at=time.time()))
