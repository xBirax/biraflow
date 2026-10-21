"""Deterministic text processing around the LLM pass (pure, unit-tested).

Order for a dictation:
    raw -> local rules (spoken punctuation, fillers, stutters)
        -> send command ("pressione enter") detected and stripped
        -> snippet triggers swapped for tokens the LLM must keep
        -> [LLM cleanup]
        -> tokens expanded, replacement rules applied, style tweaks
"""
from __future__ import annotations

import datetime as _dt
import difflib
import os
import re
import unicodedata
from dataclasses import dataclass, field

from . import i18n

TOKEN = "⟦S{}⟧"
_TOKEN_RE = re.compile(r"⟦S(\d+)⟧")

# --- local rules (work offline, before any LLM) ----------------------------------

# Spoken punctuation. "dois pontos" can be a score and "period"/"colon"/"comma" are
# ordinary English words, so they are left to the LLM. Phrases that also occur in normal
# speech ("a nova linha de produtos", "o ponto final da história") only count at a pause
# (a comma or period Whisper wrote) or at the very end.
#
# The words are the ones of the language of the interface: the catalog
# (biraflow/lang/<code>.json, section "spoken") has them for each language. Without a
# translation the built-in ones are used (Portuguese and English).
_SPOKEN_BASE: dict[str, tuple[list[str], str, bool]] = {
    "new_paragraph": (["novo parágrafo", "new paragraph"], "\n\n", False),
    "new_line": (["nova linha", "próxima linha", "new line", "next line"], "\n", True),
    "question": (["ponto de interrogação", "question mark"], "?", False),
    "exclamation": (["ponto de exclamação", "exclamation mark"], "!", False),
    "semicolon": (["ponto e vírgula", "semicolon"], ";", False),
    "period": (["ponto final", "full stop"], ".", True),
    "comma": (["vírgula"], ",", False),
    "open_paren": (["abre parênteses", "abrir parênteses", "open parenthesis"], "(", False),
    "close_paren": (["fecha parênteses", "fechar parênteses", "close parenthesis"], ")", False),
    "open_quote": (["abre aspas", "open quote"], "“", False),
    "close_quote": (["fecha aspas", "close quote"], "”", False),
    "open_bracket": (["abre colchetes", "abrir colchetes", "open bracket"], "[", False),
    "close_bracket": (["fecha colchetes", "fechar colchetes", "close bracket"], "]", False),
    "ellipsis": (["reticências", "ellipsis"], "...", False),
    "em_dash": (["travessão", "em dash"], "—", False),
}


def _spoken_re(phrase: str, ambiguous: bool, rep: str = "") -> re.Pattern:
    if not ambiguous:
        # an opening mark keeps the comma before it ("disse, “oi"), a closing one the period after it
        lead = r"\s*" if rep in ("(", "“", "[") else r"[\s,]*"
        tail = "" if rep in (")", "”", "]") else "[.,]?"
        return re.compile(rf"{lead}\b(?:{phrase})\b{tail}", re.IGNORECASE)
    return re.compile(rf"(?:(?<=[,.;:!?])\s*\b(?:{phrase})\b[.,;:!?]?"
                      rf"|[\s,]*\b(?:{phrase})\b(?:[.,;:!?]|(?=[ \t]*(?:\n|$))))", re.IGNORECASE)


_SPOKEN_CACHE: dict[str, list] = {}


def _spoken_res() -> list:
    """The spoken punctuation of the current language (compiled once per language)."""
    code = i18n.language()
    if code not in _SPOKEN_CACHE:
        rows = []
        for key, (defaults, rep, amb) in _SPOKEN_BASE.items():
            phrases = i18n.spoken_list(key, defaults)
            pattern = "|".join(re.escape(p) for p in phrases)
            rows.append((_spoken_re(pattern, amb, rep), rep, amb))
        _SPOKEN_CACHE[code] = rows
    return _SPOKEN_CACHE[code]


