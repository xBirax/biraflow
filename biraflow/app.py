"""Entry point: wires the hook, controller, pill and tray together.

Threads: Tk (main: pill + UI marshalling), keyboard hook, controller, tray, and one
worker per dictation job.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .i18n import t as _t
from . import __version__, autostart, branding, config, i18n, setup_local, win32
from .hotkey import display_hotkey
from .config import Paths

log = logging.getLogger("biraflow")

MEETING_CONSENT = (
    "O Bira Flow vai gravar o seu microfone e o que o PC toca (o outro lado da chamada) e "
    "transcrever tudo numa pasta em Documentos.\n\n"
    "Avise os participantes antes de gravar: gravar alguém sem consentimento pode violar a LGPD "
    "e as regras da sua empresa.\n\nEsta mensagem aparece só na primeira reunião.")

ENV_TEMPLATE = """\
# Chaves opcionais. Sem nenhuma chave tudo roda offline (whisper.cpp local, sem limpeza por LLM).
# Com transcription.engine = "local" o áudio nunca sai do computador; só o texto vai para o LLM.
GROQ_API_KEY=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
AI_GATEWAY_API_KEY=
"""


def setup_logging(paths: Paths, verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(paths.log, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr is not None:  # None under pythonw
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    threading.excepthook = lambda a: log.error("thread %s crashed", a.thread.name,
                                               exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


class UiBridge:
    """Marshals UI calls from any thread onto the Tk main thread."""

    def __init__(self, root, show_pill: bool = True):
        self.root = root
        self.show_pill = show_pill
        self.pill = None
        self.tray = None
        self._menu_pending = False
        self._q: queue.SimpleQueue = queue.SimpleQueue()

    def _call(self, fn) -> None:
        self._q.put(fn)

    def pump(self) -> None:
        while True:
            try:
                fn = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                log.exception("ui call failed")
        self.root.after(30, self.pump)

    def _pill(self, method: str, *args) -> None:
        if self.show_pill and self.pill is not None:
            self._call(lambda: getattr(self.pill, method)(*args))

    def recording(self, mode, started_at, deadline):
        self._pill("recording", mode, started_at, deadline)

    def busy(self, text):
        self._pill("busy", text)

    def flash(self, text, tone="info", ms=1200):
        self._pill("flash", text, tone, ms)

    def preview(self, text):
        self._pill("preview", text)

    def hide(self):
        # always forwarded: a pill shown before "show_pill" was turned off must still go away
        if self.pill is not None:
            self._call(self.pill.hide)

    def error(self, text):
        log.warning("%s", text)
        if self.show_pill:
            self._pill("flash", text, "error", 4500)
        elif self.tray is not None:
            self.tray.notify(text)

    def status(self, state):
        # through the queue: an icon that fails to render must not kill the controller thread
        if self.tray is not None:
            self._call(lambda: self.tray.set_state(state))

    def refresh_menu(self):
        """Rebuild the tray menu on one thread (the Tk one), once per burst: job, controller
        and watcher threads all ask for it, and pystray's rebuild is not thread-safe."""
        if self.tray is None or self._menu_pending:
            return
        self._menu_pending = True

        def run():
            self._menu_pending = False
            self.tray.refresh()

        self._call(run)


