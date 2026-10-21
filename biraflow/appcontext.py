"""Which app is being dictated into, and which writing style fits it."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import PureWindowsPath

_EXE = {
    "personal": {"whatsapp.exe", "whatsapp.root.exe", "telegram.exe", "signal.exe", "messenger.exe",
                 "discord.exe", "instagram.exe"},
    "work": {"slack.exe", "ms-teams.exe", "teams.exe", "zoom.exe", "mattermost.exe", "skype.exe",
             "lync.exe", "rocketchat.exe", "chatwork.exe"},
    "email": {"outlook.exe", "olk.exe", "thunderbird.exe", "hxoutlook.exe", "mailspring.exe", "em client.exe"},
    "code": {"code.exe", "code - insiders.exe", "cursor.exe", "windsurf.exe", "devenv.exe", "idea64.exe",
             "pycharm64.exe", "webstorm64.exe", "rider64.exe", "clion64.exe", "goland64.exe",
             "sublime_text.exe", "notepad++.exe", "zed.exe", "windowsterminal.exe", "wt.exe", "cmd.exe",
             "powershell.exe", "pwsh.exe", "wezterm-gui.exe", "alacritty.exe", "mintty.exe", "conhost.exe"},
}
# Browsers and app hosts: decide by the window title instead.
_TITLE = [
    ("email", ("gmail", "outlook", "proton mail", "yahoo mail", "zoho mail", "e-mail", "caixa de entrada", "inbox")),
    ("personal", ("whatsapp", "telegram", "messenger", "instagram", "discord")),
    ("work", ("slack", "microsoft teams", "google chat", "mattermost")),
    ("code", ("github", "gitlab", "codesandbox", "replit", "stackblitz", "visual studio code")),
]
TERMINALS = {"windowsterminal.exe", "wt.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe",
             "wezterm-gui.exe", "alacritty.exe", "mintty.exe"}


@dataclass(frozen=True)
class AppContext:
    exe: str = ""
    title: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.exe in TERMINALS


def classify(ctx: AppContext) -> str:
    for category, exes in _EXE.items():
        if ctx.exe in exes:
            return category
    title = ctx.title.lower()
    for category, words in _TITLE:
        if any(w in title for w in words):
            return category
    return "other"


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD))
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def foreground() -> AppContext:
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return AppContext()
    title = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(hwnd, title, 512)
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if handle:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                exe = PureWindowsPath(buf.value).name.lower()
        finally:
            _kernel32.CloseHandle(handle)
    return AppContext(exe, title.value)