# "lançamos a nova linha.", "chegamos ao ponto final.": after one of these the phrase is a noun
_DETERMINERS = {"a", "o", "as", "os", "da", "do", "das", "dos", "na", "no", "nas", "nos", "ao", "aos", "à", "às",
                "uma", "um", "umas", "uns", "sua", "seu", "suas", "seus", "essa", "esse", "esta", "este",
                "nossa", "nosso", "minha", "meu", "pela", "pelo", "nessa", "nesse", "numa", "num", "dessa",
                "desse", "nova", "novo", "última", "último", "primeira", "primeiro", "the", "an", "this",
                "that", "our", "my", "your", "new"}


def _after_determiner(text: str, start: int) -> bool:
    m = re.search(r"(\w+)[^\w\n]*$", text[:start])
    determiners = set(i18n.spoken_list("determiners", sorted(_DETERMINERS)))
    return bool(m) and m.group(1).lower() in determiners

# Hesitations that are never real words. "um" is left alone (Portuguese article) and
# the verb "é" is only dropped in the unmistakable ", é," filler position.
_FILLERS = re.compile(r"(?<![\w-])(?:hã+|ha{2,}|hum+|hmm+|hm|ahn+|ãh+|uh+|uhm+|umm+|erm+|éé+)(?![\w-])([,.?!]?)",
                      re.IGNORECASE)


def _drop_filler(m: re.Match) -> str:
    # "fui ao mercado hum. Depois": the period ends the sentence, only the "hum" goes
    before = m.string[: m.start()].rstrip()
    mark = m.group(1)
    return mark if mark in (".", "?", "!") and before and before[-1] not in ".?!\n" else ""
_E_FILLER = re.compile(r"(^|[,.!?]\s*)é,\s*", re.IGNORECASE)
# Letters only: "1, 1, 2, 3, 5" is a list, not a stutter.
_STUTTER = re.compile(r"\b([^\W\d_]+)(?:[\s,]+\1\b)+", re.IGNORECASE)
_STUTTER_OK = {"que", "não", "no", "sim", "muito", "bem", "há", "very", "bye", "ha", "rá", "tchau",
               "that", "had", "is", "do", "so", "the", "nós", "se", "já", "logo"}


def spoken_punctuation(text: str) -> str:
    # Each mark brings its own spacing, so nothing below has to guess where spaces go
    # (guessing broke URLs, e-mails, paths and times: "github.com", "C:\", "10:30").
    comma = i18n.spoken_list("comma", ["vírgula"])[0]
    text = re.sub(rf"(\d)\s+{re.escape(comma)}\s+(?=\d)", r"\1,", text, flags=re.IGNORECASE)
    for rx, rep, ambiguous in _spoken_res():
        if rep in ("\n", "\n\n"):
            out = rep
        elif rep in ("(", "“", "["):
            out = " " + rep
        elif rep == "—":
            out = " — "
        else:
            out = rep + " "
        if ambiguous:
            text = rx.sub(lambda m, out=out, t=text: m.group(0) if _after_determiner(t, m.start()) else out, text)
        else:
            text = rx.sub(lambda m, out=out: out, text)
    text = re.sub(r"([(“\[])\s+", r"\1", text)
    text = re.sub(r"\s+([)”\]])", r"\1", text)
    return spoken_email(text)


# "joao arroba gmail ponto com" / "joao arroba gmail.com" -> "joao@gmail.com"
# A "ponto final" right after the address is the end of the sentence, not ".final".
_TLD = r"(?:com|br|net|org|io|dev|edu|gov|app|info|biz|me|co|ai|[a-z]{2})"
_EMAIL_CACHE: dict[str, re.Pattern] = {}


def _alt(words: list[str]) -> str:
    """The words as an alternation that can be dropped inside a bigger pattern."""
    return "(?:" + "|".join(re.escape(w) for w in words) + ")"


