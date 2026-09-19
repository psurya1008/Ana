"""Focus score, focus debt, streaks and rollups.

The score is deliberately hard to fake: time on the declared task is the only
thing that adds to it, and everything the detectors caught subtracts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .models import SessionStatus, SessionTotals

MINUTE = 60.0


def session_score(totals: SessionTotals, planned_minutes: int,
                  interventions: int = 0, probes_failed: int = 0,
                  status: SessionStatus = SessionStatus.COMPLETE) -> float:
    """0–100. Earned by focus time actually delivered, then docked for what happened."""
    planned = max(planned_minutes * MINUTE, 1.0)
    delivered = min(totals.focus_seconds / planned, 1.0)
    base = 100.0 * delivered

    tracked = max(totals.tracked_seconds, 1.0)
    base -= 40.0 * (totals.block_seconds / tracked)
    base -= 25.0 * (totals.idle_seconds / tracked)
    base -= 15.0 * (totals.grey_seconds / tracked)
    base -= 6.0 * interventions
    base -= 8.0 * probes_failed

    if status is SessionStatus.UNVERIFIED:
        base *= 0.5          # no evidence, half credit
    elif status is SessionStatus.BROKEN:
        base *= 0.5
    elif status in (SessionStatus.MICRO, SessionStatus.DESERTED):
        base *= 0.25

    return round(max(0.0, min(100.0, base)), 1)


def counts_for_streak(status: SessionStatus, totals: SessionTotals,
                      planned_minutes: int) -> bool:
    """Only a completed, evidenced, substantially-delivered session counts."""
    if status is not SessionStatus.COMPLETE:
        return False
    return totals.focus_seconds >= 0.6 * planned_minutes * MINUTE


@dataclass
class DaySummary:
    day: str
    sessions: int = 0
    completed: int = 0
    focus_minutes: float = 0.0
    block_minutes: float = 0.0
    grey_minutes: float = 0.0
    idle_minutes: float = 0.0
    interrupted_minutes: float = 0.0
    interventions: int = 0
    broken: int = 0
    unverified: int = 0
    deserted: int = 0
    micro: int = 0
    score: float = 0.0
    target_minutes: float = 0.0

    @property
    def met_target(self) -> bool:
        return self.target_minutes > 0 and self.focus_minutes >= self.target_minutes

    @property
    def clean(self) -> bool:
        """A day that counts: target met and nothing ugly on the record."""
        return self.met_target and not (self.broken or self.deserted or self.unverified)


def summarise_day(day: str, rows: Sequence[Any], target_minutes: float,
                  interventions: int = 0) -> DaySummary:
    s = DaySummary(day=day, target_minutes=target_minutes, interventions=interventions)
    scores: list[float] = []
    for row in rows:
        s.sessions += 1
        totals = _totals_of(row)
        s.focus_minutes += totals.focus_seconds / MINUTE
        s.block_minutes += totals.block_seconds / MINUTE
        s.grey_minutes += totals.grey_seconds / MINUTE
        s.idle_minutes += totals.idle_seconds / MINUTE
        s.interrupted_minutes += totals.interrupted_seconds / MINUTE
        status = _status_of(row)
        if status is SessionStatus.COMPLETE:
            s.completed += 1
        elif status is SessionStatus.BROKEN:
            s.broken += 1
        elif status is SessionStatus.UNVERIFIED:
            s.unverified += 1
        elif status is SessionStatus.DESERTED:
            s.deserted += 1
        elif status is SessionStatus.MICRO:
            s.micro += 1
        score = _get(row, "score")
        if score is not None:
            scores.append(float(score))
    s.score = round(sum(scores) / len(scores), 1) if scores else 0.0
    for key in ("focus_minutes", "block_minutes", "grey_minutes", "idle_minutes",
                "interrupted_minutes"):
        setattr(s, key, round(getattr(s, key), 1))
    return s


def streak(day_summaries: Iterable[DaySummary]) -> int:
    """Consecutive clean days, counting back from the most recent."""
    run = 0
    for s in day_summaries:
        if s.clean:
            run += 1
        else:
            break
    return run


def debt_status(debt_seconds: float) -> tuple[bool, str]:
    """Recreation stays locked until the debt is worked off."""
    if debt_seconds <= 0:
        return True, "clear"
    return False, f"{debt_seconds / MINUTE:.0f} min of focus debt outstanding"


def repay_debt(current_seconds: float, focus_seconds_delivered: float,
               target_seconds: float) -> float:
    """Focus delivered *above* the day's target pays the debt down."""
    surplus = max(0.0, focus_seconds_delivered - target_seconds)
    return max(0.0, current_seconds - surplus)


# -- row adapters (sqlite3.Row or plain dict both work) --------------------

def _get(row: Any, key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)
    return default if value is None else value


def _totals_of(row: Any) -> SessionTotals:
    raw = _get(row, "totals", "{}")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except json.JSONDecodeError:
            raw = {}
    if isinstance(raw, SessionTotals):
        return raw
    fields = SessionTotals().__dict__.keys()
    return SessionTotals(**{k: raw.get(k, 0) for k in fields if k in raw})


def _status_of(row: Any) -> SessionStatus:
    raw = _get(row, "status", "complete")
    if isinstance(raw, SessionStatus):
        return raw
    try:
        return SessionStatus(raw)
    except ValueError:
        return SessionStatus.COMPLETE
