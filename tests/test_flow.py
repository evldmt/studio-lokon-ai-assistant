"""Offline tests: Meta & Gemini are faked. Run: pytest -q"""
import os, time, importlib, threading, asyncio, logging
import httpx
import pytest

os.environ.update(META_VERIFY_TOKEN="tok", IG_ACCESS_TOKEN="x", GEMINI_API_KEY="x", META_APP_SECRET="",
                  DEBOUNCE_SECONDS="2", FOLLOWUP_DELAY_HOURS="0.002", AUTO_REPLY_WINDOW_SEC="15",
                  HANDOFF_PAUSE_HOURS="12", TG_BOT_TOKEN="", TG_CHAT_ID="", TG_ADMIN_CHAT_ID="", IGNORE_IGSIDS="")

AUTOMATION_TEXT = ("Здравствуйте! Спасибо за интерес 💛\nЦены (работа + материал):\n• Ариэль на каркасе 60–80 см — "
                   "1 300 000 сум\n• Ариэль 90 см — 1 600 000 сум\n• Анна на каркасе — 1 300 000 сум")


def _reload(tmp_path, mode):
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_path}/t.db"
    os.environ["TEMPLATE_MODE"] = mode
    import app.config, app.db, app.lang, app.meta_api, app.llm, app.scheduler, app.main
    mods = (app.config, app.db, app.lang, app.meta_api, app.llm, app.scheduler, app.main)
    for m in mods:
        importlib.reload(m)
    return app


@pytest.fixture()
def bot(tmp_path, monkeypatch):
    app = _reload(tmp_path, getattr(bot, "mode", "template_wins"))
    sent, calls, n = [], [], [0]

    async def fake_send(rid, text):
        n[0] += 1; sent.append((rid, text)); return f"out{n[0]}"

    async def fake_llm(history, new):
        calls.append(new); return f"BOT:{new}"

    monkeypatch.setattr(app.meta_api, "send_text", fake_send)
    monkeypatch.setattr(app.llm, "generate_reply", fake_llm)
    from fastapi.testclient import TestClient
    with TestClient(app.main.app) as c:
        yield c, sent, calls, app.db


@pytest.fixture()
def bot_llm(monkeypatch, tmp_path):
    bot.mode = "llm_only"
    try:
        yield from bot.__wrapped__(tmp_path, monkeypatch)
    finally:
        bot.mode = "template_wins"


@pytest.fixture()
def mods(tmp_path):
    """Reloaded modules with a fresh DB, nothing faked (for unit tests of meta_api / llm)."""
    app = _reload(tmp_path, "llm_only")
    app.db.init_db()
    return app


def post(c, payload):
    """TestClient blocks until BackgroundTasks finish; real Meta doesn't wait. Fire in a thread."""
    t = threading.Thread(target=c.post, args=("/webhook",), kwargs={"json": payload})
    t.start()
    time.sleep(0.05)
    return t


def msg(u, mid, t, **extra):
    m = {"mid": mid, **extra}
    if t is not None:
        m["text"] = t
    return {"object": "instagram", "entry": [{"id": "PAGE", "messaging": [
        {"sender": {"id": u}, "recipient": {"id": "PAGE"}, "message": m}]}]}


def echo(u, mid, t, attachments=None):
    m = {"mid": mid, "is_echo": True}
    if t is not None:
        m["text"] = t
    if attachments:
        m["attachments"] = attachments
    return {"object": "instagram", "entry": [{"id": "PAGE", "messaging": [
        {"sender": {"id": "PAGE"}, "recipient": {"id": u}, "message": m}]}]}


def to(sent, u):
    return [s[1] for s in sent if s[0] == u]


# ---------------------------------------------------------------- webhook basics
def test_verify(bot):
    c, *_ = bot
    assert c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "42"}).text == "42"
    assert c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "1"}).status_code == 403


def test_health_reports_telegram_and_token(bot):
    c, *_ = bot
    h = c.get("/health").json()
    assert h["ok"] and h["telegram"] is False and h["token_days_left"] is None


def test_debounce_and_dedup(bot):
    c, sent, calls, _ = bot
    post(c, msg("A", "a1", "Привет"))
    c.post("/webhook", json=msg("A", "a1", "Привет"))  # Meta retry
    post(c, msg("A", "a2", "сколько стоит"))
    time.sleep(3)
    assert calls == ["Привет\nсколько стоит"]
    assert len(to(sent, "A")) == 1


