"""Wrap the interface strings in t() so they can be translated.

The source strings stay in the code (Portuguese); the catalogs in biraflow/lang/ map
them to the other languages. This script rewrites the calls that show text to the user:

    c.switch("Ditado ativado", var, "...")   ->  c.switch(_t("Ditado ativado"), var, _t("..."))

An f-string becomes a template with numbered fields, so the translator can reorder it:

    flash(f"Áudio {name} não encontrado")     ->  flash(_t("Áudio {0} não encontrado").format(name))

It edits the source in place (only the position of the literal changes, the file keeps
its formatting) and can be run again: strings already wrapped are skipped.

    python scripts/i18n_wrap.py --dry-run     # what would change
    python scripts/i18n_wrap.py               # apply
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The name used in the code. "_t" avoids clashing with the local variables called "t"
# (a Text widget, a transcription config) in the modules below.
NAME = "_t"

# Method calls and which of their positional arguments hold text for the user.
POSITIONAL: dict[str, tuple[int, ...]] = {
    "field": (0, 2),        # Card.field(label, make, help)
    "switch": (0, 2),       # Card.switch(text, var, help)
    "note": (0,),           # Card.note(text)
    "card": (0,),           # Page.card(title)
    "Page": (2, 3),         # Page(win, key, title, subtitle)
    "HotkeyField": (2, 4),  # HotkeyField(win, card, label, value, help)
    "flash": (0,),          # pill.flash(text, tone, ms)
    "busy": (0,),           # pill.busy(text)
    "preview": (0,),        # pill.preview(text)
    "notify": (0,),         # tray.notify(text)
    "Item": (0,),           # pystray.MenuItem(text, action)
    "showinfo": (0, 1),
    "showerror": (0, 1),
    "showwarning": (0, 1),
    "askyesno": (0, 1),
    "askokcancel": (0, 1),
}

# Calls whose argument is a tuple with text inside: (argument index, element index).
TUPLE_ARGS: dict[str, tuple[tuple[int, int], ...]] = {
    "post": ((1, 1),),      # self.post("job_note", (job.id, "text"))
    "_tree": ((1, 1),),     # self._tree(frame, [("raw", "Texto bruto", 180)])
}

# Widgets and methods whose text= keyword goes to the user.
TEXT_KEYWORD = {
    "Label", "Button", "Checkbutton", "Radiobutton", "Menu", "Entry",
    "configure", "heading", "insert", "title", "set_title",
}

# Files with interface text.
FILES = [
    "biraflow/settings_ui.py",
    "biraflow/tray.py",
    "biraflow/pill.py",
    "biraflow/app.py",
    "biraflow/controller.py",
    "biraflow/meeting.py",
    "biraflow/audio_ui.py",
]

# Strings that are not text for the user.
SKIP_PATTERNS = [
    re.compile(r"[{}%]"),                 # format placeholders
    re.compile(r"[<>]"),                   # markup
    re.compile(r"[\\/]"),                  # paths
    re.compile(r"::"),
    re.compile(r"^\s*$"),
    re.compile(r"^[A-Za-z0-9_.-]+\.[A-Za-z0-9_.]+$"),   # "Card.TLabel", "http://x.y"
    re.compile(r"^[a-z0-9_]+$"),          # keys, style names, ids
]


def _skip(text: str) -> bool:
    return any(p.search(text) for p in SKIP_PATTERNS)


def _func_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _strings_in(node: ast.AST) -> list[ast.AST]:
    """The string literals (and f-strings) inside a node, without going inside them."""
    out: list[ast.AST] = []
    if isinstance(node, ast.JoinedStr):
        return [node]
    if isinstance(node, ast.Constant):
        return [node] if isinstance(node.value, str) else []
    for child in ast.iter_child_nodes(node):
        out.extend(_strings_in(child))
    return out


def _positions(tree: ast.AST) -> list[ast.AST]:
    """The literals that are shown to the user."""
    out: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _func_name(node.func)
        if name in POSITIONAL:
            for index in POSITIONAL[name]:
                if index < len(node.args):
                    out.extend(_strings_in(node.args[index]))
        if name in TUPLE_ARGS:
            for arg_index, elem_index in TUPLE_ARGS[name]:
                if arg_index >= len(node.args):
                    continue
                arg = node.args[arg_index]
                if isinstance(arg, (ast.Tuple, ast.List)) and elem_index < len(arg.elts):
                    out.extend(_strings_in(arg.elts[elem_index]))
        for kw in node.keywords:
            if kw.arg == "text" and name in TEXT_KEYWORD:
                out.extend(_strings_in(kw.value))
            if name.startswith("show") and kw.arg in ("title", "message"):
                out.extend(_strings_in(kw.value))
    return out


def _already_wrapped(tree: ast.AST) -> set[int]:
    """ids of the literals that are already inside a t() call."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _func_name(node.func) in ("t", NAME):
            for arg in node.args:
                out.add(id(arg))
    return out