def _email_re() -> re.Pattern:
    """The pattern of a spoken e-mail, with the words of the current language."""
    code = i18n.language()
    if code in _EMAIL_CACHE:
        return _EMAIL_CACHE[code]
    at = _alt(i18n.spoken_list("at", ["arroba", "at"]))
    dot = _alt(i18n.spoken_list("dot", ["ponto", "dot"]))
    final = _alt(i18n.spoken_list("dot_final", ["final", "full stop"]))
    pattern = (r"(?<![\w@.])(\w[\w.+-]*(?:\s+" + dot + r"\s+(?!" + final + r"\b)\w[\w+-]*)*)\s+" + at + r"\s+"
               r"([\w-]+(?:(?:\s+" + dot + r"\s+(?!" + final + r"\b)|\.(?=\w))[\w-]+)*(?:\s+" + dot + r"\s+|\.)" + _TLD + r")\b"
               r"(\s+(?:" + dot + r"\s+)?" + final + r"\b)?")
    rx = re.compile(pattern, re.IGNORECASE)
    _EMAIL_CACHE[code] = rx
    return rx


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def spoken_email(text: str) -> str:
    dot = _alt(i18n.spoken_list("dot", ["ponto", "dot"]))

    def build(m: re.Match) -> str:
        local = re.sub(rf"\s+{dot}\s+", ".", m.group(1), flags=re.IGNORECASE)
        domain = re.sub(rf"\s+{dot}\s+", ".", m.group(2), flags=re.IGNORECASE)
        return _ascii(f"{local}@{domain}").lower() + ("." if m.group(3) else "")

    return _email_re().sub(build, text)


def remove_fillers(text: str) -> str:
    text = _FILLERS.sub(_drop_filler, text)
    text = _E_FILLER.sub(lambda m: m.group(1), text)
    text = _STUTTER.sub(lambda m: m.group(0) if m.group(1).lower() in _STUTTER_OK or len(m.group(1)) < 2
                        else m.group(1), text)
    return text