def test_bot_own_echo_does_not_pause(bot):
    c, sent, _, db = bot
    post(c, msg("A", "a1", "цена?"))
    time.sleep(3)
    post(c, echo("A", "other-mid", sent[0][1]))  # echo with unknown mid, same text
    time.sleep(3)
    assert not db.is_paused("A")


def test_mom_reply_pauses(bot):
    c, sent, _, db = bot
    post(c, msg("B", "b1", "здравствуйте"))
    time.sleep(3)
    # simulate mom answering later than the auto-reply window
    with db.session() as s:
        s.get(db.Client, "B").last_inbound_at = time.time() - 60
    post(c, echo("B", "m1", "Это мастер, сейчас отвечу"))
    time.sleep(3)
    assert db.is_paused("B")
    post(c, msg("B", "b2", "ок"))
    time.sleep(3)
    assert to(sent, "B") == ["BOT:здравствуйте"]


def test_template_wins(bot):
    c, sent, calls, db = bot
    post(c, msg("C", "c1", "прайс"))
    post(c, echo("C", "t1", "Наш прайс: ..."))  # IG auto-reply right away
    time.sleep(3)
    assert calls == [] and sent == []
    assert not db.is_paused("C")


def test_ignored_contact_gets_no_reply(bot, monkeypatch):
    c, sent, calls, _ = bot
    import app.config
    monkeypatch.setattr(app.config, "IGNORE_IGSIDS", {"Z"})
    post(c, msg("Z", "z1", "как дела?"))
    time.sleep(3)
    assert calls == [] and sent == []


# ---------------------------------------------------------------- client message labels
def test_shared_post_caption_reaches_llm(bot):
    c, sent, calls, _ = bot
    post(c, msg("G", "g1", "сколько стоит?",
                attachments=[{"type": "ig_reel", "payload": {"title": "Афролоконы Ариэль 🔥 #афролоконы #зизи"}}]))
    time.sleep(3)
    assert len(calls) == 1
    assert "Подпись поста: «Афролоконы Ариэль 🔥»" in calls[0] and "сколько стоит?" in calls[0]
    assert "#" not in calls[0]


def test_shared_post_without_caption_fetches_it(bot, monkeypatch):
    c, sent, calls, _ = bot
    import app.meta_api

    async def fake_caption(mid):
        return "Бохо косы"
    monkeypatch.setattr(app.meta_api, "fetch_caption", fake_caption)
    post(c, msg("H", "h1", None, attachments=[{"type": "ig_post", "payload": {"ig_post_media_id": "123"}}]))
    time.sleep(3)
    assert calls == ["[Клиентка переслала пост студии. Подпись поста: «Бохо косы»]"]


def test_voice_photo_video_are_labelled(bot):
    c, sent, calls, _ = bot
    post(c, msg("V1", "v1", None, attachments=[{"type": "audio", "payload": {"url": "u"}}]))
    post(c, msg("V2", "v2", "вот такие хочу", attachments=[{"type": "image", "payload": {"url": "u"}}]))
    post(c, msg("V3", "v3", None, attachments=[{"type": "video", "payload": {"url": "u"}}]))
    time.sleep(3)
    assert sorted(calls) == sorted(["[голосовое сообщение]", "[фото]\nвот такие хочу", "[видео]"])


def test_story_reply_and_mention_are_labelled(bot):
    c, sent, calls, _ = bot
    post(c, msg("S1", "s1", "🔥", reply_to={"story": {"id": "1", "url": "u"}}))
    post(c, msg("S2", "s2", None, attachments=[{"type": "story_mention", "payload": {"url": "u"}}]))
    time.sleep(3)
    assert sorted(calls) == sorted(["[ответ на историю студии]\n🔥", "[клиентка отметила студию в своей истории]"])


def test_card_numbers_are_masked(bot):
    c, sent, calls, _ = bot
    post(c, msg("N", "n1", "перевела на 8600 0000 0000 0000, цена 1 300 000 1 600 000, тел +998 00 000 00 00"))
    time.sleep(3)
    assert "[номер карты]" in calls[0] and "9860" not in calls[0]
    assert "1 300 000 1 600 000" in calls[0] and "+998 00 000 00 00" in calls[0]


