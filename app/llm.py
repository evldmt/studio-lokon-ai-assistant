import asyncio
import logging
import time
from pathlib import Path

from google import genai
from google.genai import errors, types

from . import config

log = logging.getLogger(__name__)
client = genai.Client(api_key=config.GEMINI_API_KEY)

# A model that failed is skipped for a while, so the next client doesn't wait for it again.
# 429 = free-tier quota, 503 = overloaded, timeout = model is slow right now.
COOLDOWN_SEC = {429: 30 * 60, 500: 5 * 60, 503: 5 * 60, 504: 5 * 60, 404: 24 * 3600,
                "timeout": 10 * 60, "other": 10 * 60}
_cooldown_until: dict[str, float] = {}

# How studio messages written outside the bot look to the model
ROLE_PREFIX = {"master": "(Мастер написала лично) ", "auto": "(Автоответ Instagram) "}


def load_system_prompt() -> str:
    p = Path(config.SYSTEM_PROMPT_PATH)
    return p.read_text(encoding="utf-8") if p.exists() else "Ты — вежливый администратор салона."


def _to_contents(history: list[dict], new_text: str) -> list[dict]:
    """user -> user; bot, mom and automation -> model. Neighbouring turns of one side are merged."""
    contents: list[dict] = []
    for m in history + [{"role": "user", "content": new_text}]:
        role = "user" if m["role"] == "user" else "model"
        text = ROLE_PREFIX.get(m["role"], "") + m["content"]
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"][0]["text"] += "\n" + text
        else:
            contents.append({"role": role, "parts": [{"text": text}]})
    if contents[0]["role"] == "model":
        contents.insert(0, {"role": "user", "parts": [{"text": "(начало переписки)"}]})
    return contents


def _config_for(model: str) -> types.GenerateContentConfig:
    cfg = types.GenerateContentConfig(
        system_instruction=load_system_prompt(),
        temperature=0.4,
        max_output_tokens=2048,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    if model.startswith("gemini-2.5-flash"):
        cfg.thinking_config = types.ThinkingConfig(thinking_budget=0)
    return cfg


def _cool(model: str, reason) -> None:
    _cooldown_until[model] = time.time() + COOLDOWN_SEC.get(reason, COOLDOWN_SEC["other"])


def models_in_order() -> list[str]:
    """Configured order, but models that recently failed go last."""
    now = time.time()
    ready = [m for m in config.GEMINI_MODELS if _cooldown_until.get(m, 0) <= now]
    return ready + [m for m in config.GEMINI_MODELS if m not in ready]


async def generate_reply(history: list[dict], new_text: str) -> str | None:
    """Reply text, or None if every model failed (the caller sends a fallback and hands over to mom).
    The chain is tried twice with a short pause; failed models get a cooldown."""
    contents = _to_contents(history, new_text)
    for round_no in range(2):
        for model in models_in_order():
            try:
                resp = await asyncio.wait_for(
                    client.aio.models.generate_content(model=model, contents=contents, config=_config_for(model)),
                    timeout=config.GEMINI_TIMEOUT_SEC,
                )
                text = (resp.text or "").strip()
                if text:
                    _cooldown_until.pop(model, None)
                    if model != config.GEMINI_MODELS[0]:
                        log.info("Answered by fallback model %s", model)
                    return text
                log.warning("Empty answer from %s", model)
            except errors.APIError as e:
                log.warning("Gemini %s on %s -> next model", e.code, model)
                _cool(model, e.code if e.code in COOLDOWN_SEC else "other")
            except asyncio.TimeoutError:
                log.warning("Gemini timeout on %s -> next model", model)
                _cool(model, "timeout")
            except Exception:
                log.exception("Gemini error on %s", model)
                _cool(model, "other")
        if round_no == 0:
            await asyncio.sleep(2)
    return None
