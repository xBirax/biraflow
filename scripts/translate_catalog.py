"""Translate the interface into other languages with an LLM and write the catalogs.

The source strings are the ones in the code (Portuguese). The script collects them,
asks the model to translate each one, and writes biraflow/lang/<code>.json:

    {"code": "en", "strings": {...}, "prompts": {...}, "spoken": {...}}

Any string the model refuses or mangles is left out of the catalog: the app then shows
the original text instead of a broken translation. Placeholders like {0} must survive
the translation; a translation that drops them is rejected.

    python scripts/translate_catalog.py --lang en --lang es --base-url https://... --model ...
    python scripts/translate_catalog.py --lang en --check      # what is missing

The model can be any OpenAI-compatible endpoint (Groq, OpenAI, Ollama, Vercel AI
Gateway, 9Router...). The key comes from --key-env (default: OPENAI_API_KEY).
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from biraflow import i18n, textproc  # noqa: E402
from biraflow.cleanup import (COMMAND_PROMPT, MEETING_MERGE_PROMPT, MEETING_PROMPT,  # noqa: E402
                              STYLE_PROMPTS, SUMMARY_PROMPT, SYSTEM_PROMPT)

PACKAGE = ROOT / "biraflow"
PLACEHOLDER = re.compile(r"\{\d+\}")


# --- what has to be translated -------------------------------------------------------

def ui_strings() -> list[str]:
    """Every literal wrapped in _t() in the package."""
    found: dict[str, None] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in ("_t", "t"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.strip():
                    found.setdefault(arg.value, None)
    return list(found)


def templates() -> list[str]:
    """The f-strings turned into templates ("... {0} ...")."""
    found: dict[str, None] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in ("_t", "t"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.JoinedStr):
                    parts, index = [], 0
                    for part in arg.values:
                        if isinstance(part, ast.Constant):
                            parts.append(part.value.replace("{", "{{").replace("}", "}}"))
                        elif isinstance(part, ast.FormattedValue):
                            parts.append("{" + str(index) + "}")
                            index += 1
                    text = "".join(parts)
                    if re.search(r"[A-Za-zÃ€-Ã¿]", text):
                        found.setdefault(text, None)
    return list(found)


def option_labels() -> list[str]:
    """The labels of the drop-downs and the radio buttons.

    They live in module constants (not inside a `t()` call), so the catalog needs them
    separately. The names of the languages are left out: they are how each language calls
    itself, in every list.
    """
    from biraflow import settings_ui

    found: dict[str, None] = {}
    for _code, label in settings_ui.PILL_POSITIONS:
        found.setdefault(label, None)
    for _code, label in settings_ui.PILL_SIZES:
        found.setdefault(label, None)
    for _code, label in settings_ui.PROVIDER_CHOICES:
        found.setdefault(label, None)
    for label in settings_ui.STYLE_LABELS.values():
        found.setdefault(label, None)
    for _key, name, apps in settings_ui.STYLE_CATEGORIES:
        found.setdefault(name, None)
        found.setdefault(apps, None)
    for _code, label in settings_ui.KEEP_CHOICES:
        found.setdefault(label, None)
    for _key, name, _where in settings_ui.KEY_LABELS:
        found.setdefault(name, None)
    for _key, label, _glyph in settings_ui.NAV:
        found.setdefault(label, None)
    for label in settings_ui.NAV_SECTIONS.values():
        found.setdefault(label, None)
    for _code, label in settings_ui.MODELS:
        found.setdefault(label, None)
    for _code, name, desc in settings_ui.ENGINES:
        found.setdefault(name, None)
        found.setdefault(desc, None)
    for code, label in settings_ui.LANGUAGES:
        if code == "auto":          # the other ones are the names of the languages
            found.setdefault(label, None)
    for code, label in settings_ui.LANGUAGE_OPTIONS:
        if code == "auto":
            found.setdefault(label, None)
    return list(found)


def prompts() -> dict[str, str]:
    out = {"system": SYSTEM_PROMPT, "command": COMMAND_PROMPT, "summary": SUMMARY_PROMPT,
           "meeting": MEETING_PROMPT, "meeting_merge": MEETING_MERGE_PROMPT}
    for style, text in STYLE_PROMPTS.items():
        out[f"style:{style}"] = text
    return out


# --- the model -----------------------------------------------------------------------

class Model:
    """A chat endpoint that speaks OpenAI's API (Groq, OpenAI, Ollama, gateways...)."""

    def __init__(self, base_url: str, model: str, key: str, timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.key = key
        self.timeout = timeout

    def ask(self, system: str, user: str) -> str:
        import requests

        payload = {"model": self.model, "temperature": 0, "stream": False,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        response = requests.post(f"{self.base_url}/chat/completions", json=payload, headers=headers,
                                 timeout=self.timeout)
        response.raise_for_status()
        text = response.text.strip()
        if text.startswith("data:") or text.startswith("event:"):
            return self._from_stream(text)
        data = response.json()
        return data["choices"][0]["message"]["content"]

    @staticmethod
    def _from_stream(text: str) -> str:
        """Some gateways answer in SSE even without stream=true."""
        parts: list[str] = []
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                data = json.loads(chunk)
            except ValueError:
                continue
            for choice in data.get("choices") or []:
                delta = choice.get("delta") or {}
                parts.append(delta.get("content") or "")
        return "".join(parts)


SYSTEM = """You translate the interface of a voice dictation app for Windows.
Rules:
- Translate the meaning, not word by word. The result must sound natural to a native speaker.
- Keep every placeholder exactly as it is ({0}, {1}, ...), in the same order.
- Keep the names of products, keys, file names, commands and formats as they are.
- Keep the punctuation style of the original (a question stays a question).
- Do not add anything: no explanations, no notes, no quotes around the text.
- If a line is a command, a path, a key name or a code snippet, keep it as it is.
Answer with a JSON object: the original text as the key, the translation as the value.
Only the JSON, nothing else."""


def translate_batch(model: Model, language: str, items: list[str]) -> dict[str, str]:
    """Translate a list of strings; returns the ones that came back usable."""
    payload = json.dumps(items, ensure_ascii=False, indent=0)
    user = f"Translate every string of this JSON list into {language}.\n{payload}"
    raw = model.ask(SYSTEM, user)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw).rstrip("`\n ")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("the model did not answer with a JSON object")
    data = json.loads(raw[start:end + 1])
    ok: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(value, str) or not value.strip():
            continue
        if key in items and PLACEHOLDER.findall(key) == PLACEHOLDER.findall(value):
            ok[key] = value
    return ok


def translate_list(model: Model, language: str, items: list[tuple[str, list[str]]]) -> dict[str, list[str]]:
    """Translate the spoken words: items are (key, words) pairs."""
    if not items:
        return {}
    payload = json.dumps([words for _key, words in items], ensure_ascii=False, indent=0)
    user = (f"Translate the words of this JSON list of lists into {language}. They are words the "
            f"user speaks to a voice dictation app: keep them short, in lower case, and give the "
            f"words that are actually spoken. Keep the same number of items in each list.\n{payload}")
    raw = model.ask(SYSTEM, user)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw).rstrip("`\n ")
    start, end = raw.find("["), raw.rfind("]")
    data = None
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start:end + 1])
        except ValueError:
            data = None
    if data is None:   # some models answer with an object
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end < 0:
            raise ValueError("the model did not answer with a JSON list")
        try:
            data = list(json.loads(raw[start:end + 1]).values())
        except ValueError as e:
            raise ValueError(f"the model did not answer with a JSON list ({e})") from e
    ok: dict[str, list[str]] = {}
    for (key, _words), value in zip(items, data):
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not value:
            continue
        ok[key] = [str(v) for v in value if str(v).strip()]
    return ok


