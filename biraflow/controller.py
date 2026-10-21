"""Glue between the keyboard hook, the state machine, the recorder and dictation jobs.

Runs on its own thread and consumes an event queue. Key events come from the hook
(via ChordTracker); job progress comes from worker threads.
"""
from __future__ import annotations

import difflib
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Protocol

import numpy as np

from .i18n import t as _t
from . import appcontext, audiofile, i18n, inserter, textproc, win32
from .appcontext import AppContext
from .audio import RATE, Recorder, level_from_rms, normalize, peak_rms, read_wav, trim_silence, write_wav
from .cancel import Cancelled, CancelToken
from .cleanup import Cleaner, CleanupError, sanitize
from .config import Paths, Settings, load_env, load_settings
from .hotkey import ChordTracker, Hotkey, HotkeyError, parse_hotkey
from .mediamute import MediaMuter
from .sounds import Sounds
from .statemachine import CANCEL, DISCARD, FINISH, MODE, START, Action, DictationMachine, Mode, Phase
from .store import Dictionary, History, Stats, count_words, file_lock
from .transcribe import TranscriptionError, transcribe

log = logging.getLogger(__name__)

DEFAULT_HOTKEY = "ctrl+win"
SHORT_WORDS = 2          # "ok", "pode ser": no LLM round trip, local rules are enough
CONTEXT_SECONDS = 120    # the previous dictation is context for Whisper this long
FAILED_DIR = "falhas"   # audio of dictations whose transcription failed (newest 3, 24 h)
FAILED_KEEP = 3
NO_INSERT = (0.0, 0, 0, -1)   # _last_insert when there is nothing "apagar último ditado" may delete
KEEP_CANCELLED_SECONDS = 20   # a recording this long is kept, not lost, when Esc or a screen lock cuts it


class Ui(Protocol):
    def recording(self, mode: Mode, started_at: float, deadline: float) -> None: ...
    def busy(self, text: str) -> None: ...
    def flash(self, text: str, tone: str = "info", ms: int = 1200) -> None: ...
    def error(self, text: str) -> None: ...
    def hide(self) -> None: ...
    def preview(self, text: str) -> None: ...
    def status(self, state: str) -> None: ...

    def refresh_menu(self) -> None: ...


@dataclass
class Job:
    id: int
    token: CancelToken
    mode: Mode
    context: AppContext
    seconds: float
    target: int = 0                         # window that had focus when dictation started
    after: threading.Event | None = None   # previous job's "done inserting"
    done: threading.Event = field(default_factory=threading.Event)
    retry_path: object = None               # the failed-audio file this job re-transcribes
    wake: str = ""                          # started by voice: this phrase opens the transcript

    def wait_turn(self) -> None:
        """Results are pasted in the order they were spoken."""
        while self.after is not None and not self.after.wait(0.05):
            self.token.check()
        self.token.check()

    def finish(self) -> None:
        """`done` means this job and every earlier one: a job that ended early (nothing heard,
        an error) must not let the next one paste before an earlier one still running."""
        if self.after is not None:
            self.after.wait()
        self.done.set()


def re_last_sentence(text: str, limit: int = 200) -> str:
    """The last sentence of a dictation (Whisper context), at most `limit` characters."""
    import re

    parts = [p for p in re.split(r"(?<=[.!?])\s+|\n+", text.strip()) if p.strip()]
    return (parts[-1] if parts else "")[-limit:]


def _mic_problem(message: str) -> str:
    """Add the fix when Windows' privacy settings are what blocks the microphone."""
    return win32.MIC_BLOCKED if win32.microphone_blocked() else message


def _locale(language: str) -> str:
    """A Whisper language code ("pt") as a Windows locale for the spell checker ("" for auto)."""
    if language in ("", "auto"):
        return ""
    return {"pt": "pt-BR", "en": "en-US", "es": "es-ES", "fr": "fr-FR", "de": "de-DE",
            "it": "it-IT"}.get(language.lower(), language)


def _wake_quietly(hwnd: int) -> None:
    try:
        from .editwatch import wake_chromium

        wake_chromium(hwnd)
    except Exception as e:
        log.debug("could not wake accessibility: %s", e)


def _warm_uia() -> None:
    try:
        from .editwatch import UiaReader

        UiaReader().close()
    except Exception as e:
        log.info("UI Automation unavailable: %s", e)


def extra_hotkey_problem(name: str, hk: Hotkey, main: Hotkey) -> str | None:
    """Command mode is "dictation chord + one more key"; paste-last must be unrelated to
    the dictation chord, or pressing one would also trigger the other."""
    if name == "command":
        if not (hk.contains(main) and len(hk.parts) > len(main.parts)):
            return (f"O atalho do modo comando ({hk.text}) precisa conter o de ditado ({main.text}) "
                    f"e mais uma tecla, ex.: {main.text}+alt.")
    elif main.contains(hk) or hk.contains(main):
        return f"O atalho de colar último ({hk.text}) não pode conter nem estar contido no de ditado ({main.text})."
    return None


