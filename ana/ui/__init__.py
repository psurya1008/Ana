"""User interfaces. The console one works everywhere; the tk one is the PC app."""

from __future__ import annotations

from typing import Any, Optional


def pick_ui(cfg: dict[str, Any], prefer: Optional[str] = None):
    """tk if asked for and importable, console otherwise. Never fails to give a UI."""
    choice = prefer or cfg.get("ui", "auto")
    if choice in ("tk", "auto"):
        try:
            import tkinter  # noqa: F401
            from .tkui import TkUI
            return TkUI(cfg)
        except Exception:  # noqa: BLE001 - no display, no tkinter, no problem
            if choice == "tk":
                from .console import ConsoleUI
                ui = ConsoleUI(cfg)
                ui.notice("tkinter is not available here — falling back to the console. "
                          "On Windows/macOS it ships with the standard Python installer; "
                          "on Linux install python3-tk.")
                return ui
    from .console import ConsoleUI
    return ConsoleUI(cfg)
