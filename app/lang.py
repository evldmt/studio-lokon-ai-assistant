"""Rough language detection for client messages and the few texts the bot sends without the LLM
(follow-up, fallback). Languages: ru, uz_lat (Uzbek, Latin), uz_cyr (Uzbek, Cyrillic), en."""
import re

_SYSTEM_LABELS = re.compile(r"\[[^\]]*\]")  # "[фото]", "[Клиентка пришла с рекламы…]" are ours, not the client's
_UZ_CYR_LETTERS = re.compile(r"[ўқғҳЎҚҒҲ]")
_UZ_CYR_WORDS = re.compile(
    r"\b(нарх\w*|канча|қанча|неча|нечпул|салом|ассалом\w*|қаерда|каерда|манзил\w*|керак\w*|борми|йўқ|"
    r"рахмат|раҳмат|ёзил\w*|қачон|качон|сочим\w*|ўриш|уриш|болади|бўлади|қилиб|килиб|сизга|менга|"
    r"яхши\w*|илтимос|учун)\b", re.I)
_CYRILLIC = re.compile(r"[а-яё]", re.I)
_UZ_LAT_WORDS = re.compile(
    r"\b(qancha|narx\w*|nech\w*|salom|assalom\w*|alaykum|qayerda|qaerda|manzil\w*|kerak\w*|kere|bormi|"
    r"yo'?q|rahmat|raxmat|yozil\w*|qachon|soch\w*|o'rim\w*|urim\w*|qizim\w*|uchun|bo'?ladi\w*|qilib|qilish|"
    r"qiliw|qilsak|sizga|sizni|menga|manga|iltimos|qaysi|yaxshi\w*|yahshi\w*|shunaqa|wunaqa|turadi|"
    r"uzunlig\w*|jingalak)\b", re.I)
_RU_TRANSLIT = re.compile(
    r"\b(skol\w*|stoi\w*|mozh\w*|mojn\w*|zdravst\w*|privet|spasib\w*|tsena|cena|senu|kak|gde|zapis\w*|"
    r"udobno|xodit|spat|skidk\w*|horosho|xorosho|ochen)\b", re.I)
_EN_WORDS = re.compile(
    r"\b(hi|hello|hey|how|much|price|prices|cost|thanks|thank|please|what|where|book|booking|appointment|"
    r"braids?|curls?|available|can|you|do|does)\b", re.I)


def detect_lang(text: str | None) -> str:
    t = _SYSTEM_LABELS.sub(" ", text or "")
    if _UZ_CYR_LETTERS.search(t) or _UZ_CYR_WORDS.search(t):
        return "uz_cyr"
    if _CYRILLIC.search(t):
        return "ru"
    if _UZ_LAT_WORDS.search(t):
        return "uz_lat"
    if len(_EN_WORDS.findall(t)) > len(_RU_TRANSLIT.findall(t)):
        return "en"
    return "ru"  # Russian in Latin letters ("skolko stoit") is answered in Russian too


FOLLOWUP = {
    "ru": "Здравствуйте! Подскажите, получилось выбрать день? Напишите удобную дату — мастер сверит график и "
          "подтвердит время 🙂",
    "uz_lat": "Assalomu alaykum! Qaysi kun sizga qulay? Kunni yozing — usta jadvalni tekshirib, vaqtni "
              "tasdiqlaydi 🙂",
    "uz_cyr": "Ассалому алайкум! Қайси кун сизга қулай? Кунни ёзинг — уста жадвални текшириб, вақтни "
              "тасдиқлайди 🙂",
    "en": "Hello! Have you chosen a day? Send a date that suits you — the master will check her schedule and "
          "confirm the time 🙂",
}

FALLBACK = {
    "ru": "Спасибо за сообщение! Мастер ответит вам лично в ближайшее время 🙏",
    "uz_lat": "Xabaringiz uchun rahmat! Usta tez orada o'zi javob beradi 🙏",
    "uz_cyr": "Хабарингиз учун раҳмат! Уста тез орада ўзи жавоб беради 🙏",
    "en": "Thank you for your message! The master will reply to you personally soon 🙏",
}

_REFUSAL = re.compile(
    r"не надо|не нужно|не интересно|не актуально|передумал|нет,? спасибо|kerak emas|kerakmas|"
    r"керак эмас|кераkмас|no thanks|not interested", re.I)


def followup_text(client_text: str | None) -> str:
    return FOLLOWUP[detect_lang(client_text)]


def fallback_text(client_text: str | None) -> str:
    return FALLBACK[detect_lang(client_text)]


def is_refusal(client_text: str | None) -> bool:
    return bool(_REFUSAL.search(client_text or ""))
