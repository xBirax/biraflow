"""The "Transcrever áudio" window: drop a WhatsApp voice note (or any audio file) and get
its text, ready to copy, summarize or save."""
from __future__ import annotations

import logging
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import TYPE_CHECKING

from .i18n import t as _t
from . import audiofile, branding, config
from .transcribe import API

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger(__name__)


def _clock(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


class AudioWindow:
    def __init__(self, app: "App", background: bool = False, focus: bool = True):
        """`background`: built off-screen and never activated (UI tests). `focus` False: shown
        without taking the focus from the app you are typing in."""
        import sv_ttk

        from .settings_ui import DARK, LIGHT, SettingsWindow, system_uses_dark_mode

        self.app = app
        self.paths = app.paths
        self.root = app.root
        dark = system_uses_dark_mode()
        if app.settings_window is None:   # the settings window sets the theme too; never fight it
            sv_ttk.set_theme("dark" if dark else "light")
            self.root.tk.call("configure_colors")
        self.pal = DARK if dark else LIGHT
        self.scale = self.root.winfo_fpixels("1i") / 96
        st = ttk.Style(self.root)
        st.configure("Muted.TLabel", foreground=self.pal.muted)
        st.configure("Error.TLabel", foreground=self.pal.error)
        self.queue: list[Path] = []
        self.job: threading.Thread | None = None
        self.state: dict = {}
        self.results: list[tuple[Path, str]] = []
        self.meeting = None   # the MeetingFolder shown (show_meeting)

        top = self.top = tk.Toplevel(self.root)
        top.withdraw()
        top.title("Bira Flow — Transcrever áudio")
        top.configure(bg=self.pal.bg)
        try:
            top.iconbitmap(str(branding.APP_ICO))
        except tk.TclError:
            pass
        self._build()
        w, h = self.px(680), self.px(600)
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        w, h = min(w, sw - self.px(40)), min(h, sh - self.px(90))
        top.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - self.px(30))}")
        top.minsize(self.px(520), self.px(440))
        top.protocol("WM_DELETE_WINDOW", self.close)
        top.bind("<Escape>", lambda _: self.close())
        top.bind("<Control-o>", lambda _: self.choose())
        top.bind("<Control-v>", self._paste_files)
        self._enable_drop()
        if background:
            from . import win32

            top.geometry(f"{w}x{h}+0+0")
            top.attributes("-alpha", 0.01)
            top.update_idletasks()
            hwnd = int(top.wm_frame(), 16)
            ex = win32.GetWindowLongPtrW(hwnd, win32.GWL_EXSTYLE)
            win32.SetWindowLongPtrW(hwnd, win32.GWL_EXSTYLE, ex | win32.WS_EX_NOACTIVATE | win32.WS_EX_TOOLWINDOW
                                    | win32.WS_EX_TRANSPARENT | win32.WS_EX_LAYERED)
            win32.ShowWindow(hwnd, 4)   # SW_SHOWNOACTIVATE
            win32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            top.update()
            SettingsWindow._dark_titlebar(top, dark)
            self.background = True
            return
        self.background = False
        if focus:
            top.deiconify()
        else:
            self.show_quietly()
        top.update_idletasks()
        branding.apply_window_icon(top)
        SettingsWindow._dark_titlebar(top, dark)
        if focus:
            top.lift()
            top.focus_force()

    def show_quietly(self) -> None:
        """On screen (restored if minimized) without activating it: Tk's deiconify() would make
        it the active window and take the keyboard from the app you are typing in."""
        from . import win32

        self.top.update_idletasks()
        win32.ShowWindow(int(self.top.wm_frame(), 16), 4)   # SW_SHOWNOACTIVATE
        self.top.update()

    def px(self, n: float) -> int:
        return int(round(n * self.scale))

    # --- layout ---------------------------------------------------------------------

    def _build(self) -> None:
        px, pal = self.px, self.pal
        body = ttk.Frame(self.top, padding=(px(24), px(20), px(24), px(18)))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=_t("Transcrever áudio"), font="SunValleyTitleFont").pack(anchor="w")
        ttk.Label(body, text=_t("Áudios do WhatsApp (.ogg/.opus), MP3, M4A, WAV… O texto fica pronto para copiar."),
                  style="Muted.TLabel").pack(anchor="w", pady=(px(2), px(14)))

        # drop zone: a dashed box that lights up while a file is dragged over it
        self.zone = tk.Canvas(body, height=px(130), bg=pal.bg, highlightthickness=0, bd=0)
        self.zone.pack(fill="x")
        self.zone.bind("<Configure>", lambda e: self._paint_zone())
        self._zone_hot = False
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(px(10), 0))
        ttk.Button(buttons, text=_t("Escolher arquivo…"), command=self.choose).pack(side="left")
        self.b_latest = ttk.Button(buttons, text=_t("Último áudio baixado"), command=self.latest)
        self.b_latest.pack(side="left", padx=(px(8), 0))
        self.l_latest = ttk.Label(buttons, text="", style="Muted.TLabel")
        self.l_latest.pack(side="left", padx=(px(10), 0))
        self._refresh_latest()

        status = ttk.Frame(body)
        status.pack(fill="x", pady=(px(14), px(6)))
        self.l_status = ttk.Label(status, text="", style="Muted.TLabel")
        self.l_status.pack(side="left")
        self.bar = ttk.Progressbar(status, mode="indeterminate", length=px(160))

        box = ttk.Frame(body)
        box.pack(fill="both", expand=True)
        self.text = tk.Text(box, wrap="word", relief="flat", bd=0, bg=pal.field, fg=pal.text,
                            insertbackground=pal.text, selectbackground=pal.accent, highlightthickness=1,
                            highlightbackground=pal.border, highlightcolor=pal.accent,
                            padx=px(10), pady=px(8), font="SunValleyBodyFont", height=8)
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.text.tag_configure("head", foreground=pal.muted, font="SunValleyCaptionFont")

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(px(12), 0))
        self.b_copy = ttk.Button(actions, text=_t("Copiar"), style="Accent.TButton", command=self.copy)
        self.b_copy.pack(side="left")
        self.b_summary = ttk.Button(actions, text=_t("Resumir"), command=self.summarize)
        self.b_summary.pack(side="left", padx=(px(8), 0))
        self.b_save = ttk.Button(actions, text=_t("Salvar .txt…"), command=self.save_txt)
        self.b_save.pack(side="left", padx=(px(8), 0))
        ttk.Button(actions, text=_t("Limpar"), command=self.clear).pack(side="left", padx=(px(8), 0))
        self.v_autocopy = tk.BooleanVar(value=True)
        ttk.Checkbutton(actions, text=_t("Copiar ao terminar"), variable=self.v_autocopy,
                        style="Switch.TCheckbutton").pack(side="right")
        self.meeting_actions = ttk.Frame(body)   # packed by show_meeting
        self.b_notes = ttk.Button(self.meeting_actions, text=_t("Gerar notas"), command=self.meeting_notes)
        self.b_notes.pack(side="left")
        ttk.Button(self.meeting_actions, text=_t("Abrir pasta"), command=self.open_meeting_folder).pack(
            side="left", padx=(px(8), 0))
        self.b_retry = ttk.Button(self.meeting_actions, text=_t("Tentar de novo"), command=self.meeting_retry)
        self._buttons()

    def _paint_zone(self) -> None:
        c, px, pal = self.zone, self.px, self.pal
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        color = pal.accent if self._zone_hot else pal.border
        c.create_rectangle(px(2), px(2), w - px(2), h - px(2), outline=color, width=px(2), dash=(px(6), px(4)),
                           fill=pal.nav_hover if self._zone_hot else pal.bg)
        c.create_text(w // 2, h // 2 - px(12), text=_t("Arraste o áudio para cá"),
                      fill=pal.accent if self._zone_hot else pal.text, font="SunValleySubtitleFont")
        c.create_text(w // 2, h // 2 + px(18),
                      text=_t("Do WhatsApp: ⋮ no áudio → Baixar, depois arraste da pasta Downloads (ou Ctrl+V)"),
                      fill=pal.muted, font="SunValleyCaptionFont")

    def _buttons(self) -> None:
        has_text = bool(self.text.get("1.0", "end").strip())
        busy = self.job is not None
        for b in (self.b_copy, self.b_save):
            b.state(["!disabled"] if has_text else ["disabled"])
        self.b_summary.state(["!disabled"] if has_text and not busy else ["disabled"])
        # the automatic notes are being written by the app: no second LLM call, no state race
        writing = self.meeting is not None and bool(self.meeting.state.get("notes_pending"))
        for b in (self.b_notes, self.b_retry):
            b.state(["disabled"] if busy or writing else ["!disabled"])

    def _refresh_latest(self) -> None:
        f = audiofile.latest_audio()
        self.latest_file = f
        if f is None:
            self.b_latest.state(["disabled"])
            self.l_latest.configure(text=_t("nenhum áudio baixado nas últimas 72 h"))
        else:
            self.b_latest.state(["!disabled"])
            age = time.time() - f.stat().st_mtime
            when = "agora há pouco" if age < 120 else f"há {int(age // 60)} min" if age < 3600 else \
                f"há {int(age // 3600)} h"
            name = f.name if len(f.name) <= 34 else f.name[:31] + "…"
            self.l_latest.configure(text=f"{name} · {when}")

    # --- drag and drop ---------------------------------------------------------------

    def _enable_drop(self) -> None:
        try:
            from tkinterdnd2 import DND_FILES, TkinterDnD

            TkinterDnD._require(self.root)
            for widget in (self.top, self.zone, self.text):
                TkinterDnD.DnDWrapper.drop_target_register(widget, DND_FILES)
                TkinterDnD.DnDWrapper.dnd_bind(widget, "<<Drop>>", self._on_drop)
                TkinterDnD.DnDWrapper.dnd_bind(widget, "<<DropEnter>>", lambda e: self._hot(True, e))
                TkinterDnD.DnDWrapper.dnd_bind(widget, "<<DropLeave>>", lambda e: self._hot(False, e))
        except Exception as e:   # no tkdnd: the buttons and Ctrl+V still work
            log.warning("drag and drop unavailable: %s", e)

    def _hot(self, on: bool, event=None):
        self._zone_hot = on
        self._paint_zone()
        return getattr(event, "action", None)

    def _on_drop(self, event):
        self._hot(False)
        self.add([Path(p) for p in self.top.tk.splitlist(event.data)])
        return getattr(event, "action", None)

    def _paste_files(self, _event=None):
        """Ctrl+V with audio files copied in Explorer."""
        try:
            from PIL import ImageGrab

            got = ImageGrab.grabclipboard()
        except Exception:
            got = None
        if isinstance(got, list):
            self.add([Path(p) for p in got])
            return "break"
        return None

    # --- the work ---------------------------------------------------------------------

    def choose(self) -> None:
        from tkinter import filedialog

        names = filedialog.askopenfilenames(
            parent=self.top, title="Escolha o áudio", initialdir=str(audiofile.downloads_folder()),
            filetypes=[("Áudios", " ".join(f"*{e}" for e in audiofile.AUDIO_EXTENSIONS)), ("Todos os arquivos", "*.*")])
        self.add([Path(n) for n in names])

    def latest(self) -> None:
        self._refresh_latest()
        if self.latest_file is not None:
            self.add([self.latest_file])

    def add(self, files: list[Path]) -> None:
        audio = [f for f in files if f.is_file() and audiofile.is_audio(f)]
        skipped = [f for f in files if f not in audio]
        if skipped and not audio:
            self._status(f"\"{skipped[0].name}\" não é um áudio que eu reconheça "
                         f"({', '.join(audiofile.AUDIO_EXTENSIONS[:6])}…).", error=True)
            return
        if self.meeting is not None:   # a file now: the window is "Transcrever áudio" again
            self.clear()
        self.queue += audio
        if not self.background:
            self.top.deiconify()
            self.top.lift()
        self._next()

    def _status(self, text: str, error: bool = False, busy: bool = False) -> None:
        self.l_status.configure(text=text, style="Error.TLabel" if error else "Muted.TLabel")
        if busy and not self.bar.winfo_ismapped():
            self.bar.pack(side="right")
            self.bar.start(12)
        elif not busy and self.bar.winfo_ismapped():
            self.bar.stop()
            self.bar.pack_forget()

    def _next(self) -> None:
        if self.job is not None or not self.queue:
            return
        path = self.queue.pop(0)
        from .cancel import CancelToken

        settings, env, token = self.app.controller.settings, config.load_env(self.paths), CancelToken()

        def work():
            r = audiofile.transcribe_file(path, settings, env, self.paths, token=token)
            r.text = audiofile.finish_text(r.text, self.paths)
            return r

        seconds = audiofile.duration(path)
        length = f" ({_clock(seconds)})" if seconds else ""
        more = f" · mais {len(self.queue)} na fila" if self.queue else ""
        self._work("file", f"Transcrevendo {path.name}{length}…{more}", work, path=path, token=token)

    def _work(self, kind: str, status: str, fn, **state) -> None:
        """Run `fn` on a thread; _poll hands its result (or error) to the matching *_done."""
        state = self.state = {"kind": kind, "result": None, "error": None, **state}

        def work():
            try:
                state["result"] = fn()
            except Exception as e:
                log.warning("audio window %s: %s", kind, e)
                state["error"] = str(e)

        self._status(status, busy=True)
        self.job = threading.Thread(target=work, name=f"audio-{kind}", daemon=True)
        self.job.start()
        self._buttons()
        self._poll()

    def _poll(self) -> None:
        if not self.top.winfo_exists():
            return
        if self.job is not None and self.job.is_alive():
            self.top.after(150, self._poll)
            return
        self.job = None
        st = self.state
        if st.get("kind") == "summary":
            self._summary_done(st)
        elif st.get("kind") in ("notes", "retry"):
            self._meeting_done(st)
        elif st.get("error"):
            self._status(f"{st['path'].name}: {st['error']}", error=True)
        else:
            self._file_done(st["path"], st["result"])
        self.text.edit_modified(False)   # what the app wrote is not an edit (see copy)
        self._buttons()
        self._next()

    def _file_done(self, path: Path, r) -> None:
        if not r.text.strip():
            self._status(f"{path.name}: nenhuma fala reconhecida.", error=True)
            return
        if self.text.get("1.0", "end").strip():
            self.text.insert("end", "\n\n")
        length = f" · {_clock(r.seconds)}" if r.seconds else ""
        self.text.insert("end", f"{path.name}{length}\n", "head")
        self.text.insert("end", r.text.strip())
        self.text.see("end")
        self.results.append((path, r.text))
        engine = "modelo local" if r.engine == "local" else API.get(r.engine, ("", "", r.engine))[2]
        note = f" {r.notice}" if r.notice else ""
        copied = ""
        if self.v_autocopy.get():
            self._copy_text(r.text.strip())
            copied = " Copiado: cole com Ctrl+V."
        self._status(f"Pronto em {r.elapsed:.1f} s ({engine}).{copied}{note}")
        try:
            self.app.controller.history.add(raw=f"{path.name}: {r.text}", text=r.text, app="audio-file",
                                            mode="file", seconds=r.seconds)
        except Exception:
            pass

    # --- actions ----------------------------------------------------------------------

    def _copy_text(self, text: str) -> None:
        from . import inserter

        try:
            inserter.set_text(text)
        except Exception as e:
            self._status(f"Não consegui copiar: {e}", error=True)

    def copy(self) -> None:
        try:
            text = self.text.get("sel.first", "sel.last")
        except tk.TclError:   # nothing selected: the transcription, or all the text once there is more or you edited it
            whole = len(self.results) != 1 or self.text.edit_modified()
            text = self.text.get("1.0", "end") if whole else self.results[-1][1]
        self._copy_text(text.strip())
        self._status("Copiado: cole com Ctrl+V.")

    def summarize(self) -> None:
        from .cleanup import Cleaner

        text = self.results[-1][1] if self.results else self.text.get("1.0", "end").strip()
        if not text or self.job is not None:
            return
        from .cancel import CancelToken

        controller = self.app.controller
        cleaner = Cleaner(controller.settings.cleanup, config.load_env(self.paths),
                          controller.settings.transcription.vocabulary)
        self._work("summary", "Resumindo…", lambda: cleaner.summarize(text, CancelToken()))

    def _summary_done(self, st: dict) -> None:
        if st["error"]:
            self._status(f"Não consegui resumir: {st['error']}", error=True)
            return
        self.text.insert("end", "\n\nResumo\n", "head")
        self.text.insert("end", st["result"].strip())
        self.text.see("end")
        if self.v_autocopy.get():
            self._copy_text(st["result"].strip())
            self._status("Resumo pronto e copiado.")
        else:
            self._status("Resumo pronto.")

    def save_txt(self) -> None:
        from tkinter import filedialog

        ext, kind = (".md", ("Markdown", "*.md")) if self.meeting else (".txt", ("Texto", "*.txt"))
        name = (self.meeting.folder.name if self.meeting else
                self.results[-1][0].stem if self.results else "transcricao") + ext
        path = filedialog.asksaveasfilename(parent=self.top, title="Salvar transcrição", initialfile=name,
                                            defaultextension=ext, filetypes=[kind])
        if path:
            Path(path).write_text(self.text.get("1.0", "end").strip() + "\n", encoding="utf-8")
            self._status(f"Salvo em {path}")

    # --- a meeting (docs/PLANO-REUNIOES.md, Fase 5) ----------------------------------

    def show_meeting(self, folder: Path) -> None:
        """The meeting's notes (if any) and transcript, with its own actions. Never raises or
        activates the window (the app decides; see App.show_meeting)."""
        from datetime import datetime

        from .meeting import MeetingFolder, duration_text

        m = MeetingFolder.open(folder)
        if m is None:
            self._status(f"Não consegui abrir a reunião {folder.name}.", error=True)
            return
        self.meeting = m
        started = datetime.fromisoformat(m.state["started"])
        self.top.title(f"Reunião — {started:%d/%m %H:%M} ({duration_text(m.seconds)})")
        self.b_save.configure(text=_t("Salvar como…"))
        if not self.meeting_actions.winfo_ismapped():
            self.meeting_actions.pack(fill="x", pady=(self.px(8), 0))
        self._meeting_text()
        self._status(self._meeting_summary())

    def _meeting_text(self) -> None:
        m = self.meeting
        notes = m.folder / "notas.md"
        transcript = (m.transcript.read_text(encoding="utf-8") if m.transcript.is_file() else m.render()).strip()
        self.text.delete("1.0", "end")
        shown = transcript
        if notes.is_file():
            note_text = notes.read_text(encoding="utf-8").strip()
            self.text.insert("end", "Notas\n", "head")
            self.text.insert("end", note_text + "\n\n")
            self.text.insert("end", "Transcrição\n", "head")
            shown = note_text + "\n\n" + transcript
        self.text.insert("end", transcript)
        self.text.edit_modified(False)
        self.results = [(m.transcript, shown)]
        failed = any(p["status"] in ("falhou", "pendente") for p in m.state["pieces"])
        if failed and not self.b_retry.winfo_ismapped():
            self.b_retry.pack(side="left", padx=(self.px(8), 0))
        elif not failed:
            self.b_retry.pack_forget()
        self._buttons()

    def _meeting_summary(self) -> str:
        m = self.meeting
        failed = sum(p["status"] in ("falhou", "pendente") for p in m.state["pieces"])
        if failed:
            return f"{failed} trecho(s) não foram transcritos. Tente de novo (o áudio ficou guardado)."
        if m.state.get("status") == "interrompida":
            return "Ainda há trechos para transcrever: o Bira Flow termina na próxima vez que abrir."
        if m.state.get("notes_error"):
            return f"Transcrição pronta. Notas: {m.state['notes_error']}"
        if m.state.get("notes_pending"):
            return "Transcrição pronta. Gerando as notas…"
        return f"Transcrição pronta em {m.folder}"

    def meeting_notes(self) -> None:
        from .meeting import write_notes

        m, settings = self.meeting, self.app.controller.settings
        if m is not None and self.job is None:
            self._work("notes", "Gerando as notas…", lambda: write_notes(m, settings, config.load_env(self.paths)))

    def meeting_retry(self) -> None:
        from .meeting import retry_failed

        m, settings = self.meeting, self.app.controller.settings
        if m is not None and self.job is None:
            self._work("retry", "Transcrevendo de novo os trechos que falharam…",
                       lambda: retry_failed(m, self.paths, settings, config.load_env(self.paths),
                                            settings.meeting.keep_audio))

    def _meeting_done(self, st: dict) -> None:
        if self.meeting is None:   # "Limpar" while it ran
            return
        if st["error"]:
            self._status(f"Não deu certo: {st['error']}", error=True)
            return
        self._meeting_text()
        if st["kind"] == "notes" and st["result"] is None:
            error = self.meeting.state.get("notes_error")
            self._status(f"Não consegui gerar as notas: {error}" if error else "Nada foi falado: sem notas.",
                         error=bool(error))
        else:
            self._status("Notas prontas." if st["kind"] == "notes" else self._meeting_summary())

    def open_meeting_folder(self) -> None:
        import os

        if self.meeting is not None:
            os.startfile(self.meeting.folder)

    def clear(self) -> None:
        self.text.delete("1.0", "end")
        self.results.clear()
        if self.meeting is not None:
            self.meeting = None
            self.meeting_actions.pack_forget()
            self.b_save.configure(text=_t("Salvar .txt…"))
            self.top.title("Bira Flow — Transcrever áudio")
        self._status("")
        self._buttons()

    def close(self) -> None:
        self.queue.clear()
        if (token := self.state.get("token")) is not None:
            token.cancel()   # a 1 h file must not keep the API or the GPU busy for a closed window
        self.top.destroy()
        for name in ("audio_window", "meeting_window"):
            if getattr(self.app, name, None) is self:
                setattr(self.app, name, None)
