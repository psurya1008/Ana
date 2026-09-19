"""Configuration, default rules and thresholds.

Everything lives under ``~/.ana`` (override with ``ANA_HOME``). Nothing here
touches the network, and no file outside the Ana home directory is ever written.

Rule and threshold edits are audited: see :func:`diff_for_audit`. Loosening the
rules is allowed — quietly loosening them is not.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any


def ana_home() -> Path:
    return Path(os.environ.get("ANA_HOME", Path.home() / ".ana")).expanduser()


def ensure_home() -> Path:
    home = ana_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "reports").mkdir(exist_ok=True)
    return home


def config_path() -> Path:
    return ana_home() / "config.json"


def ledger_path() -> Path:
    return ana_home() / "ledger.jsonl"


def db_path() -> Path:
    return ana_home() / "ana.db"


def heartbeat_path() -> Path:
    return ana_home() / "heartbeat.json"


# --------------------------------------------------------------------------
# Classification rules
# --------------------------------------------------------------------------
# Each rule: {"category": ..., "match": <regex over "app | title">, "label": ...}
# Rules are evaluated in order; the first match wins, so BLOCK sits above GREY.

DEFAULT_RULES: list[dict[str, str]] = [
    # --- Tier A: overt distraction ------------------------------------------
    {"category": "block", "label": "video",   "match": r"youtube|netflix|primevideo|hulu|disney\+|twitch|tiktok"},
    {"category": "block", "label": "social",  "match": r"reddit|twitter|x\.com|instagram|facebook|threads|snapchat|9gag"},
    {"category": "block", "label": "chat-fun","match": r"discord|telegram|whatsapp"},
    {"category": "block", "label": "news",    "match": r"news\.ycombinator|hacker news|bbc news|cnn|buzzfeed|dailymail"},
    {"category": "block", "label": "games",   "match": r"\bsteam\b|epic games|battle\.net|minecraft|league of legends|solitaire"},
    {"category": "block", "label": "shopping","match": r"amazon\.|ebay|aliexpress|etsy"},

    # --- Tier B: productive procrastination ---------------------------------
    {"category": "grey",  "label": "mail",    "match": r"gmail|outlook|thunderbird|mail\b|inbox"},
    {"category": "grey",  "label": "chat-work","match": r"slack|teams|zoom|meet\.google"},
    {"category": "grey",  "label": "config",  "match": r"settings|preferences|control panel|\.bashrc|\.zshrc|dotfiles|vimrc|keybindings"},
    {"category": "grey",  "label": "planning","match": r"notion|todoist|trello|asana|obsidian|roam|\btodo\b|planner"},
    {"category": "grey",  "label": "browser", "match": r"chrome|firefox|edge|safari|brave|arc\b"},

    # --- Neutral -------------------------------------------------------------
    {"category": "neutral", "label": "os",    "match": r"explorer\.exe|finder|nautilus|desktop|lock ?screen|task manager"},
    {"category": "neutral", "label": "ana",   "match": r"ana ⟶ focus|ana ⟶ dashboard|ana ⟶ interrupt"},
]

# Apps that count as *producing* something. Used by the research-spiral detector:
# a browser is only a spiral if you never come back to one of these.
DEFAULT_PRODUCE_APPS = [
    r"code|vscode|pycharm|intellij|sublime|vim|emacs|neovim|xcode|android studio",
    r"word|docs\.google|pages|scrivener|latex|texstudio|overleaf",
    r"excel|sheets|numbers|tableau|powerbi",
    r"terminal|iterm|powershell|cmd\.exe|konsole|alacritty|wezterm",
    r"figma|photoshop|illustrator|blender|premiere|davinci",
    r"powerpoint|keynote|slides\.google",
]

DEFAULT_PLANNING_APPS = [r"notion|todoist|trello|asana|obsidian|roam|\btodo\b|planner|calendar"]

DEFAULT_MEDIA_PROCS = [r"spotify|vlc|steam|game|netflix|youtube|mpv|quicktime"]


DEFAULT_THRESHOLDS: dict[str, Any] = {
    # sampling
    "sample_seconds": 2.0,

    # Tier A
    "nudge_after_seconds": 10,          # L0
    "intervene_after_seconds": 20,      # L1
    "flicker_visits": 3,                # A3: peeks...
    "flicker_max_visit_seconds": 5,     #     ...shorter than this...
    "flicker_window_seconds": 180,      #     ...within this window

    # Tier B
    "grey_budget_minutes": 5.0,         # per hour of session, default; contract can override
    "yak_shave_minutes": 6.0,           # B2/B7
    "research_spiral_minutes": 12.0,    # B3
    "task_hop_minutes": 10.0,           # B4
    "prep_loop_minutes": 8.0,           # B5
    "stall_minutes": 15.0,              # B6: same title, typing, nothing new

    # Tier C
    "idle_seconds": 120,                # C1: away from keyboard
    "passive_presence_seconds": 240,    # C2/C3: right window, no input
    "fatigue_window_minutes": 15.0,     # C4

    # Tier D
    "micro_session_ratio": 0.4,         # D3
    "max_snoozes": 2,                   # D2
    "intent_min_words": 6,              # D5
    "late_start_minutes": 15,           # D4

    # Tier E
    "heartbeat_gap_seconds": 60,        # E1
    "probes_per_session": 2,            # E4
    "probe_answer_seconds": 15,
    "probe_min_chars": 3,
    "reflect_min_chars": 40,            # L3 answer length

    # teeth
    "debt_multiplier": 1.5,
    "l2_wait_seconds": 30,
    "l3_wait_seconds": 60,
    "lockout_minutes": 20.0,
    "raise_window_attempts": 2,
}


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "user": "",
    "partner": "",                 # accountability partner, named in reports
    "daily_focus_target_minutes": 180,
    "rules": DEFAULT_RULES,
    "produce_apps": DEFAULT_PRODUCE_APPS,
    "planning_apps": DEFAULT_PLANNING_APPS,
    "media_processes": DEFAULT_MEDIA_PROCS,
    "thresholds": DEFAULT_THRESHOLDS,
    "redact_titles_matching": [],   # regexes whose matching titles are stored as "[redacted]"
    "ui": "auto",                  # auto | tk | console
}


def load(path: Path | None = None) -> dict[str, Any]:
    """Load config, filling in anything the user's file is missing."""
    path = path or config_path()
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path.exists():
        try:
            user_cfg = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cfg
        for key, value in user_cfg.items():
            if key == "thresholds" and isinstance(value, dict):
                cfg["thresholds"].update(value)
            else:
                cfg[key] = value
    return cfg


