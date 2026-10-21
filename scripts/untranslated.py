"""The interface strings that are not wrapped in t() yet.

    python scripts/untranslated.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FILES = ["settings_ui.py", "tray.py", "pill.py", "app.py", "controller.py", "meeting.py", "audio_ui.py"]
# a string that looks like a sentence for the user: two words, a space, a lowercase letter
LOOKS_LIKE_TEXT = 2


def main() -> int:
    total = 0
    for name in FILES:
        path = ROOT / "biraflow" / name
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        wrapped = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("_t", "t"):
                for arg in node.args:
                    wrapped.add(id(arg))
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in wrapped:
                continue
            text = node.value
            if len(text) < LOOKS_LIKE_TEXT or " " not in text:
                continue
            if any(ch in text for ch in "{}%<>|/\\"):
                continue
            if not any(c.islower() for c in text):
                continue
            print(f"{name}:{node.lineno}: {text[:90]!r}")
            total += 1
    print(f"\n{total} string(s) that are not wrapped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