def test_webhook_log_hides_card_numbers(mods):
    p = {"entry": [{"messaging": [{"sender": {"id": "1000000000000001"},
                                   "message": {"mid": "m", "text": "карта 8600 0000 0000 0000"}}]}]}
    out = mods.main._masked(p)
    assert out["entry"][0]["messaging"][0]["message"]["text"] == "карта [номер карты]"
    assert out["entry"][0]["messaging"][0]["sender"]["id"] == "1000000000000001"


# ---------------------------------------------------------------- handoff to mom
def test_bot_handoff_marker_pauses_hides_and_notifies(bot, monkeypatch):
    c, sent, calls, db = bot
    import app.llm, app.meta_api
    notified = []

    async def fake_llm(history, new):
        return "Передам ваш вопрос мастеру, она подскажет 🙏 [Мастер]"

    async def fake_notify(cid, txt, reply="", kind="handoff"):
        notified.append((cid, kind))
    monkeypatch.setattr(app.llm, "generate_reply", fake_llm)
    monkeypatch.setattr(app.meta_api, "notify_master", fake_notify)
    post(c, msg("K", "k1", "а такое делаете?"))
    time.sleep(3)
    assert to(sent, "K") == ["Передам ваш вопрос мастеру, она подскажет 🙏"]
    assert db.is_paused("K") and notified == [("K", "handoff")]


def test_named_day_notifies_as_booking(bot, monkeypatch):
    c, sent, calls, db = bot
    import app.llm, app.meta_api
    notified = []

    async def fake_llm(history, new):
        return "Отлично! Передам мастеру — она сверит график 🙏 [МАСТЕР]"

    async def fake_notify(cid, txt, reply="", kind="handoff"):
        notified.append(kind)
    monkeypatch.setattr(app.llm, "generate_reply", fake_llm)
    monkeypatch.setattr(app.meta_api, "notify_master", fake_notify)
    post(c, msg("K2", "k2", "давайте в субботу утром"))
    time.sleep(3)
    assert notified == ["booking"]


def test_llm_failure_sends_fallback_in_client_language_and_hands_off(bot, monkeypatch):
    c, sent, calls, db = bot
    import app.llm, app.meta_api
    notified = []

    async def dead_llm(history, new):
        return None

    async def fake_notify(cid, txt, reply="", kind="handoff"):
        notified.append(kind)
    monkeypatch.setattr(app.llm, "generate_reply", dead_llm)
    monkeypatch.setattr(app.meta_api, "notify_master", fake_notify)
    post(c, msg("Q", "q1", "Ariel narxi qancha?"))
    time.sleep(3)
    assert to(sent, "Q") == ["Xabaringiz uchun rahmat! Usta tez orada o'zi javob beradi 🙏"]
    assert db.is_paused("Q") and notified == ["error"]


# ---------------------------------------------------------------- llm_only: who wrote the echo
def test_llm_only_fast_mom_reply_pauses(bot_llm):
    c, sent, calls, db = bot_llm
    post(c, msg("E", "e1", "сколько стоит"))
    post(c, echo("E", "q1", "Цена 1 300 000"))  # mom's quick reply within seconds
    time.sleep(3)
    assert db.is_paused("E")
    post(c, msg("E", "e2", "а где вы?"))
    time.sleep(3)
    assert to(sent, "E") == []


def test_llm_only_automation_echo_is_context_not_pause(bot_llm, monkeypatch):
    c, sent, calls, db = bot_llm
    import app.llm
    seen = []

    async def spy_llm(history, new):
        seen.append(history); return "Зизи — готовые тонкие косички."
    monkeypatch.setattr(app.llm, "generate_reply", spy_llm)
    post(c, echo("F", "x1", AUTOMATION_TEXT))  # comment -> DM automation, client never wrote
    time.sleep(2)
    assert not db.is_paused("F")
    post(c, msg("F", "f1", "Что такое зизи?"))
    time.sleep(3)
    assert to(sent, "F") == ["Зизи — готовые тонкие косички."]
    assert seen[0][0]["role"] == "auto" and "Спасибо за интерес" in seen[0][0]["content"]


def test_llm_only_ad_greeting_echo_never_pauses(bot_llm, monkeypatch):
    c, sent, calls, db = bot_llm
    import app.llm

    async def fake_llm(history, new):
        return "Ариэль — 1 300 000 сум."
    monkeypatch.setattr(app.llm, "generate_reply", fake_llm)
    post(c, msg("G", "g1", "Как записаться?"))  # ad button tapped
    post(c, echo("G", "ga", "Здравствуйте! Афролоконы Ариэль — от 1 300 000 сум, работа + материал. "
                            "Напишите удобный день — мастер сверит график и подберёт время 🙂"))
    time.sleep(3)
    assert not db.is_paused("G")
    assert to(sent, "G") == ["Ариэль — 1 300 000 сум."]


