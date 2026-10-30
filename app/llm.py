"""Free AI providers with automatic fallback: Gemini -> Groq -> OpenRouter -> Ollama.

If every provider fails (limits hit, no key, down), chat() returns None and the bot
falls back to a search-only answer.
"""
import logging
import time

import httpx

from . import config

log = logging.getLogger("llm")

# provider -> unix time until which we skip it (after a 429 rate-limit)
_cooldown = {}


async def _gemini(system, messages):
    if not config.GEMINI_API_KEY:
        raise RuntimeError("no GEMINI_API_KEY")
    url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % config.GEMINI_MODEL
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                     for m in messages],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048},
    }
    async with httpx.AsyncClient(timeout=config.LLM_TIMEOUT) as c:
        r = await c.post(url, json=body, headers={"x-goog-api-key": config.GEMINI_API_KEY})
        r.raise_for_status()
        data = r.json()
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


async def _openai_compatible(base, key, model, system, messages, extra_headers=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    headers.update(extra_headers or {})
    body = {"model": model, "temperature": 0.3, "max_tokens": 800,
            "messages": [{"role": "system", "content": system}] + messages}
    async with httpx.AsyncClient(timeout=config.LLM_TIMEOUT) as c:
        r = await c.post(base + "/chat/completions", json=body, headers=headers)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


async def _groq(system, messages):
    if not config.GROQ_API_KEY:
        raise RuntimeError("no GROQ_API_KEY")
    return await _openai_compatible("https://api.groq.com/openai/v1", config.GROQ_API_KEY,
                                    config.GROQ_MODEL, system, messages)


async def _openrouter(system, messages):
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("no OPENROUTER_API_KEY")
    return await _openai_compatible("https://openrouter.ai/api/v1", config.OPENROUTER_API_KEY,
                                    config.OPENROUTER_MODEL, system, messages,
                                    {"X-Title": "Woo Chatwoot Bot"})


async def _ollama(system, messages):
    return await _openai_compatible(config.OLLAMA_URL + "/v1", "", config.OLLAMA_MODEL, system, messages)


PROVIDERS = {"gemini": _gemini, "groq": _groq, "openrouter": _openrouter, "ollama": _ollama}


async def chat(system, messages):
    """Returns (text, provider_name) or (None, None)."""
    for name in config.LLM_PROVIDERS:
        fn = PROVIDERS.get(name)
        if not fn or _cooldown.get(name, 0) > time.time():
            continue
        try:
            text = (await fn(system, messages) or "").strip()
            if text:
                return text, name
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code == 429:
                _cooldown[name] = time.time() + 60  # rate limited: skip for a minute
            log.warning("%s failed: HTTP %s %s", name, code, e.response.text[:200])
        except RuntimeError as e:  # provider not configured
            log.debug("%s skipped: %s", name, e)
        except Exception as e:
            log.warning("%s failed: %s", name, e)
    return None, None
