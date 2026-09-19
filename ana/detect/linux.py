"""Linux/X11 probe via xdotool + xprintidle, with xprop and /proc fallbacks."""

from __future__ import annotations

import os
import shutil
import subprocess

from . import WindowInfo


def _run(cmd: list[str], timeout: float = 3.0) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class LinuxProbe:
    name = "linux"

    def __init__(self) -> None:
        self.has_xdotool = shutil.which("xdotool") is not None
        self.has_xprintidle = shutil.which("xprintidle") is not None
        self.has_display = bool(os.environ.get("DISPLAY") or
                                os.environ.get("WAYLAND_DISPLAY"))
        self.available = self.has_xdotool and self.has_display
        missing = [n for n, ok in (("xdotool", self.has_xdotool),
                                   ("xprintidle", self.has_xprintidle)) if not ok]
        self.note = ("xdotool/xprintidle"
                     + (f" — install: {' '.join(missing)}" if missing else ""))

    def window(self) -> WindowInfo:
        if not self.has_xdotool:
            return WindowInfo()
        wid = _run(["xdotool", "getactivewindow"])
        if not wid:
            return WindowInfo()
        title = _run(["xdotool", "getwindowname", wid])
        pid = _run(["xdotool", "getwindowpid", wid])
        app = ""
        if pid.isdigit():
            try:
                with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
                    app = fh.read().strip()
            except OSError:
                app = ""
        return WindowInfo(app=app, title=title)

    def idle_seconds(self) -> float:
        if self.has_xprintidle:
            out = _run(["xprintidle"])
            if out.isdigit():
                return int(out) / 1000.0
        return 0.0

    def background_processes(self) -> tuple[str, ...]:
        names = set()
        try:
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    with open(f"/proc/{entry}/comm", encoding="utf-8") as fh:
                        names.add(fh.read().strip().lower())
                except OSError:
                    continue
        except OSError:
            return ()
        return tuple(sorted(names))

    def raise_window(self, app: str, title: str) -> bool:
        if not self.has_xdotool:
            return False
        if title:
            wid = _run(["xdotool", "search", "--name", title[:40]]).splitlines()
            if wid:
                _run(["xdotool", "windowactivate", wid[0]])
                return True
        if app:
            wid = _run(["xdotool", "search", "--class", app]).splitlines()
            if wid:
                _run(["xdotool", "windowactivate", wid[0]])
                return True
        return False
