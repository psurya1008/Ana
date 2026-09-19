"""Accountability: the parts that make the *user* carry the weight.

Pre-commitment (you sign before you start), scheduled blocks you can miss,
evidence you have to produce, debt you have to work off, and a record you
agreed in advance to be judged by.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from . import scoring
from .classify import check_intent, keywords_from
from .config import threshold
from .models import Contract, SessionStatus, Severity, Signal, Stake, StakeKind
from .storage import Store, day_key

MINUTE = 60.0


class ContractRejected(ValueError):
    """The intent gate said no. Sharpen the intent; do not lower the bar."""

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("; ".join(problems))


def sign_contract(intent: str, done_criteria: str, planned_minutes: int,
                  signed_by: str, cfg: dict[str, Any],
                  grey_budget_minutes: Optional[float] = None,
                  stake: Optional[Stake] = None,
                  extra_keywords: Sequence[str] = ()) -> Contract:
    """Build a Contract, refusing the ones designed to be impossible to fail."""
    gate = check_intent(intent, done_criteria,
                        int(threshold(cfg, "intent_min_words")))
    if not gate.ok:
        raise ContractRejected(gate.problems)
    if planned_minutes < 5:
        raise ContractRejected(["a session shorter than 5 minutes is not a session"])
    if not signed_by.strip():
        raise ContractRejected(["sign it — type your name"])
    return Contract(
        intent=intent.strip(),
        done_criteria=done_criteria.strip(),
        planned_minutes=int(planned_minutes),
        signed_by=signed_by.strip(),
        grey_budget_minutes=(grey_budget_minutes
                             if grey_budget_minutes is not None
                             else float(threshold(cfg, "grey_budget_minutes"))),
        stake=stake or Stake(StakeKind.DEBT, 15.0),
        keywords=keywords_from(f"{intent} {done_criteria}", extra_keywords),
    )


# ---------------------------------------------------------------------------
# Tier D: scheduled blocks you can actually miss
# ---------------------------------------------------------------------------

def _hhmm_to_seconds(hhmm: str) -> int:
    try:
        h, m = hhmm.split(":")
        return int(h) * 3600 + int(m) * 60
    except (ValueError, AttributeError):
        return 0


def _seconds_into_day(ts: float) -> int:
    lt = time.localtime(ts)
    return lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec


def due_commitments(store: Store, cfg: dict[str, Any],
                    now: Optional[float] = None) -> list[tuple[Any, int]]:
    """Pending blocks whose start time has passed. Returns (row, minutes_late)."""
    now = now if now is not None else time.time()
    today = day_key(now)
    seconds_now = _seconds_into_day(now)
    out = []
    for row in store.pending_commitments(today):
        late = seconds_now - _hhmm_to_seconds(row["start_hhmm"])
        if late >= 0:
            out.append((row, int(late // 60)))
    return out


def schedule_signals(store: Store, cfg: dict[str, Any],
                     now: Optional[float] = None) -> list[Signal]:
    """D1/D2/D4 — the failures that happen before a session ever starts."""
    now = now if now is not None else time.time()
    late_limit = int(threshold(cfg, "late_start_minutes"))
    max_snoozes = int(threshold(cfg, "max_snoozes"))
    signals: list[Signal] = []
    for row, late in due_commitments(store, cfg, now):
        if late >= late_limit:
            sev = Severity.HIGH if late >= 60 else Severity.MEDIUM
            signals.append(Signal(
                "block_missed", sev,
                f'"{row["intent"]}" was due at {row["start_hhmm"]} — {late} min ago, '
                f"not started", "D1/D4", now))
        if row["snoozes"] >= max_snoozes:
            signals.append(Signal(
                "snooze_abuse", Severity.HIGH,
                f'"{row["intent"]}" snoozed {row["snoozes"]} times', "D2", now))
    return signals


def settle_past_commitments(store: Store, cfg: dict[str, Any],
                            now: Optional[float] = None) -> list[str]:
    """At day's end, every unstarted block becomes a recorded miss. No rollover."""
    now = now if now is not None else time.time()
    missed = []
    for row in store.pending_commitments(day_key(now)):
        store.settle_commitment(row["id"], "missed")
        missed.append(row["intent"])
    return missed


# ---------------------------------------------------------------------------
# E5: evidence. "It went well" is not evidence.
# ---------------------------------------------------------------------------

@dataclass
class Evidence:
    statement: str = ""
    files_changed: tuple[str, ...] = ()
    git_stat: str = ""

    @property
    def verified(self) -> bool:
        return bool(self.statement.strip())

    def render(self) -> str:
        parts = [self.statement.strip()] if self.statement.strip() else []
        if self.files_changed:
            shown = ", ".join(self.files_changed[:8])
            more = f" (+{len(self.files_changed) - 8} more)" if len(self.files_changed) > 8 else ""
            parts.append(f"files touched: {shown}{more}")
        if self.git_stat:
            parts.append(f"git: {self.git_stat}")
        return " | ".join(parts)