class App:
    def __init__(self, paths: Paths, open_files: list[str] | None = None):
        import tkinter as tk

        from .controller import Controller
        from .pill import Pill
        from .tray import Tray

        self.paths = paths
        threading.Thread(target=setup_local.has_nvidia_gpu, name="gpu-check", daemon=True).start()
        settings, errors = config.load_settings(paths)
        self.state = config.load_state(paths)
        i18n.set_language(settings.ui.language)   # the interface language, before any text
        self.root = tk.Tk()
        self.root.withdraw()
        try:
            self.root.iconbitmap(default=str(branding.APP_ICO))  # every window, incl. dialogs
        except tk.TclError:
            pass
        self.ui = UiBridge(self.root, settings.ui.show_pill)
        self.controller = Controller(paths, settings, self.ui, enabled=self.state.get("enabled", True))
        self.ui.pill = Pill(self.root, lambda: self.controller.recorder.level, settings.ui.position,
                            settings.ui.pill_size, signal=lambda: self.controller.recorder.peak)
        self.tray = self.ui.tray = Tray(self)
        self._startup_errors = errors + self.controller.startup_errors
        self.settings_window = None
        self.audio_window = None
        self.meeting_window = None
        self.open_files = open_files or []
        self._capture = None
        self.controller.meeting_active = lambda: self.meeting is not None
        from .winhook import KeyboardHook

        self.hook = KeyboardHook(self._on_key)
        from . import _build_info
        from .updater import Updater

        c = self.controller
        self.updater = Updater(
            paths.local / "updates",
            url=lambda: c.settings.updates.url.strip() or _build_info.UPDATE_URL,
            enabled=lambda: c.settings.updates.auto,
            is_idle=self.is_idle,
            notify=lambda text, tone: self.ui.flash(text, tone, 4000),
            quit_app=self.quit,
        )

    def _on_key(self, vk: int, is_down: bool) -> bool:
        """Hook handler (hook thread): feeds a hotkey capture if one is running,
        otherwise the dictation hotkey tracker."""
        capture = self._capture
        if capture is not None and not capture.done:
            return capture.feed(vk, is_down)
        return self.controller.tracker.handle(vk, is_down)

    def start_capture(self):
        from .hotkey import ChordCapture

        self._capture = ChordCapture()
        return self._capture

    def stop_capture(self) -> None:
        self._capture = None

    # --- used by the tray menu (tray thread) ----------------------------------------

    @property
    def enabled(self) -> bool:
        return self.controller.enabled

    @property
    def last_text(self) -> str | None:
        return self.controller.last_text

    @property
    def hotkey_text(self) -> str:
        return self.controller.hotkey.text

    @property
    def paste_last_text(self) -> str:
        return self.controller.settings.hotkey.paste_last_keys.strip()

    def paste_last(self) -> None:
        # From the tray menu, focus is back on the app the user was in once the menu closes.
        threading.Timer(0.3, lambda: self.controller.post("paste_last", time.monotonic())).start()

    def engine_summary(self) -> str:
        from .cleanup import resolve_provider
        from .transcribe import TranscriptionError, resolve_engine

        s = self.controller.settings
        env = config.load_env(self.paths)
        try:
            engine = resolve_engine(s.transcription, env)
        except TranscriptionError:
            engine = f"{s.transcription.engine} (sem chave)"
        if engine == "local":
            engine = f"local ({s.transcription.local_model})"
        return _t("Transcrição: {0} · Limpeza: {1}").format(engine, resolve_provider(s.cleanup, env))

    def set_enabled(self, enabled: bool) -> None:
        self._cancel_pause()
        self.state["enabled"] = enabled
        config.save_state(self.paths, self.state)
        self.controller.post("set_enabled", enabled)
        self.ui.refresh_menu()

    # Pausing is not saved: after a restart dictation is on again.
    paused_until: float | None = None
    _pause_timer: threading.Timer | None = None

    def pause(self, minutes: int = 60) -> None:
        self._cancel_pause()
        self.paused_until = time.time() + minutes * 60
        self._pause_timer = threading.Timer(minutes * 60, self.resume)
        self._pause_timer.daemon = True
        self._pause_timer.start()
        self.controller.post("set_enabled", False)
        log.info("dictation paused for %d min", minutes)
        self.ui.refresh_menu()

    def resume(self) -> None:
        self._cancel_pause()
        self.controller.post("set_enabled", self.state.get("enabled", True))
        self.ui.refresh_menu()

    def _cancel_pause(self) -> None:
        if self._pause_timer is not None:
            self._pause_timer.cancel()
        self._pause_timer = None
        self.paused_until = None

    def pause_text(self) -> str:
        return time.strftime("%H:%M", time.localtime(self.paused_until)) if self.paused_until else ""

    @property
    def has_failed_audio(self) -> bool:
        return self.controller.has_failed_audio

    def retry_failed(self) -> None:
        # From the tray menu, focus is back on the app the user was in once the menu closes.
        threading.Timer(0.3, lambda: self.controller.post("retry_audio")).start()

    def toggle_autostart(self) -> None:
        try:
            autostart.set_enabled(not autostart.is_enabled())
        except OSError as e:
            self.ui.error(_t("Não foi possível alterar o início automático: {0}").format(e))
        self.ui.refresh_menu()

    def last_learned_text(self) -> str:
        rules = self.controller.last_learned
        return ", ".join(f"{r.heard} → {r.write}" for r, _ in rules)[:60] if rules else ""

    def undo_learned(self) -> None:
        self.controller.post("unlearn")

    def learn_from_screenshot(self) -> None:
        self.controller.post("learn_screenshot")

    def open_audio(self, files: list | None = None) -> None:
        """Tray, "Enviar para", a file dropped on the shortcut: the "Transcrever áudio" window."""
        self.ui._call(lambda: self.show_audio(files or []))

    def show_audio(self, files: list) -> None:
        from .audio_ui import AudioWindow

        if self.audio_window is None or not self.audio_window.top.winfo_exists():
            self.audio_window = AudioWindow(self)
        else:
            _bring_to_front(self.audio_window.top)
        if files:
            self.audio_window.add([Path(f) for f in files])

    def _poll_inbox(self) -> None:
        """Files sent by a second launch ("Enviar para → Bira Flow", a file dropped on the
        shortcut): it writes them here and exits, and this instance opens them."""
        try:
            for req in sorted(inbox_dir(self.paths).glob("*.json")):
                try:
                    files = json.loads(req.read_text(encoding="utf-8"))
                finally:
                    req.unlink(missing_ok=True)
                if isinstance(files, list):
                    self.show_audio([str(f) for f in files])
        except Exception:
            log.exception("reading the inbox")
        self.root.after(700, self._poll_inbox)

    def copy_last(self) -> None:
        from . import inserter

        if self.last_text:
            inserter.set_text(self.last_text)

    def open_settings(self, page: str = "") -> None:
        """Tray: show the settings window (on the Tk thread), optionally on a page."""
        self.ui._call(lambda: self.show_settings(page))

    def show_settings(self, page: str = "") -> None:
        from .settings_ui import SettingsWindow

        if self.settings_window is None:
            self.settings_window = SettingsWindow(self)
        else:
            _bring_to_front(self.settings_window.top)
        if page in self.settings_window.pages:
            self.settings_window.show(page)

    def open_settings_file(self) -> None:
        config.load_settings(self.paths)  # (re)creates the file if missing
        subprocess.Popen(["notepad.exe", str(self.paths.settings)])

    def apply_settings(self, settings, env_updates: dict, enabled: bool | None, autostart_on: bool | None,
                       dictionary=None) -> None:
        """Settings window "Salvar" (Tk thread). `enabled`/`autostart_on` None: unchanged.
        The steps that can fail (keys file, autostart) run first, so a failure leaves the
        settings untouched instead of half applied."""
        if env_updates:
            if not self.paths.env.exists():
                self.paths.env.write_text(ENV_TEMPLATE, encoding="utf-8")
            config.save_user_env(self.paths, env_updates)
        if autostart_on is not None and autostart_on != autostart.is_enabled():
            autostart.set_enabled(autostart_on)
        config.save_settings(self.paths, settings)
        if dictionary is not None:
            dictionary.save(self.paths.dictionary)
        self.ui.show_pill = settings.ui.show_pill
        self.ui.pill.position = settings.ui.position
        self.ui.pill.size = settings.ui.pill_size
        if not settings.ui.show_pill:
            self.ui.pill.hide()
        if enabled is not None:
            self.set_enabled(enabled)
        self.controller.post("reload")
        self.ui.refresh_menu()

    def open_data_folder(self) -> None:
        os.startfile(self.paths.data)

    # --- meetings (docs/PLANO-REUNIOES.md, Fase 5) -----------------------------------

    meeting = None              # the MeetingSession being recorded
    meeting_since = ""          # "14:30", for the tray menu
    _meeting_busy = False       # starting or stopping
    _recovering = False         # recover() of meetings an earlier run left unfinished
    _quitting = False
    _meeting_thread: threading.Thread | None = None
    _meeting_lock = threading.Lock()

    def is_idle(self) -> bool:
        """Updater: no dictation, no meeting, no meeting piece left to transcribe."""
        c = self.controller
        return not c.machine.recording and not c._jobs and not self.meeting_pending

    @property
    def meeting_pending(self) -> bool:
        """A meeting records, starts, stops or is being recovered: no update now."""
        return self.meeting is not None or self._meeting_busy or self._recovering

    def meeting_menu_text(self) -> str:
        # shortcut: the start time, not a running clock: pystray rebuilds the whole menu to change
        # a label, unsafe while it is open. The tooltip has the running time.
        return _t("Encerrar reunião (desde {0})").format(self.meeting_since) if self.meeting else _t("Iniciar reunião")

    def toggle_meeting(self, in_person: bool = False) -> None:
        """Tray (any thread): start or stop the meeting on a thread of its own. A click while
        one is starting or stopping is ignored: a double click never makes two meetings.
        `in_person`: a meeting around a table (MeetingSession's in_person)."""
        with self._meeting_lock:
            if self._quitting:
                return
            if self._meeting_busy:
                self.tray.notify(_t("Aguarde: a reunião está iniciando ou encerrando."))
                return
            self._meeting_busy = True
            self._meeting_thread = threading.Thread(target=self._toggle_meeting, args=(in_person,),
                                                    name="meeting", daemon=True)
        self._meeting_thread.start()

    def _toggle_meeting(self, in_person: bool = False) -> None:
        try:
            if self.meeting is None:
                self._start_meeting(in_person)
            else:
                self._stop_meeting()
        except Exception as e:
            log.exception("meeting")
            self.ui.error(f"Reunião: {e}")
        finally:
            self._meeting_busy = False
            self.ui.refresh_menu()

    def _start_meeting(self, in_person: bool = False) -> None:
        from .meeting import MeetingSession

        if not self.state.get("meeting_consent"):
            if not win32.confirm(MEETING_CONSENT, "Bira Flow: reunião"):
                return
            self.state["meeting_consent"] = True
            config.save_state(self.paths, self.state)
        c = self.controller
        m = c.settings.meeting
        session = MeetingSession(self.paths, c.settings, config.load_env(self.paths), system_audio=m.system_audio,
                                 keep_audio=m.keep_audio, auto_notes=m.auto_notes, max_hours=m.max_hours,
                                 busy=lambda: c.machine.recording or bool(c._jobs),
                                 on_warning=self._meeting_warning, on_limit=self.toggle_meeting,
                                 in_person=in_person)
        session.start()
        self.meeting, self.meeting_since = session, time.strftime("%H:%M")
        self.ui._call(self._meeting_tick)
        if in_person:
            mic = getattr(session, "mic_device", "") or "microfone padrão do Windows"
            self.tray.notify(_t('Reunião presencial iniciada: gravando pelo {0}, sem o áudio do PC. Deixe o notebook no meio da mesa. Encerre pelo ícone da bandeja.').format(mic))
        else:
            self.tray.notify(_t("Reunião iniciada: gravando o microfone")
                             + (_t(" e o áudio do PC.") if m.system_audio else _t(".")) + _t(" Encerre pelo ícone da bandeja."))

    def _meeting_warning(self, text: str) -> None:
        """The pill flashes for seconds; in a meeting nobody looks at it: Windows notification too."""
        self.ui.error(text)
        self.tray.notify(text)

    def _stop_meeting(self) -> None:
        session = self.meeting
        self.tray.notify(_t("Encerrando a reunião: transcrevendo o último trecho…"))
        m = session.meeting
        try:
            session.stop()
        finally:   # even when the last piece failed: the transcript so far, in its window
            self.meeting = None
            self.ui._call(lambda: self.show_meeting(m.folder))
        if m.state.get("notes_pending"):   # the transcript first; the notes take a while
            threading.Thread(target=self._write_notes, args=(m,), name="meeting-notes", daemon=True).start()

    def _write_notes(self, m) -> None:
        """Notes of a meeting that just ended. Quitting meanwhile is fine: "notes_pending" stays
        set and recover() writes them at the next start."""
        from .meeting import write_notes

        write_notes(m, self.controller.settings, config.load_env(self.paths))
        self.ui._call(lambda: self.show_meeting(m.folder, refresh=True))

    def _meeting_tick(self) -> None:
        """Tk thread, every second while recording (and once after): the time in the tooltip."""
        self.tray.update_tooltip()
        if self.meeting is not None:
            self.root.after(1000, self._meeting_tick)

    def _quit_meeting(self) -> None:
        """Quitting while recording: the last piece is written; whatever is not transcribed in a
        few seconds (and the notes) is finished by recover() at the next start."""
        with self._meeting_lock:
            self._quitting = True   # no meeting starts from now on (a click, the max_hours limit)
            busy, thread = self._meeting_busy, self._meeting_thread
        if busy and thread is not None:   # being started or stopped: let it write its last piece
            thread.join(15)
            if thread.is_alive():   # still transcribing: what it leaves, recover() finishes
                return
        session, self.meeting = self.meeting, None
        if session is None:
            return
        self.tray.notify(_t("Encerrando a reunião antes de sair…"))
        session.stop(wait=5)

    def _recover_meetings(self) -> None:
        from .meeting import recover

        # Its own flag: a meeting can start meanwhile (recover() lists the folders before it
        # begins, so it never touches the new one).
        self._recovering = True
        try:
            m = self.controller.settings.meeting
            for folder in recover(self.paths, self.controller.settings, config.load_env(self.paths),
                                  m.keep_audio, m.auto_notes):
                self.tray.notify(_t('Reunião recuperada: {0}').format(folder.folder.name))
        except Exception:
            log.exception("recovering meetings")
        finally:
            self._recovering = False
            self.ui.refresh_menu()

    def open_meetings(self) -> None:
        from .meeting import meetings_dir

        folder = meetings_dir(self.paths)
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    def show_meeting(self, folder: Path, refresh: bool = False) -> None:
        """Tk thread: the meeting's transcript in the audio window, never taking the focus (the
        meeting may end by itself, or seconds after "Encerrar", while you type elsewhere).
        `refresh`: only if that window still shows this meeting (its notes just arrived)."""
        from .audio_ui import AudioWindow

        w = self.meeting_window   # its own window: never mixed with "Transcrever áudio"
        alive = w is not None and w.top.winfo_exists()
        if refresh:
            if alive and w.meeting is not None and w.meeting.folder == folder and w.job is None:
                w.show_meeting(folder)   # re-rendered where it is: not raised, not restored
            return
        if not alive:
            self.meeting_window = w = AudioWindow(self, focus=False)
        elif w.job is not None:   # busy with a file dropped on it: a fresh window for the meeting
            self.meeting_window = w = AudioWindow(self, focus=False)
        else:
            w.show_quietly()
        w.show_meeting(folder)

    def open_log(self) -> None:
        subprocess.Popen(["notepad.exe", str(self.paths.log)])

    def open_readme(self) -> None:
        readme = config.PROJECT_ROOT / "README.md"
        if readme.exists():
            subprocess.Popen(["notepad.exe", str(readme)])

    def quit(self) -> None:
        if self.meeting is not None or self._meeting_busy:   # off the tray thread: stopping takes seconds
            threading.Thread(target=self._quit, name="quit", daemon=False).start()
        else:
            self._quit()

    def _quit(self) -> None:
        try:
            self._quit_meeting()
        except Exception:
            log.exception("stopping the meeting")
        self.controller.post("quit")
        self.ui._call(self.root.quit)

    # --- lifecycle ------------------------------------------------------------------

    def run(self) -> int:
        self.hook.start()
        controller_thread = threading.Thread(target=self.controller.run, name="controller", daemon=True)
        controller_thread.start()
        self.tray.start()
        self.updater.start()
        threading.Thread(target=self._recover_meetings, name="meeting-recover", daemon=True).start()
        signal.signal(signal.SIGINT, lambda *_: self.quit())
        self.root.after(30, self.ui.pump)
        self.root.after(700, self._poll_inbox)
        if self.open_files:
            self.root.after(500, lambda: self.show_audio(self.open_files))

        def check_pause() -> None:
            # wall clock: a Timer does not count the time the PC slept, the "até HH:MM" does
            if self.paused_until and time.time() >= self.paused_until:
                self.resume()
            self.root.after(30_000, check_pause)

        self.root.after(30_000, check_pause)
        updated = self.state.get("last_version") not in (None, __version__)
        if self.state.get("last_version") != __version__:
            self.state["last_version"] = __version__
            config.save_state(self.paths, self.state)
        if self._startup_errors:
            self.ui.error(self._startup_errors[0])
        elif updated:
            self.ui.flash(_t('Bira Flow atualizado para a versão {0}').format(__version__), "ok", 4000)
        else:
            self.ui.flash(_t('Bira Flow pronto · segure {0}').format(display_hotkey(self.hotkey_text)), "ok", 2200)
        log.info("Bira Flow %s started (hotkey %s)", __version__, self.hotkey_text)
        try:
            self.root.mainloop()
        finally:
            self.hook.stop()
            self.controller.post("quit")
            controller_thread.join(2)
            self.tray.stop()
            log.info("Bira Flow stopped")
        return 0