def test_llm_only_mom_message_without_recent_inbound_pauses(bot_llm):
    c, sent, calls, db = bot_llm
    post(c, echo("M", "m1", "Здравствуйте! вас все еще интересует услуга?☺️"))  # mom's own nudge
    time.sleep(2)
    assert db.is_paused("M")
    post(c, msg("M", "m2", "да, интересует"))
    time.sleep(3)
    assert to(sent, "M") == []


def test_llm_only_mom_shares_reel_long_pause(bot_llm):
    c, sent, calls, db = bot_llm
    post(c, echo("P", "p1", None, attachments=[{"type": "ig_reel", "payload": {"reel_video_id": "1"}}]))
    time.sleep(2)
    cl = db.get_client("P")
    assert cl["is_paused_until"] - time.time() > 70 * 3600
    post(c, msg("P", "p2", "Да зай видела этот рилс 👍"))
    time.sleep(3)
    assert to(sent, "P") == [] and calls == []


def test_mom_messages_reach_the_model_as_context(bot_llm, monkeypatch):
    c, sent, calls, db = bot_llm
    import app.llm, app.config
    seen = []

    async def spy_llm(history, new):
        seen.append(history); return "BOT"
    monkeypatch.setattr(app.llm, "generate_reply", spy_llm)
    monkeypatch.setattr(app.config, "HANDOFF_PAUSE_HOURS", 0.0003)  # ~1 s
    post(c, msg("R", "r1", "здравствуйте"))
    time.sleep(3)
    post(c, echo("R", "r2", "Для вашей длины будет 1 300 000"))
    time.sleep(3)  # pause is over
    post(c, msg("R", "r3", "а когда можно?"))
    time.sleep(3)
    roles = [h["role"] for h in seen[-1]]
    assert roles[-1] == "master" and "1 300 000" in seen[-1][-1]["content"]


# ---------------------------------------------------------------- follow-up
def test_followup_sent_once(bot):
    c, sent, _, _ = bot
    post(c, msg("D", "d1", "думаю"))
    time.sleep(10)
    replies = to(sent, "D")
    assert len(replies) == 2  # reply + one follow-up
    assert "получилось выбрать день" in replies[1] and "свободные окна" not in replies[1]


def test_followup_survives_postgres_float_rounding(bot):
    c, sent, _, db = bot
    post(c, msg("D2", "d1", "думаю"))
    time.sleep(3)  # bot replied
    with db.session() as s:  # Supabase returns last_inbound_at with ~15 significant digits
        cl = s.get(db.Client, "D2")
        cl.last_inbound_at = float(f"{cl.last_inbound_at:.15g}") + 0.000004
    time.sleep(7)
    assert len(to(sent, "D2")) == 2


def test_followup_in_uzbek(bot):
    c, sent, _, _ = bot
    post(c, msg("U", "u1", "Ariel narxi qancha?"))
    time.sleep(10)
    assert to(sent, "U")[1].startswith("Assalomu alaykum! Qaysi kun sizga qulay?")


def test_no_followup_when_client_waits_for_master(bot, monkeypatch):
    c, sent, calls, db = bot
    import app.llm, app.meta_api, app.config

    async def fake_llm(history, new):
        return "Спасибо за фото! Мастер посмотрит 🙏 [МАСТЕР]"

    async def fake_notify(*a, **k):
        pass
    monkeypatch.setattr(app.llm, "generate_reply", fake_llm)
    monkeypatch.setattr(app.meta_api, "notify_master", fake_notify)
    monkeypatch.setattr(app.config, "HANDOFF_PAUSE_HOURS", 0.0005)  # pause ends before the follow-up is due
    post(c, msg("W", "w1", None, attachments=[{"type": "image", "payload": {"url": "u"}}]))
    time.sleep(10)
    assert len(to(sent, "W")) == 1


def test_no_followup_after_mom_wrote(bot_llm, monkeypatch):
    c, sent, calls, db = bot_llm
    import app.config
    monkeypatch.setattr(app.config, "HANDOFF_PAUSE_HOURS", 0.0005)
    post(c, msg("X", "x1", "сколько стоит"))
    time.sleep(3)
    post(c, echo("X", "x2", "Жду вас в субботу в 10:00"))
    time.sleep(8)
    assert to(sent, "X") == ["BOT:сколько стоит"]