def tidy(text: str) -> str:
    """Whitespace and capitalization after the edits above."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    # " ," -> "," only for a mark that ends a word (".NET" and " :)" keep their space)
    text = re.sub(r"[ \t]+([,.;:!?]+)(?=\s|$)", r"\1", text)
    text = re.sub(r",([.!?])(?=\s|$)", r"\1", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"^(?:[\s,;:]|\.(?=\s))+", "", text).strip()
    # capitalize the start and every sentence start
    # (not after "...", which trails off mid-sentence)
    text = re.sub(r"(^|(?<!\.\.)[.!?]\s+|\n)([a-zà-ÿ])", lambda m: m.group(1) + m.group(2).upper(), text)
    return text


def local_rules(text: str, spoken: bool = True, fillers: bool = True) -> str:
    if spoken:
        text = spoken_punctuation(text)
    if fillers:
        text = remove_fillers(text)
    return tidy(text)


# --- inserting into existing text --------------------------------------------------------

_NO_SPACE_AFTER = set("([{“\"'‘/@#\n\t")


def adapt_to_context(text: str, before: str | None, protected: set[str] | frozenset = frozenset(),
                     is_word=None) -> str:
    """Fit a dictation to the text right before the cursor (None: unknown, unchanged).

    Mid-sentence ("Vou levar o" + "Arquivo amanhã") the first letter goes lowercase,
    unless the word is a name: listed in `protected`, or `is_word(lower)` says the
    lowercase spelling is not a word ("joão"). A space is added when the dictation would
    otherwise stick to the previous word."""
    if not text or not before:
        return text
    last = before[-1]
    if (not last.isspace() and last not in _NO_SPACE_AFTER and not text[0].isspace()
            and text[0] not in ",.;:!?)]}”…"):
        text = " " + text
    prev = before.rstrip()
    if not prev or "\n" in before[len(prev):] or not (prev[-1].isalnum() or prev[-1] in ",;:"):
        return text  # start of the field, a new line or a new sentence: keep the capital
    m = re.match(r"(\s*)(\w+)", text)
    if not m:
        return text
    word = m.group(2)
    simple_capital = word[:1].isupper() and (word[1:].islower() or (len(word) == 1 and word != "I"))
    if not simple_capital or word in protected:
        return text
    if is_word is not None and len(word) > 1 and is_word(word.lower()) is False:
        return text  # a name
    return m.group(1) + word[:1].lower() + text[m.end(1) + 1:]


# Code points an editor deletes together with the character before them (emoji variation
# selectors, skin tones, joiners, combining accents): "❤️" is two code points, one Backspace.
_NO_BACKSPACE = re.compile(r"\u200d.|[\u0300-\u036f\u200d\ufe0e\ufe0f\U0001f3fb-\U0001f3ff\U000e0020-\U000e007f]")


def backspace_count(text: str) -> int:
    """Backspaces that remove `text` just typed (Windows line breaks count as one)."""
    return len(_NO_BACKSPACE.sub("", text.replace("\r\n", "\n")))


# --- "cancelar ditado" ----------------------------------------------------------------

# Without a translation (Portuguese) the words are flexible: "cancela ditado", "apaga o
# último ditado". Other languages come from the catalog, as whole phrases.
_CANCEL_RE = re.compile(r"(?:^\s*|[,.;!?]\s*)(?:cancel(?:ar|a|e)\s+(?:o\s+|esse\s+|este\s+)?ditado"
                        r"|cancel(?:\s+the)?\s+dictation)[\s.!?]*$", re.IGNORECASE)
_UNDO_RE = re.compile(r"^\s*(?:apag(?:ar|a|ue)|desfaz(?:er)?|desfaça)\s+(?:o\s+)?último\s+ditado[\s.!?]*$"
                      r"|^\s*(?:delete|undo)\s+(?:the\s+)?last\s+dictation[\s.!?]*$", re.IGNORECASE)


def _phrase_pattern(words: list[str]) -> str:
    """A pattern that matches any of the phrases, word by word ("cancelar ditado")."""
    parts = []
    for phrase in words:
        pieces = [re.escape(w) for w in phrase.split()]
        parts.append(r"\s+".join(pieces))
    return "|".join(parts)


def _cancel_re() -> re.Pattern:
    words = i18n.spoken_list("cancel")
    if not words:
        return _CANCEL_RE
    return re.compile(rf"(?:^\s*|[,.;!?]\s*)(?:{_phrase_pattern(words)})[\s.!?]*$", re.IGNORECASE)


def _undo_re() -> re.Pattern:
    words = i18n.spoken_list("undo")
    if not words:
        return _UNDO_RE
    return re.compile(rf"^\s*(?:{_phrase_pattern(words)})[\s.!?]*$", re.IGNORECASE)


def wants_undo(text: str) -> bool:
    """The whole dictation is "apagar último ditado": remove the previous insertion."""
    return bool(_undo_re().match(text.strip()))


def wants_cancel(text: str) -> bool:
    """The dictation ends with "cancelar ditado": throw it away (when Esc is out of reach)."""
    return bool(_cancel_re().search(text.strip()))


# --- "press enter" ------------------------------------------------------------------

def split_send_command(text: str, phrases: list[str]) -> tuple[str, bool]:
    """Strip a trailing send phrase ("pressione enter"); report whether it was there."""
    for phrase in sorted((p for p in phrases if p.strip()), key=len, reverse=True):
        words = r"[\s,.-]+".join(re.escape(w) for w in phrase.split())
        m = re.search(rf"[\s,]*\b{words}\b[\s.!?]*$", text, re.IGNORECASE)
        if m:
            return text[: m.start()].rstrip(), True
    return text, False


# --- snippets -------------------------------------------------------------------------

@dataclass
class Snippet:
    trigger: str
    text: str


def _phrase_re(phrase: str, at_pause: bool = False) -> re.Pattern:
    """Whole-phrase, case-insensitive; tolerant of hyphens and spacing, so a trigger
    "meu email" also matches Whisper's "meu e-mail" (but never across a period: "use
    emoji. Certo?" is not "emoji certo"). `at_pause`: only before punctuation or the end."""
    words = []
    for w in re.split(r"[\s\-]+", phrase.strip()):
        if w:
            words.append("-?".join(re.escape(ch) for ch in w))
    tail = r"(?=\s*(?:[.,;:!?…]|$))" if at_pause else ""
    # never inside a snippet token: a rule "s1" must not rewrite ⟦S1⟧
    return re.compile(r"(?<![\w⟦])" + r"[\s\-]+".join(words) + r"(?![\w⟧])" + tail, re.IGNORECASE)