def _bring_to_front(top) -> None:
    """A window opened again from the tray: restored, on top and focused."""
    top.deiconify()
    top.lift()
    top.focus_force()


def inbox_dir(paths: Paths) -> Path:
    return paths.data / "inbox"


def send_to_running(paths: Paths, files: list[Path]) -> None:
    folder = inbox_dir(paths)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f"{time.time_ns()}.tmp"
    tmp.write_text(json.dumps([str(f.resolve()) for f in files]), encoding="utf-8")
    os.replace(tmp, tmp.with_suffix(".json"))   # the app never reads half a request


def clean_tmp(paths: Paths) -> None:
    """Delete recordings left behind by a crash: audio never lingers on disk."""
    for f in [*paths.tmp_dir.glob("rec-*"), *paths.tmp_dir.glob("wake-*")]:
        try:
            f.unlink()
        except OSError:
            pass


def check(paths: Paths) -> int:
    """`--check`: print what Bira Flow would use, without starting it."""
    from .audio import list_input_devices
    from .cleanup import resolve_provider
    from .hotkey import HotkeyError, parse_hotkey
    from .transcribe import TranscriptionError, find_whisper_cli, resolve_engine, resolve_model

    settings, errors = config.load_settings(paths)
    env = config.load_env(paths)
    t = settings.transcription
    print(f"Bira Flow {__version__}")
    print(f"  configurações : {paths.settings}")
    print(f"  chaves (.env) : {', '.join(str(p) for p in config.env_files(paths))}")
    for e in errors:
        print(f"  ! {e}")
    try:
        print(f"  atalho        : {parse_hotkey(settings.hotkey.keys).text}")
    except HotkeyError as e:
        print(f"  ! atalho inválido: {e}")
    for k in config.ENV_KEYS:
        print(f"  {k:<18}: {'definida' if env.get(k) else '-'}")
    try:
        print(f"  transcrição   : {resolve_engine(t, env)}")
    except TranscriptionError as e:
        print(f"  ! transcrição : {e}")
    cli = find_whisper_cli(t, paths)
    model = resolve_model(t, paths)
    print(f"  whisper-cli   : {cli or 'NÃO ENCONTRADO (rode scripts/setup-whisper.ps1)'}")
    print(f"  modelo local  : {model} {'(ok)' if model.is_file() else '(NÃO ENCONTRADO)'}")
    print(f"  limpeza LLM   : {resolve_provider(settings.cleanup, env)}")
    print(f"  saída         : {settings.output.method}, restaurar área de transferência = "
          f"{settings.output.restore_clipboard}")
    print(f"  início c/ Win : {'sim' if autostart.is_enabled() else 'não'}")
    try:
        devices = list_input_devices()
        print(f"  microfones    : {len(devices)}")
        for d in devices[:12]:
            print(f"      - {d}")
    except Exception as e:
        print(f"  ! microfones  : {e}")
    try:
        import sounddevice as sd

        from . import loopback
        from .meeting import meetings_dir

        same = len(loopback.capture_roles(loopback.endpoint_id(loopback.CONSOLE),
                                          loopback.endpoint_id(loopback.COMMUNICATIONS))) == 1
        print(f"  áudio do PC   : {sd.query_devices(kind='output')['name']}"
              f"{'' if same else ' + dispositivo de comunicação'}")
        print(f"  reuniões      : {meetings_dir(paths)}")
    except Exception as e:
        print(f"  ! áudio do PC : {e}")
    return 1 if errors else 0


