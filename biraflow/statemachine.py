"""Dictation state machine: hold-to-talk, double-tap hands-free, Esc, auto-stop.

Pure and clock-driven (every method takes `now`), so it is fully unit-testable.
Methods return a list of Actions for the controller to carry out.

    IDLE --press--> PRESSED (recording starts at once, so no syllable is lost)
    PRESSED --release after >= tap_max--> IDLE + FINISH          (hold-to-talk)
    PRESSED --release before tap_max--> AWAIT_SECOND_TAP
    AWAIT_SECOND_TAP --press within window--> HANDSFREE
    AWAIT_SECOND_TAP --window expires / other key--> IDLE       (single tap: dropped)
    HANDSFREE --press--> IDLE + FINISH
    PRESSED / HANDSFREE --max_seconds--> IDLE + FINISH          (auto-stop)
    any recording phase --Esc--> IDLE (recording dropped)
    IDLE while jobs run (busy) --Esc--> CANCEL (every pending transcription)

Transcription runs in the background, so the next dictation can start right away;
the controller pastes the results in the order they were spoken.

Command mode (edit the selected text by voice): the command chord (Ctrl+Win+Alt)
contains the dictation chord, so it arrives as "press" then "command"; from then on
the recording finishes as Mode.COMMAND, whatever the hold duration.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Phase(Enum):
    IDLE = "idle"
    PRESSED = "pressed"
    AWAIT_SECOND_TAP = "await_second_tap"
    HANDSFREE = "handsfree"


class Mode(Enum):
    HOLD = "hold"
    HANDSFREE = "handsfree"
    COMMAND = "command"


START = "start"        # start recording (mode=HOLD)
MODE = "mode"          # recording continues, now in `mode`
FINISH = "finish"      # stop recording and transcribe
DISCARD = "discard"    # stop recording and drop it
CANCEL = "cancel"      # cancel the running transcription job


@dataclass(frozen=True)
class Action:
    kind: str
    mode: Mode | None = None
    reason: str = ""


RECORDING_PHASES = (Phase.PRESSED, Phase.AWAIT_SECOND_TAP, Phase.HANDSFREE)


class DictationMachine:
    def __init__(self, tap_max: float = 0.25, double_tap_window: float = 0.4,
                 max_seconds: float = 300.0):
        self.tap_max = tap_max
        self.double_tap_window = double_tap_window
        self.max_seconds = max_seconds
        self.phase = Phase.IDLE
        self.started_at = 0.0
        self.deadline = 0.0       # auto-stop time of the current recording
        self._pressed_at = 0.0
        self._tap_deadline = 0.0
        self._command = False
        self.busy = False  # set by the controller while transcriptions are running

    @property
    def recording(self) -> bool:
        return self.phase in RECORDING_PHASES

    def press(self, now: float) -> list[Action]:
        actions = self.tick(now)
        if self.phase is Phase.IDLE:
            self.phase = Phase.PRESSED
            self._command = False
            self._pressed_at = self.started_at = now
            self.deadline = now + self.max_seconds
            actions.append(Action(START, Mode.HOLD))
        elif self.phase is Phase.AWAIT_SECOND_TAP:
            self.phase = Phase.HANDSFREE
            actions.append(Action(MODE, Mode.HANDSFREE))
        elif self.phase is Phase.HANDSFREE:
            self.phase = Phase.IDLE
            actions.append(Action(FINISH, self._finish_mode(Mode.HANDSFREE), "tap"))
        return actions

    def release(self, now: float) -> list[Action]:
        actions = self.tick(now)
        if self.phase is Phase.PRESSED:
            if self._command:
                self.phase = Phase.IDLE
                actions.append(Action(FINISH, Mode.COMMAND, "release"))
            elif now - self._pressed_at < self.tap_max:
                self.phase = Phase.AWAIT_SECOND_TAP
                self._tap_deadline = now + self.double_tap_window
            else:
                self.phase = Phase.IDLE
                actions.append(Action(FINISH, Mode.HOLD, "release"))
        return actions

    def command(self, now: float) -> list[Action]:
        """The command chord went down: this recording is a spoken instruction."""
        actions = self.tick(now)
        if self.phase in (Phase.PRESSED, Phase.HANDSFREE) and not self._command:
            self._command = True
            actions.append(Action(MODE, Mode.COMMAND))
        return actions

    def _finish_mode(self, mode: Mode) -> Mode:
        return Mode.COMMAND if self._command else mode

    def other_key(self, now: float) -> list[Action]:
        """A non-hotkey key went down: the chord was a shortcut, or the user is typing."""
        actions = self.tick(now)
        if self.phase in (Phase.PRESSED, Phase.AWAIT_SECOND_TAP):
            self.phase = Phase.IDLE
            actions.append(Action(DISCARD, reason="other"))
        return actions

    def escape(self, now: float) -> list[Action]:
        actions = self.tick(now)
        if self.phase in RECORDING_PHASES:
            self.phase = Phase.IDLE
            actions.append(Action(DISCARD, reason="esc"))
        elif self.busy:
            actions.append(Action(CANCEL, reason="esc"))
        return actions

    def tick(self, now: float) -> list[Action]:
        if self.phase is Phase.AWAIT_SECOND_TAP and now >= self._tap_deadline:
            self.phase = Phase.IDLE
            return [Action(DISCARD, reason="tap")]
        if self.phase in (Phase.PRESSED, Phase.HANDSFREE) and now >= self.deadline:
            mode = Mode.HANDSFREE if self.phase is Phase.HANDSFREE else Mode.HOLD
            self.phase = Phase.IDLE
            return [Action(FINISH, self._finish_mode(mode), "timeout")]
        return []

    def start_voice(self, now: float) -> list[Action]:
        """Voice activation ("Ei Bira"): record hands-free, as after a double tap."""
        if self.phase is not Phase.IDLE:
            return []
        self.phase = Phase.HANDSFREE
        self._command = False
        self.started_at = now
        self.deadline = now + self.max_seconds
        return [Action(START, Mode.HANDSFREE)]

    def finish_handsfree(self, reason: str) -> list[Action]:
        """End a hands-free recording from outside (e.g. a long silence): transcribe it."""
        if self.phase is Phase.HANDSFREE:
            self.phase = Phase.IDLE
            return [Action(FINISH, self._finish_mode(Mode.HANDSFREE), reason)]
        return []

    def reset(self) -> None:
        self.phase = Phase.IDLE