_WEEKDAYS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")
_MONTHS = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro",
           "outubro", "novembro", "dezembro")


def render_snippet(text: str, now: _dt.datetime | None = None) -> str:
    """Placeholders in snippet text: {data} 15/10/2026, {data_extenso} 15 de outubro de 2026,
    {hora} 14:30, {dia_semana} quinta-feira, {amanha} 16/10/2026, {area_de_transferencia} (the
    text you had copied). Other braces stay as they are."""
    if "{" not in text:
        return text
    now = now or _dt.datetime.now()
    tomorrow = now + _dt.timedelta(days=1)
    values = {
        "data": now.strftime("%d/%m/%Y"),
        "data_extenso": f"{now.day} de {_MONTHS[now.month - 1]} de {now.year}",
        "hora": now.strftime("%H:%M"),
        "dia_semana": _WEEKDAYS[now.weekday()],
        "amanha": tomorrow.strftime("%d/%m/%Y"),
    }
    if "{area_de_transferencia}" in text.lower():
        values["area_de_transferencia"] = clipboard_text() or ""
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1).lower(), m.group(0)), text)


def clipboard_text() -> str | None:
    """Text on the clipboard (snippets' {area_de_transferencia}); None when unavailable."""
    try:
        from . import inserter

        return inserter.get_text()
    except Exception:
        return None


# Say "emoji coração" (or "emoji de coração") to insert ❤️.
EMOJIS = {
    "coração": "❤️", "coração partido": "💔", "sorriso": "😊", "carinha feliz": "🙂", "risada": "😂",
    "rindo": "😂", "chorando de rir": "🤣", "joinha": "👍", "positivo": "👍", "negativo": "👎",
    "palmas": "👏", "fogo": "🔥", "triste": "😢", "chorando": "😭", "piscadinha": "😉", "beijo": "😘",
    "apaixonado": "😍", "festa": "🎉", "pensando": "🤔", "revirando os olhos": "🙄", "ok": "👌",
    "mãos juntas": "🙏", "rezando": "🙏", "obrigado": "🙏", "foguete": "🚀", "check": "✅",
    "certo": "✅", "errado": "❌", "estrela": "⭐", "abraço": "🤗", "óculos escuros": "😎",
    "bravo": "😠", "surpreso": "😮", "assustado": "😱", "músculo": "💪", "força": "💪",
    "cem": "💯", "olhos": "👀", "aceno": "👋", "tchau": "👋", "café": "☕", "cerveja": "🍺",
    "flor": "🌹", "sol": "☀️", "dinheiro": "💰", "presente": "🎁", "bolo": "🎂",
}
EMOJI_SNIPPETS = [Snippet(f"emoji {de}{name}", e) for name, e in EMOJIS.items() for de in ("", "de ")]


def emoji_snippets() -> list[Snippet]:
    """The emoji triggers in the language of the interface ("emoji coração", "emoji heart")."""
    of = i18n.spoken("emoji_of", "de ")
    out: list[Snippet] = []
    for name, emoji in EMOJIS.items():
        label = i18n.spoken(f"emoji:{name}", name)
        out.append(Snippet(f"emoji {label}", emoji))
        if of:
            out.append(Snippet(f"emoji {of}{label}", emoji))
    return out


def spoken_base() -> dict[str, list[str]]:
    """Every word the user speaks, with the built-in (Portuguese) wording.

    The translation script reads it to know what each language needs.
    """
    base: dict[str, list[str]] = {key: list(words) for key, (words, _rep, _amb) in _SPOKEN_BASE.items()}
    base["at"] = ["arroba"]
    base["dot"] = ["ponto"]
    base["dot_final"] = ["final"]
    base["cancel"] = ["cancelar ditado"]
    base["undo"] = ["apagar último ditado"]
    base["sign_off"] = ["att", "atte", "atenciosamente"]
    base["send"] = ["pressione enter", "aperte enter", "press enter"]
    base["emoji_of"] = ["de "]
    for name in EMOJIS:
        base[f"emoji:{name}"] = [name]
    return base


