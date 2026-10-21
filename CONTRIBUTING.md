# Contributing to Bira Flow

Thanks for taking the time. This is a Windows desktop app that people use every day to
work, so the bar is: **nothing gets slower, nothing gets lost**.

## Before you start

- **Open an issue** for anything bigger than a typo. It saves everybody the time of a
  pull request that goes in a direction the project does not want.
- **Search the issues** first: the same question has usually been asked before.

## Getting it running

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m biraflow --selftest
```

The test suite is fast (~40 s) and does not need the network, a microphone or a real
keyboard: it must stay green. If your change breaks it, the change is not ready.

## Rules that matter here

- **Never send keystrokes into the user's apps and never steal focus in tests.** UI tests use
  `SettingsWindow(app, background=True)` (on screen but transparent, click-through, at the
  bottom of the z-order, never activated) and render the pill on its hidden canvas.
- **Never type into the user's WhatsApp or other real apps.** For UI Automation experiments use
  an isolated page in an Edge `--app` window with a temporary profile.
- **Never log API keys (`.env`), the dictated text beyond what the app already logs (char
  counts), or the clipboard.**
- Every module must import: `tests/test_imports.py` guards against syntax errors in files that
  rarely run.
- Worker-thread exceptions fail the tests: the fakes in the tests must implement every method
  the controller calls (see `FakeRecorder` in `tests/test_controller.py`).
- **Settings**: a new option needs the dataclass field in `config.py`, a commented line in
  `DEFAULT_SETTINGS_TOML` (the file is rendered from that template), the settings-window control
  and `collect()`, and a test.

## Translations

The interface text lives in the code, in Portuguese; the catalogs in `biraflow/lang/` map each
source string to the other languages. If you speak a language that is not there yet, see
[docs/TRANSLATION.md](docs/TRANSLATION.md) — it is a JSON file, no tooling needed.

Two kinds of text are translated:

- the **interface** (`strings`), shown on screen;
- the **words the user speaks** (`spoken`): "comma", "press enter", "cancel dictation", the
  emoji names. These are what the app listens for, so they must be the words a person would
  really say.

## Commit messages

One line, in the imperative, in English:

```
fix: keep the sign-off out of the e-mail layout
feat: add the language picker
docs: explain how the catalogs work
```

## Pull requests

- Keep the change focused. One topic per pull request.
- Explain **why** in the description, not only what changed.
- If it changes the interface, add a line to `README.md` (and to `README.pt-BR.md`).
- If it changes the behaviour of the settings file, bump the migration in `config.py` when the
  old default has to change.

## Code of conduct

By participating you agree with the [Code of Conduct](CODE_OF_CONDUCT.md).