def _char_col(line: str, byte_col: int) -> int:
    """ast gives the column in bytes; the file is read as text."""
    return len(line.encode("utf-8")[:byte_col].decode("utf-8", "ignore"))


class Editor:
    def __init__(self, source: str):
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self.offsets = [0]
        for line in self.lines:
            self.offsets.append(self.offsets[-1] + len(line))

    def segment(self, node: ast.AST) -> str:
        first, last = self.lines[node.lineno - 1], self.lines[node.end_lineno - 1]
        sc = _char_col(first, node.col_offset)
        ec = _char_col(last, node.end_col_offset)
        if node.lineno == node.end_lineno:
            return first[sc:ec]
        return first[sc:] + "".join(self.lines[node.lineno:node.end_lineno - 1]) + last[:ec]

    def span(self, node: ast.AST) -> tuple[int, int]:
        first, last = self.lines[node.lineno - 1], self.lines[node.end_lineno - 1]
        sc = _char_col(first, node.col_offset)
        ec = _char_col(last, node.end_col_offset)
        return (self.offsets[node.lineno - 1] + sc,
                self.offsets[node.end_lineno - 1] + ec)

    def fstring(self, node: ast.JoinedStr) -> tuple[str, list[str]] | None:
        """The template of an f-string and the source of its values."""
        template: list[str] = []
        values: list[str] = []
        index = 0
        for part in node.values:
            if isinstance(part, ast.Constant):
                text = part.value.replace("{", "{{").replace("}", "}}")
                template.append(text)
            elif isinstance(part, ast.FormattedValue):
                value = self.segment(part.value)
                if not value:
                    return None
                field = "{" + str(index)
                if part.conversion is not None and part.conversion >= 0:
                    field += "!" + chr(part.conversion)
                if part.format_spec is not None:
                    spec = self.segment(part.format_spec)
                    if spec.startswith(":"):     # ast keeps the colon in the spec
                        spec = spec[1:]
                    field += ":" + spec
                template.append(field + "}")
                values.append(value)
                index += 1
            else:
                return None
        text = "".join(template)
        if not re.search(r"[A-Za-zÀ-ÿ]", text):     # only formatting, no words
            return None
        return text, values


def _replacement(editor: Editor, node: ast.AST) -> str | None:
    """The new source of a literal, or None when it must stay as it is."""
    if isinstance(node, ast.Constant):
        text = node.value
        if not text or _skip(text):
            return None
        return f"{NAME}({editor.segment(node)})"
    if isinstance(node, ast.JoinedStr):
        parts = editor.fstring(node)
        if parts is None:
            return None
        template, values = parts
        call = f"{NAME}({template!r})"
        if values:
            call += ".format(" + ", ".join(values) + ")"
        return call
    return None


def wrap_file(path: Path, dry: bool) -> int:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    editor = Editor(source)
    wrapped = _already_wrapped(tree)
    edits = []
    count = 0
    for node in _positions(tree):
        if id(node) in wrapped:
            continue
        new = _replacement(editor, node)
        if new is None:
            continue
        start, end = editor.span(node)
        edits.append((node.lineno, start, end, new))
        count += 1
    if not edits:
        return 0
    if dry:
        for lineno, _s, _e, new in edits:
            print(f"  {path.name}:{lineno}: {new[:80]}")
        return count
    for _lineno, start, end, new in sorted(edits, key=lambda e: -e[1]):
        source = source[:start] + new + source[end:]
    if f"from .i18n import t as {NAME}" not in source:
        source = _add_import(source)
    path.write_text(source, encoding="utf-8")
    return count


def _add_import(source: str) -> str:
    lines = source.splitlines(keepends=True)
    imp = f"from .i18n import t as {NAME}\n"
    for i, line in enumerate(lines):
        if line.startswith("from .") and " import " in line and i < 40:
            lines.insert(i, imp)
            return "".join(lines)
    for i, line in enumerate(lines):
        if line.startswith("import ") and i < 40:
            lines.insert(i + 1, imp)
            return "".join(lines)
    return imp + source


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="only show what would change")
    ap.add_argument("files", nargs="*", help="files to wrap (default: the interface files)")
    args = ap.parse_args()
    files = args.files or FILES
    total = 0
    for rel in files:
        path = ROOT / rel
        if not path.exists():
            print(f"not found: {path}", file=sys.stderr)
            continue
        n = wrap_file(path, args.dry_run)
        total += n
        print(f"{rel}: {n} string(s)")
    print(f"total: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
