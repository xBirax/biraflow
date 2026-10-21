# Translations

The interface text is written in the code, in Portuguese (Brazil). Each language has a catalog
in `biraflow/lang/<code>.json` that maps those source strings to the translation. A string
with no translation shows the original, so an incomplete catalog never breaks the app.

```json
{
 "code": "en",
 "name": "English",
 "strings": { "Ditado ativado": "Dictation enabled" },
 "prompts": { "style:email": "This text goes into an email..." },
 "spoken": { "comma": ["comma"], "send": ["press enter", "hit enter"] }
}
```

## The three sections

| Section | What it is |
|---|---|
| `strings` | The text on screen: buttons, labels, help, messages |
| `prompts` | The instructions sent to the LLM (cleanup, command mode, meeting notes) |
| `spoken` | The **words the user speaks**: punctuation ("comma"), sending ("press enter"), cancelling ("cancel dictation"), the emoji names |

`spoken` is the one people forget: the app only understands what is in there (plus the
built-in Portuguese and English). If a language has no `spoken` section, the Portuguese words
are used and the dictation feels broken.

## Changing a language by hand

1. Copy an existing catalog: `copy biraflow\lang\en.json biraflow\lang\fr.json`
2. Change `"code"` and `"name"`.
3. Translate the values. Keep the `{0}`, `{1}` placeholders exactly as they are, in the same
   order — a translation that drops one is rejected by the tests.
4. Add the language to `LANGUAGES` in `biraflow/i18n.py` and to `LANGUAGE_OPTIONS` in
   `biraflow/settings_ui.py`.
5. Run the tests: `.venv\Scripts\python -m pytest -q tests\test_i18n.py`

## Translating with a model

The whole catalog can be translated by an LLM (any OpenAI-compatible endpoint):

```powershell
.venv\Scripts\python scripts\translate_catalog.py --lang fr `
    --base-url https://api.groq.com/openai/v1 --model llama-3.3-70b-versatile --key-env GROQ_API_KEY
```

The script:

- collects every string wrapped in `t()` in the code, the prompts and the spoken words;
- asks the model to translate them in batches, keeping the placeholders;
- writes `biraflow/lang/<code>.json`, skipping anything that came back broken (so the app
  shows the original text instead of a mangled translation);
- **never overwrites** a translation that already exists — to force a new one, delete the
  catalog first.

What is still missing:

```powershell
.venv\Scripts\python scripts\translate_catalog.py --check --lang fr
```

## Words that need a human

The model is good at the interface, but a few spoken words are ambiguous and have to be the
ones a person would really say. They are listed in `scripts/fix_spoken.py`:

- `at` — the word for "@" in an address ("arroba", "at sign", "chiocca"…). The English "at"
  alone is too common in speech, so "at sign" is safer.
- `dot` — the word for "." in an address ("ponto", "dot", "punto"…).
- `dot_final` — the word that ends the sentence ("ponto final", "full stop"…).
- `send` — how you ask to send ("pressione enter", "press enter", "pulsa enter"…).
- `cancel`, `undo` — how you throw the dictation away.
- `sign_off` — the words that end an e-mail ("att", "regards", "cordialement"…), removed from
  the layout because the mail app adds the signature.
- `emoji_of` — the connector in "emoji of heart"; empty in languages that do not need it.

After running the model, check these and adjust them:

```powershell
.venv\Scripts\python scripts\fix_spoken.py fr
```

## Adding the language to the app

`LANGUAGES` in `biraflow/i18n.py` is the list the app shows:

```python
LANGUAGES = {
    "pt-BR": ("Português (Brasil)", "Portuguese (Brazil)"),
    "en": ("English", "English"),
    ...
}
```

The first name is how the language calls itself (that is what the user sees in the list), the
second is the English name.
