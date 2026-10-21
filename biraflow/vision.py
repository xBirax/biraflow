"""Learn corrections from a screenshot.

You show where a dictated word came out wrong (you edited it, or wrote the right
spelling in a follow-up message like "é Taspio"), and a vision-capable LLM returns the
heard -> write pairs. Your recent dictations go along so the model knows which words the
speech recognizer wrote. Sent only when you ask for it, to the cleanup provider.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from dataclasses import dataclass

from . import http
from .cleanup import PROVIDERS, CleanupError, reasoning_params, resolve_provider
from .config import CleanupCfg

log = logging.getLogger(__name__)

# models of each provider that read images (Groq's qwen3.8: ~1 s for a screenshot)
VISION_MODELS = {"groq": "qwen/qwen3.8-27b", "openai": "gpt-4.1-mini", "anthropic": "claude-haiku-5-5",
                 "vercel": "google/gemini-2.5-flash-lite"}
MAX_SIDE = 1600       # px; larger screenshots are scaled down (text stays legible)
MAX_RULES = 6
MAX_WORDS = 4

PROMPT = """This screenshot comes from someone who writes by voice dictation. The speech recognizer misspelled a word or a name, and the screenshot shows how it should be written. Typical cases:
- a dictated message says "Oi Kate" and the contact, other messages or a follow-up show the name is "Kayth" -> heard "Kate", write "Kayth";
- a dictated message mentions "TASP" and the person then sends "é Taspio" -> heard "TASP", write "Taspio";
- the person edited the text, replacing "ese" with "esse" -> heard "ese", write "esse".
"heard" is always the WRONG form the recognizer produced; "write" is the RIGHT form the person wants from now on. Never the other way around.

Recent dictations (what the recognizer wrote, newest first):
<dictations>
{dictations}
</dictations>

Only spelling and name fixes: no rewrites, no changes of meaning, no grammar, no words that were already right. Copy "heard" exactly as the recognizer wrote it and "write" with its capitalization.
Answer with JSON only: {{"corrections": [{{"heard": "...", "write": "..."}}]}} (an empty list when there is nothing to fix)."""


@dataclass
class Found:
    heard: str
    write: str
    seen: bool  # "heard" occurs in your recent dictations


def _reasoning(model: str) -> dict:
    """Unlike the quick cleanup, reading a screenshot benefits from a moment of thought:
    without it the model swapped wrong and right ("Taspio -> TASP")."""
    if "qwen3" in model.lower():
        return {"reasoning_effort": "default", "reasoning_format": "hidden"}
    return reasoning_params(model)


def _png_b64(image) -> str:
    img = image.convert("RGB")
    scale = MAX_SIDE / max(img.size)
    if scale < 1:
        img = img.resize((round(img.width * scale), round(img.height * scale)))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def _parse(reply: str) -> list[tuple[str, str]]:
    m = re.search(r"\{.*\}", reply, re.DOTALL)
    if not m:
        return []
    try:
        items = json.loads(m.group(0)).get("corrections", [])
    except (ValueError, AttributeError):
        return []
    pairs = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        heard, write = str(it.get("heard", "")).strip(" \"'“”."), str(it.get("write", "")).strip(" \"'“”.")
        if (heard and write and heard != write and len(heard.split()) <= MAX_WORDS
                and len(write.split()) <= MAX_WORDS and (heard.lower(), write.lower()) not in
                {(h.lower(), w.lower()) for h, w in pairs}):
            pairs.append((heard, write))
    return pairs[:MAX_RULES]


def _seen(word: str, texts: list[str]) -> bool:
    rx = re.compile(r"(?<!\w)" + re.escape(word) + r"(?!\w)", re.IGNORECASE)
    return any(rx.search(t) for t in texts)


def corrections_from_image(image, recent: list[str], env: dict[str, str], cfg: CleanupCfg,
                           timeout: float = 30, retry: bool = True) -> list[Found]:
    provider = resolve_provider(cfg, env)
    if provider not in VISION_MODELS:
        raise CleanupError("Para ler o print é preciso uma chave do Groq, OpenAI, Anthropic ou Vercel AI Gateway "
                           "(aba Chaves de API).")
    dictations = "\n".join(f"- {t.strip()[:300]}" for t in recent if t.strip())[:4000] or "(nenhum)"
    prompt = PROMPT.format(dictations=dictations)
    data = _png_b64(image)
    spec, model = PROVIDERS[provider], VISION_MODELS[provider]
    if spec["kind"] == "anthropic":
        r = http.post(spec["url"], timeout=(5, timeout), headers={
            "x-api-key": env[spec["key"]], "anthropic-version": "2023-06-01", "content-type": "application/json",
        }, json={"model": model, "max_tokens": 600, "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
            {"type": "text", "text": prompt}]}]})
        r.raise_for_status()
        reply = "".join(b.get("text", "") for b in r.json().get("content", []))
    else:
        r = http.post(spec["url"], timeout=(5, timeout), headers={"Authorization": f"Bearer {env[spec['key']]}"},
                      json={"model": model, "temperature": 0, **_reasoning(model), "messages": [
                          {"role": "user", "content": [
                              {"type": "text", "text": prompt},
                              {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}]}]})
        if r.status_code == 429 and retry:
            # images use a lot of the per-minute quota: wait what the provider asks, once
            import time

            try:
                wait = float(r.headers.get("retry-after", "") or 8)
            except ValueError:
                wait = 8.0
            if wait <= 20:
                time.sleep(wait)
                return corrections_from_image(image, recent, env, cfg, timeout, retry=False)
        if r.status_code == 429:
            raise CleanupError("Limite de uso do provedor atingido; tente de novo em alguns segundos.")
        r.raise_for_status()
        reply = r.json()["choices"][0]["message"]["content"] or ""
    found = [Found(h, w, _seen(h, recent)) for h, w in _parse(reply)]
    log.info("screenshot: %d correction(s) found via %s", len(found), provider)
    return found


def clipboard_image():
    """The image on the clipboard (Win+Shift+S, Print Screen, a copied image file), or None."""
    from PIL import Image, ImageGrab

    try:
        got = ImageGrab.grabclipboard()
    except Exception:
        return None
    if isinstance(got, Image.Image):
        return got
    if isinstance(got, list):  # copied files
        for path in got:
            try:
                return Image.open(path)
            except Exception:
                continue
    return None