def collect_evidence(statement: str, project_path: Optional[str] = None,
                     since: Optional[float] = None) -> Evidence:
    """Your sentence, plus whatever the filesystem is willing to corroborate."""
    files: list[str] = []
    git_stat = ""
    if project_path and since is not None:
        root = Path(project_path).expanduser()
        if root.is_dir():
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames
                               if d not in {".git", "node_modules", "__pycache__",
                                            ".venv", "venv", "dist", "build"}]
                for name in filenames:
                    p = Path(dirpath) / name
                    try:
                        if p.stat().st_mtime >= since:
                            files.append(str(p.relative_to(root)))
                    except OSError:
                        continue
                if len(files) > 200:
                    break
            git_stat = _git_stat(root)
    return Evidence(statement=statement, files_changed=tuple(sorted(files)[:50]),
                    git_stat=git_stat)


def _git_stat(root: Path) -> str:
    if not (root / ".git").exists():
        return ""
    try:
        out = subprocess.run(["git", "-C", str(root), "diff", "--shortstat"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------------------
# The standing account: what you owe and what you have earned
# ---------------------------------------------------------------------------

@dataclass
class Standing:
    day: str
    focus_minutes: float
    target_minutes: float
    debt_minutes: float
    streak: int
    recreation_unlocked: bool
    integrity_ok: bool
    integrity_note: str = ""
    missed_blocks: tuple[str, ...] = ()
    open_sessions: tuple[str, ...] = ()

    def headline(self) -> str:
        bar = f"{self.focus_minutes:.0f}/{self.target_minutes:.0f} min"
        bits = [f"focus {bar}", f"streak {self.streak}"]
        if self.debt_minutes > 0:
            bits.append(f"debt {self.debt_minutes:.0f} min")
        if not self.recreation_unlocked:
            bits.append("recreation LOCKED")
        if not self.integrity_ok:
            bits.append("INTEGRITY BREACH")
        return " · ".join(bits)


def standing(store: Store, cfg: dict[str, Any], ledger: Any = None,
             now: Optional[float] = None) -> Standing:
    now = now if now is not None else time.time()
    today = day_key(now)
    target = float(cfg.get("daily_focus_target_minutes", 180))
    rows = store.sessions_for_day(today)
    summary = scoring.summarise_day(today, rows, target)

    summaries = [scoring.summarise_day(d, store.sessions_for_day(d), target)
                 for d in store.days_with_sessions(90)]
    # Today is still in progress, so a not-yet-clean today must not zero the
    # streak you have already built — it just doesn't extend it yet. A clean
    # today does count immediately.
    if summaries and summaries[0].day == today and not summaries[0].clean:
        summaries = summaries[1:]
    run = scoring.streak(summaries)

    debt = store.debt_seconds()
    unlocked, _ = scoring.debt_status(debt)

    integrity_ok, note = True, ""
    if ledger is not None:
        v = ledger.verify()
        integrity_ok, note = v.ok, v.describe()

    missed = tuple(r["intent"] for r in store.commitments_for_day(today)
                   if r["status"] == "missed")
    open_sessions = tuple(r["id"] for r in store.active_sessions())

    return Standing(
        day=today,
        focus_minutes=summary.focus_minutes,
        target_minutes=target,
        debt_minutes=round(debt / MINUTE, 1),
        streak=run,
        recreation_unlocked=unlocked and summary.met_target,
        integrity_ok=integrity_ok,
        integrity_note=note,
        missed_blocks=missed,
        open_sessions=open_sessions,
    )


def apply_session_outcome(store: Store, cfg: dict[str, Any], session_id: str,
                          status: SessionStatus, totals: Any, contract: Contract,
                          now: Optional[float] = None) -> list[str]:
    """Book the consequences of a finished session. Returns what was charged."""
    now = now if now is not None else time.time()
    charged: list[str] = []
    mult = float(threshold(cfg, "debt_multiplier"))

    distracted = totals.block_seconds + totals.idle_seconds
    if distracted > 0:
        store.add_debt(distracted * mult, "distracted time", session_id, now)
        charged.append(f"{distracted * mult / MINUTE:.1f} min debt for distracted time")

    if status in (SessionStatus.BROKEN, SessionStatus.DESERTED, SessionStatus.MICRO):
        shortfall = max(0.0, contract.planned_minutes * MINUTE - totals.focus_seconds)
        store.add_debt(shortfall * 0.5, f"session {status.value}", session_id, now)
        charged.append(f"{shortfall * 0.5 / MINUTE:.1f} min debt for the shortfall")
        charged.append(f"stake forfeit — {contract.stake.describe()}")

    # Focus delivered beyond today's target pays the debt down.
    target = float(cfg.get("daily_focus_target_minutes", 180)) * MINUTE
    today_rows = store.sessions_for_day(day_key(now))
    delivered = sum(scoring._totals_of(r).focus_seconds for r in today_rows)
    if delivered > target:
        before = store.debt_seconds()
        after = scoring.repay_debt(before, delivered, target)
        if after < before:
            store.add_debt(-(before - after), "repaid by surplus focus", session_id, now)
            charged.append(f"{(before - after) / MINUTE:.1f} min debt repaid")
    return charged
