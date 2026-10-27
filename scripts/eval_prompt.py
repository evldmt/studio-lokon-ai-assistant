"""Live check of answer quality against real Gemini.
Run:  python scripts/eval_prompt.py      (needs GEMINI_API_KEY in .env, ~30 requests)"""
import asyncio, re, sys
sys.path.insert(0, ".")
from app import llm  # noqa

MARKER = re.compile(r"\[\s*(?:МАСТЕР|MASTER|USTA|УСТА)\s*\]", re.I)
AD_BOHO = "[Клиентка пришла с рекламы. Текст рекламы: «Бохо косы🔥 ✅Время работы 3-4 часа»]\n"
AD_ARIEL = "[Клиентка пришла с рекламы. Текст рекламы: «Афролоконы Ариэль 🔥»]\n"

# q: client message; any: one of these must appear; all: every one must appear; bad: must not appear;
# marker: True = must hand over to mom ([МАСТЕР]), False = must not, None = don't care
CASES = [
    dict(q="сколько стоит?", any=["1 300 000"]),
    dict(q="сколько стоит ариэль 90 см", any=["1 600 000"]),
    dict(q="цена на анна точечно", any=["1 800 000"]),
    dict(q="а бокс косы сколько?", any=["фото"], bad=["000"]),
    dict(q="где вы находитесь? адрес скиньте", any=["Сергели"], bad=["дом 8", "8 дом", "на дому"]),
    dict(q="запишите меня на завтра в 10:00", any=["свер", "подтверд"], bad=["записала", "вы записаны", "жду вас"],
         marker=True),
    dict(q="давайте в субботу утром", any=["свер", "подтверд"], marker=True),
    dict(q="скиньте номер карты для предоплаты", any=["мастер"], bad=["9860", "8600"], marker=True),
    dict(q="а предоплата нужна?", all=["200 000"], marker=False),
    dict(q="skolko derjitsya ariel?", any=["1", "месяц"]),
    dict(q="ребенку 7 лет косички сколько", any=["800 000"]),
    dict(q="а скидка есть?", bad=["%", "скидка 1", "скидку в"], marker=True),
    dict(q="Ariel qancha turadi?", any=["1 300 000"]),
    dict(q="Нархи?", any=["сўм", "сум"], bad=["Здравствуйте", "Какая"]),
    dict(q="Ариэль канча туради?", any=["1 300 000"], bad=["Здравствуйте", "Какая"]),
    dict(q="каерда жойлашгансиз?", any=["Сергели"], bad=["Здравствуйте", "дом 8"]),
    dict(q="how much are zizi braids?", any=["1 300 000"]),
    dict(q="[фото]\nвот такие хочу", any=["мастер"], marker=True),
    dict(q="[голосовое сообщение]", any=["голосов"], bad=["фото"], marker=True),
    dict(q="[видео]", any=["видео"], bad=["фото"], marker=True),
    dict(q="это натуральные волосы? не химия?", any=["синтет"], bad=["канекалон"], marker=False),
    dict(q="можно сделать не очень густо на каркасе?", any=["густ", "объём", "закрыть"], marker=False),
    dict(q=AD_BOHO + "Как долго длится эффект?", all=["1,5", "1 500 000"], marker=False),
    dict(q=AD_ARIEL + "Как записаться на процедуру афролоконов?", any=["день"], all=["1 300 000"], marker=False),
    dict(q=AD_ARIEL + "Можно ли сделать другим цветом?", any=["цвет"]),
    dict(q="[ответ на историю студии]\n🔥", any=["спасибо"], bad=["запис", "какой день"], marker=False),
    dict(q="а спать в них удобно?", any=["мастер"], bad=["привыка", "пару дней"]),
    dict(q="сколько easy braid", any=["45 000"]),
    dict(q="ты дура", bad=["дура"]),
]


async def main():
    ok = 0
    for case in CASES:
        q = case["q"]
        a = await llm.generate_reply([], q)
        if a is None:
            print(f"❌ {q}\n   → (все модели Gemini недоступны)\n")
            continue
        low = a.lower()
        need_any, need_all, bad, marker = case.get("any", []), case.get("all", []), case.get("bad", []), case.get("marker")
        has_marker = bool(MARKER.search(a))
        passed = ((not need_any or any(n.lower() in low for n in need_any))
                  and all(n.lower() in low for n in need_all)
                  and not any(b.lower() in low for b in bad)
                  and (marker is None or marker == has_marker))
        ok += passed
        print(f"{'✅' if passed else '❌'} {q}\n   → {a}\n")
    print(f"{ok}/{len(CASES)} passed")


asyncio.run(main())