def transcribe_file(paths: Paths, wav: Path) -> int:
    """`--transcribe FILE`: run an audio file through the whole dictation pipeline (the same
    long-audio path as a long dictation) and print each stage."""
    from . import audiofile, textproc
    from .cancel import CancelToken
    from .cleanup import Cleaner, sanitize
    from .store import Dictionary

    settings, _ = config.load_settings(paths)
    env = config.load_env(paths)
    token = CancelToken()
    t0 = time.monotonic()
    raw = sanitize(audiofile.transcribe_long(audiofile.decode(wav), settings.transcription, env, paths, token))
    t1 = time.monotonic()
    cl, out = settings.cleanup, settings.output
    dictionary = Dictionary.load(paths.dictionary)
    prepared = textproc.prepare(raw, local=cl.local_rules, spoken=cl.spoken_punctuation,
                                send_phrases=out.send_phrases if out.send_command else None,
                                snippets=dictionary.snippets)
    cleaner = Cleaner(cl, env, settings.transcription.vocabulary)
    cleaned = prepared.text
    if cleaner.active and not prepared.snippet_only and prepared.text:
        cleaned = cleaner.clean(prepared.text, token, tokens=bool(prepared.tokens))
    text = textproc.finish(cleaned, prepared, dictionary.replacements)
    t2 = time.monotonic()
    print(f"bruto  ({t1 - t0:.2f}s): {raw}")
    print(f"final  ({t2 - t1:.2f}s, LLM: {cleaner.provider}): {text}" + ("  [+ Enter]" if prepared.send else ""))
    return 0


