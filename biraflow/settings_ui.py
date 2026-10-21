"""Settings window: Windows 11 style (Sun Valley ttk theme + Fluent icons), sidebar
navigation, card-based pages; follows the system light/dark mode. Tk main thread."""
from __future__ import annotations

import copy
import csv
import ctypes
import json
import math
import os
import re
import shutil
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import winreg
from dataclasses import dataclass, fields
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING, Callable

import sv_ttk

from .i18n import t as _t
from . import autostart, branding, config, i18n, setup_local, textproc, win32
from .cleanup import PROVIDERS, resolve_provider
from .config import STYLE_CHOICES, Settings
from .hotkey import ChordCapture, HotkeyError, display_hotkey, parse_hotkey
from .store import Dictionary, History, Stats
from .textproc import Replacement, Snippet
from .transcribe import API, find_whisper_cli, resolve_model

if TYPE_CHECKING:
    from .app import App

# The voice-model download outlives the window that started it (closing it does not stop it).
_WAKE_DOWNLOAD: dict | None = None

LANGUAGES = [
    ("auto", "Detectar automaticamente (≈2 s mais lento)"),
    ("pt", "Português"), ("en", "English"), ("es", "Español"), ("fr", "Français"),
    ("de", "Deutsch"), ("it", "Italiano"), ("ja", "日本語"), ("zh", "中文"),
]
MODELS = [
    ("tiny", "tiny · 75 MB · muito rápido, menos preciso"),
    ("base", "base · 142 MB · rápido"),
    ("small", "small · 466 MB · recomendado"),
    ("medium", "medium · 1,5 GB · mais preciso, ~3× mais lento"),
    ("large-v3-turbo-q8_0", "large-v3-turbo · 874 MB · melhor (placa de vídeo)"),
    ("large-v3-turbo", "large-v3-turbo (sem compactação) · 1,6 GB"),
]
ENGINES = [
    ("local", "Local (whisper.cpp)", "Offline: o áudio nunca sai do computador."),
    ("groq", "Groq", "O mais rápido (~0,5 s). Envia o áudio para a Groq."),
    ("gateway", "Vercel AI Gateway", "Uma chave para transcrição, limpeza e notas; o áudio vai para o "
                                     "Gateway. O time pode ter modelos bloqueados (Model Allowlist)."),
    ("openai", "OpenAI", "Envia o áudio para a OpenAI."),
    ("auto", "Automático", "Usa a primeira chave que existir (Groq, OpenAI, Vercel Gateway); "
                           "sem nenhuma, o modelo local."),
]
PROVIDER_CHOICES = [
    ("auto", "Automático (primeira chave: Groq, OpenAI, Anthropic, Vercel)"),
    ("groq", "Groq"), ("openai", "OpenAI"), ("anthropic", "Anthropic (Claude)"),
    ("vercel", "Vercel AI Gateway (vários modelos)"),
    ("ollama", "Ollama (local, offline)"), ("none", "Desligado"),
]
STYLE_LABELS = {
    "neutral": "Neutro: só limpa",
    "formal": "Formal: polido, frases completas",
    "casual": "Casual: natural, de conversa",
    "very_casual": "Muito casual: curto, sem ponto final",
    "technical": "Técnico: preserva código e comandos",
    "email": "E-mail: saudação, parágrafos e despedida em linhas próprias",
}
STYLE_CATEGORIES = [
    ("personal", "Mensagens pessoais", "WhatsApp, Telegram, Signal, Messenger, Discord, Instagram"),
    ("work", "Mensagens de trabalho", "Slack, Microsoft Teams, Google Chat, Zoom"),
    ("email", "E-mail", "Outlook, Thunderbird, Gmail e outros e-mails no navegador"),
    ("code", "Código e terminal", "VS Code, Cursor, JetBrains, Windows Terminal, PowerShell"),
    ("other", "Outros aplicativos", "Todo o resto"),
]
KEY_LABELS = [
    ("GROQ_API_KEY", "Groq", "console.groq.com/keys"),
    ("OPENAI_API_KEY", "OpenAI", "platform.openai.com/api-keys"),
    ("ANTHROPIC_API_KEY", "Anthropic", "console.anthropic.com/settings/keys"),
    ("AI_GATEWAY_API_KEY", "Vercel AI Gateway", "vercel.com/ai-gateway"),
]
PILL_POSITIONS = [
    ("bottom", "Embaixo, no centro"), ("bottom-left", "Embaixo, à esquerda"), ("bottom-right", "Embaixo, à direita"),
    ("top", "Em cima, no centro"), ("top-left", "Em cima, à esquerda"), ("top-right", "Em cima, à direita"),
]
PILL_SIZES = [("small", "Pequena"), ("medium", "Média"), ("large", "Grande")]
KEEP_CHOICES = [("always", "Guardar (últimos 500)"), ("24h", "Apagar depois de 24 horas"),
                ("never", "Não guardar histórico")]
# The language of the interface. The names are in the language itself, as usual.
LANGUAGE_OPTIONS = [
    ("auto", "Automático (o idioma do Windows)"),
    ("pt-BR", "Português (Brasil)"),
    ("en", "English"),
    ("es", "Español"),
    ("fr", "Français"),
    ("de", "Deutsch"),
    ("it", "Italiano"),
    ("zh-Hans", "简体中文"),
    ("ja", "日本語"),
    ("ko", "한국어"),
]
DEFAULT_MIC = "Padrão do Windows"
LLM_MODELS = {   # suggestions in the "Modelo" box; any other name can be typed
    "groq": ["qwen/qwen3.8-27b", "openai/gpt-oss-20b", "openai/gpt-oss-120b"],
    "openai": ["gpt-4.1-mini", "gpt-4.1-nano", "gpt-5-mini"],
    "anthropic": ["claude-haiku-5-5", "claude-sonnet-5-5"],
    "vercel": ["google/gemini-2.5-flash-lite", "google/gemini-3.8-flash", "openai/gpt-oss-120b",
               "anthropic/claude-sonnet-5.5"],
    "ollama": ["llama3.2", "qwen3", "gemma3"],
}
APP_NAMES = {"audio-file": "Áudio (arquivo)", "whatsapp": "WhatsApp", "msedge": "Edge", "chrome": "Chrome", "brave": "Brave", "firefox": "Firefox",
             "winword": "Word", "outlook": "Outlook", "olk": "Outlook", "excel": "Excel", "code": "VS Code",
             "slack": "Slack", "ms-teams": "Teams", "teams": "Teams", "telegram": "Telegram", "notepad": "Bloco de Notas",
             "windowsterminal": "Terminal", "explorer": "Explorador", "claude": "Claude", "discord": "Discord"}


def merge_changes(current: Settings, at_open: Settings, edited: Settings) -> Settings:
    """`current` (the file now) with only the fields the window changed since it opened:
    edits made meanwhile in the advanced file are kept."""
    result = copy.deepcopy(current)
    for section in fields(Settings):
        old, new, out = (getattr(x, section.name) for x in (at_open, edited, result))
        for f in fields(old):
            if getattr(new, f.name) != getattr(old, f.name):
                setattr(out, f.name, copy.deepcopy(getattr(new, f.name)))
    return result


def app_label(exe: str) -> str:
    """"adr-workspace.exe" -> "Adr Workspace", "msedge.exe" -> "Edge"."""
    name = (exe or "").lower().removesuffix(".exe")
    if not name:
        return "—"
    return APP_NAMES.get(name) or " ".join(w.capitalize() for w in name.replace("_", "-").split("-") if w)

# (page key, label, Segoe Fluent Icons glyph)
NAV = [
    ("general", "Geral", ""),
    ("hotkey", "Atalhos", ""),
    ("voice", "Ativação por voz", ""),
    ("mic", "Microfone", ""),
    ("transcription", "Transcrição", ""),
    ("cleanup", "Refinamento", ""),
    ("styles", "Estilos", ""),
    ("dictionary", "Dicionário", ""),
    ("snippets", "Snippets", ""),
    ("output", "Inserção", ""),
    ("meeting", "Reuniões", ""),
    ("history", "Histórico", ""),
    ("keys", "Chaves de API", ""),
    ("about", "Sobre", ""),
]

NAV_KEYS = tuple(k for k, _, _ in NAV)   # also the build order: later pages reach into earlier ones


class _Pages(dict):
    """The pages built so far. Every page of NAV counts as present: show() builds one on demand."""

    def __contains__(self, key) -> bool:
        return key in NAV_KEYS


# small captions that group the sidebar: (first page of the group, caption)
NAV_SECTIONS = {"general": "Ditado", "transcription": "Texto", "meeting": "Dados"}


def system_uses_dark_mode() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


@dataclass
class Palette:
    bg: str
    text: str
    muted: str
    error: str
    ok: str
    field: str
    border: str
    accent: str
    nav_hover: str
    zebra: str


DARK = Palette("#1c1c1c", "#fafafa", "#a0a0a0", "#ff99a4", "#6ccb5f", "#2d2d2d", "#3d3d3d", "#57c8ff", "#2b2b2b",
               "#242424")
LIGHT = Palette("#fafafa", "#1c1c1c", "#5c5c5c", "#c42b1c", "#0f7b0f", "#ffffff", "#d0d0d0", "#005fb8", "#ececec",
                "#f2f2f2")


class LevelMeter:
    """Segmented live level bar for the microphone test (a thin progress bar barely moved)."""

    SEGMENTS = 28

    def __init__(self, parent, pal: Palette, px):
        self.pal = pal
        self.w, self.h, self.gap = px(250), px(14), px(2)
        self.canvas = tk.Canvas(parent, width=self.w, height=self.h, bg=pal.bg, highlightthickness=0, bd=0)
        seg = (self.w - self.gap * (self.SEGMENTS - 1)) / self.SEGMENTS
        self.items = [self.canvas.create_rectangle(i * (seg + self.gap), 0, i * (seg + self.gap) + seg, self.h,
                                                   fill=pal.border, outline="") for i in range(self.SEGMENTS)]
        self.set(0)

    def set(self, level: float) -> None:
        lit = round(max(0.0, min(1.0, level)) * self.SEGMENTS)
        for i, item in enumerate(self.items):
            color = self.pal.border
            if i < lit:
                color = self.pal.ok if i < self.SEGMENTS * 0.7 else "#f5a623" if i < self.SEGMENTS * 0.9 else self.pal.error
            self.canvas.itemconfigure(item, fill=color)


class Choice:
    """Read-only combobox showing labels for underlying codes."""

    def __init__(self, parent, options: list[tuple[str, str]], value: str, width: int = 40,
                 on_change: Callable[[str], None] | None = None):
        self.options = list(options)
        if value not in [code for code, _ in self.options]:
            self.options.append((value, value))
        self.var = tk.StringVar(value=_t(dict(self.options)[value]))
        self.widget = ttk.Combobox(parent, textvariable=self.var, state="readonly", width=width,
                                   values=[_t(label) for _, label in self.options])
        if on_change:
            self.widget.bind("<<ComboboxSelected>>", lambda _: on_change(self.get()))

    def get(self) -> str:
        label = self.var.get()
        return next((code for code, lbl in self.options if _t(lbl) == label), label)


