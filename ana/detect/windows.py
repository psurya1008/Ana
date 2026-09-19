"""Windows probe. ctypes only — no third-party dependency, no admin rights."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import re
from typing import Optional

from . import WindowInfo


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class WindowsProbe:
    name = "windows"
    note = "Win32 GetForegroundWindow + GetLastInputInfo"

    def __init__(self) -> None:
        self.available = os.name == "nt"
        if self.available:
            self.user32 = ctypes.windll.user32          # type: ignore[attr-defined]
            self.kernel32 = ctypes.windll.kernel32      # type: ignore[attr-defined]
            self.psapi = ctypes.windll.psapi            # type: ignore[attr-defined]

    # -- foreground window -------------------------------------------------
    def window(self) -> WindowInfo:
        hwnd = self.user32.GetForegroundWindow()
        if not hwnd:
            return WindowInfo()
        length = self.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(hwnd, buf, length + 1)
        return WindowInfo(app=self._process_name(hwnd), title=buf.value)

    def _process_name(self, hwnd: int) -> str:
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                           False, pid.value)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = wintypes.DWORD(512)
            if self.kernel32.QueryFullProcessImageNameW(handle, 0, buf,
                                                        ctypes.byref(size)):
                return os.path.basename(buf.value)
            return ""
        finally:
            self.kernel32.CloseHandle(handle)

    # -- idle time ---------------------------------------------------------
    def idle_seconds(self) -> float:
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not self.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        millis = self.kernel32.GetTickCount() - info.dwTime
        return max(0.0, millis / 1000.0)

    # -- background processes ---------------------------------------------
    def background_processes(self) -> tuple[str, ...]:
        try:
            import psutil  # optional
        except ImportError:
            return ()
        names = set()
        for proc in psutil.process_iter(["name"]):
            n = proc.info.get("name")
            if n:
                names.add(n.lower())
        return tuple(sorted(names))

    # -- put the work back in front ----------------------------------------
    def raise_window(self, app: str, title: str) -> bool:
        target: Optional[int] = None
        pattern = re.compile(re.escape(title[:40]), re.I) if title else None

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, _lparam):
            nonlocal target
            if target is not None or not self.user32.IsWindowVisible(hwnd):
                return True
            length = self.user32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            self.user32.GetWindowTextW(hwnd, buf, length + 1)
            if pattern and pattern.search(buf.value):
                target = hwnd
            elif app and self._process_name(hwnd).lower() == app.lower():
                target = hwnd
            return True

        self.user32.EnumWindows(_enum, 0)
        if target is None:
            return False
        SW_RESTORE = 9
        self.user32.ShowWindow(target, SW_RESTORE)
        return bool(self.user32.SetForegroundWindow(target))
