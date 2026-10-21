# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Send it privately to the
maintainer through GitHub's *Security → Report a vulnerability* tab of the repository
(the "Report a vulnerability" button), or by email if one is listed there.

Include:

- what the problem is and what an attacker could do with it;
- the steps to reproduce (version, Windows version, which settings were on);
- anything you already tried.

You will get an acknowledgement within a few days. Fixes for serious problems are released as
a new version, and the report is credited unless you prefer otherwise.

## What this app does with your data

Bira Flow is local-first. It is worth knowing exactly what leaves the machine:

| What | Where it goes | When |
|---|---|---|
| Your voice | Nowhere | With the local model (`engine = "local"`) |
| Your voice | Groq / OpenAI / the Vercel AI Gateway | With a cloud engine and an API key |
| The dictated text | The LLM provider (Groq, OpenAI, Anthropic, Vercel or Ollama) | With cleanup on and a key |
| The meeting audio | Nowhere | With the local model |
| The meeting audio | The transcription provider | With a cloud engine |
| Settings, dictionary, history | `%APPDATA%\BiraFlow` | Always (local files) |
| A screenshot of your screen | The vision provider | Only when you ask for it |

No account, no telemetry, no analytics. The installed version only contacts the update channel,
and that can be turned off in Settings → About.

## Hardening notes

- API keys are stored in `%APPDATA%\BiraFlow\.env` in plain text, in your user profile. They
  are never logged and never leave the machine except to the provider they belong to.
- The app installs a global keyboard hook (`WH_KEYBOARD_LL`) and simulates keystrokes
  (`SendInput`). This is what a dictation app does; it is also why some antivirus programs
  complain — the code is local, inside the `.venv` folder of the project.
- The automatic update only accepts an installer signed with the pinned certificate and checks
  its SHA-256 against the manifest.
- The code-signing private key lives in `packaging/signing/` and is **not** in git. If it ever
  leaks, revoke it: the automatic updates would have to be re-keyed.