def insert_snippet_tokens(text: str, snippets: list[Snippet],
                          now: _dt.datetime | None = None) -> tuple[str, dict[str, str]]:
    """Swap spoken triggers for ⟦S1⟧-style tokens the LLM is told to keep."""
    mapping: dict[str, str] = {}
    for sn in sorted(snippets, key=lambda s: len(s.trigger), reverse=True):
        if not sn.trigger.strip():
            continue
        # "emoji certo para ela" is a sentence; "... emoji certo." / "emoji certo" alone is the emoji
        rx = _phrase_re(sn.trigger, at_pause=sn.trigger.startswith("emoji "))
        if rx.search(text):
            token = TOKEN.format(len(mapping) + 1)
            mapping[token] = render_snippet(sn.text, now)
            text = rx.sub(token, text)
    return text, mapping


def is_only_tokens(text: str) -> bool:
    return bool(_TOKEN_RE.search(text)) and not re.sub(r"[\s.,;:!?]", "", _TOKEN_RE.sub("", text))


def expand_tokens(text: str, mapping: dict[str, str]) -> str | None:
    """Replace tokens with snippet text; None if the LLM lost or mangled a token."""
    if any(tok not in text for tok in mapping):
        return None
    for tok, value in mapping.items():
        text = text.replace(tok, value)
    return text


# --- replacement rules ("ese" -> "esse") ------------------------------------------------

@dataclass
class Replacement:
    heard: str
    write: str


def apply_replacements(text: str, rules: list[Replacement]) -> str:
    """Whole-word, case-insensitive match; the saved spelling is kept, except that a
    lowercase replacement at the start of a sentence gets a capital letter."""
    for rule in sorted(rules, key=lambda r: len(r.heard), reverse=True):
        if not rule.heard.strip():
            continue
        rx = _phrase_re(rule.heard)

        def sub(m: re.Match, write=rule.write) -> str:
            before = text_ref[0][: m.start()].rstrip(" \t“\"(")
            if write[:1].islower() and (not before or before[-1] in ".!?\n"):
                return write[:1].upper() + write[1:]
            return write

        text_ref = [text]
        text = rx.sub(sub, text)
    return text


# --- styles: local tweaks that also work without an LLM ---------------------------------

# Gmail/Outlook add the signature: a dictated "att, Bira" would sign twice. Only after a sentence
# end, a comma or a line break ("respondeu atenciosamente a todos" is not a sign-off).
_SIGN_OFF_WORDS = ["att", "atte", "atenciosamente", "regards", "best regards", "sincerely",
                   "kind regards", "cheers"]


def _sign_off_re() -> re.Pattern:
    words = i18n.spoken_list("sign_off", _SIGN_OFF_WORDS)
    return re.compile(rf"(?:^|(?<=[.!?\n])|,)[ \t]*\n*[ \t]*(?:{_phrase_pattern(words)})\b[.,]?"
                      r"(?:[ \t]*\n?[ \t]*[^\W\d_][\w'-]*){0,3}[ \t]*[.!]?\s*$", re.IGNORECASE)


def style_tweaks(text: str, style: str) -> str:
    if style == "email" and (m := _sign_off_re().search(text)):
        text = text[:m.start()].rstrip()
        if text[-1:].isalnum():   # "Obrigado, att Bira." -> "Obrigado."
            text += "."
    one_sentence = len(re.findall(r"[.!?](\s|$)", text)) <= 1 and "\n" not in text
    if style in ("very_casual", "technical") and one_sentence and text.endswith(".") and not text.endswith("..."):
        text = text[:-1]
    if style == "very_casual" and one_sentence and text[:1].isupper() and text[1:2].islower():
        text = text[:1].lower() + text[1:]
    return text


# --- learning from corrections -------------------------------------------------------------

_WORD = re.compile(r"[\w@'’]+(?:-[\w@'’]+)*", re.UNICODE)


def _words(text: str) -> list[tuple[str, bool]]:
    """(word, starts_a_sentence) pairs."""
    out = []
    for m in _WORD.finditer(text):
        before = text[: m.start()].rstrip(" \t“\"(")
        out.append((m.group(0), not before or before[-1] in ".!?\n"))
    return out