class Controller:
    def __init__(self, paths: Paths, settings: Settings, ui: Ui, enabled: bool = True):
        self.paths = paths
        self.settings = settings
        self.ui = ui
        self.events: queue.Queue = queue.Queue()
        self.startup_errors: list[str] = []
        self.hotkey = self._parse_hotkey(settings.hotkey.keys) or parse_hotkey(DEFAULT_HOTKEY)
        self.extras = self._parse_extras(settings, self.startup_errors)
        self.tracker = ChordTracker(self.hotkey, self._key_event, suppress=settings.hotkey.suppress_keys,
                                    extras=self.extras)
        self.machine = DictationMachine()
        self._configure_machine()
        self.recorder = Recorder(settings.recording.input_device, settings.recording.keep_mic_ready)
        self.sounds = Sounds(paths.sounds_dir)
        self.sounds.enabled = settings.ui.sounds
        self.muter = MediaMuter()
        self.history = History(paths.history, settings.history.keep, settings.history.max_items)
        self.stats = Stats(paths.stats)
        self.enabled = enabled
        self.tracker.set_enabled(enabled)
        recent = self.history.entries()
        self.last_text: str | None = recent[0]["text"] if recent else None
        self._context = AppContext()
        self._target = 0
        self._jobs: list[Job] = []  # running transcriptions, oldest first
        self.meeting_active: Callable[[], bool] = lambda: False   # set by the App
        self._job_seq = 0
        self._running = True
        self._status = ""
        self._settings_mtime = self._mtime()
        self._reload_pending = False
        self.last_learned: list = []   # [(rule, previous spelling or None)] from the last edit learned
        self._previous = (0.0, "", "")  # (when, app, text) of the last insertion: context for Whisper
        # what the last insertion typed, for "apagar último ditado": (when, window, chars, key activity)
        self._last_insert = NO_INSERT
        self._last_voice = 0.0          # when the mic last heard something (hands-free silence stop)
        self._mic_warned = False
        self._warned_deadline = 0.0
        self.failed_dir = paths.tmp_dir / FAILED_DIR
        self._prune_failed_audio()
        self._cleanup_warned = -1e9
        self._fallback_warned = -1e9
        from .editwatch import EditWatcher

        self._key_activity = 0  # key presses seen by the hook; the edit watcher reads only after one
        self.edits = EditWatcher(lambda rules: self.post("learned", rules), activity=lambda: self._key_activity)
        if settings.history.learn_from_edits:
            # comtypes generates its UI Automation wrappers on first use (seconds): do it
            # now, so the first edit after the first dictation is not missed
            threading.Thread(target=_warm_uia, name="uia-warmup", daemon=True).start()
        self._prewarm()
        self.wake = None          # voice activation listener ("Ei Bira")
        self._wake_key = None
        self._voice = False       # the current recording was started by voice
        self._configure_wake()

    def _warm_cloud(self) -> None:
        from . import http
        from .cleanup import resolve_provider
        from .transcribe import resolve_engine

        env = load_env(self.paths)
        try:
            engine = resolve_engine(self.settings.transcription, env)
        except TranscriptionError:
            engine = "local"
        gateway = "https://ai-gateway.vercel.sh/v1/models"   # transcription ("gateway") and cleanup ("vercel")
        hosts = {"groq": "https://api.groq.com/openai/v1/models", "openai": "https://api.openai.com/v1/models",
                 "anthropic": "https://api.anthropic.com/v1/models", "vercel": gateway, "gateway": gateway}
        for url in {hosts[p] for p in (engine, resolve_provider(self.settings.cleanup, env)) if p in hosts}:
            http.warm(url)

    def _prewarm(self) -> None:
        """Load the local model on the GPU now, not on the first dictation."""
        from .transcribe import resolve_engine
        from .whisper_server import SERVER, prewarm

        try:
            local = resolve_engine(self.settings.transcription, load_env(self.paths)) == "local"
        except TranscriptionError:
            return
        # the GPU model stays loaded when it is the engine; as a mere fallback it is
        # unloaded after 10 idle minutes, so games and other apps get the VRAM back
        SERVER.idle_limit = None if local else 600.0
        if not self.settings.transcription.keep_loaded:
            SERVER.stop()   # switched off: give the VRAM back now, not at the next restart
        elif local:
            prewarm(self.settings.transcription, self.paths)

    # --- thread-safe entry points -------------------------------------------------

    def post(self, kind: str, payload=None) -> None:
        self.events.put((kind, payload))

    def _key_event(self, kind: str, t: float) -> None:
        self.events.put((kind, t))

    # --- main loop ----------------------------------------------------------------

    def run(self) -> None:
        self._update_status()
        next_stale = next_file = 0.0
        next_prune = time.monotonic() + 600
        while self._running:
            try:
                batch = [self.events.get(timeout=0.05)]
            except queue.Empty:
                batch = []
            # Drain everything queued before ticking, so a press that happened before
            # a deadline is never judged after it. The clock is read first: a slow handler
            # below must not push the tick past a press that is still waiting in the queue.
            now = time.monotonic()
            while True:
                try:
                    batch.append(self.events.get_nowait())
                except queue.Empty:
                    break
            for kind, payload in batch:
                try:
                    self._handle(kind, payload)
                except Exception:   # one failing event must not drop the others (a lost
                    log.exception("controller event %s", kind)   # job_done would hang the queue)
            if self.wake is not None:
                # a meeting too: someone in the room (or the call) saying something close to
                # the phrase would paste text into whatever app is in front
                self.wake.busy = self.machine.recording or bool(self._jobs) or self.meeting_active()
            try:
                self._apply(self.machine.tick(now))
                if now >= next_stale:
                    next_stale = now + 0.25
                    self.tracker.drop_stale(win32.is_key_down)
                    if self.machine.recording:
                        self._watch_recording(now)
                if now >= next_file:
                    next_file = now + 1.0
                    self._check_settings_file()
                if now >= next_prune:   # saved audio and "24h" history expire while running too
                    next_prune = now + 600
                    self._prune_failed_audio()
                    self.history.prune()
            except Exception:
                log.exception("controller loop")
            self._update_status()
        # unmute first: the App waits only 2 s for this thread, and wake.stop() can take 3
        self.muter.restore()
        self.recorder.abort()
        self.recorder.close()
        if self.wake is not None:
            self.wake.stop()
        for job in self._jobs:
            job.token.cancel()
        from .whisper_server import SERVER

        SERVER.stop()
        self.edits.finalize(timeout=0.5)

    def _needs_mask(self) -> bool:
        return self.hotkey.needs_mask or any(h.needs_mask for h in self.extras.values())

    def _watch_recording(self, now: float) -> None:
        """While recording: stop a forgotten hands-free dictation after a long silence, beep
        10 s before the auto-stop, and drop the recording when the PC is locked (nobody is
        dictating on the lock screen)."""
        if win32.session_locked():
            log.info("recording discarded (screen locked)")
            if now - self.machine.started_at >= KEEP_CANCELLED_SECONDS:
                # a long hands-free dictation outlived the screen lock: "Transcrever de novo" has it
                self._keep_failed_audio(self.recorder.stop())
            self._apply(self.machine.escape(now))
            return
        deadline = self.machine.deadline
        if 0 < deadline - now <= 10 and self._warned_deadline != deadline:
            self._warned_deadline = deadline   # once per recording: the auto-stop is 10 s away
            self.sounds.play("warn")
        voice = level_from_rms(max(self.settings.recording.silence_threshold, 0.002) * 1.5)
        if self.recorder.level >= voice:
            self._last_voice = now
        limit = self.settings.wake.end_silence if self._voice else self.settings.recording.handsfree_silence_stop
        if limit > 0 and now - self._last_voice > limit:
            actions = self.machine.finish_handsfree("silence")
            if actions:
                log.info("hands-free stopped after %ss of silence", limit)
                self._apply(actions)

    def _missing_mic_notice(self) -> bool:
        """Once per run: the microphone chosen in the settings is gone (the default is used)."""
        if self.recorder.missing_device and not self._mic_warned:
            self._mic_warned = True
            self.ui.flash(_t('Microfone "{0}" não encontrado: usando o padrão do Windows').format(self.settings.recording.input_device), "info", 4000)
            return True
        return False

    def _ignored_app(self) -> bool:
        apps = {a.strip().lower() for a in self.settings.hotkey.ignore_apps if a.strip()}
        if not apps:
            return False
        exe = appcontext.foreground().exe.lower()
        return exe in apps or exe.removesuffix(".exe") in apps

    def _handle(self, kind: str, payload) -> None:
        m = self.machine
        if kind in ("press", "command", "paste_last") and self._needs_mask():
            inserter.tap_mask_key()  # keep Win/Alt from opening the Start menu / menu bar
        if kind in ("press", "command") and not m.recording and self._ignored_app():
            log.info("hotkey ignored: %s is in ignore_apps", appcontext.foreground().exe)
            return
        if kind == "voice_call":
            self._voice_call()
            return
        if kind == "press":
            self._apply(m.press(payload))
        elif kind == "release":
            self._apply(m.release(payload))
        elif kind == "command":
            self._apply(m.command(payload))
        elif kind == "paste_last":
            self._paste_last()
        elif kind == "retry_audio":
            self._retry_audio()
        elif kind == "job_cancelled":
            if self._end_job(payload) and not m.recording:
                self.ui.flash(_t("Ditado cancelado"), "info", 1500)
        elif kind == "job_note":
            job_id, message = payload
            if self._end_job(job_id) and not m.recording:
                self.ui.flash(message, "info", 2500)
        elif kind == "other":
            self._key_activity += 1
            self._apply(m.other_key(payload))
        elif kind == "esc":
            self._apply(m.escape(payload))
        elif kind == "job_stage":
            job_id, text = payload
            if self._jobs and self._jobs[0].id == job_id and not m.recording:
                self.ui.busy(text)
        elif kind == "job_done":
            job_id, text = payload
            if self._end_job(job_id):
                if text:
                    self._set_last_text(text)
                if m.recording:
                    pass  # the pill is showing the next dictation
                elif self._missing_mic_notice():
                    pass
                elif self._jobs:
                    self.ui.busy(_t("Transcrevendo…"))
                elif text and self.settings.ui.show_preview:
                    self.ui.preview(text)
                elif text:
                    self.ui.hide()
                else:
                    self.ui.flash(_t("Nada foi reconhecido"), "info", 1500)
        elif kind == "job_held":
            job_id, text, message = payload
            if self._end_job(job_id):
                self._set_last_text(text)
                if len(text) >= 1000:   # a long dictation: say how much is waiting for Ctrl+V
                    message += f" ({len(text):,} caracteres)".replace(",", ".")
                if not m.recording:
                    self.ui.flash(message, "info", 4500)
        elif kind == "job_failed":
            job_id, message = payload
            if self._end_job(job_id):
                if m.recording:
                    log.warning("%s", message)  # don't cover the pill of the current dictation
                else:
                    self.ui.error(message)
        elif kind == "api_fallback":
            # transcribed locally because the API failed; say why, at most every 10 minutes
            now = time.monotonic()
            if now - self._fallback_warned > 600:
                self._fallback_warned = now
                self.ui.flash(_t('Usando o modelo local: {0}').format(payload)[:90], "info", 5000)
        elif kind == "cleanup_failed":
            # The text was inserted unrefined; say so, at most every 10 minutes.
            now = time.monotonic()
            if now - self._cleanup_warned > 600:
                self._cleanup_warned = now
                log.warning("refinement skipped: %s", payload)
                self.ui.flash(_t('Refinamento indisponível: {0}').format(payload)[:90], "error", 5000)
        elif kind == "learned":
            self._learn(payload)
        elif kind == "unlearn":
            self._unlearn()
        elif kind == "learn_screenshot":
            threading.Thread(target=self._learn_screenshot, name="learn-screenshot", daemon=True).start()
        elif kind == "learned_explicit":
            self._learn(payload, explicit=True)
        elif kind == "set_enabled":
            self._set_enabled(bool(payload))
        elif kind == "reload":
            self._reload_pending = True   # applied once idle (not mid-recording or mid-job)
            self._check_settings_file()
        elif kind == "quit":
            self._running = False

    def _apply(self, actions: list[Action]) -> None:
        for a in actions:
            if a.kind == START:
                self._start_recording(a.mode or Mode.HOLD)
            elif a.kind == MODE:
                self.ui.recording(a.mode, self.machine.started_at, self.machine.deadline)
            elif a.kind == FINISH:
                self._finish(a)
            elif a.kind == DISCARD:
                self.recorder.abort()
                self._end_voice()
                self.muter.restore()
                if a.reason != "tap":
                    log.info("recording discarded (%s)", a.reason)
                if a.reason == "esc":
                    self.sounds.play("cancel")
                    self.ui.flash(_t("Cancelado"), "info", 900)
                else:
                    self.ui.hide()
            elif a.kind == CANCEL:
                for job in self._jobs:
                    job.token.cancel()
                    log.info("job %d cancelled by Esc", job.id)
                self._jobs.clear()
                self.machine.busy = False
                self.sounds.play("cancel")
                self.ui.flash(_t("Cancelado"), "info", 900)

    # --- recording ------------------------------------------------------------------

    def _start_recording(self, mode: Mode = Mode.HOLD) -> None:
        try:
            self._context = appcontext.foreground()  # the app being dictated into
        except Exception:
            self._context = AppContext()
        self._target = win32.foreground_root()
        self.edits.stop()
        if self.settings.history.learn_from_edits and self._target:
            # browsers/chat apps expose the message box to UI Automation only after a
            # screen-reader-style request; make it now, while you are still talking
            target = self._target
            threading.Thread(target=_wake_quietly, args=(target,), name="uia-wake", daemon=True).start()
        # the pill first: opening the microphone takes 100–300 ms and the pill should not lag
        self.ui.recording(mode, self.machine.started_at, self.machine.deadline)
        self._last_voice = time.monotonic()
        if self._voice:
            self._last_voice += 2.0   # a moment to start talking after the phrase
            self.recorder.start_external()
            if self.wake is None or not self.wake.take_over(self.recorder.feed):
                self.recorder.abort()
                self._voice = False
                self.machine.reset()
                self.ui.hide()
                return
            self.sounds.play("start")
            self._warm_cloud()
            return
        try:
            self.recorder.start()
        except Exception as e:
            log.warning("could not start recording: %s", e)
            self.machine.reset()
            self.ui.error(_mic_problem(f"Microfone indisponível: {e}"))
            return
        self.sounds.play("start")
        self._warm_cloud()  # TLS handshake runs while you talk, not after you stop
        if self.settings.ui.mute_media and not self.meeting_active():   # the meeting records the PC audio
            self.muter.mute()   # never raises: no output device is logged inside

    def _finish(self, action: Action) -> None:
        try:
            self._finish_recording(action)
        except Exception as e:
            log.exception("finishing the recording")
            self.recorder.abort()
            self.muter.restore()
            self.machine.reset()
            self.ui.error(f"Erro ao encerrar a gravação: {e}")

    def _finish_recording(self, action: Action) -> None:
        audio = self.recorder.stop()
        voice = self._end_voice()
        self.muter.restore()
        rec = self.settings.recording
        if action.reason == "timeout":
            log.info("auto-stopped after %ss", rec.max_seconds)
        if audio.size < rec.min_seconds * RATE:
            log.info("recording dropped: too short (%.2fs)", audio.size / RATE)
            self.machine.reset()
            self.ui.hide()
            return
        level = peak_rms(audio)
        if rec.silence_threshold > 0 and level < rec.silence_threshold:
            log.info("recording dropped: no speech (%.1fs, peak level %.4f < silence_threshold %.4f)",
                     audio.size / RATE, level, rec.silence_threshold)
            self.machine.reset()
            if level == 0.0 and win32.microphone_blocked():
                self.ui.error(_mic_problem("Nenhuma fala detectada"))
            else:
                self.ui.flash(_t("Nenhuma fala detectada"), "info", 1500)
            return
        self.sounds.play("stop")
        getattr(self.recorder, "refresh_devices", lambda: None)()  # pick up a newly plugged/default mic for next time
        audio = trim_silence(audio)
        if rec.normalize:
            audio = normalize(audio)
        self._start_job(audio, action.mode or Mode.HOLD, wake=self.settings.wake.phrase if voice else "")

    def _start_job(self, audio: np.ndarray, mode: Mode, retry_path=None, wake: str = "") -> None:
        self._job_seq += 1
        job = Job(self._job_seq, CancelToken(), mode, self._context, audio.size / RATE,
                  target=self._target, after=self._jobs[-1].done if self._jobs else None, retry_path=retry_path,
                  wake=wake)
        self._jobs.append(job)
        self.machine.busy = True
        self.ui.busy(_t("Executando comando…") if job.mode is Mode.COMMAND
                     else _t('Transcrevendo {0:.0f} min…').format(job.seconds / 60) if job.seconds >= 60
                     else _t('Transcrevendo {0:.0f} s…').format(job.seconds) if job.seconds >= 15 else _t("Transcrevendo…"))
        target = self._run_command_job if job.mode is Mode.COMMAND else self._run_job
        threading.Thread(target=target, args=(job, audio, self.settings),
                         name=f"job-{job.id}", daemon=True).start()

    def _retry_audio(self) -> None:
        """Transcribe again the audio of a dictation whose transcription failed (no
        internet and no local model, API down...), into the window in front now."""
        if self.machine.recording or self._jobs:
            return
        files = self._failed_files()
        try:
            path = files[-1]          # the most recent one
            audio = read_wav(path)
        except (IndexError, OSError, EOFError, ValueError) as e:
            log.info("no failed audio to retry: %s", e)
            self.ui.flash(_t("Nenhum áudio guardado para transcrever"), "info", 1500)
            return
        self._context = appcontext.foreground()
        self._target = win32.foreground_root()
        self._start_job(audio, Mode.HOLD, retry_path=path)

    def _paste_last(self) -> None:
        if self.machine.recording or self._jobs:
            return
        if not self.last_text:
            self.ui.flash(_t("Nenhum ditado para colar ainda"), "info", 1500)
            return
        text, output = self.last_text, replace(self.settings.output, trailing_space=False)

        self._last_insert = NO_INSERT  # "apagar último ditado" must not count this paste

        def run() -> None:
            try:
                restore = inserter.insert(text, output, lambda: None)
                if restore:
                    restore()
            except Exception as e:
                self.ui.error(f"Não foi possível colar: {e}")

        threading.Thread(target=run, name="paste-last", daemon=True).start()

    # --- jobs (worker threads) ------------------------------------------------------

    def _transcribe(self, job: Job, audio: np.ndarray, settings: Settings, env: dict) -> str:
        t0 = time.monotonic()
        notices: list[str] = []
        cfg = self._transcription_cfg(settings, job)
        if audio.size > audiofile.SHORT_SECONDS * RATE:   # long dictation: Opus pieces
            raw = sanitize(audiofile.transcribe_long(audio, cfg, env, self.paths, job.token, notices))
        else:
            wav = self.paths.tmp_dir / f"rec-{os.getpid()}-{job.id}.wav"
            try:
                write_wav(wav, audio)
                raw = sanitize(transcribe(wav, cfg, env, self.paths, job.token, notices))
            finally:
                wav.unlink(missing_ok=True)
        if notices:
            self.post("api_fallback", notices[0])
        job.token.check()
        log.info("job %d: %.1fs of audio transcribed in %.2fs", job.id, job.seconds, time.monotonic() - t0)
        return raw

    def _transcription_cfg(self, settings: Settings, job: Job):
        """Whisper hints: your vocabulary plus the spellings you taught the dictionary
        (so "GitHub" is heard right, not just fixed afterwards), and the previous
        sentence when it went to the same app a moment ago (names, topic, tone)."""
        cfg = settings.transcription
        hints = audiofile.hints(cfg.vocabulary, self.paths)
        if job.wake:
            hints.insert(0, job.wake)   # so Whisper writes the phrase as such and it can be removed
        when, app, text = self._previous
        context = ""
        if text and app == job.context.exe and time.monotonic() - when < CONTEXT_SECONDS:
            context = re_last_sentence(text)
        return replace(cfg, vocabulary=hints, context=context)

    def _protected_words(self, settings: Settings) -> set[str]:
        learned = {r.write for r in Dictionary.load(self.paths.dictionary).replacements}
        return set(settings.transcription.vocabulary) | learned

    def _fit_to_cursor(self, text: str, settings: Settings) -> str:
        """Mid-sentence, start in lowercase and add the missing space."""
        if not settings.output.smart_insert:
            return text
        from .editwatch import text_before_caret

        before = text_before_caret()
        if before is None:
            return text
        from . import spell

        lang = _locale(settings.transcription.language)
        return textproc.adapt_to_context(text, before, self._protected_words(settings),
                                         lambda w: spell.is_real_word(w, lang))

    def _style(self, settings: Settings, ctx: AppContext) -> str:
        if not settings.styles.enabled:
            return "neutral"
        return getattr(settings.styles, appcontext.classify(ctx), "neutral")

    def _record(self, job: Job, raw: str, text: str) -> None:
        try:
            self.history.add(raw=raw, text=text, app=job.context.exe, mode=job.mode.value, seconds=job.seconds)
            self.stats.add(count_words(text), job.seconds)
        except OSError as e:
            log.warning("could not save history: %s", e)

    def _run_job(self, job: Job, audio: np.ndarray, settings: Settings) -> None:
        restore = None
        raw = text = ""
        try:
            env = load_env(self.paths)  # re-read so new keys apply without a restart
            raw = self._transcribe(job, audio, settings, env)
            if job.wake:
                from .wakeword import strip_phrase

                raw = strip_phrase(raw, job.wake)
            if job.retry_path is not None:
                job.retry_path.unlink(missing_ok=True)
                self.ui.refresh_menu()
            if not raw:
                self.post("job_done", (job.id, ""))
                return
            if textproc.wants_undo(raw):
                job.wait_turn()
                self.post("job_note", (job.id, self._undo_last(job)))
                return
            if textproc.wants_cancel(raw):
                log.info("job %d: cancelled by voice", job.id)
                self.post("job_cancelled", job.id)
                return
            cl, out = settings.cleanup, settings.output
            dictionary = Dictionary.load(self.paths.dictionary)
            # the phrases of the interface language are understood too ("pressione enter",
            # "press enter", "pulsa intro"...), whatever the user has in the settings
            send_phrases = (list(out.send_phrases) + i18n.spoken_list("send")) if out.send_command else None
            prepared = textproc.prepare(raw, local=cl.local_rules, spoken=cl.spoken_punctuation,
                                        send_phrases=send_phrases,
                                        snippets=dictionary.snippets)
            style = self._style(settings, job.context)
            cleaner = Cleaner(cl, env, settings.transcription.vocabulary)
            cleaned = prepared.text
            if (cleaner.active and not prepared.snippet_only and prepared.text
                    and count_words(prepared.text) > SHORT_WORDS):
                self.post("job_stage", (job.id, _t("Refinando…")))
                t1 = time.monotonic()
                cleaned = cleaner.clean(prepared.text, job.token, style=style, tokens=bool(prepared.tokens),
                                        progress=lambda i, n: self.post("job_stage", (job.id, _t('Refinando {0}/{1}…').format(i, n))))
                if cleaner.last_error:
                    self.post("cleanup_failed", cleaner.last_error)
                log.info("job %d: cleanup via %s (%s style) in %.2fs", job.id, cleaner.provider, style,
                         time.monotonic() - t1)
            text = textproc.finish(cleaned, prepared, dictionary.replacements, style)
            if not text:
                if prepared.send:
                    job.wait_turn()
                    self._last_insert = NO_INSERT
                    # only the send phrase: Enter, but never into another window or with
                    # Ctrl/Win still held (Win+Ctrl+Enter starts the Narrator)
                    if inserter.wait_for_modifiers_released() and win32.foreground_root() == job.target:
                        inserter.press_enter()
                    else:
                        self.post("job_note", (job.id, _t("Enter não enviado: a janela mudou ou havia teclas pressionadas")))
                        return
                self.post("job_done", (job.id, ""))
                return
            job.wait_turn()
            self.edits.stop()  # learn from edits to the previous dictation; never wait for it
            text = self._fit_to_cursor(text, settings)
            used = replace(out, trailing_space=out.trailing_space and not prepared.send)
            activity = self._key_activity   # keys typed from here on block "apagar último ditado"
            try:
                restore = inserter.insert(text, used, job.token.check, target=job.target)
            except inserter.NotPasted as e:
                log.info("job %d: not pasted (%s)", job.id, e)
                self._record(job, raw, text)
                self.post("job_held", (job.id, text, str(e)))
                return
            if prepared.send:
                time.sleep(0.05)
                inserter.press_enter()
            log.info("job %d: inserted %d chars (%s%s)", job.id, len(text), out.method,
                     ", Enter" if prepared.send else "")
            if restore:  # before the next job may touch the clipboard
                try:
                    restore()
                except inserter.ClipboardError as e:   # the text is pasted: only the old clipboard is lost
                    log.warning("job %d: clipboard not restored (%s)", job.id, e)
                restore = None
            self._previous = (time.monotonic(), job.context.exe, text)
            if prepared.send:
                self._last_insert = NO_INSERT
            else:
                typed = textproc.backspace_count(text) + (
                    1 if used.trailing_space and not text.endswith((" ", "\n")) else 0)
                self._last_insert = (time.monotonic(), job.target, typed, activity)
            if settings.history.learn_from_edits and not prepared.send:
                self.edits.watch(text, job.target)
            self._record(job, raw, text)
            self.post("job_done", (job.id, text))
        except Cancelled:
            # Esc reaches the hook from any app: minutes of dictation are not thrown away by it
            if job.seconds >= KEEP_CANCELLED_SECONDS and self._keep_failed_audio(audio, job):
                log.info("job %d: cancelled; its audio waits in Transcrever de novo", job.id)
        except TranscriptionError as e:
            log.warning("job %d failed: %s", job.id, e)
            message = str(e)
            if self._keep_failed_audio(audio, job):
                message += " O áudio foi guardado: bandeja → Transcrever de novo."
            self.post("job_failed", (job.id, message))
        except Exception as e:
            if isinstance(e, inserter.ClipboardError):
                log.warning("job %d failed: %s", job.id, e)
                message = str(e)
            else:
                log.exception("job %d crashed", job.id)
                message = f"Erro: {e}"
            if text:   # transcribed already: keep it for "Colar último ditado"
                try:
                    self._record(job, raw, text)
                except Exception:
                    pass
                self.post("job_held", (job.id, text, message + " O texto ficou em Colar último ditado."))
            else:
                if self._keep_failed_audio(audio, job):
                    message += " O áudio foi guardado: bandeja → Transcrever de novo."
                self.post("job_failed", (job.id, message))
        finally:
            job.finish()

    def _undo_last(self, job: Job) -> str:
        """"Apagar último ditado": backspace over the last insertion, but only when nothing
        else could be deleted by mistake (same window, nothing typed since, 2 minutes)."""
        when, target, chars, activity = self._last_insert
        if (not chars or time.monotonic() - when > 120 or activity != self._key_activity
                or target != job.target or win32.foreground_root() != target):
            return "Nada para apagar: só funciona logo depois de colar, sem digitar nada no meio"
        if not inserter.wait_for_modifiers_released():   # Ctrl+Backspace would delete whole words
            return "Nada apagado: solte as teclas e diga de novo"
        inserter.backspace(chars)
        self._last_insert = NO_INSERT
        self._previous = (0.0, "", "")
        log.info("job %d: undid the last dictation (%d chars)", job.id, chars)
        return "Último ditado apagado"

    def _keep_failed_audio(self, audio: np.ndarray, job: Job | None = None) -> bool:
        """A dictation is never lost: its audio waits for "Transcrever de novo"."""
        if audio is None or not audio.size:
            return False
        retry = getattr(job, "retry_path", None)
        if retry is not None and retry.exists():
            # a retry that failed again: the same file stays (a copy would push the
            # other saved dictations out of the "newest few")
            try:
                os.utime(retry)
            except OSError:
                pass
            return True
        try:
            self.failed_dir.mkdir(parents=True, exist_ok=True)
            write_wav(self.failed_dir / f"falha-{time.time_ns()}.wav", audio)  # one file per failure
            self._prune_failed_audio()
            self.ui.refresh_menu()
            return True
        except OSError as e:
            log.warning("could not keep the failed audio: %s", e)
            return False

    def _failed_files(self) -> list:
        try:
            return sorted(self.failed_dir.glob("falha-*.wav"))
        except OSError:
            return []

    def _prune_failed_audio(self) -> None:
        """Audio does not linger: only the newest few, for at most a day."""
        files = self._failed_files()
        for i, f in enumerate(files):
            try:
                if i < len(files) - FAILED_KEEP or time.time() - f.stat().st_mtime > 86400:
                    f.unlink()
            except OSError:
                pass

    @property
    def has_failed_audio(self) -> bool:
        return bool(self._failed_files())

    def _run_command_job(self, job: Job, audio: np.ndarray, settings: Settings) -> None:
        """Command mode: copy the selection, transcribe the instruction, let the LLM
        rewrite the selection (or write new text), paste the result over it."""
        saved = seq = None
        clipboard_settled = False   # True once the clipboard holds what it should (result or restored)
        try:
            env = load_env(self.paths)
            cleaner = Cleaner(settings.cleanup, env, settings.transcription.vocabulary)
            if not cleaner.active:
                raise CleanupError("O modo comando precisa de um provedor de LLM (aba Chaves de API).")
            job.wait_turn()  # earlier dictations land first; the selection is read after them
            saved = inserter.snapshot()
            # Ctrl+C in a terminal would interrupt the running program: no selection there.
            selection = "" if job.context.is_terminal else inserter.copy_selection()
            seq = inserter.sequence_number()
            job.token.check()
            instruction = self._transcribe(job, audio, settings, env)
            if not instruction:
                self.post("job_done", (job.id, ""))
                return
            self.post("job_stage", (job.id, _t("Executando comando…")))
            t1 = time.monotonic()
            result = cleaner.command(instruction, selection, job.token)
            log.info("job %d: command via %s in %.2fs (selection %d chars)", job.id, cleaner.provider,
                     time.monotonic() - t1, len(selection))
            try:
                inserter.insert(result, replace(settings.output, restore_clipboard=False, trailing_space=False),
                                job.token.check, target=job.target)
            except inserter.NotPasted as e:
                clipboard_settled = True   # the result waits on the clipboard for Ctrl+V
                self._record(job, instruction, result)
                self.post("job_held", (job.id, result, str(e)))
                return
            clipboard_settled = True
            self._last_insert = NO_INSERT   # "apagar último ditado" must not cut into this result
            if settings.output.restore_clipboard and saved is not None:
                seq = inserter.sequence_number()
                time.sleep(0.6)
                if inserter.sequence_number() == seq:  # not if the user copied something meanwhile
                    inserter.restore(saved)
            self._record(job, instruction, result)
            self.post("job_done", (job.id, result))
        except Cancelled:
            pass
        except (TranscriptionError, CleanupError, inserter.ClipboardError) as e:
            log.warning("job %d failed: %s", job.id, e)
            self.post("job_failed", (job.id, str(e)))
        except Exception as e:
            log.exception("job %d crashed", job.id)
            self.post("job_failed", (job.id, _t('Erro: {0}').format(e)))
        finally:
            # Esc, an error or an empty instruction after Ctrl+C: give the user's clipboard back
            # (unless they copied something else while the command ran)
            if (saved is not None and not clipboard_settled and settings.output.restore_clipboard
                    and (seq is None or inserter.sequence_number() == seq)):
                try:
                    inserter.restore(saved)
                except Exception as e:
                    log.warning("job %d: clipboard not restored (%s)", job.id, e)
            job.finish()

    def _learn_screenshot(self) -> None:
        """Tray "Aprender com o print copiado": read the corrections shown in a screenshot."""
        from . import vision

        image = vision.clipboard_image()
        if image is None:
            self.ui.flash(_t("Copie um print primeiro (Win+Shift+S) e tente de novo"), "info", 3500)
            return
        self.ui.busy(_t("Lendo o print…"))
        try:
            recent = [e.get("text", "") for e in self.history.entries()[:15]]
            found = vision.corrections_from_image(image, recent, load_env(self.paths), self.settings.cleanup)
        except Exception as e:
            log.warning("screenshot learning failed: %s", e)
            self.ui.flash(_t('Não consegui ler o print: {0}').format(e)[:110], "error", 5000)
            return
        if not found:
            self.ui.flash(_t("Nenhuma correção encontrada no print"), "info", 3000)
            return
        self.post("learned_explicit", [textproc.Replacement(f.heard, f.write) for f in found])

    def _learn(self, rules, explicit: bool = False) -> None:
        """Corrections learned from an edit made in the app right after a paste, or
        (explicit) ones you pointed at in a screenshot."""
        if not explicit:
            rules = self._typo_fixes(rules)
        if not rules:
            return
        with file_lock(self.paths.dictionary):   # also the settings window's lock
            dictionary = Dictionary.load(self.paths.dictionary)
            before = {r.heard.lower(): r.write for r in dictionary.replacements}
            added = dictionary.add_replacements(rules)
            if added:
                dictionary.save(self.paths.dictionary)
                self.last_learned = [(r, before.get(r.heard.lower())) for r in added]
                self.ui.refresh_menu()
        if explicit and not added:
            self.ui.flash(_t("Essas correções já estavam no Dicionário"), "info", 2500)
        if added and (explicit or (not self.machine.recording and not self._jobs)):
            self.ui.flash(_t("Aprendido: ") + _t(", ").join(f"{r.heard} → {r.write}" for r in added), "ok", 3500)

    def _typo_fixes(self, rules: list) -> list:
        """Keep only transcription errors. Between two ordinary words ("casa" -> "caso",
        "esta" -> "está") the fix was an edit of what you meant: a rule would rewrite every
        "casa" from now on. But Whisper also writes a real word for a name, brand or acronym
        it does not know ("cloud" -> "Claude", "DR" -> "ADR", "codec" -> "codex"): that is
        learned, as long as the two sound alike ("João" -> "José" is someone else)."""
        from . import spell

        lang = _locale(self.settings.transcription.language)
        kept = []
        for r in rules:
            if r.heard.lower() != r.write.lower() and spell.is_real_word(r.heard, lang) is True:
                ordinary = r.write.islower() and spell.is_real_word(r.write, lang) is True
                if ordinary or difflib.SequenceMatcher(None, r.heard.lower(), r.write.lower()).ratio() < 0.6:
                    log.info("not learned: \"%s\" -> \"%s\" (an edit of what was said)", r.heard, r.write)
                    continue
            kept.append(r)
        return kept

    def _unlearn(self) -> None:
        """Undo the corrections learned from the last edit (a fix that was not a typo)."""
        learned, self.last_learned = self.last_learned, []
        if not learned:
            return
        with file_lock(self.paths.dictionary):   # also the settings window's lock
            dictionary = Dictionary.load(self.paths.dictionary)
            for rule, previous in learned:
                for i, r in enumerate(dictionary.replacements):
                    if r.heard.lower() == rule.heard.lower() and r.write == rule.write:
                        if previous is None:
                            del dictionary.replacements[i]
                        else:
                            dictionary.replacements[i] = textproc.Replacement(r.heard, previous)
                        break
            dictionary.save(self.paths.dictionary)
        log.info("undid %d learned correction(s)", len(learned))
        self.ui.refresh_menu()
        self.ui.flash(_t("Aprendizado desfeito: ") + _t(", ").join(f"{r.heard} → {r.write}" for r, _ in learned),
                      "info", 2500)

    def _set_last_text(self, text: str) -> None:
        first = self.last_text is None
        self.last_text = text
        if first:   # pystray builds the menu ahead: "Colar/Copiar último ditado" were greyed out
            self.ui.refresh_menu()

    def _end_job(self, job_id: int) -> bool:
        """Forget a finished job; False if it was already cancelled."""
        for i, job in enumerate(self._jobs):
            if job.id == job_id:
                del self._jobs[i]
                self.machine.busy = bool(self._jobs)
                return True
        return False

    # --- settings & state -----------------------------------------------------------

    def _parse_hotkey(self, spec: str) -> Hotkey | None:
        try:
            return parse_hotkey(spec)
        except HotkeyError as e:
            self.startup_errors.append(f"Atalho inválido: {e}")
            return None

    @staticmethod
    def _parse_extras(settings: Settings, errors: list[str]) -> dict[str, Hotkey]:
        extras = {}
        try:
            main = parse_hotkey(settings.hotkey.keys)
        except HotkeyError:
            main = parse_hotkey(DEFAULT_HOTKEY)
        for name, spec, label in (("command", settings.hotkey.command_keys, "modo comando"),
                                  ("paste_last", settings.hotkey.paste_last_keys, "colar último ditado")):
            if not spec.strip():
                continue
            try:
                hk = parse_hotkey(spec)
            except HotkeyError as e:
                errors.append(f"Atalho do {label} inválido: {e}")
                continue
            problem = extra_hotkey_problem(name, hk, main)
            if problem:
                errors.append(problem)
                continue
            extras[name] = hk
        return extras

    def _configure_machine(self) -> None:
        hk, rec = self.settings.hotkey, self.settings.recording
        self.machine.tap_max = hk.tap_max_ms / 1000
        self.machine.double_tap_window = hk.double_tap_ms / 1000
        self.machine.max_seconds = max(5, rec.max_seconds)

    def _mtime(self) -> int:
        try:
            return self.paths.settings.stat().st_mtime_ns
        except OSError:
            return 0

    def _check_settings_file(self) -> None:
        mtime = self._mtime()
        if mtime != self._settings_mtime:
            self._settings_mtime = mtime
            self._reload_pending = True
        if self._reload_pending and self.machine.phase is Phase.IDLE and not self._jobs:
            self._reload()

    def _reload(self) -> None:
        self._reload_pending = False
        self._settings_mtime = self._mtime()  # don't reload again for the same write
        settings, errors = load_settings(self.paths)
        try:
            hotkey = parse_hotkey(settings.hotkey.keys)
        except HotkeyError as e:
            errors.insert(0, f"Atalho inválido: {e}")
            hotkey = self.hotkey
            settings.hotkey.keys = self.hotkey.text
        self.settings = settings
        i18n.set_language(settings.ui.language)   # the user may have changed it
        self.hotkey = hotkey
        self.extras = self._parse_extras(settings, errors)
        self.tracker.configure(hotkey, settings.hotkey.suppress_keys, self.extras)
        self._configure_machine()
        if self.recorder.device != settings.recording.input_device:
            self.recorder.close()   # a mic kept ready would keep recording from the old device
        self.recorder.device = settings.recording.input_device
        self.recorder.keep_ready = settings.recording.keep_mic_ready
        self._mic_warned = False
        self.sounds.enabled = settings.ui.sounds
        self.history.keep = settings.history.keep
        self.history.max_items = settings.history.max_items
        self.history.prune()
        self._prewarm()  # restarts the GPU server if the model or language changed
        self._configure_wake()
        self.ui.refresh_menu()   # the tray shows the hotkey and the engines
        log.info("settings reloaded (hotkey %s)", hotkey.text)
        if self.machine.recording:
            for e in errors:
                log.warning("%s", e)
        elif errors:
            self.ui.error(errors[0])
        else:
            self.ui.flash(_t("Configurações aplicadas"), "ok", 1200)

    def _set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.tracker.set_enabled(enabled)
        if not enabled and self.machine.recording:
            self._apply(self.machine.escape(time.monotonic()))
        if not enabled:
            self.recorder.close()  # no microphone kept ready while dictation is off
        self._configure_wake()     # nor listening for the wake phrase
        self.ui.refresh_menu()  # "Ativado" / "Pausar" show the new state
        self.ui.flash(_t("Ditado ativado") if enabled else _t("Ditado desativado"), "info", 1000)

    # --- voice activation ------------------------------------------------------------

    def _configure_wake(self) -> None:
        """Start, restart or stop the "Ei Bira" listener to match the settings."""
        from . import wakeword

        cfg = self.settings.wake
        want = cfg.enabled and self.enabled and wakeword.model_installed(self.paths)
        key = (cfg.phrase.lower(), cfg.confirm, self.settings.recording.input_device) if want else None
        if key == self._wake_key:
            return
        if self.wake is not None:
            self.wake.stop()
            self.wake = None
        self._wake_key = key
        if want:
            self.wake = wakeword.WakeListener(wakeword.model_dir(self.paths), cfg.phrase,
                                              self.settings.recording.input_device,
                                              confirm=self._confirm_wake if cfg.confirm else None,
                                              on_call=lambda: self.post("voice_call"))
            self.wake.start()

    def wake_status(self) -> tuple[str, str]:
        """(state, what the detector heard last), for the settings page."""
        from . import wakeword

        if self.wake is not None:
            return self.wake.status, self.wake.last_heard
        if not self.settings.wake.enabled:
            return "desligada", ""
        if not wakeword.model_installed(self.paths):
            return "falta baixar o modelo de voz", ""
        return ("pausada (ditado desativado)" if not self.enabled else "parada"), ""

    def _confirm_wake(self, snippet: np.ndarray) -> bool:
        """Stage 2 (listener thread): did Whisper hear the phrase itself? With the local
        model installed the check runs on this PC, so room audio never leaves it; only
        without it the ~2.5 s go to the transcription API."""
        from . import wakeword
        from .transcribe import local_available, transcribe_local

        phrase = self.settings.wake.phrase
        cfg = replace(self.settings.transcription, vocabulary=[phrase], context="")
        wav = self.paths.tmp_dir / f"wake-{os.getpid()}.wav"
        try:
            write_wav(wav, snippet)
            if local_available(cfg, self.paths):
                where = "local"
                text = transcribe_local(wav, cfg, self.paths, CancelToken())
            else:
                where = "API"
                text = transcribe(wav, cfg, load_env(self.paths), self.paths, CancelToken())
        finally:
            wav.unlink(missing_ok=True)
        ok = wakeword.confirmed(text, phrase)
        log.info("wake phrase %s (%s check)", "confirmed" if ok else "not confirmed", where)
        log.debug("wake check heard %r", text.strip()[:60])
        return ok

    def _voice_call(self) -> None:
        w = self.wake
        if w is None:
            return
        if not self.enabled or self.machine.recording or self._jobs or self._ignored_app():
            w.release()
            return
        actions = self.machine.start_voice(time.monotonic())
        if not actions:
            w.release()
            return
        self._voice = True
        self._apply(actions)

    def _end_voice(self) -> bool:
        voice, self._voice = self._voice, False
        if voice and self.wake is not None:
            self.wake.release()
        return voice

    def _update_status(self) -> None:
        if not self.enabled:
            status = "disabled"
        elif self.machine.recording:
            status = "recording"
        elif self._jobs:
            status = "busy"
        else:
            status = "idle"
        if status != self._status:
            self._status = status
            self.ui.status(status)
