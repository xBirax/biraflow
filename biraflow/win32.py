"""Minimal ctypes bindings for the Win32 APIs Bira Flow uses.

Every function gets explicit argtypes/restype: on 64-bit Windows the ctypes default
(c_int) would truncate handles and pointers.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    shcore = ctypes.WinDLL("shcore")
except OSError:  # pragma: no cover - pre-8.1 Windows
    shcore = None

LRESULT = ctypes.c_ssize_t
LONG_PTR = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t

# Marker placed in dwExtraInfo of every key event we inject, so our own hook skips
# them. Events injected by other tools (AutoHotkey remaps, on-screen keyboards) are
# still seen, so remapping a key to the hotkey works.
INJECT_TAG = 0x57464C57


def _bind(dll, name, restype, *argtypes):
    func = getattr(dll, name)
    func.restype = restype
    func.argtypes = argtypes
    return func


# --- keyboard hook --------------------------------------------------------------

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
WM_QUIT = 0x0012


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

SetWindowsHookExW = _bind(user32, "SetWindowsHookExW", wintypes.HHOOK,
                          ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
UnhookWindowsHookEx = _bind(user32, "UnhookWindowsHookEx", wintypes.BOOL, wintypes.HHOOK)
CallNextHookEx = _bind(user32, "CallNextHookEx", LRESULT,
                       wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
GetMessageW = _bind(user32, "GetMessageW", wintypes.BOOL,
                    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
TranslateMessage = _bind(user32, "TranslateMessage", wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
DispatchMessageW = _bind(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wintypes.MSG))
PostThreadMessageW = _bind(user32, "PostThreadMessageW", wintypes.BOOL,
                           wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
GetCurrentThreadId = _bind(kernel32, "GetCurrentThreadId", wintypes.DWORD)
GetModuleHandleW = _bind(kernel32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
GetAsyncKeyState = _bind(user32, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)


def is_key_down(vk: int) -> bool:
    return bool(GetAsyncKeyState(vk) & 0x8000)


# --- input injection ------------------------------------------------------------

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

# Keys that live on the extended part of the keyboard and need KEYEVENTF_EXTENDEDKEY.
EXTENDED_VKS = frozenset({
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,  # PgUp PgDn End Home arrows
    0x2C, 0x2D, 0x2E,                                # PrtSc Insert Delete
    0x5B, 0x5C, 0x5D,                                # Win Win Apps
    0x6F, 0x90, 0xA3, 0xA5,                          # Num/ NumLock RCtrl RAlt
})


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


SendInput = _bind(user32, "SendInput", wintypes.UINT,
                  wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
MapVirtualKeyW = _bind(user32, "MapVirtualKeyW", wintypes.UINT, wintypes.UINT, wintypes.UINT)


def key_input(vk: int = 0, scan: int = 0, flags: int = 0, tag: int = INJECT_TAG) -> INPUT:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=tag)
    return inp


def vk_input(vk: int, up: bool, tag: int = INJECT_TAG) -> INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    return key_input(vk, MapVirtualKeyW(vk, 0), flags, tag)


def send_inputs(inputs: list[INPUT]) -> int:
    if not inputs:
        return 0
    arr = (INPUT * len(inputs))(*inputs)
    return SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


# --- clipboard ------------------------------------------------------------------

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

OpenClipboard = _bind(user32, "OpenClipboard", wintypes.BOOL, wintypes.HWND)
CloseClipboard = _bind(user32, "CloseClipboard", wintypes.BOOL)
EmptyClipboard = _bind(user32, "EmptyClipboard", wintypes.BOOL)
GetClipboardData = _bind(user32, "GetClipboardData", wintypes.HANDLE, wintypes.UINT)
SetClipboardData = _bind(user32, "SetClipboardData", wintypes.HANDLE, wintypes.UINT, wintypes.HANDLE)
EnumClipboardFormats = _bind(user32, "EnumClipboardFormats", wintypes.UINT, wintypes.UINT)
RegisterClipboardFormatW = _bind(user32, "RegisterClipboardFormatW", wintypes.UINT, wintypes.LPCWSTR)
GetClipboardSequenceNumber = _bind(user32, "GetClipboardSequenceNumber", wintypes.DWORD)

GlobalAlloc = _bind(kernel32, "GlobalAlloc", wintypes.HGLOBAL, wintypes.UINT, ctypes.c_size_t)
GlobalLock = _bind(kernel32, "GlobalLock", wintypes.LPVOID, wintypes.HGLOBAL)
GlobalUnlock = _bind(kernel32, "GlobalUnlock", wintypes.BOOL, wintypes.HGLOBAL)
GlobalSize = _bind(kernel32, "GlobalSize", ctypes.c_size_t, wintypes.HGLOBAL)
GlobalFree = _bind(kernel32, "GlobalFree", wintypes.HGLOBAL, wintypes.HGLOBAL)


# --- windows & monitors ---------------------------------------------------------

HWND_MESSAGE = wintypes.HWND(-3)
HWND_TOPMOST = wintypes.HWND(-1)
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
MONITOR_DEFAULTTONEAREST = 2


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


CreateWindowExW = _bind(user32, "CreateWindowExW", wintypes.HWND,
                        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
DestroyWindow = _bind(user32, "DestroyWindow", wintypes.BOOL, wintypes.HWND)
GetParent = _bind(user32, "GetParent", wintypes.HWND, wintypes.HWND)
GetForegroundWindow = _bind(user32, "GetForegroundWindow", wintypes.HWND)
GetAncestor = _bind(user32, "GetAncestor", wintypes.HWND, wintypes.HWND, wintypes.UINT)
GA_ROOTOWNER = 3


def is_elevated(pid: int | None = None) -> bool | None:
    """Whether a process (default: this one) runs as administrator. None: unknown."""
    # private instances: setting argtypes on ctypes.windll would change them app-wide, and
    # GetLastError is only reliable through use_last_error
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
    if pid is None:
        process, close = kernel32.GetCurrentProcess(), False
    else:
        process = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        close = True
        if not process:
            return None
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            # a normal app may not read an elevated app's token: that is the answer
            return True if pid is not None and ctypes.get_last_error() == 5 else None
        try:
            value, size = wintypes.DWORD(), wintypes.DWORD()
            if not advapi32.GetTokenInformation(token, 20, ctypes.byref(value), 4, ctypes.byref(size)):
                return None  # 20 = TokenElevation
            return bool(value.value)
        finally:
            kernel32.CloseHandle(token)
    finally:
        if close:
            kernel32.CloseHandle(process)


def known_folder(folder_id: str) -> Path | None:
    """A shell known folder (FOLDERID_*, e.g. Documents), wherever the user moved it."""
    import uuid

    try:
        guid = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(folder_id).bytes_le)
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0:
            path = Path(out.value)
            ctypes.windll.ole32.CoTaskMemFree(out)
            return path
    except Exception:
        pass
    return None


MIC_BLOCKED = ("O Windows está bloqueando o microfone para apps: Configurações → Privacidade e "
               "segurança → Microfone → permitir apps da área de trabalho.")


def microphone_blocked() -> bool:
    """Windows privacy settings deny desktop apps the microphone (Settings > Privacy >
    Microphone): the mic then delivers pure silence or fails to open."""
    import winreg

    keys = (r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone",
            r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\NonPackaged")
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key in keys:
            try:
                with winreg.OpenKey(root, key) as k:
                    if str(winreg.QueryValueEx(k, "Value")[0]).lower() == "deny":
                        return True
            except OSError:
                continue
    return False


def session_locked() -> bool:
    """True on the lock screen (or another secure desktop): nobody is dictating."""
    user32 = ctypes.windll.user32
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.SwitchDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    desk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not desk:
        return True
    try:
        return not user32.SwitchDesktop(desk)
    finally:
        user32.CloseDesktop(desk)


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return pid.value


def blocked_by_elevation(hwnd: int) -> bool:
    """Windows drops simulated keys (our Ctrl+V) sent to an app running as administrator
    when we are not: detect it instead of pasting into the void."""
    if not hwnd or is_elevated() is not False:
        return False
    pid = window_pid(hwnd)
    return bool(pid) and is_elevated(pid) is True


def foreground_root() -> int:
    """The top-level owner of the focused window: stable across an app's own popups."""
    hwnd = GetForegroundWindow()
    return (GetAncestor(hwnd, GA_ROOTOWNER) or hwnd or 0) if hwnd else 0
