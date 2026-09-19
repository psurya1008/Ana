"""Per-OS probes: what window is in front, and how long since you touched anything.

This is the only place in Ana that talks to the operating system. Every probe
degrades gracefully: if a platform API is missing, Ana still runs, it just sees
less, and it says so rather than pretending.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from typing import Callable, Optional, Protocol


@dataclass(frozen=True)
class WindowInfo:
    app: str = ""
    title: str = ""


class Probe(Protocol):
    name: str

    def window(self) -> WindowInfo: ...
    def idle_seconds(self) -> float: ...
    def background_processes(self) -> tuple[str, ...]: ...
    def raise_window(self, app: str, title: str) -> bool: ...


class NullProbe:
    """Used when no platform probe works. Honest about seeing nothing."""

    name = "null"
    available = True
    note = ("no window probe for this platform — Ana will time your sessions but "
            "cannot see what you are doing")

    def window(self) -> WindowInfo:
        return WindowInfo("unknown", "")

    def idle_seconds(self) -> float:
        return 0.0

    def background_processes(self) -> tuple[str, ...]:
        return ()

    def raise_window(self, app: str, title: str) -> bool:
        return False


class ScriptedProbe:
    """A probe you drive yourself — used by tests and by `ana demo`."""

    name = "scripted"
    available = True
    note = "scripted probe (demo/test)"

    def __init__(self, steps: list[tuple[str, str, float]] | None = None):
        self.steps = steps or []
        self.i = 0
        self.raised: list[tuple[str, str]] = []

    def _current(self) -> tuple[str, str, float]:
        if not self.steps:
            return ("unknown", "", 0.0)
        step = self.steps[min(self.i, len(self.steps) - 1)]
        self.i += 1
        return step

    def window(self) -> WindowInfo:
        app, title, _ = self._current()
        return WindowInfo(app, title)

    def idle_seconds(self) -> float:
        return self.steps[min(self.i - 1, len(self.steps) - 1)][2] if self.steps else 0.0

    def background_processes(self) -> tuple[str, ...]:
        return ()

    def raise_window(self, app: str, title: str) -> bool:
        self.raised.append((app, title))
        return True


def get_probe(force: Optional[str] = None) -> Probe:
    """Pick the best probe available on this machine."""
    system = force or platform.system()
    candidates: list[Callable[[], object]] = []
    if system == "Windows":
        from .windows import WindowsProbe
        candidates.append(WindowsProbe)
    elif system == "Darwin":
        from .macos import MacProbe
        candidates.append(MacProbe)
    else:
        from .linux import LinuxProbe
        candidates.append(LinuxProbe)

    for factory in candidates:
        try:
            probe = factory()
            if getattr(probe, "available", False):
                return probe  # type: ignore[return-value]
        except Exception:  # noqa: BLE001 - a broken probe must never stop the app
            continue
    return NullProbe()


def describe_probe(probe: Probe) -> str:
    return f"{probe.name}: {getattr(probe, 'note', '')}".strip(": ")
