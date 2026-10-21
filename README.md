<p align="center"><img src="biraflow/assets/logo.png" alt="bira flow" width="420"></p>

# Bira Flow for Windows

**Voice dictation for every Windows app, in the style of Wispr Flow.** Hold a key, speak,
release: the text is transcribed, cleaned up and pasted wherever your cursor is.

> The Portuguese version of this README is [README.pt-BR.md](README.pt-BR.md).
> The app speaks **9 languages** (English, Portuguese, Spanish, French, German, Italian,
> Chinese, Japanese, Korean) — see [Languages](#languages).

- **Push to talk**: records while the shortcut is held; on release it transcribes and pastes.
- **Double tap = hands-free**: records without holding anything; another tap stops it. It stops
  by itself after 30 minutes of silence (up to 1 hour).
- **Meetings**: records your microphone and what the PC plays (Teams, Zoom, Meet…), with no bot
  in the call. The transcript arrives in pieces, and at the end you get the notes: summary,
  decisions and next steps.
- **Cloud transcription**: with a key in `.env`, the default is the Whisper API of **Groq** (or
  **OpenAI**); without a key, the **local model** (whisper.cpp, fully offline).
- **LLM cleanup**: removes speech fillers ("um", "uh", "né"), fixes punctuation and capitals,
  without translating or rewriting what you said.
- **Paste at the cursor**: clipboard + Ctrl+V (or simulated typing). The dictation stays in the
  clipboard so you can paste it again.
- **Floating pill** while recording, showing the mode (Dictating / Hands-free / Command), the
  time and the microphone level. It never steals the focus.
- **Dictionary** with corrections ("ese" → "esse", "cloud code" → "Claude Code") that **learns**
  when you fix a dictation in the history.
- **Snippets**: say "my email" and your address goes in; say "signature" and your signature does.
- **Styles per app**: casual on WhatsApp, technical in VS Code and, in Outlook/Gmail, **e-mail
  layout**: the greeting on its own line, short paragraphs with a blank line between them, the
  closing and the sign-off on their own lines — only what you said (the signature is the mail
  app's job).
- **Command mode** (**Ctrl+Win+Alt**): select a text and say "make it more formal" or "translate
  it to English"; with no selection, it writes what you ask for.
- **Paste last dictation** (**Shift+Alt+Z**), a searchable **history** with the raw text and
  statistics (words per minute, time saved).
- **"Press enter"** at the end of a dictation sends the message; spoken punctuation ("comma",
  "new line") and offline filler cleanup work even without an LLM.
- **Windows 11 settings window** (sidebar, Fluent icons, light/dark mode), with shortcut
  recording and a microphone test.
- **No accounts, no telemetry**. Without any key it works 100% offline with the local model.

## Languages

The interface, the spoken commands and the LLM prompts follow the language of the interface
(Settings → General → Interface language, or `language = "en"` in `settings.toml`).

| Language | Interface | Spoken commands | LLM prompts |
|---|---|---|---|
| Português (Brasil) | ✓ | ✓ | ✓ |
| English | ✓ | ✓ | ✓ |
| Español | ✓ | ✓ | ✓ |
| Français | ✓ | ✓ | ✓ |
| Deutsch | ✓ | ✓ | ✓ |
| Italiano | ✓ | ✓ | ✓ |
| 简体中文 | ✓ | ✓ | ✓ |
| 日本語 | ✓ | ✓ | ✓ |
| 한국어 | ✓ | ✓ | ✓ |

"Auto" follows the language of Windows. The transcription language is separate and can be
pinned (`language = "en"` in `[transcription]`) to make the local model faster.

Translations live in `biraflow/lang/<code>.json` — plain JSON, one file per language, easy to
review and to change. See [docs/TRANSLATION.md](docs/TRANSLATION.md).

## Installation

Requirements: Windows 10/11 64-bit and [Python 3.11+](https://www.python.org/downloads/)
(in the installer, mark "Add python.exe to PATH").

```powershell
# 1. Virtual environment, dependencies, keys file and Start Menu shortcut
powershell -ExecutionPolicy Bypass -File scripts\install.ps1

# 2. Local model (OPTIONAL, ~470 MB in %LOCALAPPDATA%\BiraFlow): without it the dictation
#    uses the Groq API (just the key in .env); with it, everything works offline.
powershell -ExecutionPolicy Bypass -File scripts\setup-whisper.ps1
#    more accurate (slower):        -Model medium
#    CPU only, still fast:          -Build blas
#    NVIDIA GPU:                    -Build cuda12 -Model large-v3-turbo

# 3. Check what was detected
.venv\Scripts\python -m biraflow --check
```

Start it from the Start Menu (or run `.venv\Scripts\biraflow.exe`). To see the log in a
terminal, use `.venv\Scripts\python -m biraflow`. To start with Windows, tick **Start with
Windows** in the tray menu.

**For a team, everything in the cloud** (no local model, on weak computers): see
[docs/EQUIPE-NUVEM.md](docs/EQUIPE-NUVEM.md) and `scripts\setup-team.ps1`, which installs
silently, writes the keys and sets `engine = "auto"` with the cleanup on Vercel AI Gateway.

## Windows installer (signed) and automatic updates

To use Bira Flow without Python, build the installer:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-installer.ps1 -Repo user/bira-flow
```

The script installs PyInstaller and Inno Setup (both free), creates the signing certificate the
first time, and produces `dist\BiraFlow-Setup-<version>.exe` (~24 MB) and `dist\latest.json`.
The installer does not ask for administrator rights: it installs in
`%LOCALAPPDATA%\Programs\Bira Flow`, creates the Start Menu shortcut and, if you tick it,
starts with Windows. Settings, keys and models stay in `%APPDATA%\BiraFlow` and
`%LOCALAPPDATA%\BiraFlow` and are kept when you uninstall. The installation includes
`biraflow-cli.exe --check` and `--selftest` for diagnostics.

**Signing.** The executables and the installer are signed with a code-signing certificate
(SHA-256, with a free timestamp). It guarantees the file was not changed and came from us.
For Windows to show the publisher as valid, each PC has to trust the certificate once
(Win+R):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\trust-signing-cert.ps1
```

On other PCs, without trusting the certificate, Windows SmartScreen warns "unknown publisher"
when the installer is downloaded from the internet. A trusted signature on every PC is free in
two cases: an open source project approved by the SignPath Foundation, or a publication on the
Microsoft Store (which signs the package). Otherwise, a code-signing certificate costs from
about US$ 100/year.

The private key stays in `packaging\signing\` (outside git). **Back it up**: automatic updates
only accept installers signed with that same certificate.

**Automatic updates.** The installed version checks a manifest (`latest.json`) in the GitHub
Releases when it opens and every 6 hours. When there is a new version, it downloads the
installer, checks the SHA-256 and the signature (it must be the certificate pinned in the
build; a tampered file is refused), waits until you stop dictating, installs silently and
reopens the app. To release a version: bump `__version__` in `biraflow\__init__.py`, run
`build-installer.ps1` and then:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\publish-release.ps1 -Notes "What changed"
```

You can turn it off, or check right now, in Settings → About → Updates.

## Usage

| Action | What happens |
|---|---|
| Hold the shortcut (default **Ctrl+Win**) | Records while held; on release, transcribes and pastes |
| Tap twice quickly | Hands-free: records without holding anything |
| Tap once (in hands-free) | Stops, transcribes and pastes |
| **Esc** while recording | Throws the recording away |
| **Esc** while it says "Transcribing…" | Cancels: nothing is pasted |
| A short tap alone | Ignored (nothing is recorded) |
| Shortcut + another key (e.g. Ctrl+Win+D) | Handled as a normal Windows shortcut; the recording is dropped |
| Hold **Ctrl+Win+Alt** with a text selected | Command mode: the spoken instruction rewrites the selection |
| **Shift+Alt+Z** | Pastes the last dictation again |
| End the dictation with "press enter" | Pastes and presses Enter (sends the message) |
| End the dictation with "cancel dictation" | Throws the dictation away (like Esc) |
| Dictate only "delete last dictation" | Deletes the text that was just pasted (if you have not typed since, in the same window, within 2 min) |
| Hands-free left on | Stops and transcribes by itself after 60 s of silence (Microphone → Limits) |
| Lock the PC (Win+L) while recording | The recording is dropped |

The pill shows your voice as a wave that rises and falls while you speak. In Settings → General
you can pick its position (top or bottom, left, centre or right), its size (small, medium,
large) and whether it shows the text right after pasting.

Pill colours: red = holding, purple = hands-free, turquoise = command, amber = transcribing.
In the last 30 seconds of hands-free it warns "stops in Ns". If the microphone gives absolute
silence, it warns "Microphone with no sound (muted?)"; if Windows is blocking the microphone
for apps (Privacy → Microphone), the message says how to unblock it.

**Microphone ready** (Settings → Microphone, off by default): keeps the microphone open for 30 s
after each dictation, so the next one starts right away and the first syllable is never cut.
During that time the Windows microphone icon stays on; nothing is recorded and nothing leaves
the machine outside a dictation.

## Voice activation ("Hey Bira")

Say the activation phrase and the dictation starts without touching the keyboard. Configure it
in **Settings → Voice activation**: turn it on, choose the phrase (default "Ei Bira", change it
to whatever you want), the silence that ends the dictation and whether the phrase is confirmed
by Whisper. The first time, click **Download voice model** (32 MB, once).

How it works:

1. **Always-on local detector.** An offline recogniser (Vosk, small Portuguese model) listens to
   the microphone and looks only for the phrase, using ~2–3% of a CPU core. The small model does
   not know names like "Bira", so the phrase is written with words it knows and that sound the
   same ("bi ra", "vira", "beira").
2. **Confirmed by Whisper.** When the detector hears something close, only that snippet
   (~2.5 s) is checked by Whisper with the phrase as a hint: "Ei Bira" passes, "ei, vira aqui"
   does not.
3. **The dictation.** On confirmation the pill appears in hands-free mode with the audio from
   the phrase on: nothing you said right after is lost, even without a pause. The phrase is
   removed from the text ("Ei Bira, send the file" becomes "Send the file"). The dictation ends
   after the configured silence (1.5 s), with a tap on the shortcut or with Esc (cancels).
   "Press enter" at the end still sends.

While the activation is on, the Windows microphone icon stays on. It pauses when dictation is
off (or paused for an hour from the tray) and does not react while you are already dictating.
Prefer two words ("Ei Bira", "Ok Bira"): a single word, or common words, fire by accident. The
page shows the state, the microphone level and what the detector heard last, live.

## Transcribing audio files (WhatsApp)

Open **tray → Transcribe audio…** and drop an audio file on the window: the text appears in
seconds and is already copied, ready to paste with Ctrl+V (turn "Copy when done" off if you
prefer).

- **From WhatsApp**: in the audio, ⋮ → **Download** (WhatsApp Web/Desktop saves an .ogg/.opus in
  Downloads); then drop the file on the window, or click **Last downloaded audio**, which picks
  the most recent audio in the Downloads folder.
- **From the Explorer**: right-click the audio → **Send to → Bira Flow (transcribe audio)**, drop
  the file on the Bira Flow shortcut, or copy the file and press Ctrl+V in the window. Several
  files at once go into a queue.
- **Formats**: .ogg/.opus (WhatsApp), .mp3, .m4a, .wav, .flac, .webm, .mp4.
- **Engine**: the same one used for dictation. With Groq the file goes up as it is (a 24 s
  audio takes ~1 s); long recordings are split into 10-minute parts, cut at a pause. Without
  internet, the local model transcribes it (.ogg/.opus, .mp3, .wav and .flac; .m4a only through
  the API).
- **Summary**: a summary in bullets (subject, dates, amounts, requests and an "Action:" when
  something is asked), made by the refinement provider. Useful for long audio files.
- Your Dictionary corrections apply here too ("Kate" → "Kayth"), and every transcribed file
  enters the History as "Audio (file)".

## Meetings

**Tray → Start meeting** records your microphone and what the PC plays (the other side of the
call), without putting any bot in the meeting. To finish, **tray → Stop meeting**. In a few
seconds a window opens with the notes and the transcript, ready to copy or save.

- **On the first meeting** Bira Flow asks you to confirm that you will let the participants
  know. Recording someone without consent can break the law and your company rules.
- **During the meeting**: the tray icon tip shows the recording time. Dictation keeps working
  (it has priority in the transcription), and "mute music" does not mute the PC.
- **Transcript in pieces**: every ~90 s a chunk is transcribed, so at the end only the last one
  is missing. Who is speaking is labelled by channel: **Me** (your microphone) and
  **Participants** (the PC audio). With a **headset** the separation is almost perfect; with
  speakers, your voice leaks into the other people's microphones and the label may be wrong
  more often.
- **Notes**: at the end, the refinement provider writes the title, the summary, the decisions
  (with the timestamp), the next steps ("who — what — when") and the open questions, without
  inventing names or dates. Without an LLM provider, only the transcript is kept. The
  **Generate notes** button in the window does it again.
- **Folder**: each meeting goes to `Documents\Bira Flow\Meetings\YYYY-MM-DD_HHMM`, with
  `transcript.md`, `notes.md` and the audio (`audio\part-NNN.ogg`, stereo Opus, ~21 MB/h).
  **Tray → Open meetings…** opens the folder.
- **Crash-proof**: if the app closes, the PC crashes or you quit in the middle, the next start
  finishes the transcription and the notes on its own. If the PC suspends, the pause is marked
  in the transcript. A chunk that failed (no internet, for example) is tried again; if it keeps
  failing, a **Try again** button appears in the meeting window.
- **Cost**: with Groq, about US$ 0.04 per meeting hour, and one hour uses half the free plan's
  audio-per-hour limit. The local model (GPU) is free and does not upload the audio. The notes
  are one or a few LLM calls.
- **Settings → Meetings**: record the PC audio or not, keep or delete the audio after
  transcribing, generate the notes when it stops and stop by itself after so many hours
  (default 4, from 1 to 8).

## Transcription

```toml
[transcription]
engine = "auto"         # default: API if there is a key (Groq, OpenAI or Vercel Gateway),
                        # otherwise local
                        # "local" | "groq" | "openai" | "gateway" pin an engine
gateway_model = "spacexai/grok-stt"   # "gateway" engine: the transcription model on the AI Gateway
language = "auto"       # or "pt", "en"…
local_model = "small"   # "small" | "medium" | "large-v3-turbo" | path to a .bin
vocabulary = ["Kubernetes", "Ubiratan"]   # names the recognition must get right
```

| Engine | Privacy | Measured time (9 s of speech) | Note |
|---|---|---|---|
| `local` **large-v3-turbo** + NVIDIA GPU | The audio never leaves the PC | **~0.30 s** (RTX 4050) | Best quality; the same model Groq uses |
| `local` small + NVIDIA GPU | The audio never leaves the PC | ~0.25 s (RTX 4050) | Misses more names and technical terms |
| `local` small, `language = "pt"` | The audio never leaves the PC | ~1.5 s (i7-13700H, CPU) | Same text as the original in the test |
| `local` small, `language = "auto"` | The audio never leaves the PC | ~3.4 s | Language detection costs ~2 s |
| `local` medium | The audio never leaves the PC | ~3× small | More accurate, ~1.5 GB |
| `groq` | The audio goes to Groq | **~0.44 s** measured from Brazil | `whisper-large-v3-turbo`, more stable than a laptop GPU |
| `openai` | The audio goes to OpenAI | ~1–2 s (estimated) | `whisper-1` |

**NVIDIA GPU:** in Settings → Transcription → Local installation, pick the "NVIDIA GPU (CUDA
12)" package (~700 MB) and click **Install whisper.cpp**. With "Keep the model loaded on the GPU"
on (default), the model stays in the GPU while the app is open (~500 MB of video memory) and
each dictation takes ~0.25 s. The first start after installing can take ~15 s (the driver
prepares the card once); until then, dictations use the processor.

**Speed tip:** pin the language (`language = "pt"`) if you always dictate in the same language.
With `local_fast = true` (default), whisper.cpp uses greedy decoding and an encoding window the
size of the speech instead of Whisper's fixed 30 s: ~2.5× faster, with the same text on
dictation recordings. Above 28 s, the full window is used again.

The times are estimates and depend on the hardware. With `fallback_to_local = true`, if the API
fails (no internet, for example), Bira Flow falls back to the local model.

**Tips for Whisper**: besides `vocabulary`, the spellings you taught the Dictionary ("GitHub",
"Kayth") go to the transcription as hints, so they are heard right and not only corrected
afterwards. The last sentence dictated in the same app (less than 2 minutes ago) also goes as
context. When the cloud transcribes and the GPU is only the backup, the local model leaves
video memory after 10 minutes without use (it comes back on its own when needed).

## LLM cleanup

The raw text goes through a short prompt that removes speech fillers, fixes punctuation and
capitals, turns "new paragraph" into a line break and never answers what was dictated.

```toml
[cleanup]
provider = "auto"    # first key in .env: Groq → OpenAI → Anthropic → Vercel; "ollama" = 100% local; "none" off
model = ""           # empty = the provider default (Groq: qwen/qwen3.8-27b, OpenAI: gpt-4.1-mini, Anthropic: claude-haiku-5-5, Vercel: google/gemini-2.5-flash-lite)
extra_instructions = "Use Brazilian Portuguese."
```

**Groq limit (429 error):** the free plan limits requests per minute *per model*. When the
default model hits the limit, Bira Flow switches at once to `openai/gpt-oss-20b` (and then
`gpt-oss-120b`), and lets the limited model rest for as long as Groq asks. Only when all of
them are limited does the text come out without refinement, with a warning on the pill. This is
for the default model; if you chose a model, it is respected.

If the LLM fails, takes longer than `timeout_seconds` or answers something that does not look
like the cleaned dictation, the raw text is pasted. You never lose what you said. Without any
key, cleanup is off and nothing leaves the computer.

The keys are typed in the **API keys** tab and stored in `%APPDATA%\BiraFlow\.env`:

```ini
GROQ_API_KEY=gsk_...
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
AI_GATEWAY_API_KEY=vck_...
```

**Vercel AI Gateway:** one key for models from several labs, at each one's list price (no Vercel
fee). The default is `google/gemini-2.5-flash-lite` (0.65 s per sentence, measured), which also
reads the screenshots. For nicer meeting notes, use `google/gemini-3.8-flash` or
`anthropic/claude-sonnet-5.5` in `model`. If your Vercel team uses a *Model Allowlist*, the
chosen model has to be allowed there; otherwise the app warns and pastes the text without
refinement. The Gateway is for the LLM only: transcription stays on Groq, OpenAI or the local
model.

## Insertion and clipboard

```toml
[output]
method = "paste"            # "paste" (Ctrl+V) or "type" (types character by character)
restore_clipboard = false   # true = gives back what was in the clipboard before
paste_keys = "ctrl+v"       # some terminals prefer "shift+insert"
trailing_space = true
smart_insert = true         # fits into the text that is already there (see below)
```

- **Fitting into existing text** (`smart_insert`): before pasting, Bira Flow reads the 3
  characters before the cursor. In the middle of a sentence ("I will bring the |") the
  dictation starts lowercase and gets the missing space; proper nouns (that the Windows
  spellchecker only accepts capitalised, or that are in your vocabulary/dictionary) stay
  capitalised. If the app does not report the text, nothing changes.
- **App running as administrator**: Windows blocks simulated keys in that case. Instead of
  "pasting into the void", Bira Flow warns and leaves the text in the clipboard (Ctrl+V).
- With `restore_clipboard = false` (default), the dictation stays in the clipboard so you can
  paste it again. **Copy last dictation** in the tray does the same.
- With `restore_clipboard = true`, everything that was there before (text, images, rich
  formats) comes back right after the paste, and the temporary dictation does not enter the
  Win+V history. If you copy something in the meantime, Bira Flow does not overwrite it.
- The dictation is never sent to the Windows cloud clipboard.

## Voice commands and text shortcuts

| Say | You get |
|---|---|
| "john at gmail dot com" | john@gmail.com |
| "ellipsis", "em dash", "open/close brackets" | `...`, ` — `, `[ ]` |
| "emoji heart", "emoji thumbs up", "emoji laughing"… | ❤️ 👍 😂 (the list is in `textproc.EMOJIS`) |
| "… cancel dictation" (at the end) | nothing: the dictation is dropped, like Esc |
| "… press enter" (at the end) | pastes and presses Enter (sends) |

Snippets accept fields filled in at the moment: `{data}` (15/10/2026), `{data_extenso}`
(15 de outubro de 2026), `{hora}`, `{dia_semana}` and `{amanha}`. Example: trigger "signature",
text `Bira — {data_extenso}`.

Dictations of up to 2 words ("ok", "sure") do not go through the LLM: they come out at once,
with the local rules only.

## Permissions on Windows

On macOS you would have to grant Accessibility and Input Monitoring. **Windows has no such
permissions**: the global keyboard hook (`WH_KEYBOARD_LL`) and the simulated keys (`SendInput`)
work without special authorisation. What does have to be checked:

1. **Microphone** (required). Open *Settings → Privacy & security → Microphone* and turn on:
   *Microphone access* and *Let desktop apps access your microphone*.
   Bira Flow is a desktop app (Python), so it does not show up in the Store list; the system
   option above is the one that matters. While it records, Windows shows the microphone icon in
   the tray. Outside a recording the microphone stays closed.
2. **Apps running as administrator.** Windows isolates elevated windows (UIPI): while an app
   opened "as administrator" is in focus, the shortcut is not detected and the paste is
   blocked. If you need to dictate in those apps, start Bira Flow as administrator too.
   Normally it is not needed.
3. **Protected screens.** The lock screen, the UAC prompt and Ctrl+Alt+Del do not accept
   dictation, by design of Windows.
4. **Antivirus.** Some antivirus programs dislike programs with a keyboard hook. If yours
   complains, allow the `.venv` folder of the project. The code is all local and lives there.
5. **Firewall.** No port is opened. The only outgoing connections are to the APIs you configure.

## Dictionary, snippets and history

- **Corrections** (Dictionary page): "when you hear" → "write". Applied after the LLM, so it
  cannot undo them. At the start of a sentence, a lowercase correction gets a capital.
- **Learn from your edits** (like Wispr Flow): if you fix a word in the app right after
  dictating ("ese" → "esse"), the swap becomes a correction on its own and the pill says
  "Learned: ese → esse". Bira Flow reads, through Windows UI Automation, only the field where
  the text was pasted, for up to 5 minutes (if you change windows it pauses and resumes when
  you come back), never password fields, and keeps only the rule. It works in apps that expose
  the text: Windows text boxes, Word and the apps made with Chromium (Chrome, Edge, Brave,
  WhatsApp Web and WhatsApp Desktop, Teams, Slack…). In Chromium, the page only shows the text
  to Windows when a screen reader asks for it; so when you start recording in one of those
  windows, Bira Flow makes that request (like Narrator does) and the browser starts exposing
  the field. Fix it **before sending the message**: after the send the field is empty, and
  Bira Flow learns from the last version it saw. Can be turned off in the Dictionary page.
- **Learn from the history**: in the History, select a dictation → **Correct…** → edit → **Save
  correction**. Spelling swaps of up to 3 words become corrections (e.g. "ese" → "esse",
  "github" → "GitHub"). Content changes ("no" → "yes") and numbers do not become rules.
- **Learn from a screenshot**: take a screenshot with the correction on it (Win+Shift+S). Then
  click **Learn from the copied screenshot** in the tray menu, or **Learn from a screenshot**
  (or Ctrl+V) in the Dictionary page. A vision model of your refinement provider (Groq, OpenAI
  or Anthropic) reads the screenshot together with your last dictations and gives back the
  swaps (e.g. "TASP" → "Taspio", "Kate" → "Kayth"), which enter the Dictionary right away. The
  image is only uploaded when you ask for it. If something wrong comes in, use **Undo
  learning** in the tray.
- **Snippets**: the trigger can appear in the middle of the sentence ("send it to my email") or
  alone. The LLM receives a marker in place of the snippet, so the saved text goes in exactly
  as it is.
- **History**: `%APPDATA%\BiraFlow\history.json` (local only). The statistics keep counters
  (words and seconds), never text. The search looks in the text and in the app name; double
  click corrects; **Export…** saves everything as `.txt` or `.csv` (spreadsheet).
- **Dictionary backup**: Dictionary → **Export…/Import…** takes the words, corrections and
  snippets (JSON) to another computer; importing merges them with what is already there.
- **Snippets with fields**: `{data}`, `{data_extenso}`, `{hora}`, `{dia_semana}`, `{amanha}` and
  `{area_de_transferencia}` (the text you had copied).

## Privacy

- No accounts, no telemetry. The installed version only reaches the update channel (can be
  turned off in Settings → About).
- With `engine = "local"` and no keys in `.env`, nothing leaves the computer.
- The audio is recorded to a temporary WAV in `%LOCALAPPDATA%\BiraFlow\tmp`, deleted right
  after the transcription (and on the next start, if the program closed in the middle).
- The log (`%APPDATA%\BiraFlow\biraflow.log`) records times and errors, never the dictated text.

## Development

```bash
.venv/Scripts/python -m pytest -q             # the whole suite (~40 s); it must stay green
.venv/Scripts/python -m biraflow --selftest   # imports every UI piece, opens the settings invisibly
.venv/Scripts/python -m biraflow --check      # paths, keys, hotkey, engine
.venv/Scripts/python -m compileall -q biraflow
```

The app is a Python 3.12 + Tkinter (sv_ttk) desktop app: the keyboard hook (`winhook.py`) posts
events to the controller (`controller.py`, own thread, event queue) and each dictation runs in
a job thread. Tk lives on the main thread; every other thread talks to the UI through
`app.UiBridge` (a queue pumped every 30 ms).

| Path | What |
|---|---|
| `biraflow/` | The app package |
| `biraflow/lang/` | The translation catalogs (one JSON per language) |
| `tests/` | pytest suite (fast, no network, no real keyboard input) |
| `packaging/` | PyInstaller spec, Inno Setup script, code-signing certificate (`signing/` is gitignored) |
| `scripts/` | Install, whisper.cpp setup, signing, installer build, release publishing, translations |
| `dist/` | Build output: `BiraFlow-Setup-<version>.exe`, `latest.json` (update manifest) |

Translations are plain JSON and the whole pipeline is scripted:

```powershell
# translate the interface into a language with an LLM (any OpenAI-compatible endpoint)
.venv\Scripts\python scripts\translate_catalog.py --lang en --base-url https://api.groq.com/openai/v1 `
    --model llama-3.3-70b-versatile --key-env GROQ_API_KEY
# what is still missing
.venv\Scripts\python scripts\translate_catalog.py --check --lang en
```

See [docs/TRANSLATION.md](docs/TRANSLATION.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Short version: open an issue before a big change, keep
the tests green, and add yourself to the translators if you fix a language.

## License

MIT — see [LICENSE](LICENSE).