def save(cfg: dict[str, Any], path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, sort_keys=True), encoding="utf-8")


def threshold(cfg: dict[str, Any], key: str) -> Any:
    return cfg.get("thresholds", {}).get(key, DEFAULT_THRESHOLDS[key])


def diff_for_audit(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Human-readable list of what changed, for the rule-change audit (E3/E6).

    Only the things that affect how strictly you are judged are reported; cosmetic
    settings are not worth the noise.
    """
    watched = ("rules", "thresholds", "produce_apps", "planning_apps",
               "daily_focus_target_minutes", "redact_titles_matching")
    changes: list[str] = []
    for key in watched:
        o, n = old.get(key), new.get(key)
        if o == n:
            continue
        if key == "thresholds" and isinstance(o, dict) and isinstance(n, dict):
            for tk in sorted(set(o) | set(n)):
                if o.get(tk) != n.get(tk):
                    changes.append(f"threshold {tk}: {o.get(tk)!r} -> {n.get(tk)!r}")
        elif key == "rules" and isinstance(o, list) and isinstance(n, list):
            old_m = {r.get("match"): r.get("category") for r in o}
            new_m = {r.get("match"): r.get("category") for r in n}
            for m in sorted(set(old_m) - set(new_m)):
                changes.append(f"rule removed: {old_m[m]} {m!r}")
            for m in sorted(set(new_m) - set(old_m)):
                changes.append(f"rule added: {new_m[m]} {m!r}")
            for m in sorted(set(new_m) & set(old_m)):
                if old_m[m] != new_m[m]:
                    changes.append(f"rule recategorised: {m!r} {old_m[m]} -> {new_m[m]}")
        else:
            changes.append(f"{key}: {o!r} -> {n!r}")
    return changes