def _similar(a: str, b: str, min_similarity: float) -> bool:
    """Compare what changed, not what stayed: a long shared ending made "resende ou @empresa"
    -> "taspio@empresa" look like a spelling fix. Short remainders ("kate" -> "kayth") keep
    the whole words, or every one-letter fix would look like a rewrite."""
    p = len(os.path.commonprefix([a, b]))
    s = len(os.path.commonprefix([a[p:][::-1], b[p:][::-1]]))
    ra, rb = a[p:len(a) - s], b[p:len(b) - s]
    if min(len(ra), len(rb)) >= 4:
        a, b = ra, rb
    return difflib.SequenceMatcher(None, a, b).ratio() >= min_similarity


def learn_replacements(original: str, corrected: str, max_words: int = 3,
                       min_similarity: float = 0.5) -> list[Replacement]:
    """Word-level, case-sensitive diff: short substitutions become replacement rules
    ("ese" -> "esse", "cloud code" -> "Claude Code", "github" -> "GitHub")."""
    a, b = _words(original), _words(corrected)
    rules: dict[str, Replacement] = {}
    matcher = difflib.SequenceMatcher(a=[w for w, _ in a], b=[w for w, _ in b], autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op != "replace" or (i2 - i1) > max_words or (j2 - j1) > max_words:
            continue
        heard = [w for w, _ in a[i1:i2]]
        write = [w for w, _ in b[j1:j2]]
        # A capital that only marks the start of a sentence is not part of the rule.
        if a[i1][1] and heard[0][:1].isupper():
            heard[0] = heard[0][:1].lower() + heard[0][1:]
        if b[j1][1] and write[0][:1].isupper() and write[0][1:2].islower():
            write[0] = write[0][:1].lower() + write[0][1:]
        h, w = " ".join(heard), " ".join(write)
        # numbers are what was said: "3" -> "4" or "10h" -> "15h" is an edit ("mp3" -> "MP3" is not)
        if h == w or re.sub(r"\D", "", h) != re.sub(r"\D", "", w):
            continue
        # "ese" -> "esse" is a spelling fix; "não" -> "sim" is an edit of what was said
        # and must not become a global rule.
        if not _similar(h.lower(), w.lower(), min_similarity):
            continue
        rules[h.lower()] = Replacement(h, w)
    return list(rules.values())


# --- the whole thing ------------------------------------------------------------------------

@dataclass
class Prepared:
    text: str                       # what goes to the LLM (snippet tokens inside)
    tokens: dict[str, str] = field(default_factory=dict)
    send: bool = False              # press Enter after inserting
    snippet_only: bool = False      # the dictation was just a snippet trigger


def prepare(raw: str, *, local: bool, spoken: bool, send_phrases: list[str] | None,
            snippets: list[Snippet]) -> Prepared:
    text = local_rules(raw, spoken=spoken, fillers=local) if (local or spoken) else raw.strip()
    send = False
    if send_phrases:
        text, send = split_send_command(text, send_phrases)
        if send and (local or spoken):
            # "ok ponto final pressione enter": the ambiguous mark is only at the end now
            text = local_rules(text, spoken=spoken, fillers=local)
    if spoken:
        snippets = list(snippets) + emoji_snippets()
    text, tokens = insert_snippet_tokens(text, snippets)
    return Prepared(text=text, tokens=tokens, send=send, snippet_only=is_only_tokens(text))


def finish(cleaned: str, prepared: Prepared, replacements: list[Replacement], style: str = "neutral") -> str:
    if prepared.snippet_only:
        return expand_tokens(re.sub(r"[\s.,;:!?]+$", "", prepared.text.strip()), prepared.tokens) or ""
    # Rules run while snippets are still tokens, so saved snippet text is never rewritten.
    text = expand_tokens(apply_replacements(cleaned, replacements), prepared.tokens)
    if text is None:  # the LLM dropped a token: fall back to the locally cleaned text
        text = expand_tokens(apply_replacements(prepared.text, replacements), prepared.tokens) or prepared.text
    return style_tweaks(text.strip(), style)
