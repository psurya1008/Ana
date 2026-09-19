"""The detector suite: one detector per row of the taxonomy in docs/PLAN.md §1.

Every detector is pure. It is fed :class:`Tick` objects (a sample plus the
classifier's verdict plus the session clock) and emits :class:`Signal` objects.
No OS calls, no wall clock, no UI — which is why all of this is unit-tested.

Tier A is the easy half. Tiers B–C are the half that beats other tools, because
avoidance is disguised as work: the window looks legitimate, the keyboard is
busy, and nothing gets made.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

from .classify import Classifier, Verdict
from .models import Category, Contract, Sample, Severity, Signal, SessionTotals

MINUTE = 60.0


@dataclass
class Tick:
    """Everything a detector is allowed to know about this instant."""

    ts: float
    dt: float                     # seconds since the previous tick
    sample: Sample
    verdict: Verdict
    totals: SessionTotals
    elapsed: float                # seconds since the session started
    contract: Contract
    cfg: dict[str, Any]
    classifier: Classifier

    @property
    def idle(self) -> bool:
        return self.sample.idle_seconds >= self.thr("idle_seconds")

    @property
    def window_key(self) -> str:
        return f"{self.sample.app}|{self.sample.title}"

    def thr(self, key: str) -> Any:
        from .config import threshold
        return threshold(self.cfg, key)


class Detector:
    """Base class. Subclasses implement :meth:`observe`."""

    name = "detector"
    taxonomy = ""

    def __init__(self) -> None:
        self._last_fired: float = -1e9

    def cooldown_seconds(self) -> float:
        return 120.0

    def ready(self, ts: float) -> bool:
        return ts - self._last_fired >= self.cooldown_seconds()

    def fire(self, ts: float, severity: Severity, detail: str, **extra: Any) -> Signal:
        self._last_fired = ts
        return Signal(kind=self.name, severity=severity, detail=detail,
                      taxonomy=self.taxonomy, ts=ts)

    def observe(self, tick: Tick) -> list[Signal]:  # pragma: no cover - interface
        return []

    def reset(self) -> None:
        self._last_fired = -1e9


# ---------------------------------------------------------------------------
# Tier A — overt distraction
# ---------------------------------------------------------------------------

class BlockDwellDetector(Detector):
    """A1/A2: sitting on a known distraction. The bread-and-butter case."""

    name = "block_dwell"
    taxonomy = "A1/A2"

    def __init__(self) -> None:
        super().__init__()
        self.dwell = 0.0
        self._nudged = False
        self._escalations = 0

    def cooldown_seconds(self) -> float:
        # Ana gets more patient about *re-asking*, not more lenient: each rung
        # already costs more than the last, so re-firing every 20s on one long
        # stretch is just noise that gets an app uninstalled.
        return 20.0 + 70.0 * self._escalations

    def observe(self, tick: Tick) -> list[Signal]:
        if tick.verdict.category is not Category.BLOCK:
            self.dwell = 0.0
            self._nudged = False
            self._escalations = 0
            return []

        self.dwell += tick.dt
        nudge_at = tick.thr("nudge_after_seconds")
        act_at = tick.thr("intervene_after_seconds")
        what = tick.sample.title or tick.sample.app

        if self.dwell >= act_at and self.ready(tick.ts):
            self._escalations += 1
            sev = Severity.HIGH if self._escalations > 1 else Severity.MEDIUM
            return [self.fire(tick.ts, sev,
                              f"{int(self.dwell)}s on {what} ({tick.verdict.label})")]
        if self.dwell >= nudge_at and not self._nudged:
            self._nudged = True
            return [Signal(self.name + "_nudge", Severity.LOW,
                           f"drifting into {what}", self.taxonomy, tick.ts)]
        return []


class FlickerDetector(Detector):
    """A3: the alt-tab peek. Individually trivial, collectively a shredded hour."""

    name = "flicker"
    taxonomy = "A3"

    def __init__(self) -> None:
        super().__init__()
        self._current: Optional[str] = None
        self._current_started: float = 0.0
        self._current_cat: Category = Category.NEUTRAL
        self._peeks: deque[float] = deque()

    def cooldown_seconds(self) -> float:
        return 300.0

    def observe(self, tick: Tick) -> list[Signal]:
        key = tick.window_key
        if key != self._current:
            if self._current is not None:
                duration = tick.ts - self._current_started
                if (self._current_cat in (Category.BLOCK, Category.GREY)
                        and duration <= tick.thr("flicker_max_visit_seconds")):
                    self._peeks.append(tick.ts)
            self._current = key
            self._current_started = tick.ts
            self._current_cat = tick.verdict.category

        window = tick.thr("flicker_window_seconds")
        while self._peeks and tick.ts - self._peeks[0] > window:
            self._peeks.popleft()

        if len(self._peeks) >= tick.thr("flicker_visits") and self.ready(tick.ts):
            n = len(self._peeks)
            self._peeks.clear()
            return [self.fire(tick.ts, Severity.MEDIUM,
                              f"{n} sub-{int(tick.thr('flicker_max_visit_seconds'))}s peeks "
                              f"at off-task windows in the last "
                              f"{int(window / MINUTE)} min")]
        return []


class BackgroundMediaDetector(Detector):
    """A4: the distraction you left running on the other monitor."""

    name = "background_media"
    taxonomy = "A4"

    def cooldown_seconds(self) -> float:
        return 600.0

    def observe(self, tick: Tick) -> list[Signal]:
        if not tick.sample.background:
            return []
        patterns = [re.compile(p, re.I) for p in tick.cfg.get("media_processes", [])]
        hits = [p for p in tick.sample.background
                if any(rx.search(p) for rx in patterns)]
        if hits and self.ready(tick.ts):
            return [self.fire(tick.ts, Severity.LOW,
                              f"running in the background: {', '.join(sorted(set(hits)))}")]
        return []


# ---------------------------------------------------------------------------
# Tier B — productive procrastination
# ---------------------------------------------------------------------------

class GreyBudgetDetector(Detector):
    """B1: 'just clearing comms'. Grey is allowed, but it is metered."""

    name = "grey_over_budget"
    taxonomy = "B1"

    def cooldown_seconds(self) -> float:
        return 180.0

    def budget_seconds(self, tick: Tick) -> float:
        per_hour = tick.contract.grey_budget_minutes or tick.thr("grey_budget_minutes")
        hours = max(1.0, tick.contract.planned_minutes / 60.0)
        return per_hour * MINUTE * hours

    def observe(self, tick: Tick) -> list[Signal]:
        if tick.verdict.category is not Category.GREY:
            return []
        budget = self.budget_seconds(tick)
        used = tick.totals.grey_seconds
        if used <= budget:
            return []
        over = used - budget
        sev = Severity.HIGH if over > budget else Severity.MEDIUM
        if self.ready(tick.ts):
            return [self.fire(tick.ts, sev,
                              f"grey budget spent ({used / MINUTE:.1f} of "
                              f"{budget / MINUTE:.1f} min) — "
                              f"{tick.verdict.label} is costing you now")]
        return []


class YakShaveDetector(Detector):
    """B2/B7: sharpening the axe forever. Includes tuning Ana itself."""

    name = "yak_shave"
    taxonomy = "B2/B7"

    def __init__(self) -> None:
        super().__init__()
        self.seconds = 0.0

    def cooldown_seconds(self) -> float:
        return 600.0

    def observe(self, tick: Tick) -> list[Signal]:
        if tick.verdict.label not in ("config", "ana"):
            return []
        self.seconds += tick.dt
        limit = tick.thr("yak_shave_minutes") * MINUTE
        if self.seconds >= limit and self.ready(tick.ts):
            subject = "your tools" if tick.verdict.label == "config" else "Ana itself"
            return [self.fire(tick.ts, Severity.MEDIUM,
                              f"{self.seconds / MINUTE:.0f} min spent configuring "
                              f"{subject} during a work session")]
        return []


class ResearchSpiralDetector(Detector):
    """B3: nine tabs of legitimate reading, each one further from the task."""

    name = "research_spiral"
    taxonomy = "B3"

    def __init__(self) -> None:
        super().__init__()
        self.browsing = 0.0
        self._titles: set[str] = set()

    def cooldown_seconds(self) -> float:
        return 420.0

    def observe(self, tick: Tick) -> list[Signal]:
        if tick.classifier.is_produce_app(tick.sample):
            self.browsing = 0.0
            self._titles.clear()
            return []
        if tick.verdict.category is Category.NEUTRAL or tick.idle:
            return []
        self.browsing += tick.dt
        if tick.sample.title:
            self._titles.add(tick.sample.title)

        limit = tick.thr("research_spiral_minutes") * MINUTE
        if self.browsing >= limit and self.ready(tick.ts):
            return [self.fire(tick.ts, Severity.MEDIUM,
                              f"{self.browsing / MINUTE:.0f} min of reading across "
                              f"{len(self._titles)} windows without once returning to "
                              f"something you make things in")]
        return []


class TaskHopDetector(Detector):
    """B4: real work, wrong work. Switching to another genuine task to dodge this one."""

    name = "task_hop"
    taxonomy = "B4"

    def __init__(self) -> None:
        super().__init__()
        self._last_on_intent: Optional[float] = None

    def cooldown_seconds(self) -> float:
        return 420.0

    def observe(self, tick: Tick) -> list[Signal]:
        if tick.verdict.matched_keyword:
            self._last_on_intent = tick.ts
            return []
        if self._last_on_intent is None:
            self._last_on_intent = tick.ts  # grace period from session start
            return []
        if tick.idle or tick.verdict.category is not Category.FOCUS:
            return []  # some other detector owns those cases
        gap = tick.ts - self._last_on_intent
        limit = tick.thr("task_hop_minutes") * MINUTE
        if gap >= limit and self.ready(tick.ts):
            return [self.fire(tick.ts, Severity.HIGH,
                              f"{gap / MINUTE:.0f} min of real work that is not the work "
                              f"you declared: \"{tick.contract.intent}\"")]
        return []


class PrepLoopDetector(Detector):
    """B5: planning the work is not the work. Fires once, early, when it counts."""

    name = "prep_loop"
    taxonomy = "B5"

    def __init__(self) -> None:
        super().__init__()
        self.seconds = 0.0
        self._done = False

    def observe(self, tick: Tick) -> list[Signal]:
        if self._done:
            return []
        if not tick.classifier.is_planning_app(tick.sample):
            return []
        self.seconds += tick.dt
        limit = tick.thr("prep_loop_minutes") * MINUTE
        if self.seconds >= limit:
            self._done = True
            return [self.fire(tick.ts, Severity.MEDIUM,
                              f"{self.seconds / MINUTE:.0f} min organising the work "
                              f"instead of starting it")]
        return []


class StallDetector(Detector):
    """B6: the perfection stall — busy hands, one screen, no movement."""

    name = "stall"
    taxonomy = "B6"

    def __init__(self) -> None:
        super().__init__()
        self._title: Optional[str] = None
        self._since: float = 0.0

    def cooldown_seconds(self) -> float:
        return 900.0

    def observe(self, tick: Tick) -> list[Signal]:
        if tick.idle or tick.verdict.category is not Category.FOCUS:
            self._title = None
            return []
        if tick.sample.title != self._title:
            self._title = tick.sample.title
            self._since = tick.ts
            return []
        held = tick.ts - self._since
        limit = tick.thr("stall_minutes") * MINUTE
        if held >= limit and self.ready(tick.ts):
            return [self.fire(tick.ts, Severity.LOW,
                              f"{held / MINUTE:.0f} min on the same screen, still typing, "
                              f"nothing new opened or finished — circling?")]
        return []


# ---------------------------------------------------------------------------
# Tier C — absence and passivity
# ---------------------------------------------------------------------------

class IdleDetector(Detector):
    """C1: away from keyboard. The clock stops; the absence is recorded."""

    name = "idle"
    taxonomy = "C1"

    def __init__(self) -> None:
        super().__init__()
        self._announced = False

    def cooldown_seconds(self) -> float:
        return 0.0

    def observe(self, tick: Tick) -> list[Signal]:
        if not tick.idle:
            self._announced = False
            return []
        if self._announced:
            return []
        self._announced = True
        secs = int(tick.sample.idle_seconds)
        return [self.fire(tick.ts, Severity.MEDIUM,
                          f"no input for {secs}s — the session clock is paused")]


class PassivePresenceDetector(Detector):
    """C2/C3: the right window, the phone in your hand. Looks perfect on a graph."""

    name = "passive_presence"
    taxonomy = "C2/C3"

    def __init__(self) -> None:
        super().__init__()
        self.seconds = 0.0

    def cooldown_seconds(self) -> float:
        return 600.0

    def observe(self, tick: Tick) -> list[Signal]:
        soft_idle = tick.sample.idle_seconds >= 45
        if not (soft_idle and tick.verdict.category is Category.FOCUS):
            return []
        self.seconds += tick.dt
        limit = tick.thr("passive_presence_seconds")
        if self.seconds >= limit and self.ready(tick.ts):
            return [self.fire(tick.ts, Severity.HIGH,
                              f"{self.seconds / MINUTE:.0f} min parked on the work without "
                              f"touching it — present, not working")]
        return []


class FatigueDetector(Detector):
    """C4: output collapsed a while ago and you are still sitting there."""

    name = "fatigue"
    taxonomy = "C4"

    def __init__(self) -> None:
        super().__init__()
        self._window: deque[tuple[float, bool]] = deque()
        self._focus_all = 0.0
        self._total_all = 0.0

    def cooldown_seconds(self) -> float:
        return 1200.0

    def observe(self, tick: Tick) -> list[Signal]:
        focused = tick.verdict.category is Category.FOCUS and not tick.idle
        self._window.append((tick.ts, focused))
        self._total_all += tick.dt
        if focused:
            self._focus_all += tick.dt

        span = tick.thr("fatigue_window_minutes") * MINUTE
        while self._window and tick.ts - self._window[0][0] > span:
            self._window.popleft()

        if self._total_all < 30 * MINUTE or len(self._window) < 10:
            return []
        recent = sum(1 for _, f in self._window if f) / len(self._window)
        overall = self._focus_all / max(self._total_all, 1e-6)
        if recent < overall * 0.5 and self.ready(tick.ts):
            return [self.fire(tick.ts, Severity.LOW,
                              f"focus in the last {int(span / MINUTE)} min is "
                              f"{recent * 100:.0f}% against {overall * 100:.0f}% for the "
                              f"session — you are running on fumes, take a real break")]
        return []


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def default_detectors() -> list[Detector]:
    return [
        BlockDwellDetector(),
        FlickerDetector(),
        BackgroundMediaDetector(),
        GreyBudgetDetector(),
        YakShaveDetector(),
        ResearchSpiralDetector(),
        TaskHopDetector(),
        PrepLoopDetector(),
        StallDetector(),
        IdleDetector(),
        PassivePresenceDetector(),
        FatigueDetector(),
    ]


# ---------------------------------------------------------------------------
# Tier D / E — checks that run at the edges of a session, not during it
# ---------------------------------------------------------------------------

def micro_session_signal(elapsed_seconds: float, planned_minutes: int,
                         ratio: float = 0.4) -> Optional[Signal]:
    """D3: quitting far short of what you signed for."""
    planned = planned_minutes * MINUTE
    if planned <= 0 or elapsed_seconds >= planned * ratio:
        return None
    return Signal("micro_session", Severity.HIGH,
                  f"quit after {elapsed_seconds / MINUTE:.0f} of {planned_minutes} "
                  f"declared minutes ({elapsed_seconds / planned * 100:.0f}%)", "D3")


def desertion_signal(gap_seconds: float, threshold: float = 60.0) -> Optional[Signal]:
    """E1: the tracker stopped while a session was open."""
    if gap_seconds < threshold:
        return None
    return Signal("desertion", Severity.CRITICAL,
                  f"a session was left open and the tracker stopped for "
                  f"{gap_seconds / MINUTE:.0f} min — recorded as desertion", "E1")


def integrity_signal(verification: Any) -> Optional[Signal]:
    """E2: the ledger chain does not verify."""
    if getattr(verification, "ok", True):
        return None
    return Signal("integrity_breach", Severity.CRITICAL, verification.describe(), "E2")


def rule_change_signal(changes: list[str], during_session: bool) -> Optional[Signal]:
    """E3/E6: loosening the rules is allowed. Doing it quietly is not."""
    if not changes:
        return None
    sev = Severity.HIGH if during_session else Severity.MEDIUM
    when = "rules changed mid-session" if during_session else "rules changed"
    return Signal("rule_change", sev, f"{when}: " + "; ".join(changes[:5]), "E3/E6")