def test_no_followup_when_client_declined(bot):
    c, sent, _, _ = bot
    post(c, msg("Y", "y1", "Спасибо не надо"))
    time.sleep(10)
    assert len(to(sent, "Y")) == 1


# ---------------------------------------------------------------- concurrency
def test_message_during_generation_gives_one_reply(bot, monkeypatch):
    c, sent, calls, db = bot
    import app.llm

    async def slow_llm(history, new):
        calls.append(new); await asyncio.sleep(2); return f"BOT:{new}"
    monkeypatch.setattr(app.llm, "generate_reply", slow_llm)
    post(c, msg("L", "l1", "[пост Ариэль]"))
    time.sleep(2.5)                      # debounce passed, generation in progress
    post(c, msg("L", "l2", "сколько стоит?"))
    time.sleep(6)
    replies = [s for s in to(sent, "L") if s.startswith("BOT:")]
    assert len(replies) == 1 and "сколько стоит?" in replies[0] and "[пост Ариэль]" in replies[0]


def test_message_during_generation_gets_one_combined_reply(bot, monkeypatch):
    c, sent, calls, _ = bot
    import app.llm

    async def slow_llm(history, new):
        calls.append(new)
        await asyncio.sleep(3)  # Gemini overloaded -> slow
        return f"BOT:{new}"
    monkeypatch.setattr(app.llm, "generate_reply", slow_llm)
    post(c, msg("L", "l1", "얼마일까요?"))
    time.sleep(2.5)            # debounce passed, generation started
    post(c, msg("L", "l2", "가격"))
    time.sleep(10)
    replies = [s for s in to(sent, "L") if s.startswith("BOT:")]
    assert replies == ["BOT:얼마일까요?\n가격"]


# ---------------------------------------------------------------- units: language, llm, meta_api
def test_detect_lang(mods):
    d = mods.lang.detect_lang
    assert d("Сколько стоит?") == "ru"
    assert d("skolko stoit ariel") == "ru"
    assert d("Ariel narxi qancha?") == "uz_lat"
    assert d("Salom afralokon qiliw qancha") == "uz_lat"
    assert d("Нархи?") == "uz_cyr"
    assert d("Ариэль канча туради?") == "uz_cyr"
    assert d("how much are zizi braids?") == "en"
    assert d("[Клиентка пришла с рекламы. Текст рекламы: «Ариэль»]\nQancha") == "uz_lat"


def test_contents_merge_roles_and_mark_mom(mods):
    contents = mods.llm._to_contents(
        [{"role": "auto", "content": "Прайс"}, {"role": "user", "content": "а"}, {"role": "user", "content": "б"},
         {"role": "assistant", "content": "ответ"}, {"role": "master", "content": "Жду вас"}], "в")
    assert [x["role"] for x in contents] == ["user", "model", "user", "model", "user"]
    assert contents[1]["parts"][0]["text"] == "(Автоответ Instagram) Прайс"
    assert contents[2]["parts"][0]["text"] == "а\nб"
    assert contents[3]["parts"][0]["text"] == "ответ\n(Мастер написала лично) Жду вас"


def test_failed_model_is_skipped_for_a_while(mods, monkeypatch):
    llm = mods.llm
    from google.genai import errors
    called = []

    class Resp:
        text = "Ответ"

    async def fake_generate(model, contents, config):
        called.append(model)
        if model == "m1":
            raise errors.APIError(429, {"error": {"code": 429, "message": "quota"}})
        return Resp()

    monkeypatch.setattr(mods.config, "GEMINI_MODELS", ["m1", "m2"])
    monkeypatch.setattr(llm.client.aio.models, "generate_content", fake_generate)
    assert asyncio.run(llm.generate_reply([], "привет")) == "Ответ"
    assert asyncio.run(llm.generate_reply([], "привет")) == "Ответ"
    assert called == ["m1", "m2", "m2"]  # second time m1 is cooling down


def test_all_models_failing_returns_none(mods, monkeypatch):
    llm = mods.llm
    from google.genai import errors

    async def fake_generate(model, contents, config):
        raise errors.APIError(503, {"error": {"code": 503, "message": "overloaded"}})

    monkeypatch.setattr(mods.config, "GEMINI_MODELS", ["m1"])
    monkeypatch.setattr(llm.client.aio.models, "generate_content", fake_generate)
    assert asyncio.run(llm.generate_reply([], "привет")) is None


