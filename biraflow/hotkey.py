"""Hotkey parsing and chord tracking.

Pure logic (no Win32 calls) so it can be unit-tested. The keyboard hook feeds raw
key events into ChordTracker.handle(), which reports hotkey press/release, Esc and
"other key" events through a callback.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Callable

VK_ESCAPE = 0x1B

# Low-level hooks report side-specific modifier codes (VK_LCONTROL, ...), so the
# generic names map to both sides.
_NAMED: dict[str, tuple[int, ...]] = {
    "ctrl": (0xA2, 0xA3), "control": (0xA2, 0xA3),
    "lctrl": (0xA2,), "leftctrl": (0xA2,), "rctrl": (0xA3,), "rightctrl": (0xA3,),
    "shift": (0xA0, 0xA1), "lshift": (0xA0,), "leftshift": (0xA0,),
    "rshift": (0xA1,), "rightshift": (0xA1,),
    "alt": (0xA4, 0xA5), "lalt": (0xA4,), "leftalt": (0xA4,),
    "ralt": (0xA5,), "rightalt": (0xA5,), "altgr": (0xA5,),
    "win": (0x5B, 0x5C), "windows": (0x5B, 0x5C),
    "lwin": (0x5B,), "leftwin": (0x5B,), "rwin": (0x5C,), "rightwin": (0x5C,),
    "menu": (0x5D,), "apps": (0x5D,), "contextmenu": (0x5D,),
    "space": (0x20,), "tab": (0x09,), "enter": (0x0D,), "return": (0x0D,),
    "backspace": (0x08,), "capslock": (0x14,), "scrolllock": (0x91,), "pause": (0x13,),
    "insert": (0x2D,), "ins": (0x2D,), "delete": (0x2E,), "del": (0x2E,),
    "home": (0x24,), "end": (0x23,), "pageup": (0x21,), "pgup": (0x21,),
    "pagedown": (0x22,), "pgdn": (0x22,), "printscreen": (0x2C,), "prtsc": (0x2C,),
    "up": (0x26,), "down": (0x28,), "left": (0x25,), "right": (0x27,),
    "grave": (0xC0,), "backquote": (0xC0,),
    "volumemute": (0xAD,), "volumedown": (0xAE,), "volumeup": (0xAF,),
    "medianext": (0xB0,), "mediaprev": (0xB1,), "mediastop": (0xB2,), "mediaplaypause": (0xB3,),
}

MODIFIER_VKS = frozenset({0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C})
# Releasing Win or Alt on its own opens the Start menu / the app's menu bar, unless
# some other key went by in between. We send a harmless "mask" key to prevent that.
_MASK_NEEDED = frozenset({0x5B, 0x5C, 0xA4, 0xA5})


class HotkeyError(ValueError):
    pass


@dataclass(frozen=True)
class Hotkey:
    """A chord such as ctrl+win: each part is the set of VKs that satisfy it."""

    parts: tuple[frozenset[int], ...]
    text: str

    @property
    def vks(self) -> frozenset[int]:
        return frozenset().union(*self.parts)

    @property
    def suppressible(self) -> frozenset[int]:
        """Keys we may swallow. Modifiers are never swallowed (stuck-key risk)."""
        return frozenset(vk for vk in self.vks if vk not in MODIFIER_VKS)

    @property
    def needs_mask(self) -> bool:
        return bool(self.vks & _MASK_NEEDED)

    def contains(self, other: "Hotkey") -> bool:
        """Every key of `other` is also part of this chord (ctrl+win+alt contains ctrl+win)."""
        return all(part in self.parts for part in other.parts)

    def is_active(self, down: set[int] | frozenset[int]) -> bool:
        return all(part & down for part in self.parts)

    def injection_vks(self) -> list[int]:
        """One concrete VK per part (left side for generic modifiers), modifiers first."""
        vks = [min(part) for part in self.parts]
        return sorted(vks, key=lambda vk: vk not in MODIFIER_VKS)


def _parse_key(name: str) -> tuple[int, ...]:
    key = re.sub(r"[\s_\-]", "", name.lower())
    if not key:
        raise HotkeyError("Atalho com tecla vazia (\"++\"?).")
    if key == "fn":
        raise HotkeyError(
            "A tecla Fn é tratada pelo próprio teclado e não chega ao Windows. "
            "Escolha outra tecla (veja o README)."
        )
    if key in ("esc", "escape"):
        raise HotkeyError("Esc é reservado para cancelar; escolha outra tecla.")
    if key in _NAMED:
        return _NAMED[key]
    if m := re.fullmatch(r"f(\d{1,2})", key):
        n = int(m.group(1))
        if 1 <= n <= 24:
            return (0x6F + n,)
    if m := re.fullmatch(r"(?:numpad|num)(\d)", key):
        return (0x60 + int(m.group(1)),)
    if m := re.fullmatch(r"vk(?:0x)?([0-9a-f]{1,2})", key):
        return (int(m.group(1), 16),)
    if len(key) == 1 and (key.isascii() and key.isalnum()):
        return (ord(key.upper()),)
    raise HotkeyError(f"Tecla desconhecida no atalho: \"{name.strip()}\".")


def parse_hotkey(spec: str) -> Hotkey:
    if not spec or not spec.strip():
        raise HotkeyError("O atalho está vazio.")
    parts = tuple(frozenset(_parse_key(p)) for p in spec.split("+"))
    if len(set(parts)) != len(parts):
        raise HotkeyError(f"Tecla repetida no atalho \"{spec}\".")
    return Hotkey(parts=parts, text=spec.strip())


class ChordTracker:
    """Turns raw key events into press / release / esc / other events.

    handle() runs inside the low-level keyboard hook callback: it must stay fast and
    never block. It returns True when the key should be swallowed, which only ever
    happens for non-modifier keys of the hotkey (and never for Esc), so Esc and
    everything else always reaches the app being typed in.
    """

    STALE_AFTER = 1.5  # seconds without any event (held keys auto-repeat faster)

    def __init__(self, hotkey: Hotkey, emit: Callable[[str, float], None],
                 suppress: bool = True, clock: Callable[[], float] | None = None,
                 extras: dict[str, Hotkey] | None = None):
        import time

        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._emit = emit
        self.hotkey = hotkey
        self.extras: dict[str, Hotkey] = dict(extras or {})  # e.g. {"command": ..., "paste_last": ...}
        self.suppress = suppress
        self.enabled = True
        self._down: set[int] = set()
        self._last_seen: dict[int, float] = {}
        self._suppressed: set[int] = set()
        self._active = False
        self._extra_active: set[str] = set()

    @property
    def active(self) -> bool:
        return self._active

    def _all_vks(self) -> frozenset[int]:
        return self.hotkey.vks.union(*(h.vks for h in self.extras.values()))

    def _resync(self) -> None:
        self._active = self.enabled and self.hotkey.is_active(self._down)
        self._extra_active = {n for n, h in self.extras.items() if self.enabled and h.is_active(self._down)}

    def configure(self, hotkey: Hotkey, suppress: bool, extras: dict[str, Hotkey] | None = None) -> None:
        with self._lock:
            self.hotkey = hotkey
            self.suppress = suppress
            if extras is not None:
                self.extras = dict(extras)
            self._resync()

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.enabled = enabled
            self._resync()

    def handle(self, vk: int, is_down: bool) -> bool:
        now = self._clock()
        events: list[str] = []
        with self._lock:
            hk = self.hotkey
            if is_down:
                repeat = vk in self._down
                self._down.add(vk)
                self._last_seen[vk] = now
                if not repeat:
                    if vk == VK_ESCAPE:
                        events.append("esc")
                    elif vk not in self._all_vks():
                        events.append("other")
                    elif self.enabled and self.suppress and any(
                            vk in h.suppressible and h.is_active(self._down)
                            for h in (hk, *self.extras.values())):
                        self._suppressed.add(vk)
                swallow = vk in self._suppressed
                self._key_was_down = not repeat and vk not in MODIFIER_VKS and vk != VK_ESCAPE
            else:
                self._key_was_down = False
                self._down.discard(vk)
                self._last_seen.pop(vk, None)
                swallow = vk in self._suppressed
                self._suppressed.discard(vk)
            self._update_active(events)
            # A key that is part of some hotkey (the Z of Shift+Alt+Z, Ctrl+Z...) but did not
            # trigger one is still typing: it must count as activity ("other").
            if self._key_was_down and not events and self.enabled:
                events.append("other")
        for kind in events:
            self._emit(kind, now)
        return swallow

    def drop_stale(self, is_physically_down: Callable[[int], bool]) -> None:
        """Forget keys whose key-up we missed (e.g. after Win+L locked the screen).

        A key counts as stale only if it stopped auto-repeating AND the OS says it is
        up. Swallowed keys are skipped: the OS key state never saw them go down.
        """
        now = self._clock()
        events: list[str] = []
        with self._lock:
            stale = {
                vk for vk in self._down
                if vk not in self._suppressed
                and now - self._last_seen.get(vk, now) > self.STALE_AFTER
                and not is_physically_down(vk)
            }
            if not stale:
                return
            self._down -= stale
            for vk in stale:
                self._last_seen.pop(vk, None)
            self._update_active(events)
        for kind in events:
            self._emit(kind, now)

    def _update_active(self, events: list[str]) -> None:
        active = self.enabled and self.hotkey.is_active(self._down)
        if active != self._active:
            self._active = active
            events.append("press" if active else "release")
        # Extra chords report only their activation, after the main press, so that
        # holding Ctrl+Win and then adding Alt reads as "press" then "command".
        for name, hk in self.extras.items():
            on = self.enabled and hk.is_active(self._down)
            if on and name not in self._extra_active:
                self._extra_active.add(name)
                events.append(name)
            elif not on:
                self._extra_active.discard(name)


# --- capturing a new hotkey ("press the keys you want") ---------------------------

_SIDE_NAMES = {0xA2: "lctrl", 0xA3: "rctrl", 0xA0: "lshift", 0xA1: "rshift",
               0xA4: "lalt", 0xA5: "ralt", 0x5B: "lwin", 0x5C: "rwin"}
_GENERIC = {0xA2: "ctrl", 0xA3: "ctrl", 0xA0: "shift", 0xA1: "shift",
            0xA4: "alt", 0xA5: "alt", 0x5B: "win", 0x5C: "win"}
_MOD_ORDER = ("ctrl", "shift", "alt", "win")
_DISPLAY = {"ctrl": "Ctrl", "lctrl": "Ctrl esquerdo", "rctrl": "Ctrl direito",
            "shift": "Shift", "lshift": "Shift esquerdo", "rshift": "Shift direito",
            "alt": "Alt", "lalt": "Alt esquerdo", "ralt": "Alt direito (AltGr)",
            "win": "Win", "lwin": "Win esquerdo", "rwin": "Win direito",
            "capslock": "CapsLock", "scrolllock": "ScrollLock", "space": "Espaço",
            "pageup": "PgUp", "pagedown": "PgDn", "printscreen": "PrtSc"}


def key_name(vk: int) -> str:
    if vk in _SIDE_NAMES:
        return _SIDE_NAMES[vk]
    if 0x70 <= vk <= 0x87:
        return f"f{vk - 0x6F}"
    if 0x60 <= vk <= 0x69:
        return f"numpad{vk - 0x60}"
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk).lower()
    for name, vks in _NAMED.items():
        if vks == (vk,):
            return name
    return f"vk{vk:02X}"


def chord_spec(vks: list[int]) -> str:
    """Hotkey spec for keys pressed together. A lone modifier keeps its side
    ("rctrl"); in a chord, modifiers are generic ("ctrl+win")."""
    vks = list(dict.fromkeys(vks))
    if len(vks) == 1:
        return key_name(vks[0])
    mods = {_GENERIC[vk] for vk in vks if vk in _GENERIC}
    others = [key_name(vk) for vk in vks if vk not in _GENERIC]
    return "+".join([m for m in _MOD_ORDER if m in mods] + others)


def display_hotkey(spec: str) -> str:
    """"ctrl+win" -> "Ctrl + Win" for the settings window."""
    parts = []
    for raw in spec.split("+"):
        key = re.sub(r"[\s_\-]", "", raw.lower())
        parts.append(_DISPLAY.get(key, key.upper() if len(key) <= 3 else key.capitalize()))
    return " + ".join(parts)


class ChordCapture:
    """Records the next chord typed (fed from the keyboard hook). Done when every
    pressed key is released; Esc cancels."""

    def __init__(self, limit: float = 10.0) -> None:
        import time

        self._down: set[int] = set()
        self.pressed: list[int] = []
        self.done = False
        self.cancelled = False
        # while capturing, every key press in every app is swallowed: never longer than this
        self._deadline = time.monotonic() + limit

    def feed(self, vk: int, is_down: bool) -> bool:
        """Returns True to swallow the key: keys typed into the capture reach no app
        (so Win does not open the Start menu). Key-ups of keys held from before the
        capture are let through, so nothing gets stuck."""
        import time

        if not self.done and time.monotonic() > self._deadline:
            self.cancelled = self.done = True
        if self.done:
            return False
        if is_down:
            if vk == VK_ESCAPE:
                self.cancelled = self.done = True
                return True
            self._down.add(vk)
            if vk not in self.pressed:
                self.pressed.append(vk)
            return True
        if vk not in self._down:
            return False
        self._down.discard(vk)
        if not self._down:
            self.done = True
        return True

    @property
    def spec(self) -> str:
        return chord_spec(self.pressed) if self.pressed else ""