ShowWindow = _bind(user32, "ShowWindow", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
SetWindowPos = _bind(user32, "SetWindowPos", wintypes.BOOL, wintypes.HWND, wintypes.HWND,
                     ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT)
GetWindowLongPtrW = _bind(user32, "GetWindowLongPtrW", LONG_PTR, wintypes.HWND, ctypes.c_int)
SetWindowLongPtrW = _bind(user32, "SetWindowLongPtrW", LONG_PTR, wintypes.HWND, ctypes.c_int, LONG_PTR)
MonitorFromWindow = _bind(user32, "MonitorFromWindow", wintypes.HMONITOR, wintypes.HWND, wintypes.DWORD)
GetMonitorInfoW = _bind(user32, "GetMonitorInfoW", wintypes.BOOL,
                        wintypes.HMONITOR, ctypes.POINTER(MONITORINFO))
MessageBoxW = _bind(user32, "MessageBoxW", ctypes.c_int,
                    wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT)


GetDpiForMonitor = (_bind(shcore, "GetDpiForMonitor", ctypes.c_long, wintypes.HMONITOR, ctypes.c_int,
                          ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT))
                    if shcore is not None else None)


def work_area_and_dpi(hwnd=None) -> tuple[tuple[int, int, int, int], int]:
    """Work area (left, top, right, bottom) and DPI of the monitor showing `hwnd`
    (default: the foreground window)."""
    hmon = MonitorFromWindow(hwnd or GetForegroundWindow(), MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    GetMonitorInfoW(hmon, ctypes.byref(info))
    r = info.rcWork
    dpi = 96
    if shcore is not None:
        x, y = wintypes.UINT(), wintypes.UINT()
        if GetDpiForMonitor(hmon, 0, ctypes.byref(x), ctypes.byref(y)) == 0:
            dpi = x.value or 96
    return (r.left, r.top, r.right, r.bottom), dpi


def enable_dpi_awareness() -> None:
    try:
        fn = _bind(user32, "SetProcessDpiAwarenessContext", wintypes.BOOL, wintypes.HANDLE)
        if fn(wintypes.HANDLE(-4)):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            return
    except AttributeError:
        pass
    try:
        if shcore is not None:
            shcore.SetProcessDpiAwareness(2)
            return
    except OSError:
        pass
    user32.SetProcessDPIAware()


# --- single instance ------------------------------------------------------------

ERROR_ALREADY_EXISTS = 183
CreateMutexW = _bind(kernel32, "CreateMutexW", wintypes.HANDLE,
                     wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
_instance_mutex = None


OpenMutexW = _bind(kernel32, "OpenMutexW", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
CloseHandle = _bind(kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
SYNCHRONIZE = 0x00100000


def acquire_single_instance(name: str = "Local\\BiraFlow-SingleInstance",
                            old_name: str = "Local\\Whisperflow-SingleInstance") -> bool:
    """False if Bira Flow is already running, including a copy from before the rename
    (two copies would paste every dictation twice)."""
    global _instance_mutex
    old = OpenMutexW(SYNCHRONIZE, False, old_name)
    if old:
        CloseHandle(old)
        return False
    if ctypes.get_last_error() == 5:   # exists but belongs to an elevated copy: it is running
        return False
    _instance_mutex = CreateMutexW(None, False, name)
    error = ctypes.get_last_error()
    # ACCESS_DENIED: an instance started as administrator owns it (two would paste twice)
    return bool(_instance_mutex) and error not in (ERROR_ALREADY_EXISTS, 5)


def message_box(text: str, title: str = "Bira Flow") -> None:
    MessageBoxW(None, text, title, 0x40)  # MB_ICONINFORMATION


def confirm(text: str, title: str = "Bira Flow") -> bool:
    """OK / Cancelar, on top of everything (asked from the tray, no window of ours is open)."""
    return MessageBoxW(None, text, title, 0x1 | 0x40 | 0x10000 | 0x40000) == 1  # OKCANCEL, info, foreground, topmost