def test_automation_detection(mods):
    la = mods.meta_api.looks_like_automation
    assert la(AUTOMATION_TEXT)
    assert la("Assalomu alaykum!\n• Ariel — 1 300 000 so'm\n• Anna — 1 300 000 so'm\n• Zizi — 1 300 000 so'm")
    assert la("Hi! Thanks 💛 Ariel curls: 1,300,000 UZS")
    assert la("Ariel 60-80 sm — 1 300 000 so'm, 90 sm — 1 600 000 so'm, nuqtali — 1 800 000 so'm")
    assert not la("Здравствуйте! вас все еще интересует услуга?☺️")
    assert not la("Цена на данное плетение 1 млн 300тыс \nРабота + материал")
    assert not la("Для вашей длины 1 300 000 сум, могу в четверг")
    assert la("Здравствуйте! Чем мы можем вам помочь?")
    assert la("Здравствуйте!  Детские косички, работа + материал: до 6 лет — 700 000 сум")
    assert not la("Здравствуйте! Можем в субботу в 10:00")


def _mock_http(monkeypatch, meta_api, handler):
    monkeypatch.setattr(meta_api, "_http", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_token_refresh_is_stored_and_used(mods, monkeypatch):
    ma = mods.meta_api

    def handler(req):
        assert req.url.path == "/refresh_access_token" and req.url.params["access_token"] == "x"
        return httpx.Response(200, json={"access_token": "NEW", "token_type": "bearer", "expires_in": 5184000})
    _mock_http(monkeypatch, ma, handler)
    assert asyncio.run(ma.refresh_access_token()) == 60
    assert ma.access_token() == "NEW" and ma.token_days_left() in (59, 60)
    # env token replaced by hand later -> the stored one is ignored
    monkeypatch.setattr(mods.config, "IG_ACCESS_TOKEN", "HAND")
    ma._token_cache.update(value=None, at=0)
    assert ma.access_token() == "HAND"


def test_rejected_refreshed_token_falls_back_to_env(mods, monkeypatch):
    ma = mods.meta_api
    mods.db.set_kv("ig_token", "BAD")
    mods.db.set_kv("ig_token_env", ma._env_fingerprint())

    def handler(req):
        if req.headers["Authorization"] == "Bearer BAD":
            return httpx.Response(400, json={"error": {"code": 190, "message": "expired"}})
        return httpx.Response(200, json={"message_id": "mid1"})
    _mock_http(monkeypatch, ma, handler)
    assert asyncio.run(ma.send_text("C1", "hi")) == "mid1"
    assert mods.db.get_kv("ig_token") == ""


def test_notify_master_message(mods, monkeypatch):
    ma = mods.meta_api
    monkeypatch.setattr(mods.config, "TG_BOT_TOKEN", "T")
    monkeypatch.setattr(mods.config, "TG_CHAT_IDS", ["1", "2"])
    tg = []

    def handler(req):
        if req.url.host == "api.telegram.org":
            import json
            tg.append(json.loads(req.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"username": "client1"})
    _mock_http(monkeypatch, ma, handler)
    asyncio.run(ma.notify_master("C1", "давайте в субботу", "Передам мастеру 🙏", "booking"))
    assert [m["chat_id"] for m in tg] == ["1", "2"]
    text = tg[0]["text"]
    assert text.startswith("🟢 Клиентка хочет записаться") and "@client1" in text and "ig.me/m/client1" in text
    assert "давайте в субботу" in text and "Передам мастеру" in text


def test_httpx_request_logging_is_off(mods):
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_bot_handoff_pause_is_short(bot, monkeypatch):
    c, sent, calls, db = bot
    import app.llm, app.meta_api

    async def fake_llm(history, new):
        return "Передам мастеру 🙏 [МАСТЕР]"

    async def fake_notify(*a, **k):
        pass
    monkeypatch.setattr(app.llm, "generate_reply", fake_llm)
    monkeypatch.setattr(app.meta_api, "notify_master", fake_notify)
    post(c, msg("SH", "sh1", "скидка будет?"))
    time.sleep(3)
    left = db.get_client("SH")["is_paused_until"] - time.time()
    assert 1.9 * 3600 < left <= 2 * 3600
