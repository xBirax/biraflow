"""Mute other apps' audio while dictating ("mute music while dictating").

Uses the Windows Core Audio session API through raw COM vtables (no extra
dependency). Only sessions that are playing and not already muted are touched, and
exactly those are unmuted afterwards, so the user's own mute choices are respected.
"""
from __future__ import annotations

import ctypes
import logging
import os
import uuid
from ctypes import wintypes

log = logging.getLogger(__name__)

HRESULT = ctypes.c_long
CLSCTX_ALL = 23
_ole32 = ctypes.OleDLL("ole32")
_ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
_ole32.CoInitializeEx.restype = HRESULT
_ole32.CoCreateInstance.argtypes = (ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_void_p))
_ole32.CoCreateInstance.restype = HRESULT


class GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]

    @classmethod
    def of(cls, text: str) -> "GUID":
        g = cls()
        ctypes.memmove(ctypes.byref(g), uuid.UUID(text).bytes_le, 16)
        return g


CLSID_MMDeviceEnumerator = GUID.of("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = GUID.of("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAudioSessionManager2 = GUID.of("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
IID_IAudioSessionControl2 = GUID.of("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
IID_ISimpleAudioVolume = GUID.of("87CE5498-68D6-44E5-9215-6DA47EF883D8")


def _check(hr: int, what: str) -> int:
    if hr < 0:  # failure HRESULTs are negative; S_FALSE and other success codes are not
        raise OSError(f"{what} falhou (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
    return hr


def _method(ptr: ctypes.c_void_p, index: int, *argtypes):
    if not ptr or not ptr.value:
        raise OSError("ponteiro COM nulo")
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    func = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])
    return lambda *args: _check(func(ptr, *args), f"chamada COM #{index}")


def _release(ptr) -> None:
    if ptr is not None and ptr.value:
        vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtbl[2])(ptr)


def _qi(ptr: ctypes.c_void_p, iid: GUID) -> ctypes.c_void_p:
    out = ctypes.c_void_p()
    _method(ptr, 0, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(iid), ctypes.byref(out))
    return out


def _co_init() -> None:
    try:
        _ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED; S_FALSE if already done
    except OSError:
        pass  # RPC_E_CHANGED_MODE: COM already initialized differently on this thread


def playing_sessions():
    """Yields (pid, ISimpleAudioVolume*) for active, non-system sessions on the default
    output device. The caller releases each volume pointer."""
    _co_init()
    enum = ctypes.c_void_p()
    _check(_ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                                   ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(enum)),
           "CoCreateInstance(MMDeviceEnumerator)")
    device, mgr, sessions = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        # IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender, eMultimedia)
        _method(enum, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(0, 1, ctypes.byref(device))
        # IMMDevice::Activate(IAudioSessionManager2)
        _method(device, 3, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
            ctypes.byref(IID_IAudioSessionManager2), CLSCTX_ALL, None, ctypes.byref(mgr))
        # IAudioSessionManager2::GetSessionEnumerator
        _method(mgr, 5, ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(sessions))
        count = ctypes.c_int()
        _method(sessions, 3, ctypes.POINTER(ctypes.c_int))(ctypes.byref(count))
        for i in range(count.value):
            ctl = ctypes.c_void_p()
            _method(sessions, 4, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(i, ctypes.byref(ctl))
            ctl2 = None
            try:
                state = ctypes.c_int()
                _method(ctl, 3, ctypes.POINTER(ctypes.c_int))(ctypes.byref(state))
                ctl2 = _qi(ctl, IID_IAudioSessionControl2)
                pid = wintypes.DWORD()
                try:
                    _method(ctl2, 14, ctypes.POINTER(wintypes.DWORD))(ctypes.byref(pid))
                except OSError:
                    pid.value = 0  # multi-process session: no single pid, still mutable
                try:
                    is_system = _method(ctl2, 15)() == 0  # S_OK means "system sounds"
                except OSError:
                    is_system = True  # can't tell: leave it alone
                if state.value == 1 and not is_system:  # AudioSessionStateActive
                    yield pid.value, _qi(ctl, IID_ISimpleAudioVolume)
            finally:
                _release(ctl2)
                _release(ctl)
    finally:
        for p in (sessions, mgr, device, enum):
            _release(p)


def get_mute(vol: ctypes.c_void_p) -> bool:
    muted = wintypes.BOOL()
    _method(vol, 6, ctypes.POINTER(wintypes.BOOL))(ctypes.byref(muted))
    return bool(muted.value)


def set_mute(vol: ctypes.c_void_p, mute: bool) -> None:
    _method(vol, 5, wintypes.BOOL, ctypes.c_void_p)(mute, None)


class MediaMuter:
    """mute() then restore(), on the same thread."""

    def __init__(self) -> None:
        self._muted: list[ctypes.c_void_p] = []

    def mute(self) -> int:
        if self._muted:
            return len(self._muted)
        me = os.getpid()
        try:
            for pid, vol in playing_sessions():
                if pid != me and not get_mute(vol):
                    set_mute(vol, True)
                    self._muted.append(vol)
                else:
                    _release(vol)
        except Exception as e:  # no output device, audio service restarting, ...
            log.warning("could not mute other apps: %s", e)
        return len(self._muted)

    def restore(self) -> None:
        for vol in self._muted:
            try:
                set_mute(vol, False)
            except Exception as e:
                log.warning("could not unmute a session: %s", e)
            _release(vol)
        self._muted = []