class Page:
    """A scrollable content page holding cards."""

    def __init__(self, win: "SettingsWindow", key: str, title: str, subtitle: str = ""):
        self.win = win
        px = win.px
        self.outer = ttk.Frame(win.content)
        canvas = tk.Canvas(self.outer, highlightthickness=0, bd=0, bg=win.pal.bg)
        bar = ttk.Scrollbar(self.outer, orient="vertical", command=canvas.yview)
        self.inner = ttk.Frame(canvas, padding=(px(28), px(22), px(28), px(28)))
        window = canvas.create_window(0, 0, window=self.inner, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        self.canvas = canvas

        def refresh(_=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            needed = self.inner.winfo_reqheight() > canvas.winfo_height()
            if needed and not bar.winfo_ismapped():
                bar.pack(side="right", fill="y", before=canvas)
            elif not needed and bar.winfo_ismapped():
                bar.pack_forget()

        self.inner.bind("<Configure>", refresh)
        canvas.bind("<Configure>", lambda e: (canvas.itemconfigure(window, width=e.width), refresh()))

        def wheel(e):
            if e.delta and self.inner.winfo_reqheight() > canvas.winfo_height():
                steps = round(e.delta / 40) or (1 if e.delta > 0 else -1)   # touchpads send deltas under 120
                canvas.yview_scroll(-steps, "units")

        self.wheel = wheel   # bound once on the window (SettingsWindow._wheel)

        self.helps: list[tuple[ttk.Label, int]] = []

        def wrap_helps(e):
            full = max(px(360), e.width - px(56) - px(40))
            for lbl, column in self.helps:
                lbl.configure(wraplength=max(px(260), full - px(252)) if column else full)

        canvas.bind("<Configure>", wrap_helps, add="+")
        ttk.Label(self.inner, text=title, font="SunValleyTitleFont").pack(anchor="w")
        self.subtitle = None
        if subtitle:
            self.subtitle = ttk.Label(self.inner, text=subtitle, style="Muted.TLabel",
                                      wraplength=px(640), justify="left")
            self.subtitle.pack(anchor="w", pady=(px(2), 0))
            # wrap to the window's width, not a fixed one (no ragged short lines on a wide window)
            canvas.bind("<Configure>", lambda e: self.subtitle.configure(
                wraplength=max(px(360), e.width - px(80))), add="+")
        win.pages[key] = self

    def card(self, title: str | None = None) -> "Card":
        return Card(self, title)


class Card:
    def __init__(self, page: Page, title: str | None):
        self.win = page.win
        self.page = page
        px = self.win.px
        if title:
            ttk.Label(page.inner, text=title, font="SunValleyBodyStrongFont").pack(anchor="w", pady=(px(18), px(6)))
        self.frame = ttk.Frame(page.inner, style="Card.TFrame", padding=(px(18), px(12)))
        self.frame.pack(fill="x", pady=(0 if title else px(14), 0))
        self.frame.columnconfigure(0, minsize=px(232))  # every card lines up its fields
        self.frame.columnconfigure(1, weight=1)
        self.row = 0

    def _help(self, text: str, column: int = 1, span: int = 1) -> ttk.Label:
        lbl = ttk.Label(self.frame, text=text, style="CardMuted.TLabel",
                        wraplength=self.win.px(460 if column else 640), justify="left")
        lbl.grid(row=self.row, column=column, columnspan=span, sticky="w", pady=(0, self.win.px(6)))
        self.row += 1
        self.page.helps.append((lbl, column))
        return lbl

    def field(self, label: str, make: Callable[[ttk.Frame], tk.Widget], help: str = "",
              sticky: str = "w") -> tk.Widget:
        px = self.win.px
        ttk.Label(self.frame, text=label, style="Card.TLabel").grid(
            row=self.row, column=0, sticky="w", padx=(0, px(20)), pady=px(6))
        widget = make(self.frame)
        widget.grid(row=self.row, column=1, sticky=sticky, pady=px(6))
        self.row += 1
        if help:
            self._help(help)
        return widget

    def switch(self, text: str, var: tk.BooleanVar, help: str = "") -> None:
        ttk.Checkbutton(self.frame, text=text, variable=var, style="Switch.TCheckbutton").grid(
            row=self.row, column=0, columnspan=2, sticky="w", pady=(self.win.px(6), self.win.px(2)))
        self.row += 1
        if help:
            self._help(help, column=0, span=2)

    def full(self, make: Callable[[ttk.Frame], tk.Widget], sticky: str = "ew") -> tk.Widget:
        widget = make(self.frame)
        widget.grid(row=self.row, column=0, columnspan=2, sticky=sticky, pady=self.win.px(4))
        self.row += 1
        return widget

    def note(self, text: str) -> ttk.Label:
        return self._help(text, column=0, span=2)

    def divider(self) -> None:
        ttk.Separator(self.frame).grid(row=self.row, column=0, columnspan=2, sticky="ew", pady=self.win.px(8))
        self.row += 1


class HotkeyField:
    """Keycap display + "Gravar" (capture through the global hook) + optional "Desativar"."""

    def __init__(self, win: "SettingsWindow", card: Card, label: str, value: str, help: str,
                 optional: bool = False):
        self.win = win
        self.optional = optional
        self.var = tk.StringVar(value=value)
        self.capture: ChordCapture | None = None
        self.deadline = 0.0

        def make(parent):
            f = ttk.Frame(parent, style="Inner.TFrame")
            self.keycap = tk.Frame(f, bg=win.pal.bg)
            self.keycap.pack(side="left", padx=(0, win.px(20)))
            self.button = ttk.Button(f, text=_t("Gravar"), command=self.start)
            self.button.pack(side="left")
            if optional:
                ttk.Button(f, text=_t("Desativar"), command=lambda: self.var.set("")).pack(side="left", padx=(win.px(6), 0))
            return f

        card.field(label, make)
        self.status = card.note(help)
        self.help = help
        self.var.trace_add("write", lambda *_: self.validate())
        self.validate()

    def _caps(self, text: str, keys: bool = True, active: bool = False) -> None:
        """Each key as a keycap chip ("Ctrl" + "Win"); plain text for states."""
        pal, px = self.win.pal, self.win.px
        for child in self.keycap.winfo_children():
            child.destroy()
        if not keys:
            tk.Label(self.keycap, text=text, bg=pal.bg, fg=pal.accent if active else pal.muted,
                     font="SunValleyBodyFont").pack(side="left")
            return
        for i, part in enumerate(text.split(" + ")):
            if i:
                tk.Label(self.keycap, text=_t("+"), bg=pal.bg, fg=pal.muted, font="SunValleyCaptionFont").pack(
                    side="left", padx=px(4))
            tk.Label(self.keycap, text=part, bg=pal.field, fg=pal.text, font="SunValleyBodyStrongFont",
                     padx=px(9), pady=px(3), highlightthickness=1,
                     highlightbackground=pal.accent if active else pal.border).pack(side="left")

    def error(self) -> str | None:
        """What is wrong with the hotkey (no change to the widgets)."""
        spec = self.var.get().strip()
        if not spec and self.optional:
            return None
        try:
            parse_hotkey(spec)
        except HotkeyError as e:
            return str(e)
        return None

    def validate(self) -> str | None:
        spec = self.var.get().strip()
        if not spec and self.optional:
            self._caps("Desativado", keys=False)
            self.status.configure(text=self.help, style="CardMuted.TLabel")
            return None
        try:
            parse_hotkey(spec)
        except HotkeyError as e:
            self._caps(spec or "—", keys=False)
            self.status.configure(text=str(e), style="CardError.TLabel")
            return str(e)
        self._caps(display_hotkey(spec))
        self.status.configure(text=self.help, style="CardMuted.TLabel")
        return None

    def start(self) -> None:
        if self.win.active_field is not None:
            return
        self.win.active_field = self
        self.capture = self.win.app.start_capture()
        self.deadline = time.monotonic() + 10
        self.button.configure(text=_t("Cancelar"), command=self.cancel)
        self._caps("Pressione a combinação…", keys=False, active=True)
        self.status.configure(text=_t("Solte todas as teclas para concluir. Esc cancela."), style="CardMuted.TLabel")
        self._poll()

    def cancel(self) -> None:
        if self.capture is not None:
            self.capture.cancelled = self.capture.done = True

    def _poll(self) -> None:
        cap = self.capture
        if cap is None:
            return
        if not cap.done and time.monotonic() < self.deadline:
            if cap.pressed:
                self._caps(display_hotkey(cap.spec), active=True)
            self.win.top.after(40, self._poll)
            return
        self.stop()
        if cap.done and not cap.cancelled and cap.spec:
            self.var.set(cap.spec)
        self.validate()

    def stop(self) -> None:
        if self.capture is not None:
            self.win.app.stop_capture()
            self.capture = None
        self.win.active_field = None
        self.button.configure(text=_t("Gravar"), command=self.start)


class SettingsWindow:
    def __init__(self, app: "App", background: bool = False, layout: bool = True, lazy: bool | None = None):
        """`background`: built off-screen and never activated (self-test, UI tests), so it
        does not steal the focus from whatever you are typing in.
        `layout`: only with `background`. False stops right after the widget tree exists:
        nothing is measured, mapped or painted. The UI tests read widget state (values,
        text, which manager holds them), never pixels; the first layout of the ~580 widgets
        costs seconds on a busy PC, and they pay it once per test.
        `lazy` (default: when shown for real): the start page first and the window on screen, then
        the other pages one per idle moment — building and laying out all 14 took 1.4 s."""
        self.app = app
        self.background = background
        self.root = app.root
        self.paths = app.paths
        self.s: Settings = copy.deepcopy(app.controller.settings)
        self.env_all = config.load_env(self.paths)
        self.env_user = config.read_user_env(self.paths)
        self.dictionary = Dictionary.load(self.paths.dictionary)
        self._dictionary_at_open = {r.heard.lower() for r in self.dictionary.replacements}
        # the controller's instance: one lock for every writer of history.json
        self.history = getattr(app.controller, "history", None) or History(
            self.paths.history, self.s.history.keep, self.s.history.max_items)
        self.stats = Stats(self.paths.stats)
        self.active_field: HotkeyField | None = None
        self.mic_test = None
        self.setup_job = None   # close() reads it, maybe before the Transcrição page is built
        self._preview_saved = None
        self._placement_saved = None   # pill position/size in use before any preview
        self._saved = False
        self._preview_job = None
        self.pages: dict[str, Page] = _Pages()
        self._lazy = not background if lazy is None else lazy
        self.nav_items: dict[str, tuple] = {}
        self.current = ""

        dark = system_uses_dark_mode()
        sv_ttk.set_theme("dark" if dark else "light")
        # sv-ttk applies its colors on <<ThemeChanged>>, which only arrives after we
        # build the window; apply them now so the first paint is already right.
        self.root.tk.call("configure_colors")
        self.pal = DARK if dark else LIGHT
        self.scale = self.root.winfo_fpixels("1i") / 96
        families = set(tkfont.families(self.root))
        self.icon_font = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in families), None)

        self.top = tk.Toplevel(self.root)
        self.top.withdraw()
        self.top.title("Bira Flow — Configurações")
        self.top.configure(bg=self.pal.bg)
        from PIL import ImageTk

        try:
            self.top.iconbitmap(str(branding.APP_ICO))
        except tk.TclError:
            pass
        # brand artwork in the theme's text color (white on dark, near-black on light)
        self._logo_small = ImageTk.PhotoImage(branding.logo(self.px(30), self.pal.text))
        self._logo_big = ImageTk.PhotoImage(branding.logo(self.px(64), self.pal.text))
        self._styles()
        self._build()

        sw, sh = self.top.winfo_screenwidth(), self.top.winfo_screenheight()
        w, h = min(self.px(1020), sw - self.px(40)), min(self.px(780), sh - self.px(90))
        self.top.geometry(self._saved_geometry(w, h) or
                          f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - self.px(30))}")
        self.top.minsize(self.px(820), self.px(560))
        self.top.protocol("WM_DELETE_WINDOW", self.cancel)
        self.top.bind("<Escape>", lambda _: self.cancel() if self.active_field is None else None)
        # one binding for the window: bind_all on every <Enter> left a Tcl command (and this
        # whole window) alive in the root for the rest of the process
        self.top.bind("<MouseWheel>", self._wheel)
        self._snapshot = None   # taken once every page exists (_build_next)
        self._built = False
        if self._lazy:
            self.top.after(1, self._build_next)
        else:
            self._build_next()
        self.top.bind("<Control-s>", lambda _: self.save())
        if background:
            if not layout:
                return
            # Drawn for real (Tk paints only what is on a screen) but invisible, click-through,
            # behind everything and never activated.
            self.top.geometry(f"{w}x{h}+0+0")
            self.top.attributes("-alpha", 0.01)
            self.top.update_idletasks()
            hwnd = int(self.top.wm_frame(), 16)
            ex = win32.GetWindowLongPtrW(hwnd, win32.GWL_EXSTYLE)
            win32.SetWindowLongPtrW(hwnd, win32.GWL_EXSTYLE, ex | win32.WS_EX_NOACTIVATE | win32.WS_EX_TOOLWINDOW
                                    | win32.WS_EX_TRANSPARENT | win32.WS_EX_LAYERED)
            win32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
            win32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # HWND_BOTTOM, no size/move/activate
            self.top.update()
            self._dark_titlebar(self.top, dark)
            return
        self.top.deiconify()
        self.top.update_idletasks()
        branding.apply_window_icon(self.top)
        self._dark_titlebar(self.top, dark)
        self.top.lift()
        self.top.focus_force()

    # --- helpers --------------------------------------------------------------------

    def _saved_geometry(self, w: int, h: int) -> str | None:
        """The size and place the window had last time, if it still fits on a screen."""
        geo = config.load_state(self.paths).get("settings_geometry", "")
        m = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geo or "")
        if not m:
            return None
        gw, gh, gx, gy = map(int, m.groups())
        u = ctypes.windll.user32
        vx, vy, vw, vh = (u.GetSystemMetrics(76), u.GetSystemMetrics(77), u.GetSystemMetrics(78),
                          u.GetSystemMetrics(79))   # the virtual screen (all monitors)
        if gw < self.px(820) or gh < self.px(560) or not (vx <= gx + gw // 2 <= vx + vw and vy <= gy <= vy + vh - 40):
            return None
        return geo

    def _current_state(self):
        """Everything "Salvar" would write, to tell whether something changed."""
        try:
            settings = self.collect()
        except Exception:
            return None
        return (settings, [(r.heard, r.write) for r in self.dictionary.replacements],
                [(sn.trigger, sn.text) for sn in self.dictionary.snippets],
                {k: v.get().strip() for k, v in self.v_keys.items()}, self.v_enabled.get(), self.v_autostart.get())

    def dirty(self) -> bool:
        if not self._built:   # closed within the first second, pages still being built: nothing to compare
            return False
        state = self._current_state()
        if state is None:   # a field is invalid right now (or was already at open): never close silently
            return True
        return state != self._snapshot

    def _wheel(self, e) -> None:
        page = self.pages.get(self.current)
        if page is None:
            return
        under, outer = str(self.top.tk.call("winfo", "containing", e.x_root, e.y_root)), str(page.outer)
        if under == outer or under.startswith(outer + "."):   # only over the page, not the sidebar
            page.wheel(e)

    def _watch_changes(self) -> None:
        if not self.top.winfo_exists():
            return
        if self.active_field is None:  # not while a hotkey is being recorded
            self.l_dirty.configure(text=_t("●  Alterações não salvas") if self.dirty() else "")
        self.top.after(800, self._watch_changes)

    def px(self, n: float) -> int:
        return int(round(n * self.scale))

    def _styles(self) -> None:
        st = ttk.Style(self.top)
        bg = self.pal.bg  # sv-ttk cards are outlined: their inside is the page color
        st.configure("Muted.TLabel", foreground=self.pal.muted)
        st.configure("Ok.TLabel", foreground=self.pal.ok)
        st.configure("Card.TLabel", background=bg)
        st.configure("Inner.TFrame", background=bg)   # a group inside a card: no outline of its own
        st.configure("CardMuted.TLabel", background=bg, foreground=self.pal.muted, font="SunValleyCaptionFont")
        st.configure("CardStrong.TLabel", background=bg, font="SunValleyBodyStrongFont")
        st.configure("CardOk.TLabel", background=bg, foreground=self.pal.ok)
        st.configure("CardError.TLabel", background=bg, foreground=self.pal.error, font="SunValleyCaptionFont")
        st.configure("Error.TLabel", foreground=self.pal.error)
        st.configure("Card.TCheckbutton", background=bg)
        st.configure("Card.TRadiobutton", background=bg)
        st.configure("Keycap.TLabel", background=bg, font="SunValleyBodyLargeFont")
        st.configure("Accent.TLabel", foreground=self.pal.accent, font="SunValleyCaptionFont")
        st.configure("Metric.TLabel", background=bg, font="SunValleySubtitleFont")
        st.configure("Treeview", rowheight=self.px(28))
        st.configure("Treeview.Heading", font="SunValleyCaptionFont")

    @staticmethod
    def _dark_titlebar(window: tk.Toplevel, dark: bool) -> None:
        try:
            hwnd = int(window.wm_frame(), 16)
            value = ctypes.c_int(1 if dark else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 20,
                                                      ctypes.byref(value), ctypes.sizeof(value))
        except Exception:
            pass

    def _text(self, parent, value: str, height: int, width: int = 60) -> tk.Text:
        t = tk.Text(parent, height=height, width=width, wrap="word", relief="flat", bd=0,
                    bg=self.pal.field, fg=self.pal.text, insertbackground=self.pal.text,
                    selectbackground=self.pal.accent, highlightthickness=1,
                    highlightbackground=self.pal.border, highlightcolor=self.pal.accent,
                    padx=self.px(8), pady=self.px(6), font="SunValleyBodyFont")
        t.insert("1.0", value)
        return t

    def _spin(self, parent, var: tk.StringVar, lo: float, hi: float, step: float, width: int = 8):
        return ttk.Spinbox(parent, from_=lo, to=hi, increment=step, textvariable=var, width=width)

    def _background(self, work, done, tick=None, every: int = 150) -> threading.Thread:
        """`work` on a thread; on the Tk thread, `tick()` every `every` ms while it runs and `done()`
        once it ended. Never after the window is gone: a label it would update no longer exists."""
        thread = threading.Thread(target=work, name="settings-work", daemon=True)
        thread.start()

        def poll() -> None:
            if not self.top.winfo_exists():
                return
            alive = thread.is_alive()   # read first, so what `work` wrote just before ending is shown
            if tick:
                tick()
            if alive:
                self.top.after(every, poll)
            else:
                done()

        poll()
        return thread

    def _tree(self, parent, columns: list[tuple[str, str, int]], height: int = 8) -> ttk.Treeview:
        frame = ttk.Frame(parent, style="Card.TFrame")
        tree = ttk.Treeview(frame, columns=[c for c, _, _ in columns], show="headings", height=height,
                            selectmode="browse")
        for key, title, width in columns:
            tree.heading(key, text=title, anchor="w")
            tree.column(key, width=self.px(width), anchor="w", stretch=key == columns[-1][0])
        bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        tree.tag_configure("odd", background=self.pal.zebra)
        tree.frame = frame
        tree.empty = ttk.Label(frame, text="", style="Muted.TLabel", justify="center")
        return tree

    @staticmethod
    def _rows(tree: ttk.Treeview, rows, empty: str) -> None:
        """Fill a table (zebra striped); show `empty` in it when there is nothing to list."""
        tree.delete(*tree.get_children())
        for i, (iid, values) in enumerate(rows):
            tree.insert("", "end", iid=iid, values=values, tags=("odd",) if i % 2 else ())
        if tree.get_children():
            tree.empty.place_forget()
        else:
            tree.empty.configure(text=empty)
            tree.empty.place(relx=0.5, rely=0.55, anchor="center")

    # --- layout ---------------------------------------------------------------------

    def _build(self) -> None:
        px = self.px
        footer = ttk.Frame(self.top, padding=(px(20), px(12)))
        footer.pack(fill="x", side="bottom")
        ttk.Button(footer, text=_t("Abrir arquivo avançado…"), command=self.app.open_settings_file).pack(side="left")
        ttk.Label(footer, text=_t("Ctrl+S salva · Esc fecha · Ctrl+PgDn troca de página"), style="Muted.TLabel",
                  font="SunValleyCaptionFont").pack(side="left", padx=px(14))
        ttk.Button(footer, text=_t("Salvar"), style="Accent.TButton", width=12, command=self.save).pack(side="right")
        ttk.Button(footer, text=_t("Cancelar"), width=12, command=self.cancel).pack(side="right", padx=(0, px(8)))
        self.status = ttk.Label(footer, text="", style="Error.TLabel")
        self.status.pack(side="right", padx=px(12))
        self.l_dirty = ttk.Label(footer, text="", style="Accent.TLabel")
        self.l_dirty.pack(side="right", padx=(0, px(4)))
        ttk.Separator(self.top).pack(fill="x", side="bottom")

        body = ttk.Frame(self.top)
        body.pack(fill="both", expand=True)
        self._sidebar(body)
        self.content = ttk.Frame(body)
        self.content.pack(side="left", fill="both", expand=True)

        self._pending = list(NAV_KEYS)
        self.show(self._initial_page())
        for seq, step in (("<Control-Next>", 1), ("<Control-Prior>", -1), ("<Control-Tab>", 1),
                          ("<Control-Shift-Tab>", -1)):
            self.top.bind(seq, lambda _e, d=step: self._nav_step(d))
        self.top.bind("<Control-f>", lambda _e: self._find_in_history())

    def _sidebar(self, parent) -> None:
        px, pal = self.px, self.pal
        side = tk.Frame(parent, bg=pal.bg, width=px(220))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        head = tk.Frame(side, bg=pal.bg)
        head.pack(fill="x", padx=px(18), pady=(px(18), px(14)))
        tk.Label(head, image=self._logo_small, bg=pal.bg).pack(anchor="w")
        tk.Label(head, text=_t("Ditado por voz"), bg=pal.bg, fg=pal.muted, font="SunValleyCaptionFont").pack(
            anchor="w", pady=(px(4), 0))

        for key, label, glyph in NAV:
            if key in NAV_SECTIONS:
                tk.Label(side, text=_t(NAV_SECTIONS[key]).upper(), bg=pal.bg, fg=pal.muted, font="SunValleyCaptionFont",
                         anchor="w").pack(fill="x", padx=px(22), pady=(px(10) if key != NAV[0][0] else 0, px(2)))
            row = tk.Frame(side, bg=pal.bg, cursor="hand2")
            row.pack(fill="x", padx=px(8), pady=0)
            bar = tk.Frame(row, bg=pal.bg, width=px(3), height=px(18))
            bar.pack(side="left", padx=(px(2), px(8)))
            widgets = [row]
            if self.icon_font:
                ico = tk.Label(row, text=glyph, bg=pal.bg, fg=pal.text, font=(self.icon_font, 11))
                ico.pack(side="left", padx=(0, px(10)), pady=px(5))
                widgets.append(ico)
            txt = tk.Label(row, text=_t(label), bg=pal.bg, fg=pal.text, font="SunValleyBodyFont", anchor="w")
            txt.pack(side="left", fill="x", expand=True, pady=px(5))
            widgets.append(txt)
            if key == "voice":   # lit while the app is listening for the wake phrase
                self.voice_dot = tk.Label(row, text="", bg=pal.bg, fg=pal.ok, font="SunValleyCaptionFont")
                self.voice_dot.pack(side="right", padx=(0, px(10)))
                widgets.append(self.voice_dot)
            self.nav_items[key] = (widgets, bar)
            for w in widgets:
                w.bind("<Button-1>", lambda _e, k=key: self._nav_to(k))
                w.bind("<Enter>", lambda _e, k=key: self._nav_paint(k, hover=True))
                w.bind("<Leave>", lambda _e, k=key: self._nav_paint(k, hover=False))
        from . import __version__

        tk.Label(side, text=_t('versão {0}').format(__version__), bg=pal.bg, fg=pal.muted, font="SunValleyCaptionFont",
                 anchor="w").pack(side="bottom", fill="x", padx=px(22), pady=(px(6), px(14)))
        tk.Frame(parent, bg=pal.border, width=1).pack(side="left", fill="y")

    def _build_to(self, key: str | None = None) -> None:
        """Build the pages in NAV order until `key` exists (None: all of them)."""
        while self._pending and (key is None or self.pages.get(key) is None):
            getattr(self, f"_page_{self._pending.pop(0)}")()

    def _build_next(self) -> None:
        """One more page per idle moment when lazy (all at once otherwise); then watch for changes."""
        if not self.top.winfo_exists():
            return
        if self._lazy and self._pending:
            self._build_to(self._pending[0])
            self.top.after(1, self._build_next)
            return
        self._build_to()
        self._snapshot = self._current_state()
        self._built = True
        self.top.after(800, self._watch_changes)

    def _nav_to(self, key: str) -> None:
        self.show(key)
        self.pages[key].canvas.yview_moveto(0)

    def _nav_step(self, step: int) -> str:
        keys = [k for k, _, _ in NAV]
        self._nav_to(keys[(keys.index(self.current) + step) % len(keys)])
        return "break"

    def _find_in_history(self) -> str:
        self._nav_to("history")
        if hasattr(self, "e_search"):
            self.e_search.focus_set()
            self.e_search.select_range(0, "end")
        return "break"

    def _initial_page(self) -> str:
        page = config.load_state(self.paths).get("settings_page", "general")
        return page if page in self.pages else "general"

    def _nav_paint(self, key: str, hover: bool = False) -> None:
        widgets, bar = self.nav_items[key]
        selected = key == self.current
        bg = self.pal.nav_hover if (selected or hover) else self.pal.bg
        for w in widgets:
            w.configure(bg=bg)
        bar.configure(bg=self.pal.accent if selected else bg)

    def show(self, key: str) -> None:
        self._build_to(key)
        if self.current:
            self.pages[self.current].outer.pack_forget()
        previous, self.current = self.current, key
        self.pages[key].outer.pack(fill="both", expand=True)
        for k in (previous, key):
            if k:
                self._nav_paint(k)
        if key == "history":
            self._history_refresh()

    # --- pages ------------------------------------------------------------------------

    def _page_general(self) -> None:
        s = self.s
        p = Page(self, "general", _t("Geral"), _t("Ligue ou desligue o ditado e ajuste como ele aparece e soa."))
        count, words = self.stats.today()
        total = self.stats.read()
        saved = total["saved_minutes"]
        c = p.card()
        metrics = ttk.Frame(c.frame, style="Inner.TFrame")
        metrics.grid(row=0, column=0, columnspan=2, sticky="ew")
        for col, (value, caption) in enumerate([
                (str(count), _t("ditados hoje")), (str(words), _t("palavras hoje")),
                (f"{saved:.0f} min" if saved < 120 else f"{saved / 60:.1f} h", _t("economizados ao todo"))]):
            metrics.columnconfigure(col, weight=1, uniform="metric")
            box = tk.Frame(metrics, bg=self.pal.bg)
            box.grid(row=0, column=col, sticky="w")
            ttk.Label(box, text=value, style="Metric.TLabel").pack(anchor="w")
            ttk.Label(box, text=caption, style="CardMuted.TLabel").pack(anchor="w")
        c.row = 1
        # the saved choice, not the live state (a 1-hour pause is not "off")
        self.v_enabled = tk.BooleanVar(value=getattr(self.app, "state", {}).get("enabled", self.app.enabled))
        self.v_autostart = tk.BooleanVar(value=autostart.is_enabled())
        self._enabled_at_open, self._autostart_at_open = self.v_enabled.get(), self.v_autostart.get()
        c = p.card(_t("Funcionamento"))
        c.switch(_t("Ditado ativado"), self.v_enabled, _t("Desligado, os atalhos voltam a funcionar como teclas normais."))
        c.divider()
        c.switch(_t("Iniciar com o Windows"), self.v_autostart, _t("Abre o Bira Flow na bandeja ao entrar na sua conta."))

        c = p.card(_t("Pílula flutuante"))
        self.v_show_pill = tk.BooleanVar(value=s.ui.show_pill)
        c.switch(_t("Mostrar a pílula durante a gravação"), self.v_show_pill,
                 _t("Mostra o modo (Ditando, Mãos livres, Comando), o tempo e o nível do microfone. Nunca rouba o foco."))
        self.c_position = Choice(c.frame, PILL_POSITIONS, s.ui.position, width=30)
        c.field(_t("Posição"), lambda _: self.c_position.widget)
        self.c_pill_size = Choice(c.frame, PILL_SIZES, s.ui.pill_size, width=30)
        c.field(_t("Tamanho"), lambda _: self.c_pill_size.widget)

        def preview_button(parent):
            return ttk.Button(parent, text=_t("Ver como fica"), command=self._pill_preview)

        c.field("", preview_button)
        c.divider()
        self.v_preview = tk.BooleanVar(value=s.ui.show_preview)
        c.switch(_t("Mostrar o texto inserido"), self.v_preview,
                 _t("Depois de colar, a pílula mostra o começo do texto por alguns segundos, para você conferir."))

        c = p.card(_t("Som"))
        self.v_sounds = tk.BooleanVar(value=s.ui.sounds)
        self.v_mute = tk.BooleanVar(value=s.ui.mute_media)
        c.switch(_t("Sons de início, fim e cancelamento"), self.v_sounds, _t("Toques curtos e baixos para confirmar sem olhar."))
        c.divider()
        c.switch(_t("Silenciar música e outros apps durante o ditado"), self.v_mute,
                 _t("Só os apps que estão tocando são silenciados, e o som volta assim que a gravação termina."))

        c = p.card(_t("Como usar"))
        self.howto = {}
        for key, desc in [
            ("hold", _t("Grava enquanto estiver pressionado; ao soltar, transcreve e cola onde está o cursor.")),
            ("double", _t("Modo mãos livres: grava sem segurar. Um novo toque encerra (parada automática em 5 min).")),
            ("command", _t("Modo comando: selecione um texto e diga o que fazer (\"deixe mais formal\", \"traduza\").")),
            ("paste", _t("Cola de novo o último ditado.")),
            ("esc", _t("Cancela a gravação ou a transcrição em andamento.")),
        ]:
            k = ttk.Label(c.frame, text="", style="CardStrong.TLabel")
            k.grid(row=c.row, column=0, sticky="nw", padx=(0, self.px(20)), pady=self.px(5))
            ttk.Label(c.frame, text=desc, style="Card.TLabel", wraplength=self.px(440), justify="left").grid(
                row=c.row, column=1, sticky="w", pady=self.px(5))
            c.row += 1
            self.howto[key] = k
        self.howto["double"].configure(text=_t("Toque duplo"))
        self.howto["esc"].configure(text=_t("Esc"))

        c = p.card(_t("Estatísticas"))
        c.note(self.stats.summary())

        c = p.card(_t("Idioma"))
        self.c_language = Choice(c.frame, LANGUAGE_OPTIONS, s.ui.language, width=34)
        c.field(_t("Idioma da interface"), lambda _: self.c_language.widget,
                _t("Muda os textos do programa. As traduções ficam em biraflow/lang; "
                   "faltando alguma, aparece o texto original em português."))

    def _pill_preview(self) -> None:
        """Show the pill with the chosen position and size, with a simulated voice."""
        pill = getattr(getattr(self.app, "ui", None), "pill", None)
        if pill is None:
            return

        pill.hide()
        if self._preview_saved is None:   # the real meter and placement, saved only once
            self._preview_saved = (pill._level, pill.position, pill.size)
        if self._placement_saved is None:
            self._placement_saved = (pill.position, pill.size)
        pill.position, pill.size = self.c_position.get(), self.c_pill_size.get()
        t0 = time.monotonic()
        pill._level = lambda: 0.5 + 0.45 * math.sin((time.monotonic() - t0) * 7)
        from .statemachine import Mode

        pill.recording(Mode.HOLD, t0, t0 + 300)
        if self._preview_job is not None:
            self.root.after_cancel(self._preview_job)

        def back():
            self._preview_job = None
            self._end_preview(show_text=True)

        # on the root, not on this window: it must run even if the window closes meanwhile
        self._preview_job = self.root.after(2500, back)

    def _end_preview(self, show_text: bool = False) -> None:
        pill = getattr(getattr(self.app, "ui", None), "pill", None)
        if pill is None or self._preview_saved is None:
            return
        pill._level = self._preview_saved[0]
        self._preview_saved = None
        if show_text:
            pill.preview(_t("Assim fica o texto inserido depois de cada ditado."))
        else:
            pill.hide()

    def _update_howto(self) -> None:
        if not hasattr(self, "howto"):
            return

        def shown(field: HotkeyField) -> str:
            spec = field.var.get().strip()
            if not spec:
                return "Desativado"
            try:
                parse_hotkey(spec)
                return display_hotkey(spec)
            except HotkeyError:
                return spec

        self.howto["hold"].configure(text=_t('Segure {0}').format(shown(self.f_hotkey)))
        self.howto["command"].configure(text=shown(self.f_command))
        self.howto["paste"].configure(text=shown(self.f_paste))

    def _page_hotkey(self) -> None:
        hk = self.s.hotkey
        p = Page(self, "hotkey", _t("Atalhos"), _t("Clique em Gravar e pressione a combinação. A tecla Fn não chega ao Windows."))
        c = p.card(_t("Ditado"))
        self.f_hotkey = HotkeyField(self, c, _t("Segurar para falar"), hk.keys,
                                    _t("Segure para ditar; toque duas vezes para o modo mãos livres."))
        c = p.card(_t("Modo comando"))
        self.f_command = HotkeyField(self, c, _t("Editar por voz"), hk.command_keys,
                                     _t("Selecione um texto, segure e fale a instrução. Sem seleção, escreve o "
                                     "que você pedir. Precisa de um provedor de LLM."), optional=True)
        c = p.card(_t("Colar último ditado"))
        self.f_paste = HotkeyField(self, c, _t("Colar de novo"), hk.paste_last_keys,
                                   _t("Insere novamente o último texto ditado onde está o cursor."), optional=True)
        for f in (self.f_hotkey, self.f_command, self.f_paste):
            f.var.trace_add("write", lambda *_: self._update_howto())
        self._update_howto()

        c = p.card(_t("Tempos"))
        self.v_tap = tk.StringVar(value=str(hk.tap_max_ms))
        self.v_double = tk.StringVar(value=str(hk.double_tap_ms))
        c.field(_t("Duração de um toque (ms)"), lambda f: self._spin(f, self.v_tap, 100, 600, 10),
                _t("Apertar e soltar mais rápido que isto conta como toque, não como segurar."))
        c.field(_t("Intervalo do toque duplo (ms)"), lambda f: self._spin(f, self.v_double, 200, 1000, 25),
                _t("Tempo máximo entre os dois toques que ativam o modo mãos livres."))

        c = p.card(_t("Apps ignorados"))
        self.v_ignore = tk.StringVar(value=", ".join(hk.ignore_apps))
        c.field(_t("Não ditar nestes apps"), lambda f: ttk.Entry(f, textvariable=self.v_ignore, width=50),
                _t("Nomes do .exe separados por vírgula (ex.: valorant.exe, cs2.exe). Com um deles em foco, "
                "o atalho não faz nada, para não atrapalhar jogos e programas que usam a mesma tecla."))

        c = p.card(_t("Comportamento"))
        self.v_suppress = tk.BooleanVar(value=hk.suppress_keys)
        c.switch(_t("Bloquear teclas comuns dos atalhos"), self.v_suppress,
                 _t("Ex.: com CapsLock como atalho, ele não alterna maiúsculas; o Z de Shift+Alt+Z não é digitado. "
                 "Ctrl, Shift, Alt, Win e Esc nunca são bloqueados."))

    def _page_voice(self) -> None:
        from . import wakeword

        w = self.s.wake
        p = Page(self, "voice", _t("Ativação por voz"),
                 _t("Diga a frase de ativação e o ditado começa, sem tocar no teclado, como na Alexa."))

        # live status first: is it listening, how loud is the mic, what did it hear
        c = p.card()

        def status(parent):
            f = ttk.Frame(parent, style="Inner.TFrame")
            self.l_wake_dot = tk.Label(f, text=_t("●"), bg=self.pal.bg, fg=self.pal.muted, font="SunValleyBodyFont")
            self.l_wake_dot.pack(side="left")
            self.l_wake_state = ttk.Label(f, text="", style="CardStrong.TLabel")
            self.l_wake_state.pack(side="left", padx=(self.px(6), self.px(18)))
            self.pb_wake_level = ttk.Progressbar(f, length=self.px(180), mode="determinate", maximum=100)
            self.pb_wake_level.pack(side="left")
            return f

        c.full(status, sticky="w")
        self.l_wake_live = c.note("")

        c = p.card(_t("Frase de ativação"))
        self.v_wake = tk.BooleanVar(value=w.enabled)
        c.switch(_t("Ativar ditado por voz"), self.v_wake,
                 _t("Com isto ligado, o microfone fica aberto e o ícone de microfone do Windows aparece aceso. "
                 "Um detector local, no seu PC, procura só a frase. Nada é gravado ou enviado até ele ouvi-la."))
        self.v_wake_phrase = tk.StringVar(value=w.phrase)
        c.field(_t("Frase"), lambda f: ttk.Entry(f, textvariable=self.v_wake_phrase, width=28))
        self.l_wake_phrase = c._help("")
        self.v_wake_phrase.trace_add("write", lambda *_: self._wake_phrase_hint())
        self._wake_phrase_hint()
        self.v_wake_silence = tk.StringVar(value=f"{w.end_silence:g}")
        c.field(_t("Encerrar após silêncio de (s)"), lambda f: self._spin(f, self.v_wake_silence, 0.8, 5, 0.1),
                _t("Quando você para de falar por esse tempo, o ditado termina e o texto é colado. "
                "Também termina com Esc (cancela) ou com um toque no atalho."))
        self.v_wake_confirm = tk.BooleanVar(value=w.confirm)
        c.switch(_t("Confirmar a frase com o Whisper (recomendado)"), self.v_wake_confirm,
                 _t("Quando o detector ouve algo parecido com a frase, só esse trecho (~2,5 s) é conferido pelo "
                 "Whisper, e assim \"ei, vira aqui\" não dispara. Com o modelo local instalado (aba Transcrição), "
                 "a conferência roda no seu PC e o áudio do ambiente nunca sai dele; sem ele, usa a API. "
                 "Desligado, decide só o detector local, que dispara por engano mais vezes."))

        c = p.card(_t("Modelo de voz"))
        self.l_wake_model = c.note("")

        def actions(parent):
            f = ttk.Frame(parent, style="Inner.TFrame")
            self.b_wake_model = ttk.Button(f, text=_t('Baixar modelo de voz ({0} MB)').format(wakeword.MODEL_MB),
                                           command=self._wake_download)
            self.b_wake_model.pack(side="left")
            # shown only while downloading
            self.pb_wake = ttk.Progressbar(f, length=self.px(220), mode="determinate")
            return f

        c.full(actions, sticky="w")
        self._wake_model_label()
        if _WAKE_DOWNLOAD is not None and not _WAKE_DOWNLOAD["finished"]:
            self._wake_download()   # still downloading from a window closed earlier: show it, never a second one
        self._wake_live()

    def _wake_phrase_hint(self) -> None:
        words = self.v_wake_phrase.get().split()
        letters = sum(len(x) for x in words)
        if letters < 4:
            text, style = "Escreva a frase com pelo menos 4 letras (ex.: Ei Bira).", "CardError.TLabel"
        elif len(words) == 1:
            text, style = ("Uma palavra só dispara por engano mais vezes. Prefira duas: \"Ei Bira\", "
                           "\"Ok Bira\"."), "CardError.TLabel"
        else:
            text, style = ("Duas palavras funcionam melhor: \"Ei Bira\", \"Ok Bira\", \"Oi Flow\". Evite palavras "
                           "comuns na conversa."), "CardMuted.TLabel"
        self.l_wake_phrase.configure(text=text, style=style)

    def _wake_model_label(self) -> None:
        from . import wakeword

        if wakeword.model_installed(self.paths):
            self.l_wake_model.configure(text=_t("Modelo de voz em português instalado ✓ (Vosk, roda só no seu PC)."),
                                        style="CardOk.TLabel")
            self.b_wake_model.configure(text=_t("Baixar de novo"))
        else:
            self.l_wake_model.configure(text=_t('Falta baixar o modelo de voz em português ({0} MB, uma vez só).').format(wakeword.MODEL_MB), style="CardError.TLabel")

    def _wake_download(self) -> None:
        global _WAKE_DOWNLOAD

        from . import wakeword

        if _WAKE_DOWNLOAD is None or _WAKE_DOWNLOAD["finished"]:
            state = _WAKE_DOWNLOAD = {"done": 0, "total": 0, "error": None, "finished": False}
            controller = self.app.controller

            def progress(label, done, total):
                state.update(done=done, total=total)

            def work():
                try:
                    wakeword.download_model(self.paths, progress, threading.Event())
                except Exception as e:
                    state["error"] = str(e)
                state["finished"] = True
                if state["error"] is None:
                    controller.post("reload")   # start listening if it is switched on, window open or not

            threading.Thread(target=work, name="wake-model", daemon=True).start()
        self.b_wake_model.state(["disabled"])
        self.pb_wake.pack(side="left", padx=(self.px(12), 0))
        self._wake_poll(_WAKE_DOWNLOAD)

    def _wake_poll(self, state: dict) -> None:
        if not self.top.winfo_exists():
            return
        if state["total"]:
            self.pb_wake.configure(value=state["done"] * 100 / state["total"])
        if not state["finished"]:
            self.l_wake_model.configure(text=_t('Baixando… {0:.0f} MB').format(state['done'] / 1e6), style="CardMuted.TLabel")
            self.top.after(150, self._wake_poll, state)
            return
        self.pb_wake.configure(value=0)
        self.pb_wake.pack_forget()
        self.b_wake_model.state(["!disabled"])
        if state["error"]:
            self.l_wake_model.configure(text=_t('Falhou: {0}').format(state['error']), style="CardError.TLabel")
        else:
            self._wake_model_label()

    def _wake_live(self) -> None:
        """Every half second: the sidebar dot, and on this page what the listener is doing."""
        if not self.top.winfo_exists():
            return
        try:
            state, heard = self.app.controller.wake_status()
            listener = getattr(self.app.controller, "wake", None)
            level = getattr(listener, "level", 0.0) if listener is not None else 0.0
        except Exception:
            state, heard, level = "indisponível", "", 0.0
        listening = state == "ouvindo"
        if hasattr(self, "voice_dot"):
            self.voice_dot.configure(text=_t("●") if listening else "")
        if self.current == "voice":
            self.l_wake_dot.configure(fg=self.pal.ok if listening else self.pal.muted)
            self.l_wake_state.configure(text=(_t("Ouvindo") if listening else state[:1].upper() + state[1:]))
            self.pb_wake_level.configure(value=level * 100 if listening else 0)
            if listening:
                text = (f"Ouviu por último: \"{heard}\"" if heard else "Diga a frase perto do microfone.")
                text += "  ·  Ao confirmar, a pílula aparece e o ditado começa."
            else:
                text = "Ligue a ativação, baixe o modelo de voz e clique em Salvar para começar a ouvir."
            self.l_wake_live.configure(text=text)
        self.top.after(250 if self.current == "voice" else 500, self._wake_live)

    def _page_mic(self) -> None:
        r = self.s.recording
        p = Page(self, "mic", _t("Microfone"), _t("Qual microfone usar e quando uma gravação é descartada."))
        try:
            from .audio import list_input_devices
            devices = list_input_devices()
        except Exception:
            devices = []
        c = p.card()
        values = [DEFAULT_MIC] + devices
        current = r.input_device or DEFAULT_MIC
        if current not in values:
            values.append(current)
        self.v_device = tk.StringVar(value=current)
        c.field(_t("Microfone"), lambda f: ttk.Combobox(f, textvariable=self.v_device, values=values, width=50),
                _t("Use o padrão do Windows ou escolha um dispositivo (também aceita parte do nome, ex.: USB)."), sticky="ew")

        def tester(parent):
            f = ttk.Frame(parent, style="Inner.TFrame")
            self.b_mic = ttk.Button(f, text=_t("Testar microfone"), command=self.test_mic)
            self.b_mic.pack(side="left")
            self.meter = LevelMeter(f, self.pal, self.px)
            self.meter.canvas.pack(side="left", padx=self.px(14))
            return f

        c.field(_t("Teste"), tester)
        self.l_mic = c.note(_t("Fale algo por 3 segundos depois de clicar."))
        if win32.microphone_blocked():
            c.note(win32.MIC_BLOCKED).configure(style="CardError.TLabel")
            c.full(lambda f: ttk.Button(f, text=_t("Abrir privacidade do microfone"),
                                        command=lambda: os.startfile("ms-settings:privacy-microphone")), sticky="w")
        c.divider()
        self.v_normalize = tk.BooleanVar(value=r.normalize)
        c.switch(_t("Aumentar o volume de falas baixas"), self.v_normalize,
                 _t("Ajuda quando você fala baixinho ou longe do microfone. Nunca reduz o volume."))
        c.divider()
        self.v_ready = tk.BooleanVar(value=r.keep_mic_ready > 0)
        c.switch(_t("Manter o microfone pronto por 30 s depois de ditar"), self.v_ready,
                 _t("O próximo ditado começa na hora e não corta a primeira sílaba. Enquanto isso, o ícone de "
                 "microfone do Windows fica aceso (nada é gravado nem enviado fora de um ditado)."))

        c = p.card(_t("Limites"))
        self.v_max = tk.StringVar(value=str(r.max_seconds))
        self.v_min = tk.StringVar(value=str(r.min_seconds))
        self.v_silence = tk.StringVar(value=str(r.silence_threshold))
        c.field(_t("Parada automática (s)"), lambda f: self._spin(f, self.v_max, 10, 1800, 10),
                _t("Encerra qualquer gravação, inclusive no mãos livres, depois deste tempo."))
        c.field(_t("Gravação mínima (s)"), lambda f: self._spin(f, self.v_min, 0.1, 3, 0.1),
                _t("Gravações mais curtas são descartadas."))
        c.field(_t("Limite de silêncio"), lambda f: self._spin(f, self.v_silence, 0, 0.05, 0.001),
                _t("Abaixo deste volume a gravação conta como silêncio e não é transcrita. 0 desliga."))
        self.v_hf_silence = tk.StringVar(value=str(r.handsfree_silence_stop))
        c.field(_t("Mãos livres: parar após silêncio (s)"), lambda f: self._spin(f, self.v_hf_silence, 0, 600, 10),
                _t("Se você esquecer o modo mãos livres ligado, ele para e transcreve sozinho. 0 desliga."))

    def test_mic(self) -> None:
        from .audio import Recorder

        device = self.v_device.get().strip()
        rec = Recorder("" if device == DEFAULT_MIC else device)
        try:
            rec.start()
        except Exception as e:
            self.l_mic.configure(text=_t('Não foi possível abrir o microfone: {0}').format(e), style="CardError.TLabel")
            return
        self.b_mic.state(["disabled"])
        self.l_mic.configure(text=_t("Ouvindo… fale algo."), style="CardMuted.TLabel")
        self.mic_test = (rec, time.monotonic() + 3)
        self._poll_mic()

    def _poll_mic(self) -> None:
        from .audio import peak_rms

        if self.mic_test is None:
            return
        rec, end = self.mic_test
        if time.monotonic() < end:
            self.meter.set(rec.level)
            self.top.after(50, self._poll_mic)
            return
        audio = rec.stop()
        self.mic_test = None
        self.meter.set(0)
        self.b_mic.state(["!disabled"])
        peak = peak_rms(audio)
        try:
            threshold = float(self.v_silence.get().replace(",", "."))
        except ValueError:
            threshold = self.s.recording.silence_threshold
        if peak >= max(threshold * 3, 0.01):
            self.l_mic.configure(text=_t('Nível de pico {0:.3f}: ótimo, fala detectada ✓').format(peak), style="CardOk.TLabel")
        elif peak >= threshold:
            self.l_mic.configure(text=_t('Nível de pico {0:.3f}: baixo, mas acima do limite ({1}).').format(peak, threshold),
                                 style="CardMuted.TLabel")
        elif peak == 0 and win32.microphone_blocked():
            self.l_mic.configure(text=_t("Silêncio total. ") + win32.MIC_BLOCKED, style="CardError.TLabel")
        else:
            self.l_mic.configure(text=_t('Nível de pico {0:.3f}: abaixo do limite de silêncio ({1}). Verifique o microfone, o volume ou a permissão de microfone do Windows.').format(peak, threshold),
                                 style="CardError.TLabel")

    def _page_transcription(self) -> None:
        t = self.s.transcription
        p = Page(self, "transcription", _t("Transcrição"), _t("Como a fala vira texto."))
        local_ok = find_whisper_cli(t, self.paths) is not None
        c = p.card(_t("Motor"))
        self.v_engine = tk.StringVar(value=t.engine)
        for code, name, desc in ENGINES:
            ttk.Radiobutton(c.frame, text=_t(name), value=code, variable=self.v_engine,
                            style="Card.TRadiobutton").grid(row=c.row, column=0, sticky="w", pady=(self.px(6), 0))
            if code == "local":
                status = (_t("whisper.cpp instalado ✓") if local_ok
                          else _t("não instalado: rode scripts\\setup-whisper.ps1"))
            elif code in API:
                status = (_t("chave configurada ✓") if self.env_all.get(API[code][1])
                          else _t("sem chave (página Chaves de API)"))
            else:
                status = ""
            ttk.Label(c.frame, text=_t(desc) + (f"  ·  {status}" if status else ""), style="CardMuted.TLabel",
                      wraplength=self.px(460), justify="left").grid(row=c.row, column=1, sticky="w",
                                                                   pady=(self.px(6), 0))
            c.row += 1

        c = p.card(_t("Idioma e modelo local"))
        self.c_language = Choice(c.frame, LANGUAGES, t.language, width=40)
        c.field(_t("Idioma"), lambda _: self.c_language.widget,
                _t("Fixar o idioma deixa o modelo local cerca de 2 s mais rápido por ditado."))
        models = [(code, label + (" · " + _t("instalado ✓") if resolve_model(
            config.TranscriptionCfg(local_model=code), self.paths).is_file() else "")) for code, label in MODELS]
        self.c_model = Choice(c.frame, models, t.local_model, width=max(50, max(len(lbl) for _, lbl in models) + 2))
        c.field(_t("Modelo local"), lambda _: self.c_model.widget,
                "Para baixar outro: scripts\\setup-whisper.ps1 -Model medium (por exemplo).")
        self.v_fast = tk.BooleanVar(value=t.local_fast)
        self.v_keep = tk.BooleanVar(value=t.keep_loaded)
        self.v_fallback = tk.BooleanVar(value=t.fallback_to_local)
        c.switch(_t("Modo rápido"), self.v_fast,
                 _t("Decodificação gulosa e janela do tamanho da fala: ~2,5× mais rápido, mesmo texto em ditados."))
        c.switch(_t("Manter o modelo carregado na placa de vídeo"), self.v_keep,
                 _t("Com o whisper.cpp para NVIDIA, cada ditado leva ~0,3 s em vez de ~1–3 s. Usa ~500 MB de "
                 "memória de vídeo; quando ele é só a reserva da nuvem, sai da memória após 10 min sem uso. "
                 "Sem placa NVIDIA, não tem efeito."))
        c.switch(_t("Usar o modelo local se a API falhar"), self.v_fallback,
                 _t("Sem internet, a transcrição continua funcionando com o whisper.cpp."))

        c = p.card(_t("Instalação local"))
        self.l_local = c.note("")

        def build_choice(parent):
            self.has_gpu = setup_local.has_nvidia_gpu()   # cached: checked once, at app start
            self.c_build = Choice(parent, [(k, v[0]) for k, v in setup_local.BUILDS.items()],
                                  "cuda12" if self.has_gpu else "cpu", width=48)
            return self.c_build.widget

        c.field(_t("Pacote do whisper.cpp"), build_choice)

        def actions(parent):
            f = ttk.Frame(parent, style="Inner.TFrame")
            self.b_install = ttk.Button(f, text=_t("Instalar whisper.cpp"), command=lambda: self._setup("whisper"))
            self.b_install.pack(side="left")
            self.b_model = ttk.Button(f, text=_t("Baixar o modelo escolhido"), style="Accent.TButton",
                                      command=lambda: self._setup("model"))
            self.b_model.pack(side="left", padx=(self.px(8), 0))
            self.b_setup_cancel = ttk.Button(f, text=_t("Cancelar"), command=self._setup_cancel)
            return f

        c.full(actions, sticky="w")
        self.pb_setup = ttk.Progressbar(c.frame, maximum=100)
        c.full(lambda _: self.pb_setup)
        self.l_setup = c.note("Baixa direto do GitHub (ggml-org/whisper.cpp) e do Hugging Face (modelos Whisper).")
        self.setup_job = None
        self.c_model.widget.bind("<<ComboboxSelected>>", lambda _: self._local_status(), add="+")
        self._local_status()

        c = p.card(_t("Avançado"))
        self.v_groq_model = tk.StringVar(value=t.groq_model)
        self.v_openai_model = tk.StringVar(value=t.openai_model)
        self.v_gateway_model = tk.StringVar(value=t.gateway_model)
        self.v_api_timeout = tk.StringVar(value=str(t.timeout_seconds))
        self.v_threads = tk.StringVar(value=str(t.threads))
        self.v_cli = tk.StringVar(value=t.whisper_cli)
        c.field(_t("Modelo da Groq"), lambda f: ttk.Entry(f, textvariable=self.v_groq_model, width=30))
        c.field(_t("Modelo da OpenAI"), lambda f: ttk.Entry(f, textvariable=self.v_openai_model, width=30))
        c.field(_t("Modelo do Gateway"), lambda f: ttk.Entry(f, textvariable=self.v_gateway_model, width=30),
                "ex.: spacexai/grok-stt (padrão), openai/whisper-1. Se o time não liberar o modelo no "
                "AI Gateway, o app tenta o openai/whisper-1.")
        c.field(_t("Tempo limite da API (s)"), lambda f: self._spin(f, self.v_api_timeout, 5, 300, 5))
        c.field(_t("Threads da CPU"), lambda f: self._spin(f, self.v_threads, 0, 64, 1), _t("0 = automático."))
        c.field("whisper-cli.exe", lambda f: ttk.Entry(f, textvariable=self.v_cli, width=50),
                "Vazio = instalado pelo setup em %LOCALAPPDATA%\\BiraFlow\\whisper.", sticky="ew")

    def _local_status(self) -> None:
        t = copy.deepcopy(self.s.transcription)
        t.local_model = self.c_model.get()
        t.whisper_cli = self.v_cli.get().strip() if hasattr(self, "v_cli") else t.whisper_cli
        cli_ok = find_whisper_cli(t, self.paths) is not None
        model = resolve_model(t, self.paths)
        ok = cli_ok and model.is_file()
        build = setup_local.installed_build(self.paths)
        kind = {"cuda12": "placa NVIDIA", "blas": "CPU + OpenBLAS", "cpu": "CPU"}.get(build, "")
        text = (f"whisper.cpp ✓ ({kind})" if cli_ok else "whisper.cpp não instalado") + "  ·  " + (
            f"modelo {t.local_model} ✓" if model.is_file() else f"modelo {t.local_model} não baixado")
        if cli_ok and build != "cuda12" and getattr(self, "has_gpu", False):
            text += "  ·  Há uma placa NVIDIA: instale o pacote NVIDIA para ditados ~10× mais rápidos."
            ok = False
        self.l_local.configure(text=text, style="CardOk.TLabel" if ok else "CardError.TLabel")

    def _setup(self, what: str) -> None:
        if self.setup_job is not None:
            return

        cancel = threading.Event()
        state = {"label": "Conectando…", "done": 0, "total": 0, "result": None, "error": None}
        model = self.c_model.get()
        build = self.c_build.get()
        if what == "model" and model not in setup_local.MODEL_SIZES_MB:
            self.l_setup.configure(text=_t("Escolha um dos modelos da lista para baixar."), style="CardError.TLabel")
            return

        def progress(label, done, total):
            state.update(label=label, done=done, total=total)

        def work():
            try:
                if what == "whisper":
                    tag = setup_local.install_whisper(self.paths, build, progress, cancel)
                    state["result"] = f"whisper.cpp {tag} instalado ✓"
                else:
                    setup_local.download_model(self.paths, model, progress, cancel)
                    state["result"] = f"Modelo {model} baixado ✓"
            except setup_local.SetupCancelled:
                state["error"] = "Download cancelado."
            except Exception as e:  # network, disk, GitHub API limits
                state["error"] = f"Falhou: {e}"

        def tick():
            total, done = state["total"], state["done"]
            if total:
                self.pb_setup.configure(mode="determinate", value=done * 100 / total)
                self.l_setup.configure(text=_t('Baixando {0}: {1:.0f} de {2:.0f} MB').format(state['label'], done / 1e6, total / 1e6),
                                       style="CardMuted.TLabel")
            else:
                self.l_setup.configure(text=state["label"], style="CardMuted.TLabel")

        def finished():
            self.setup_job = None
            self.pb_setup.configure(value=0)
            self.b_setup_cancel.pack_forget()
            for b in (self.b_install, self.b_model):
                b.state(["!disabled"])
            self.l_setup.configure(text=state["error"] or state["result"],
                                   style="CardError.TLabel" if state["error"] else "CardOk.TLabel")
            self._local_status()

        self.setup_job = cancel   # "Cancelar" and close() set it
        for b in (self.b_install, self.b_model):
            b.state(["disabled"])
        self.b_setup_cancel.pack(side="left", padx=(self.px(8), 0))
        self._background(work, finished, tick)

    def _setup_cancel(self) -> None:
        if self.setup_job is not None:
            self.setup_job.set()

    def _page_cleanup(self) -> None:
        cl = self.s.cleanup
        p = Page(self, "cleanup", _t("Refinamento"),
                 _t("Um LLM remove vícios de fala, corrige pontuação e maiúsculas e aplica o estilo do app. "
                 "Nunca traduz nem responde ao que foi ditado. Se falhar, o texto é inserido como está."))
        c = p.card(_t("Limpeza local (funciona offline)"))
        self.v_local_rules = tk.BooleanVar(value=cl.local_rules)
        self.v_spoken = tk.BooleanVar(value=cl.spoken_punctuation)
        c.switch(_t("Remover hesitações e palavras repetidas"), self.v_local_rules,
                 _t("Tira \"hã\", \"hum\", \"uh\", repetições como \"eu eu\" e ajusta as maiúsculas, mesmo sem LLM."))
        c.switch(_t("Pontuação e comandos falados"), self.v_spoken,
                 "\"vírgula\", \"ponto final\", \"ponto de interrogação\", \"nova linha\", \"novo parágrafo\", "
                 "\"reticências\", \"travessão\", \"abre/fecha aspas, parênteses, colchetes\" viram pontuação; "
                 "\"fulano arroba gmail ponto com\" vira e-mail; \"emoji coração\" vira ❤️.")
        c.note(_t("Também valem sempre: terminar com \"cancelar ditado\" descarta o ditado; dizer só "
               "\"apagar último ditado\" apaga o que acabou de ser colado (se você não digitou nada depois)."))

        c = p.card(_t("LLM"))
        self.c_provider = Choice(c.frame, PROVIDER_CHOICES, cl.provider, width=48,
                                 on_change=lambda _: self._provider_changed())
        c.field(_t("Provedor"), lambda _: self.c_provider.widget)
        self.l_provider = c.note("")
        self.v_llm_model = tk.StringVar(value=cl.model)
        def model_box(f):
            self.cb_llm_model = ttk.Combobox(f, textvariable=self.v_llm_model, width=30)
            return self.cb_llm_model

        c.field(_t("Modelo"), model_box)
        self.l_llm_default = c.note("")
        self.v_llm_timeout = tk.StringVar(value=str(cl.timeout_seconds))
        c.field(_t("Tempo limite (s)"), lambda f: self._spin(f, self.v_llm_timeout, 2, 60, 1),
                _t("Se o LLM demorar mais que isto, o texto é inserido sem o refinamento."))

        c = p.card(_t("Instruções extras"))
        c.note(_t("Opcional. Ex.: \"Use português do Brasil.\" ou \"Escreva números por extenso.\""))
        self.t_extra = c.full(lambda f: self._text(f, cl.extra_instructions, 3))

        c = p.card(_t("Ollama"))
        self.v_ollama = tk.StringVar(value=cl.ollama_url)
        c.field(_t("Endereço"), lambda f: ttk.Entry(f, textvariable=self.v_ollama, width=50),
                _t("Só usado com o provedor Ollama, que roda modelos no seu computador."), sticky="ew")

    def _provider_changed(self) -> None:
        if not hasattr(self, "l_provider"):
            return
        code = self.c_provider.get()
        env = dict(self.env_all)
        for key, var in getattr(self, "v_keys", {}).items():
            if var.get().strip():
                env[key] = var.get().strip()
        used = resolve_provider(config.CleanupCfg(provider=code), env)
        names = dict(PROVIDER_CHOICES)
        if used == "none":
            msg = ("Refinamento por LLM desligado: só a limpeza local é aplicada." if code == "none" else
                   "Nenhuma chave encontrada: só a limpeza local é aplicada, e o modo comando fica indisponível.")
            self.l_provider.configure(text=msg, style="CardMuted.TLabel")
        else:
            self.l_provider.configure(text=_t('Em uso: {0} ✓').format(names.get(used, used)), style="CardOk.TLabel")
        default = PROVIDERS.get(used, {}).get("model", "")
        self.l_llm_default.configure(text=_t('Vazio = padrão do provedor ({0}).').format(default) if default else
                                     _t("Vazio = padrão do provedor."))
        if hasattr(self, "cb_llm_model"):
            self.cb_llm_model.configure(values=[""] + LLM_MODELS.get(used, []))

    def _page_styles(self) -> None:
        st = self.s.styles
        p = Page(self, "styles", _t("Estilos"),
                 _t("O tom do texto muda conforme o app em que você dita. Aplicado pelo LLM; sem LLM, só os "
                 "estilos \"Muito casual\" e \"Técnico\" têm efeito (tiram o ponto final de frases curtas)."))
        c = p.card()
        self.v_styles = tk.BooleanVar(value=st.enabled)
        c.switch(_t("Adaptar o estilo ao aplicativo"), self.v_styles)
        self.c_styles: dict[str, Choice] = {}
        options = [(code, STYLE_LABELS[code]) for code in STYLE_CHOICES]
        for key, name, apps in STYLE_CATEGORIES:
            c.divider()
            ch = self.c_styles[key] = Choice(c.frame, options, getattr(st, key), width=38)
            c.field(_t(name), lambda _, ch=ch: ch.widget, _t(apps))

    def _page_dictionary(self) -> None:
        p = Page(self, "dictionary", _t("Dicionário"),
                 _t("Ensine palavras ao reconhecimento e corrija erros que se repetem."))
        c = p.card(_t("Palavras"))
        c.note(_t("Nomes e termos que a transcrição deve acertar, um por linha (ex.: Kubernetes, Ubiratan)."))
        self.t_vocab = c.full(lambda f: self._text(f, "\n".join(self.s.transcription.vocabulary), 4))

        c = p.card(_t("Correções"))
        c.note(_t("Quando a transcrição escrever a palavra da esquerda, ela é trocada pela da direita. "
               "Palavra inteira, sem diferenciar maiúsculas. Ex.: ese → esse, cloud code → Claude Code."))
        self.v_heard = tk.StringVar()
        self.v_write = tk.StringVar()

        def form(parent):
            f = ttk.Frame(parent, style="Inner.TFrame")
            ttk.Label(f, text=_t("Quando ouvir"), style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")
            ttk.Label(f, text=_t("Escrever"), style="CardMuted.TLabel").grid(row=0, column=2, sticky="w")
            heard = ttk.Entry(f, textvariable=self.v_heard, width=22)
            heard.grid(row=1, column=0)
            tk.Label(f, text=_t("→"), bg=self.pal.bg, fg=self.pal.muted, font="SunValleyBodyFont").grid(
                row=1, column=1, padx=self.px(8))
            e = ttk.Entry(f, textvariable=self.v_write, width=22)
            e.grid(row=1, column=2)
            for entry in (heard, e):
                entry.bind("<Return>", lambda _: self._rep_add())
            ttk.Button(f, text=_t("Adicionar"), style="Accent.TButton", command=self._rep_add).grid(
                row=1, column=3, padx=(self.px(10), 0))
            self.b_rep_remove = ttk.Button(f, text=_t("Remover"), command=self._rep_remove)
            self.b_rep_remove.grid(row=1, column=4, padx=(self.px(6), 0))
            return f

        c.full(form, sticky="w")
        self.tree_rep = self._tree(c.frame, [("heard", "Quando ouvir", 220), ("write", _t("Escrever"), 300)], height=7)
        c.full(lambda _: self.tree_rep.frame)
        self.tree_rep.bind("<<TreeviewSelect>>", lambda _: self._rep_select())
        self.tree_rep.bind("<Delete>", lambda _: self._rep_remove())
        self._rep_refresh()

        c_shot = p.card(_t("Aprender com um print"))
        c_shot.note(_t("Tire um print onde aparece o jeito certo (Win+Shift+S): o texto que você corrigiu, uma "
                    "mensagem como \"é Taspio\" ou o nome escrito em outro lugar. Depois clique no botão ou "
                    "aperte Ctrl+V nesta página. Também dá pelo menu da bandeja."))
        c_shot.full(lambda f: ttk.Button(f, text=_t("Aprender com o print copiado"), style="Accent.TButton",
                                         command=self._rep_from_screenshot), sticky="w")
        self.l_shot = c_shot.note("")
        self.top.bind("<Control-v>", self._paste_screenshot, add="+")
        c.divider()
        self.v_learn = tk.BooleanVar(value=self.s.history.auto_learn)
        c.switch(_t("Aprender com as correções do Histórico"), self.v_learn,
                 _t("Ao corrigir um ditado na página Histórico, as palavras trocadas viram correções."))
        self.v_learn_edits = tk.BooleanVar(value=self.s.history.learn_from_edits)
        c.switch(_t("Aprender quando eu corrijo o texto onde ele foi colado"), self.v_learn_edits,
                 _t("Se você corrigir uma palavra logo depois de ditar (\"ese\" → \"esse\") no WhatsApp, "
                 "navegador, Word etc., a troca entra aqui sozinha e a pílula avisa. Corrija antes de enviar "
                 "a mensagem. O Bira Flow lê só o campo onde colou o texto, por até 5 minutos (pausa se você trocar de janela), nunca campos "
                 "de senha. Só ajustes de grafia viram regra; mudanças de conteúdo são ignoradas."))

        c = p.card(_t("Cópia de segurança"))
        c.note(_t("Leve suas palavras, correções e snippets para outro computador, ou guarde uma cópia."))

        def backup(parent):
            f = ttk.Frame(parent, style="Inner.TFrame")
            ttk.Button(f, text=_t("Exportar…"), command=self._dict_export).pack(side="left")
            ttk.Button(f, text=_t("Importar…"), command=self._dict_import).pack(side="left", padx=(self.px(8), 0))
            return f

        c.full(backup, sticky="w")
        self.l_backup = c.note("")

    def _rep_refresh(self) -> None:
        self._selection_buttons([b for b in [getattr(self, "b_rep_remove", None)] if b], False)
        self._rows(self.tree_rep, [(str(i), (r.heard, r.write)) for i, r in enumerate(self.dictionary.replacements)],
                   "Nenhuma correção ainda.\nElas aparecem aqui quando você corrige um ditado.")

    def _dict_export(self) -> None:
        path = filedialog.asksaveasfilename(parent=self.top, title=_t("Exportar dicionário"),
                                            defaultextension=".json", initialfile="bira-flow-dicionario.json",
                                            filetypes=[("Dicionário do Bira Flow", "*.json")])
        if not path:
            return
        data = {"vocabulary": [w.strip() for w in self.t_vocab.get("1.0", "end").splitlines() if w.strip()],
                "replacements": [{"heard": r.heard, "write": r.write} for r in self.dictionary.replacements],
                "snippets": [{"trigger": sn.trigger, "text": sn.text} for sn in self.dictionary.snippets]}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            self.l_backup.configure(text=_t('Não foi possível exportar: {0}').format(e), style="CardError.TLabel")
            return
        self.l_backup.configure(text=_t('Exportado ✓ ({0} correções, {1} snippets)').format(len(data['replacements']), len(data['snippets'])), style="CardOk.TLabel")

    def _dict_import(self) -> None:
        path = filedialog.askopenfilename(parent=self.top, title=_t("Importar dicionário"),
                                          filetypes=[("Dicionário do Bira Flow", "*.json")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
            reps = [Replacement(str(r["heard"]), str(r["write"])) for r in data.get("replacements", [])
                    if str(r.get("heard", "")).strip() and str(r.get("write", "")).strip()]
            snips = [Snippet(str(s["trigger"]), str(s["text"])) for s in data.get("snippets", [])
                     if str(s.get("trigger", "")).strip()]
            vocab = [str(w).strip() for w in data.get("vocabulary", []) if str(w).strip()]
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            self.l_backup.configure(text=_t('Arquivo inválido: {0}').format(e), style="CardError.TLabel")
            return
        self.dictionary.add_replacements(reps)
        for sn in snips:
            for i, old in enumerate(self.dictionary.snippets):
                if old.trigger.lower() == sn.trigger.lower():
                    self.dictionary.snippets[i] = sn
                    break
            else:
                self.dictionary.snippets.append(sn)
        current = [w.strip() for w in self.t_vocab.get("1.0", "end").splitlines() if w.strip()]
        merged = current + [w for w in vocab if w not in current]
        self.t_vocab.delete("1.0", "end")
        self.t_vocab.insert("1.0", "\n".join(merged))
        self._rep_refresh()
        self._snip_refresh()
        self.l_backup.configure(text=_t('Importado ✓ ({0} correções, {1} snippets, {2} palavras). Clique em Salvar para guardar.').format(len(reps), len(snips), len(vocab)), style="CardOk.TLabel")

    def _paste_screenshot(self, _event=None) -> None:
        """Ctrl+V on the Dicionário page with an image on the clipboard (not in a text field)."""
        if self.current != "dictionary" or self.top.focus_get().winfo_class() in ("TEntry", "Entry", "Text"):
            return
        from . import vision

        if vision.clipboard_image() is not None:
            self._rep_from_screenshot()

    def _rep_from_screenshot(self) -> None:
        """Read the corrections shown in a screenshot (clipboard, or a file you pick)."""
        from . import vision

        if getattr(self, "shot_job", None) is not None:
            return
        image = vision.clipboard_image()
        if image is None:
            from PIL import Image

            path = filedialog.askopenfilename(parent=self.top, title=_t("Escolha o print com a correção"),
                                              filetypes=[("Imagens", "*.png *.jpg *.jpeg *.bmp *.webp *.gif"),
                                                         ("Todos os arquivos", "*.*")])
            if not path:
                return
            try:
                image = Image.open(path)
                image.load()
            except Exception as e:
                self.l_shot.configure(text=_t('Não consegui abrir a imagem: {0}').format(e), style="CardError.TLabel")
                return
        recent = [e.get("text", "") for e in self.history.entries()[:15]]
        env, cfg = config.load_env(self.paths), copy.deepcopy(self.s.cleanup)
        state: dict = {}

        def work() -> None:
            try:
                state["found"] = vision.corrections_from_image(image, recent, env, cfg)
            except Exception as e:
                state["error"] = str(e)

        self.l_shot.configure(text=_t("Lendo o print…"), style="CardMuted.TLabel")
        self.shot_job = self._background(work, lambda: self._screenshot_done(state))

    def _screenshot_done(self, state: dict) -> None:
        self.shot_job = None
        if "error" in state:
            self.l_shot.configure(text=_t('Não consegui ler o print: {0}').format(state['error']), style="CardError.TLabel")
            return
        rules = [Replacement(f.heard, f.write) for f in state.get("found", [])]
        if not rules:
            self.l_shot.configure(text=_t("Nenhuma correção encontrada nesse print."), style="CardMuted.TLabel")
            return
        learned = self.dictionary.add_replacements(rules)
        Dictionary.learn(self.paths.dictionary, rules)   # takes effect now, without "Salvar"
        self._rep_refresh()
        text = ", ".join(f"{r.heard} → {r.write}" for r in rules)
        self.l_shot.configure(text=(_t("Aprendido: ") if learned else _t("Já estava no dicionário: ")) + text,
                              style="CardOk.TLabel")

    def _rep_select(self) -> None:
        self._selection_buttons([self.b_rep_remove], bool(self.tree_rep.selection()))
        sel = self.tree_rep.selection()
        if sel:
            r = self.dictionary.replacements[int(sel[0])]
            self.v_heard.set(r.heard)
            self.v_write.set(r.write)

    def _rep_add(self) -> None:
        heard, write = self.v_heard.get().strip(), self.v_write.get().strip()
        if not heard or not write:
            return
        self.dictionary.add_replacements([Replacement(heard, write)])
        self.v_heard.set("")
        self.v_write.set("")
        self._rep_refresh()

    def _rep_remove(self) -> None:
        sel = self.tree_rep.selection()
        if sel:
            del self.dictionary.replacements[int(sel[0])]
            self._rep_refresh()

    def _page_snippets(self) -> None:
        p = Page(self, "snippets", _t("Snippets"),
                 _t("Atalhos de voz: diga o gatilho em qualquer ponto do ditado e ele vira o texto salvo. "
                 "Ditar só o gatilho insere só o texto."))
        c = p.card(_t("Novo snippet"))
        self.v_trigger = tk.StringVar()
        c.field(_t("Quando eu disser"), lambda f: ttk.Entry(f, textvariable=self.v_trigger, width=34),
                _t("Ex.: meu email, minha assinatura, link da agenda."))
        c.note("No texto, {data}, {data_extenso}, {hora}, {dia_semana}, {amanha} e {area_de_transferencia} "
               "são preenchidos na hora (ex.: \"Bira — {data_extenso}\").")
        self.t_snippet = c.field(_t("Inserir"), lambda f: self._text(f, "", 4, 54), sticky="ew")

        def buttons(parent):
            f = ttk.Frame(parent, style="Inner.TFrame")
            ttk.Button(f, text=_t("Salvar snippet"), style="Accent.TButton", command=self._snip_add).pack(side="left")
            self.b_snip_remove = ttk.Button(f, text=_t("Remover"), command=self._snip_remove)
            self.b_snip_remove.pack(side="left", padx=(self.px(8), 0))
            ttk.Button(f, text=_t("Limpar"), command=self._snip_clear).pack(side="left", padx=(self.px(8), 0))
            return f

        c.field("", buttons)
        c = p.card(_t("Seus snippets"))
        self.tree_snip = self._tree(c.frame, [("trigger", "Gatilho", 200), ("text", _t("Texto"), 360)], height=8)
        c.full(lambda _: self.tree_snip.frame)
        self.tree_snip.bind("<<TreeviewSelect>>", lambda _: self._snip_select())
        self.tree_snip.bind("<Delete>", lambda _: self._snip_remove())
        self._snip_refresh()

    def _snip_refresh(self) -> None:
        self._selection_buttons([b for b in [getattr(self, "b_snip_remove", None)] if b], False)
        self._rows(self.tree_snip, [(str(i), (sn.trigger, sn.text.replace("\n", " ⏎ ")[:80]))
                                    for i, sn in enumerate(self.dictionary.snippets)],
                   "Nenhum snippet ainda.\nCrie um acima: diga o gatilho e o texto entra sozinho.")

    def _snip_select(self) -> None:
        self._selection_buttons([self.b_snip_remove], bool(self.tree_snip.selection()))
        sel = self.tree_snip.selection()
        if sel:
            sn = self.dictionary.snippets[int(sel[0])]
            self.v_trigger.set(sn.trigger)
            self.t_snippet.delete("1.0", "end")
            self.t_snippet.insert("1.0", sn.text)

    def _snip_add(self) -> None:
        trigger = self.v_trigger.get().strip()
        text = self.t_snippet.get("1.0", "end").rstrip("\n")
        if not trigger or not text.strip():
            return
        for i, sn in enumerate(self.dictionary.snippets):
            if sn.trigger.lower() == trigger.lower():
                self.dictionary.snippets[i] = Snippet(trigger, text)
                break
        else:
            self.dictionary.snippets.append(Snippet(trigger, text))
        self._snip_clear()
        self._snip_refresh()

    def _snip_remove(self) -> None:
        sel = self.tree_snip.selection()
        if sel:
            del self.dictionary.snippets[int(sel[0])]
            self._snip_clear()
            self._snip_refresh()

    def _snip_clear(self) -> None:
        self.v_trigger.set("")
        self.t_snippet.delete("1.0", "end")

    def _page_output(self) -> None:
        o = self.s.output
        p = Page(self, "output", _t("Inserção"), _t("Como o texto chega ao aplicativo em que você está digitando."))
        c = p.card(_t("Método"))
        self.v_method = tk.StringVar(value=o.method)
        for code, name, desc in [
            ("paste", "Colar", "Área de transferência + Ctrl+V. Rápido e funciona em quase todos os apps."),
            ("type", "Digitar", "Simula a digitação caractere por caractere. Para apps que bloqueiam colar."),
        ]:
            ttk.Radiobutton(c.frame, text=name, value=code, variable=self.v_method,
                            style="Card.TRadiobutton").grid(row=c.row, column=0, sticky="w", pady=(self.px(6), 0))
            ttk.Label(c.frame, text=desc, style="CardMuted.TLabel", wraplength=self.px(460)).grid(
                row=c.row, column=1, sticky="w", pady=(self.px(6), 0))
            c.row += 1
        c.divider()
        self.v_paste_keys = tk.StringVar(value=o.paste_keys)
        c.field(_t("Teclas de colar"), lambda f: ttk.Combobox(
            f, textvariable=self.v_paste_keys, values=["ctrl+v", "shift+insert", "ctrl+shift+v"], width=18),
            _t("Alguns terminais preferem shift+insert."))
        self.v_trailing = tk.BooleanVar(value=o.trailing_space)
        c.switch(_t("Espaço no final"), self.v_trailing, _t("Ditados seguidos não ficam grudados."))
        self.v_smart = tk.BooleanVar(value=o.smart_insert)
        c.switch(_t("Encaixar no texto que já existe"), self.v_smart,
                 _t("Olha o que vem antes do cursor: no meio de uma frase, começa com minúscula e põe o espaço "
                 "que faltar. Nomes próprios continuam com maiúscula."))

        c = p.card(_t("Enviar com a voz"))
        self.v_send = tk.BooleanVar(value=o.send_command)
        c.switch(_t("Apertar Enter quando o ditado terminar com uma frase de envio"), self.v_send,
                 _t("Ótimo para chats: dite a mensagem e termine com \"pressione enter\" para enviar."))
        self.v_send_phrases = tk.StringVar(value=", ".join(o.send_phrases))
        spoken_send = i18n.spoken_list("send")
        c.field(_t("Frases"), lambda f: ttk.Entry(f, textvariable=self.v_send_phrases, width=50),
                _t("Separadas por vírgula. Use frases que você não falaria no fim de um texto comum.")
                + (_t(' Neste idioma, o app também entende: {0}.').format(', '.join(spoken_send)) if spoken_send else ""),
                sticky="ew")

        c = p.card(_t("Área de transferência"))
        self.v_restore = tk.BooleanVar(value=o.restore_clipboard)
        c.switch(_t("Restaurar o conteúdo anterior depois de colar"), self.v_restore,
                 _t("Desligado: o ditado fica na área de transferência para colar de novo. "
                 "Ligado: o que você tinha copiado volta (texto, imagens, tudo), e o ditado não entra no histórico do Win+V."))
        c.note(_t("O ditado nunca é enviado para a área de transferência na nuvem do Windows."))

    def _page_meeting(self) -> None:
        m = self.s.meeting
        p = Page(self, "meeting", _t("Reuniões"),
                 _t("Grava o microfone e o que o PC toca, transcreve aos poucos e guarda tudo numa pasta por reunião."))
        c = p.card(_t("Gravação"))
        self.v_meet_system = tk.BooleanVar(value=m.system_audio)
        c.switch(_t("Gravar também o áudio do PC"), self.v_meet_system,
                 "O outro lado da chamada (Teams, Zoom, Meet…). Desligado: só o seu microfone. "
                 "O volume e o mudo do Windows não mudam o que é gravado (medido em 18/10): o app "
                 "capta o que o PC está tocando, não o que sai pelo alto-falante.")
        self.v_meet_hours = tk.StringVar(value=str(m.max_hours))
        c.field(_t("Encerrar sozinho depois de (horas)"), lambda f: self._spin(f, self.v_meet_hours, 1, 8, 1),
                _t("Evita gravar o dia todo se você esquecer a reunião ligada."))
        c = p.card(_t("Depois de encerrar"))
        self.v_meet_keep = tk.BooleanVar(value=m.keep_audio)
        c.switch(_t("Guardar o áudio na pasta da reunião"), self.v_meet_keep,
                 _t("Cerca de 21 MB por hora. Desligado: o áudio é apagado depois de transcrito."))
        self.v_meet_notes = tk.BooleanVar(value=m.auto_notes)
        c.switch(_t("Gerar as notas (resumo, decisões, próximos passos)"), self.v_meet_notes,
                 _t("Usa o mesmo provedor do Refinamento. Sem provedor, fica só a transcrição."))
        c.full(lambda f: ttk.Button(f, text=_t("Abrir pasta das reuniões"), command=lambda: self.app.open_meetings()),
               sticky="w")
        c = p.card(_t("Consentimento"))
        c.note(_t("Avise os participantes antes de gravar: gravar alguém sem consentimento pode violar a LGPD "
               "e as regras da sua empresa. A transcrição usa o motor escolhido em Transcrição."))

    def _page_history(self) -> None:
        h = self.s.history
        p = Page(self, "history", _t("Histórico"),
                 _t("Seus ditados recentes, guardados só neste computador. As ações desta página valem na hora."))
        c = p.card()
        self.c_keep = Choice(c.frame, KEEP_CHOICES, h.keep, width=34)
        c.field(_t("Guardar ditados"), lambda _: self.c_keep.widget, _t("A mudança vale ao salvar as configurações."))
        self.l_stats = c.note(self.stats.summary())

        c = p.card(_t("Ditados"))
        self.v_search = tk.StringVar()

        def search(parent):
            e = self.e_search = ttk.Entry(parent, textvariable=self.v_search, width=40)
            self.v_search.trace_add("write", lambda *_: self._history_refresh())
            return e

        c.field(_t("Buscar"), search, sticky="ew")
        self.tree_hist = self._tree(c.frame, [("when", "Quando", 105), ("app", _t("App"), 125), ("text", "Texto", 380)],
                                    height=9)
        c.full(lambda _: self.tree_hist.frame)
        self.tree_hist.bind("<<TreeviewSelect>>", lambda _: self._history_select())
        self.tree_hist.bind("<Double-1>", lambda _: self._history_fix())
        self.tree_hist.bind("<Delete>", lambda _: self._history_delete())

        def buttons(parent):
            f = ttk.Frame(parent, style="Inner.TFrame")
            self.b_hist = []
            for text, cmd, style in (("Copiar", self._history_copy, "TButton"),
                                     ("Corrigir…", self._history_fix, "Accent.TButton"),
                                     ("Excluir", self._history_delete, "TButton"),
                                     ("Exportar…", self._history_export, "TButton"),
                                     ("Limpar histórico", self._history_clear, "TButton")):
                b = ttk.Button(f, text=text, command=cmd, style=style)
                b.pack(side="left", padx=(0, self.px(8)))
                if text in ("Copiar", "Corrigir…", "Excluir"):   # these act on the selected dictation
                    self.b_hist.append(b)
            return f

        c.full(buttons, sticky="w")
        self.l_hist_detail = c.note(_t("Selecione um ditado para ver o texto final e a transcrição bruta. "
                                    "Duplo clique corrige."))
        self.t_hist_detail = c.full(lambda f: self._text(f, "", 4))
        self.t_hist_detail.configure(state="disabled", fg=self.pal.muted)
        self._hist_items: dict[str, dict] = {}

    def _history_refresh(self) -> None:
        if not hasattr(self, "tree_hist"):
            return
        self._selection_buttons(getattr(self, "b_hist", []), False)   # the list is rebuilt: no selection
        query = self.v_search.get().strip().lower()
        self._hist_items = {}
        rows = []
        for e in self.history.entries():
            app = app_label(e.get("app", ""))
            if query and not any(query in v.lower() for v in (e.get("text", ""), e.get("raw", ""), app,
                                                              e.get("app", ""))):
                continue
            when = datetime.fromtimestamp(e.get("ts", 0)).strftime("%d/%m %H:%M")
            if e.get("mode") == "command":
                app = f"{app} · comando"
            rows.append((e["id"], (when, app, e.get("text", "").replace("\n", " ⏎ ")[:140])))
            self._hist_items[e["id"]] = e
        self._rows(self.tree_hist, rows, "Nada encontrado." if query else
                   "Nenhum ditado guardado ainda.\nSegure o atalho e fale: seus ditados aparecem aqui.")
        self.l_stats.configure(text=self.stats.summary())

    def _history_selected(self) -> dict | None:
        sel = self.tree_hist.selection()
        return self._hist_items.get(sel[0]) if sel else None

    def _selection_buttons(self, buttons, selected: bool) -> None:
        for b in buttons:
            b.state(["!disabled"] if selected else ["disabled"])

    def _history_select(self) -> None:
        e = self._history_selected()
        self._selection_buttons(getattr(self, "b_hist", []), e is not None)
        if e:
            label = "Instrução falada" if e.get("mode") == "command" else "Transcrição bruta"
            self.t_hist_detail.configure(state="normal")
            self.t_hist_detail.delete("1.0", "end")
            self.t_hist_detail.insert("1.0", f"{e.get('text', '')}\n\n{label}: {e.get('raw', '')}")
            self.t_hist_detail.configure(state="disabled")

    def _history_export(self) -> None:
        path = filedialog.asksaveasfilename(parent=self.top, title=_t("Exportar histórico"), defaultextension=".txt",
                                            initialfile="bira-flow-historico.txt",
                                            filetypes=[("Texto", "*.txt"), ("Planilha (CSV)", "*.csv")])
        if not path:
            return
        entries = self.history.entries()
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                if path.lower().endswith(".csv"):
                    w = csv.writer(f, delimiter=";")
                    w.writerow(["data", "app", "texto", "transcrição bruta"])
                    for e in entries:
                        w.writerow([datetime.fromtimestamp(e.get("ts", 0)).strftime("%d/%m/%Y %H:%M"),
                                    app_label(e.get("app", "")), e.get("text", ""), e.get("raw", "")])
                else:
                    for e in entries:
                        f.write(f"[{datetime.fromtimestamp(e.get('ts', 0)).strftime('%d/%m/%Y %H:%M')} · "
                                f"{app_label(e.get('app', ''))}]\n{e.get('text', '')}\n\n")
        except OSError as e:
            self.l_hist_detail.configure(text=_t('Não foi possível exportar: {0}').format(e), style="CardError.TLabel")
            return
        self.l_hist_detail.configure(text=_t('{0} ditados exportados ✓').format(len(entries)), style="CardOk.TLabel")

    def _history_copy(self) -> None:
        e = self._history_selected()
        if e:
            from . import inserter
            inserter.set_text(e.get("text", ""))
            self.l_hist_detail.configure(text=_t("Copiado para a área de transferência ✓"), style="CardOk.TLabel")

    def _history_delete(self) -> None:
        e = self._history_selected()
        if e:
            self.history.delete(e["id"])
            self._history_refresh()

    def _history_clear(self) -> None:
        if messagebox.askyesno(_t("Bira Flow"), _t("Apagar todo o histórico de ditados?"), parent=self.top):
            self.history.clear()
            self._history_refresh()

    def _history_fix(self) -> None:
        e = self._history_selected()
        if not e:
            self.l_hist_detail.configure(text=_t("Selecione um ditado para corrigir."), style="CardMuted.TLabel")
            return
        dlg = tk.Toplevel(self.top)
        dlg.title("Corrigir ditado")
        dlg.configure(bg=self.pal.bg)
        dlg.transient(self.top)
        frame = ttk.Frame(dlg, padding=self.px(20))
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=_t("Corrija o texto como deveria ter saído:"), font="SunValleyBodyStrongFont").pack(anchor="w")
        text = self._text(frame, e.get("text", ""), 6, 64)
        text.pack(fill="both", expand=True, pady=self.px(10))
        result = ttk.Label(frame, text="", style="Muted.TLabel", wraplength=self.px(520), justify="left")
        result.pack(anchor="w")
        self.fix_dialog = (dlg, text, result)

        def apply():
            corrected = text.get("1.0", "end").strip()
            if not corrected or corrected == e.get("text"):
                dlg.destroy()
                return
            self.history.update(e["id"], text=corrected)
            learned = []
            if self.v_learn.get():
                rules = textproc.learn_replacements(e.get("text", ""), corrected)
                # like an edit after a paste: "casa" -> "caso" is a change of meaning, not a typo
                rules = getattr(self.app.controller, "_typo_fixes", lambda r: r)(rules)
                learned = self.dictionary.add_replacements(rules)
                if learned:
                    Dictionary.learn(self.paths.dictionary, learned)
                    self._rep_refresh()
            self._history_refresh()
            if learned:
                result.configure(text=_t("Aprendido: ") + _t(", ").join(f"{r.heard} → {r.write}" for r in learned),
                                 style="Ok.TLabel")
                dlg.after(2200, dlg.destroy)
            else:
                dlg.destroy()

        self.fix_apply = apply
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(self.px(8), 0))
        ttk.Button(row, text=_t("Salvar correção"), style="Accent.TButton", command=apply).pack(side="right")
        ttk.Button(row, text=_t("Cancelar"), command=dlg.destroy).pack(side="right", padx=self.px(8))
        dlg.geometry(f"{self.px(600)}x{self.px(340)}")
        dlg.update_idletasks()
        self._dark_titlebar(dlg, self.pal is DARK)
        dlg.grab_set()
        text.focus_set()

    def _page_keys(self) -> None:
        p = Page(self, "keys", _t("Chaves de API"),
                 _t("Opcionais. Sem chaves, tudo funciona offline com o modelo local e a limpeza local."))
        c = p.card()
        self.v_keys: dict[str, tk.StringVar] = {}
        self.e_keys: list[ttk.Entry] = []
        for key, name, where in KEY_LABELS:
            var = self.v_keys[key] = tk.StringVar(value=self.env_user.get(key, ""))

            def make(f, var=var):
                e = ttk.Entry(f, textvariable=var, show="•", width=56)
                self.e_keys.append(e)
                return e

            hint = _t("Crie em {0}.").format(where)
            if self.env_all.get(key) and not self.env_user.get(key):
                hint += " " + _t("Já definida fora deste arquivo (variável de ambiente ou .env do projeto).")
            c.field(_t(name), make, sticky="ew")
            self.l_key_status = getattr(self, "l_key_status", {})
            self.l_key_status[key] = c._help(hint)
            self.l_key_status[key].hint = hint
            var.trace_add("write", lambda *_: self._provider_changed())
        self.v_show_keys = tk.BooleanVar(value=False)

        def tools(parent):
            f = ttk.Frame(parent, style="Inner.TFrame")
            ttk.Checkbutton(f, text=_t("Mostrar chaves"), variable=self.v_show_keys, style="Card.TCheckbutton",
                            command=lambda: [e.configure(show="" if self.v_show_keys.get() else "•")
                                             for e in self.e_keys]).pack(side="left")
            ttk.Button(f, text=_t("Testar chaves"), command=self._test_keys).pack(side="left", padx=(self.px(16), 0))
            return f

        c.field("", tools)
        c = p.card(_t("Privacidade"))
        c.note(_t('As chaves ficam só neste computador, em {0}, e são enviadas apenas ao respectivo provedor. O Bira Flow não tem contas nem telemetria.').format(self.paths.env))
        self._provider_changed()

    def _test_keys(self) -> None:
        """Ask each provider whether the typed key works (lists its models: free, nothing else)."""
        from .cleanup import check_key

        results: dict[str, tuple[bool, str]] = {}
        todo = {k: v.get().strip() or self.env_all.get(k, "") for k, v in self.v_keys.items()}
        for key, value in todo.items():
            label = self.l_key_status[key]
            if value:
                label.configure(text=_t("Testando…"), style="CardMuted.TLabel")
            else:
                label.configure(text=label.hint, style="CardMuted.TLabel")

        def work():
            for key, value in todo.items():
                if value:
                    results[key] = check_key(key, value)

        def show():
            for key, (ok, message) in list(results.items()):
                self.l_key_status[key].configure(text=_t('Chave {0}').format(message),
                                                 style="CardOk.TLabel" if ok else "CardError.TLabel")

        self._background(work, lambda: None, show, every=200)

    def _page_about(self) -> None:
        from . import __version__

        p = Page(self, "about", _t("Sobre"))
        c = p.card()
        c.full(lambda f: tk.Label(f, image=self._logo_big, bg=self.pal.bg), sticky="w")
        c.note(_t('{0} · versão {1}').format(branding.TAGLINE, __version__))
        c.note(_t("Sem contas e sem telemetria. Com o modelo local, nada sai do seu computador."))
        c = p.card(_t("Atualizações"))
        self.v_auto_update = tk.BooleanVar(value=self.s.updates.auto)
        c.switch(_t("Atualizar automaticamente"), self.v_auto_update,
                 _t("Novas versões são baixadas e instaladas sozinhas, só quando você não está ditando. O "
                 "instalador só é aceito se tiver a assinatura digital do Bira Flow."))

        c.full(lambda f: ttk.Button(f, text=_t("Verificar agora"), command=self._check_updates), sticky="w")
        updater = getattr(self.app, "updater", None)
        self.l_update = c.note(updater.status if updater else _t("Atualizações indisponíveis aqui."))

        c = p.card(_t("Seus dados"))

        def links(parent):
            f = ttk.Frame(parent, style="Inner.TFrame")
            ttk.Button(f, text=_t("Abrir pasta de dados"), command=self.app.open_data_folder).pack(side="left")
            ttk.Button(f, text=_t("Abrir log"), command=self.app.open_log).pack(side="left", padx=(self.px(8), 0))
            ttk.Button(f, text=_t("Abrir README"), command=self.app.open_readme).pack(side="left", padx=(self.px(8), 0))
            return f

        c.full(links, sticky="w")
        c.note(_t('Configurações, chaves, dicionário e histórico: {0}\nwhisper.cpp, modelos e áudio temporário: {1}').format(self.paths.data, self.paths.local))
        legacy = config.legacy_dirs(self.paths)
        if legacy:
            size = sum(setup_local.folder_size(d) for d in legacy)
            c.divider()
            self.l_legacy = c.note(_t('Pastas antigas do Whisperflow (o nome anterior do app) ainda ocupam {0:.0f} MB: ').format(size / 1e6) + _t(", ").join(str(d) for d in legacy))

            def remove(parent):
                return ttk.Button(parent, text=_t("Apagar pastas antigas"), command=lambda: self._remove_legacy(legacy))

            self.b_legacy = c.full(remove, sticky="w")
        c = p.card(_t("Restaurar"))
        c.note(_t("Volta todas as configurações ao padrão (atalhos, motor, estilos...). Chaves de API, dicionário, "
               "snippets e histórico continuam como estão."))
        c.full(lambda f: ttk.Button(f, text=_t("Restaurar padrões…"), command=self._restore_defaults), sticky="w")

        c = p.card(_t("Créditos"))
        c.note(_t("Transcrição: whisper.cpp (MIT) e modelos Whisper da OpenAI (MIT). Interface: Sun Valley ttk "
               "theme (MIT), ícones Segoe Fluent. Áudio: PortAudio via sounddevice."))

    def _restore_defaults(self) -> None:
        if not messagebox.askyesno(_t("Bira Flow"), _t("Voltar todas as configurações ao padrão?\n\nChaves de API, "
                                   "dicionário, snippets e histórico não são alterados."), parent=self.top):
            return
        defaults = Settings()
        defaults.transcription.vocabulary = list(self.s.transcription.vocabulary)  # part of the dictionary
        try:
            self.app.apply_settings(defaults, {}, None, None, None)
        except OSError as e:
            self.status.configure(text=_t('Não foi possível restaurar: {0}').format(e))
            return
        self._saved = True   # the pill follows the defaults, not the placement before a preview
        self.close()

    def _check_updates(self) -> None:
        updater = getattr(self.app, "updater", None)
        if updater is None:
            return

        self.l_update.configure(text=_t("Verificando…"), style="CardMuted.TLabel")
        threading.Thread(target=updater.check, name="update-check", daemon=True).start()

        def poll(n=0):
            if not self.top.winfo_exists():
                return
            self.l_update.configure(text=updater.status)
            if updater._lock.locked() and n < 600:  # still checking or downloading
                self.top.after(500, lambda: poll(n + 1))

        self.top.after(500, poll)

    def _remove_legacy(self, dirs) -> None:
        if not messagebox.askyesno(_t("Bira Flow"), _t("Apagar definitivamente as pastas antigas do Whisperflow?\n\n")
                                   + "\n".join(str(d) for d in dirs)
                                   + _t("\n\nAs configurações e o modelo em uso (BiraFlow) não são afetados."),
                                   parent=self.top):
            return
        errors = []
        for d in dirs:
            try:
                shutil.rmtree(d)
            except OSError as e:
                errors.append(f"{d}: {e}")
        self.b_legacy.state(["disabled"])
        self.l_legacy.configure(text=_t("Não foi possível apagar tudo: ") + _t("; ").join(errors) if errors
                                else _t("Pastas antigas apagadas ✓"),
                                style="CardError.TLabel" if errors else "CardOk.TLabel")

    # --- save -----------------------------------------------------------------------

    def _number(self, var: tk.StringVar, kind, lo, hi, label: str, page: str):
        try:
            value = kind(var.get().strip().replace(",", "."))
        except ValueError:
            raise ValueError(page, f"{label}: número inválido.")
        if not lo <= value <= hi:
            raise ValueError(page, f"{label}: use um valor entre {lo} e {hi}.")
        return value

    def collect(self) -> Settings:
        self._build_to()   # saved or closed before the last pages were built
        s = copy.deepcopy(self.s)
        for field, label in ((self.f_hotkey, "Atalho de ditado"), (self.f_command, "Atalho do modo comando"),
                             (self.f_paste, "Atalho de colar último")):
            error = field.error()
            if error:
                raise ValueError("hotkey", f"{label}: {error}")
        s.hotkey.keys = self.f_hotkey.var.get().strip()
        if not s.hotkey.keys:
            raise ValueError("hotkey", "O atalho de ditado não pode ficar vazio.")
        s.hotkey.command_keys = self.f_command.var.get().strip()
        s.hotkey.paste_last_keys = self.f_paste.var.get().strip()
        from .controller import extra_hotkey_problem

        main = parse_hotkey(s.hotkey.keys)
        for name, spec in (("command", s.hotkey.command_keys), ("paste_last", s.hotkey.paste_last_keys)):
            if spec:
                problem = extra_hotkey_problem(name, parse_hotkey(spec), main)
                if problem:
                    raise ValueError("hotkey", problem)
        s.hotkey.tap_max_ms = self._number(self.v_tap, int, 100, 600, "Duração de um toque", "hotkey")
        s.hotkey.double_tap_ms = self._number(self.v_double, int, 200, 1000, "Intervalo do toque duplo", "hotkey")
        s.hotkey.suppress_keys = self.v_suppress.get()
        s.hotkey.ignore_apps = [a if a.endswith(".exe") else a + ".exe"
                                for a in (x.strip().lower() for x in self.v_ignore.get().replace(";", ",").split(","))
                                if a]

        s.wake.enabled = self.v_wake.get()
        phrase = " ".join(self.v_wake_phrase.get().split())
        if s.wake.enabled and len(phrase.replace(" ", "")) < 4:
            raise ValueError("voice", "Escreva uma frase de ativação com pelo menos 4 letras (ex.: Ei Bira).")
        s.wake.phrase = phrase or "Ei Bira"
        s.wake.end_silence = self._number(self.v_wake_silence, float, 0.8, 5, "Silêncio para encerrar", "voice")
        s.wake.confirm = self.v_wake_confirm.get()

        r = s.recording
        device = self.v_device.get().strip()
        r.input_device = "" if device == DEFAULT_MIC else device
        r.max_seconds = self._number(self.v_max, int, 10, 3600, "Parada automática", "mic")
        r.min_seconds = self._number(self.v_min, float, 0.1, 3, "Gravação mínima", "mic")
        r.silence_threshold = self._number(self.v_silence, float, 0, 0.05, "Limite de silêncio", "mic")
        r.normalize = self.v_normalize.get()
        r.keep_mic_ready = (r.keep_mic_ready or 30) if self.v_ready.get() else 0
        r.handsfree_silence_stop = self._number(self.v_hf_silence, int, 0, 600, "Parada por silêncio", "mic")

        t = s.transcription
        t.engine = self.v_engine.get()
        t.language = self.c_language.get()
        t.local_model = self.c_model.get()
        t.local_fast = self.v_fast.get()
        t.keep_loaded = self.v_keep.get()
        t.fallback_to_local = self.v_fallback.get()
        t.vocabulary = [w.strip() for w in self.t_vocab.get("1.0", "end").splitlines() if w.strip()]
        t.groq_model = self.v_groq_model.get().strip() or config.TranscriptionCfg.groq_model
        t.openai_model = self.v_openai_model.get().strip() or config.TranscriptionCfg.openai_model
        t.gateway_model = self.v_gateway_model.get().strip() or config.TranscriptionCfg.gateway_model
        t.timeout_seconds = self._number(self.v_api_timeout, int, 5, 300, "Tempo limite da API", "transcription")
        t.threads = self._number(self.v_threads, int, 0, 64, "Threads da CPU", "transcription")
        t.whisper_cli = self.v_cli.get().strip()

        cl = s.cleanup
        cl.provider = self.c_provider.get()
        cl.model = self.v_llm_model.get().strip()
        cl.timeout_seconds = self._number(self.v_llm_timeout, int, 2, 60, "Tempo limite do refinamento", "cleanup")
        cl.extra_instructions = self.t_extra.get("1.0", "end").strip()
        cl.ollama_url = self.v_ollama.get().strip() or config.CleanupCfg.ollama_url
        cl.local_rules = self.v_local_rules.get()
        cl.spoken_punctuation = self.v_spoken.get()

        s.styles.enabled = self.v_styles.get()
        for key, ch in self.c_styles.items():
            setattr(s.styles, key, ch.get())

        o = s.output
        o.method = self.v_method.get()
        o.restore_clipboard = self.v_restore.get()
        keys = self.v_paste_keys.get().strip() or "ctrl+v"
        try:
            parse_hotkey(keys)
        except HotkeyError as e:
            raise ValueError("output", f"Teclas de colar: {e}")
        o.paste_keys = keys
        o.trailing_space = self.v_trailing.get()
        o.smart_insert = self.v_smart.get()
        o.send_command = self.v_send.get()
        o.send_phrases = [x.strip() for x in self.v_send_phrases.get().split(",") if x.strip()]

        s.ui.show_pill = self.v_show_pill.get()
        s.ui.position = self.c_position.get()
        s.ui.pill_size = self.c_pill_size.get()
        s.ui.show_preview = self.v_preview.get()
        s.ui.sounds = self.v_sounds.get()
        s.ui.mute_media = self.v_mute.get()
        s.ui.language = self.c_language.get()
        s.history.keep = self.c_keep.get()
        s.history.auto_learn = self.v_learn.get()
        s.history.learn_from_edits = self.v_learn_edits.get()
        s.updates.auto = self.v_auto_update.get()
        s.meeting.system_audio = self.v_meet_system.get()
        s.meeting.keep_audio = self.v_meet_keep.get()
        s.meeting.auto_notes = self.v_meet_notes.get()
        s.meeting.max_hours = self._number(self.v_meet_hours, int, 1, 8, "Encerrar sozinho", "meeting")
        return s

    def save(self) -> None:
        try:
            settings = self.collect()
        except ValueError as e:
            page, message = e.args if len(e.args) == 2 else ("general", str(e))
            self.show(page)
            self.status.configure(text=message)
            return
        # keep corrections learned from edits while this window was open
        learned_meanwhile = [r for r in Dictionary.load(self.paths.dictionary).replacements
                             if r.heard.lower() not in self._dictionary_at_open]
        self.dictionary.add_replacements(learned_meanwhile)
        env_updates = {k: v.get().strip() for k, v in self.v_keys.items()
                       if v.get().strip() != self.env_user.get(k, "")}
        if self._snapshot is not None:
            settings = merge_changes(config.load_settings(self.paths)[0], self._snapshot[0], settings)
        enabled = self.v_enabled.get() if self.v_enabled.get() != self._enabled_at_open else None
        autostart_on = self.v_autostart.get() if self.v_autostart.get() != self._autostart_at_open else None
        try:
            self.app.apply_settings(settings, env_updates, enabled, autostart_on, self.dictionary)
        except OSError as e:
            self.status.configure(text=_t('Não foi possível salvar: {0}').format(e))
            return
        self._saved = True   # the pill now follows the saved placement
        if settings.ui.language != self.s.ui.language:
            # the texts of this window are in the old language: open it again in the new one
            page = self.current
            i18n.set_language(settings.ui.language)
            self.close()
            self.app.show_settings()
            window = self.app.settings_window
            if window is not None and page:
                window.show(page)
            return
        self.close()

    def cancel(self) -> None:
        """Cancelar / Esc / the window's X: ask before throwing changes away."""
        if not self.background and self.dirty():
            if not messagebox.askyesno(_t("Bira Flow"), _t("Descartar as alterações que não foram salvas?"), parent=self.top):
                return
        self.close()

    def close(self) -> None:
        if not self.background:
            try:
                if self.top.state() == "normal":
                    geometry = self.top.winfo_geometry()
                    state = config.load_state(self.paths)
                    state["settings_geometry"] = geometry
                    state["settings_page"] = self.current
                    config.save_state(self.paths, state)
                    if isinstance(getattr(self.app, "state", None), dict):  # the app saves its copy too
                        self.app.state["settings_geometry"] = geometry
            except (OSError, tk.TclError):
                pass
        if self.active_field is not None:
            self.active_field.stop()
        if self.mic_test is not None:
            self.mic_test[0].abort()
            self.mic_test = None
        if self.setup_job is not None:   # never leave a download writing in the background
            self.setup_job.set()
        if self._preview_job is not None:
            self.root.after_cancel(self._preview_job)
            self._preview_job = None
        self._end_preview()
        pill = getattr(getattr(self.app, "ui", None), "pill", None)
        if pill is not None and self._placement_saved is not None and not self._saved:
            pill.position, pill.size = self._placement_saved   # closed without saving
        self.top.destroy()
        self.app.settings_window = None
