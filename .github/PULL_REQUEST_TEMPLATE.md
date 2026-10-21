## What this changes

<!-- One or two sentences. What is different after this pull request? -->

## Why

<!-- The problem, not the solution. Link the issue if there is one. -->

## How it was tested

<!-- What you ran and what you saw. -->

- [ ] `python -m pytest -q` is green
- [ ] `python -m biraflow --selftest` opens the settings window without errors
- [ ] I tried it in the app (the hotkey, the pill, the paste)

## Checklist

- [ ] The tests stay green
- [ ] The log still never shows the dictated text, the clipboard or the API keys
- [ ] Nothing was typed into my own apps during the tests
- [ ] If the interface changed: `README.md` and `README.pt-BR.md` are updated
- [ ] If it is a new setting: the field is in `config.py`, in `DEFAULT_SETTINGS_TOML`, in the
      settings window and there is a test
