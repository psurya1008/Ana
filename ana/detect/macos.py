"""macOS probe. Prefers Quartz when PyObjC is present, falls back to osascript."""

from __future__ import annotations

import subprocess

from . import WindowInfo

FRONT_APP_SCRIPT = """
tell application "System Events"
    set frontApp to name of first application process whose frontmost is true
    set windowTitle to ""
    try
        tell process frontApp
            set windowTitle to name of front window
        end tell
    end try
    return frontApp & "\t" & windowTitle
end tell
"""


class MacProbe:
    name = "macos"
    note = "Quartz HIDIdleTime + System Events frontmost window"

    def __init__(self) -> None:
        self.available = self._osascript_works()
        self._quartz = self._load_quartz()

    @staticmethod
    def _osascript_works() -> bool:
        try:
            subprocess.run(["osascript", "-e", "return 1"], capture_output=True,
                           timeout=5, check=True)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _load_quartz():
        try:
            import Quartz  # type: ignore
            return Quartz
        except ImportError:
            return None

    def window(self) -> WindowInfo:
        try:
            out = subprocess.run(["osascript", "-e", FRONT_APP_SCRIPT],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return WindowInfo()
        app, _, title = out.stdout.strip().partition("\t")
        return WindowInfo(app=app, title=title)

    def idle_seconds(self) -> float:
        if self._quartz is not None:
            try:
                return float(self._quartz.CGEventSourceSecondsSinceLastEventType(
                    self._quartz.kCGEventSourceStateHIDSystemState,
                    self._quartz.kCGAnyInputEventType))
            except Exception:  # noqa: BLE001
                pass
        try:
            out = subprocess.run(
                "ioreg -c IOHIDSystem | awk '/HIDIdleTime/ {print $NF; exit}'",
                shell=True, capture_output=True, text=True, timeout=5)
            return int(out.stdout.strip()) / 1_000_000_000
        except (OSError, ValueError, subprocess.SubprocessError):
            return 0.0

    def background_processes(self) -> tuple[str, ...]:
        try:
            out = subprocess.run(["ps", "-Aco", "comm"], capture_output=True,
                                 text=True, timeout=5)
            return tuple(sorted({l.strip().lower()
                                 for l in out.stdout.splitlines()[1:] if l.strip()}))
        except (OSError, subprocess.SubprocessError):
            return ()

    def raise_window(self, app: str, title: str) -> bool:
        if not app:
            return False
        script = f'tell application "{app}" to activate'
        try:
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
            return True
        except (OSError, subprocess.SubprocessError):
            return False
