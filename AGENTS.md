# AGENTS.md — Bira Flow

Instructions for coding agents (Claude Code, Codex, etc.) working in this repository.
The user-facing documentation is [README.md](README.md); this file is about how to work here.

## What this is

Bira Flow is a system-wide voice dictation app for **Windows 11 64-bit** (a Wispr Flow
alternative): hold a hotkey, speak, release, and the cleaned-up text is pasted at the cursor
of any app. Python 3.12, Tkinter (+ sv_ttk) UI, pystray tray icon, Win32 through `ctypes`,
UI Automation through `comtypes`. Speech-to-text: Groq / OpenAI APIs or local whisper.cpp
(CLI or a GPU `whisper-server`). Cleanup: an LLM (Groq/OpenAI/Anthropic/Ollama) plus offline
rules. The interface strings are the **source language** (Portuguese, Brazil) and the
translations live in `biraflow/lang/`; code, comments and logs are in English.

## Layout

| Path | What |
|---|---|
| `biraflow/` | The app package (module table in README → Development) |
| `biraflow/assets/` | Logo, `app.ico` (regenerate with `branding.write_app_ico()`), tray artwork |
| `biraflow/lang/` | The translation catalogs, one JSON per language |
| `tests/` | pytest suite (fast, no network, no real keyboard input) |
| `packaging/` | PyInstaller spec, Inno Setup script, code-signing certificate (`signing/` is gitignored: PFX + password) |
| `scripts/` | Install, whisper.cpp setup, signing, installer build, release publishing, translations |
| `dist/` | Build output: `BiraFlow-Setup-<version>.exe`, `latest.json` (update manifest) |

Threads: the **keyboard hook** (`winhook.py`) → posts events to the **controller**
(`controller.py`, own thread, event queue) → each dictation is a **job thread**. Tk lives on
the **main thread**; every other thread talks to the UI through `app.UiBridge` (a queue pumped
every 30 ms). The tray menu runs on the pystray thread: never touch Tk from there directly.

## Commands

```bash
.venv/Scripts/python -m pytest -q             # full suite (~30 s); must stay green
.venv/Scripts/python -m biraflow --selftest   # imports every UI piece, opens the settings invisibly
.venv/Scripts/python -m biraflow --check      # paths, keys, hotkey, engine
.venv/Scripts/python -m compileall -q biraflow
```

Build a signed installer (PowerShell; takes a few minutes, run it in the background):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-installer.ps1 -Notes "..."
```

Before building: bump the version in **both** `biraflow/__init__.py` (`__version__`) and
`pyproject.toml`, run the tests and `--selftest`. After building: run
`dist\BiraFlow\biraflow-cli.exe --selftest` and check `dist\latest.json`. Publishing a GitHub
release (`scripts\publish-release.ps1`) is outward-facing: only with the user's explicit OK.

## Rules that matter here

- **Never send keystrokes into the user's apps** and never steal focus in tests. UI tests use
  `SettingsWindow(app, background=True)` (on-screen but transparent, click-through, bottom of the
  z-order, never activated) and render the pill on its hidden canvas. Screenshots of the settings
  window can be taken the same way with `PrintWindow` — see `tests/test_settings_ui.py`.
- **Never type into the user's WhatsApp or other real apps.** For UI Automation / edit-learning
  experiments, use an isolated page in an Edge `--app` window with a temporary profile and drive
  the page by script.
- Never print or log API keys (`.env`), dictated text beyond what the app already logs (char
  counts), or the clipboard.
- Every module must import: `tests/test_imports.py` guards against syntax errors in rarely-run
  files (a literal newline inside an f-string once shipped in a build).
- Worker-thread exceptions fail the tests (`filterwarnings` in `pyproject.toml`): fakes in tests
  must implement every method the controller calls (see `FakeRecorder` in `tests/test_controller.py`).
- Settings: a new option needs the dataclass field in `config.py`, a commented line in
  `DEFAULT_SETTINGS_TOML` (the file is rendered from that template), the settings-window control
  and `collect()`, and a test. Per-dictation fields that must never be saved (like
  `TranscriptionCfg.context`) stay out of the template.
- Patches with non-ASCII text or backslashes: write the patch script with the editor tool (raw
  strings) instead of shell heredocs — heredocs have mangled `\n` and accents before.

## Translations

- The text shown to the user is wrapped in `t()` (imported as `_t` where a local variable
  called `t` exists). `scripts/i18n_wrap.py` rewrites the calls automatically; it can be run
  again and skips what is already wrapped.
- `biraflow/lang/<code>.json` has three sections: `strings` (the interface), `prompts` (the
  LLM instructions) and `spoken` (the words the user says: "comma", "press enter", "cancel
  dictation", the emoji names).
- A string with no translation shows the source text: an incomplete catalog never breaks the
  app.
- The **spoken** words are the ones the app listens for. `scripts/fix_spoken.py` fixes the
  ambiguous ones by hand ("at" is too common in English, "at sign" is safer).
- `scripts/translate_catalog.py` translates a whole catalog with an LLM and never overwrites
  an existing translation.
- `tests/test_i18n.py` checks that every catalog is valid and that no translation loses a
  `{0}` placeholder.

## Gotchas

- **`--selftest` and `--check` use the real settings** (the data of the logged-in user) and run
  the one-time default migrations (`config._migrate`). While an older build is running,
  point `BIRAFLOW_HOME` to a temp folder, or that build reloads a value it does not know and warns.
- **Low-level keyboard hook**: Windows silently drops it if the callback is slow; `winhook.py`
  re-installs it every 2 minutes. Keep the callback trivial.
- **Chromium apps** (WhatsApp, Edge, Chrome, Brave, Teams…) expose page text to UI Automation only
  after an accessibility request (`editwatch.wake_chromium`, done when a recording starts).
- **comtypes** generates UI Automation wrappers at first use; the PyInstaller spec bundles
  `comtypes.gen` so the installed app does not regenerate them at every start.
- **Tk**: creating many `tk.Tk()` roots in one process can make Tcl fail to start — share one
  root per test module.
- **Icons**: Windows blurs icons it has to rescale; tray and window icons are rendered at the exact
  pixel size (`branding.tray_icon_sizes`, `apply_window_icon`).
- The code-signing certificate is self-signed by default; on a PC that does not trust it the
  signature shows as `UnknownError` (expected). `scripts\trust-signing-cert.ps1` is a security
  setting the user runs themselves. See `docs/SIGNING.md` for the SignPath path (free for
  open source).