def selftest(paths: Paths) -> int:
    """`--selftest`: build every piece of the UI and the audio/automation stack, then exit.
    Used to check a packaged build without starting the app (no hotkey, no mutex)."""
    import tkinter as tk

    results: list[tuple[str, str]] = []

    def step(name, fn):
        try:
            fn()
            results.append((name, "ok"))
        except Exception as e:
            results.append((name, f"FALHOU: {type(e).__name__}: {e}"))

    win32.enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()

    def pill():
        from .pill import Pill
        from .statemachine import Mode

        p = Pill(root, lambda: 0.5)
        p.recording(Mode.HOLD, time.monotonic(), time.monotonic() + 60)
        root.update()
        p.hide()

    def settings_window():
        from .settings_ui import SettingsWindow

        class Stub:
            pass

        app = Stub()
        app.root, app.paths, app.enabled = root, paths, True
        app.controller = Stub()
        app.controller.settings, _ = config.load_settings(paths)
        app.settings_window = None
        app.engine_summary = lambda: ""
        app.start_capture = app.stop_capture = app.open_settings_file = lambda *a: None
        app.open_data_folder = app.open_log = app.open_readme = lambda *a: None
        w = SettingsWindow(app, background=True)  # invisible: the self-test never steals focus
        for key in list(w.pages):
            w.show(key)
            root.update()
        w.close()

    def tray_icons():
        from . import branding

        for state in ("idle", "recording", "busy", "disabled"):
            branding.tray_icon(state, False)
        assert branding.APP_ICO.exists()

    def microphone():
        from .audio import list_input_devices

        assert list_input_devices(), "nenhum microfone"

    def ui_automation():
        from .editwatch import UiaReader

        UiaReader().close()

    def pystray_backend():
        import pystray._win32  # noqa: F401

        from . import tray  # noqa: F401  (the tray module itself, not only pystray)

    step("pílula", pill)
    step("janela de configurações", settings_window)
    step("ícones e logo", tray_icons)
    step("microfone (PortAudio)", microphone)
    step("UI Automation (comtypes)", ui_automation)
    step("bandeja (pystray)", pystray_backend)

    def voice_activation():
        import vosk  # loads libvosk.dll: the voice-activation detector can start

        vosk.SetLogLevel(-1)

    step("ativação por voz (Vosk)", voice_activation)

    def audio_files():
        import numpy as np
        import soundfile as sf

        from .audiofile import decode

        wav = paths.tmp_dir / "selftest.ogg"
        sf.write(str(wav), np.zeros(16000, dtype="float32"), 16000, format="OGG", subtype="OPUS")
        try:
            assert decode(wav).size > 0       # libsndfile reads WhatsApp's Ogg/Opus
        finally:
            wav.unlink(missing_ok=True)

    def drag_and_drop():
        import tkinter as tk

        from tkinterdnd2 import TkinterDnD

        r = tk.Tk()
        r.withdraw()
        try:
            TkinterDnD._require(r)            # loads the tkdnd extension
        finally:
            r.destroy()

    def system_audio():
        from .loopback import SystemAudio

        s = SystemAudio()
        s.start()
        got, deadline = 0, time.monotonic() + 3.0   # a busy PC can take over 0.3 s to open the device
        while not got and time.monotonic() < deadline:
            time.sleep(0.1)
            got = s.read().size
        s.stop()
        assert got > 0, "o áudio do PC não entregou nada"

    def stereo_opus():
        import numpy as np
        import soundfile as sf

        ogg = paths.tmp_dir / "selftest-estereo.ogg"
        sf.write(str(ogg), np.zeros((16000, 2), dtype="float32"), 16000, format="OGG", subtype="OPUS")
        try:
            data, rate = sf.read(str(ogg), always_2d=True)
            assert data.shape[1] == 2 and rate == 16000
        finally:
            ogg.unlink(missing_ok=True)

    def meetings_folder():
        from .meeting import meetings_dir

        folder = meetings_dir(paths)
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".selftest"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()

    step("áudios do WhatsApp (Opus)", audio_files)
    step("áudio do PC (loopback)", system_audio)
    step("reunião: Opus estéreo", stereo_opus)
    step("reunião: pasta gravável", meetings_folder)
    step("arrastar e soltar (tkdnd)", drag_and_drop)
    root.destroy()
    for name, result in results:
        print(f"  {name:<28} {result}")
    return 0 if all(r == "ok" for _, r in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="biraflow", description="Ditado por voz para Windows.")
    parser.add_argument("--check", action="store_true", help="mostra a configuração detectada e sai")
    parser.add_argument("--transcribe", metavar="WAV", type=Path, help="transcreve um arquivo e sai")
    parser.add_argument("--selftest", action="store_true", help="testa interface, áudio e dependências e sai")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("files", nargs="*", type=Path,
                        help="áudios para transcrever (\"Enviar para\" ou arrastar sobre o atalho)")
    args = parser.parse_args(argv)

    paths = Paths.default()
    paths.ensure()
    if args.check:
        return check(paths)
    if args.transcribe:
        return transcribe_file(paths, args.transcribe)
    if args.selftest:
        return selftest(paths)

    setup_logging(paths, args.verbose)
    if not win32.acquire_single_instance():
        if args.files:   # the running app opens them
            send_to_running(paths, args.files)
            return 0
        win32.message_box("O Bira Flow já está em execução (veja o ícone na bandeja).")
        return 1
    win32.enable_dpi_awareness()
    branding.set_app_user_model_id()
    autostart.migrate()
    clean_tmp(paths)
    try:
        return App(paths, open_files=[str(f) for f in args.files]).run()
    except Exception as e:
        log.exception("fatal error")
        win32.message_box(f"O Bira Flow não pôde iniciar:\n\n{e}\n\nDetalhes em {paths.log}")
        return 1