# --- writing the catalog --------------------------------------------------------------

def write_catalog(code: str, strings: dict, prompts_: dict, spoken: dict) -> Path:
    path = i18n.LANG_DIR / f"{code}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"code": code, "name": i18n.LANGUAGES.get(code, (code, code))[0],
            "strings": strings, "prompts": prompts_, "spoken": spoken}
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lang", action="append", default=[], help="target language (en, es, fr...)")
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", ""), help="OpenAI-compatible endpoint")
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", ""), help="model name")
    ap.add_argument("--key-env", default="OPENAI_API_KEY", help="environment variable with the key")
    ap.add_argument("--key-file", default="", help="file with the key (a trailing newline is ignored)")
    ap.add_argument("--batch", type=int, default=25, help="strings per request")
    ap.add_argument("--check", action="store_true", help="only report what is missing")
    args = ap.parse_args()

    strings = ui_strings() + templates() + option_labels()
    spoken = textproc.spoken_base()
    prompt_map = prompts()

    if args.check:
        for code in args.lang or list(i18n.LANGUAGES):
            data = i18n._read(code)
            have = data.get("strings") or {}
            missing = [s for s in strings if s not in have]
            print(f"{code}: {len(have)}/{len(strings)} textos, {len(missing)} faltando, "
                  f"{len(data.get('prompts') or {})}/{len(prompt_map)} prompts, "
                  f"{len(data.get('spoken') or {})}/{len(spoken)} falas")
        return 0

    if not args.lang or not args.base_url or not args.model:
        ap.error("--lang, --base-url and --model are needed (or --check)")

    key = os.environ.get(args.key_env, "")
    if args.key_file:
        key = Path(args.key_file).read_text(encoding="utf-8-sig").strip()
    model = Model(args.base_url, args.model, key)

    for code in args.lang:
        name = i18n.LANGUAGES.get(code, (code, code))[0]
        print(f"== {code} ({name})")
        old = i18n._read(code)
        done_strings = dict(old.get("strings") or {})
        done_prompts = dict(old.get("prompts") or {})
        done_spoken = dict(old.get("spoken") or {})

        todo = [s for s in strings if s not in done_strings]
        for i in range(0, len(todo), args.batch):
            chunk = todo[i:i + args.batch]
            for attempt in range(2):
                try:
                    done_strings.update(translate_batch(model, name, chunk))
                    break
                except Exception as e:
                    print(f"  ! batch {i // args.batch + 1} (tentativa {attempt + 1}): {e}")
                    time.sleep(3)
        print(f"  textos: {len(done_strings)}/{len(strings)}")

        for key_name, text in prompt_map.items():
            if key_name in done_prompts:
                continue
            translated = None
            for attempt in range(2):
                try:
                    translated = translate_batch(model, name, [text]).get(text)
                    break
                except Exception as e:
                    print(f"  ! prompt {key_name} (tentativa {attempt + 1}): {e}")
                    time.sleep(3)
            if translated:
                done_prompts[key_name] = translated
        print(f"  prompts: {len(done_prompts)}/{len(prompt_map)}")

        todo_spoken = [k for k in spoken if k not in done_spoken]
        for i in range(0, len(todo_spoken), 10):
            chunk = todo_spoken[i:i + 10]
            for attempt in range(2):
                try:
                    done_spoken.update(translate_list(model, name, [(k, spoken[k]) for k in chunk]))
                    break
                except Exception as e:
                    print(f"  ! falas {i // 10 + 1} (tentativa {attempt + 1}): {e}")
                    time.sleep(3)
        print(f"  falas: {len(done_spoken)}/{len(spoken)}")

        path = write_catalog(code, done_strings, done_prompts, done_spoken)
        print(f"  -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

