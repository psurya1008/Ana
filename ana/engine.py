"""FocusEngine — the pure state machine.

Samples in, Actions out. No OS calls, no wall clock, no UI. Everything the
system decides happens here, which is why the whole rulebook can be tested in
milliseconds against a synthetic day.

``runtime.py`` is the only module that turns Actions into things that happen on
a real machine.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Optional

from . import avoidance, penalties, scoring
from .classify import Classifier, Verdict
from .models import (
    Action,
    ActionKind,
    Category,
    Challenge,
    Contract,
    Intervention,
    Sample,
    Severity,
    SessionStatus,
    SessionTotals,
    Signal,
)

MINUTE = 60.0
PROBE_QUESTION = "In one line: what are you doing right now?"


@dataclass
class ProbeSlot:
    at: float
    fired: bool = False


@dataclass
class PendingIntervention:
    intervention: Intervention
    issued_at: float
    attempts: int = 0


class FocusEngine:
    def __init__(self, contract: Contract, cfg: dict[str, Any],
                 rng: Optional[random.Random] = None,
                 recent_reflections: Optional[list[str]] = None):
        self.contract = contract
        self.cfg = cfg
        self.rng = rng or random.Random()
        self.classifier = Classifier(cfg, contract)
        self.detectors = avoidance.default_detectors()
        self.escalator = penalties.Escalator(cfg, contract)
        self.escalator.recent_reflections = list(recent_reflections or [])
        self.totals = SessionTotals()

        self.started_at: Optional[float] = None
        self.last_ts: Optional[float] = None
        self.last_window: Optional[str] = None
        self.last_focus_window: Optional[Sample] = None
        self.paused = False
        self.pending: Optional[PendingIntervention] = None
        self.probe_slots: list[ProbeSlot] = []
        self.awaiting_probe: Optional[float] = None
        self.gap_seconds: float = 0.0
        self.excused_seconds: float = 0.0
        self.closed = False
        self.status: SessionStatus = SessionStatus.ACTIVE
        self.signals: list[Signal] = []          # everything caught, for the report
        self.raise_attempts = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self, ts: float) -> list[Action]:
        self.started_at = ts
        self.last_ts = ts
        self._schedule_probes(ts)
        return [Action(ActionKind.LOG, ts,
                       message=f'session started: "{self.contract.intent}"')]

    def _schedule_probes(self, ts: float) -> None:
        from .config import threshold
        n = int(threshold(self.cfg, "probes_per_session"))
        span = self.contract.planned_minutes * MINUTE
        if n <= 0 or span <= 0:
            return
        # Probes land in the middle 80% of the session, never in the first or
        # last few minutes — nobody is avoiding anything 30 seconds in.
        points = sorted(self.rng.uniform(0.1, 0.9) for _ in range(n))
        self.probe_slots = [ProbeSlot(at=ts + p * span) for p in points]

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.last_ts or self.started_at) - self.started_at

    def thr(self, key: str) -> Any:
        from .config import threshold
        return threshold(self.cfg, key)

    def excuse(self, seconds: float) -> None:
        """Discount time the user spent inside Ana's own dialogs.

        The supervisor loop blocks while an overlay or probe is up, so without
        this the next tick sees a multi-second jump and books it as a sleeping
        machine — which zeroes the time and, past 60s, fabricates a desertion
        signal against someone who was sitting there answering Ana's question.
        """
        if seconds > 0:
            self.excused_seconds += seconds
    def tick(self, sample: Sample) -> list[Action]:
        if self.closed:
            return []
        if self.started_at is None:
            return self.start(sample.ts) + self.tick(sample)

        actions: list[Action] = []
        dt = sample.ts - (self.last_ts or sample.ts)

        # Time inside Ana's own prompts is neither focus nor distraction: it is
        # the cost of being interrupted. Take it off the clock before the gap
        # check, so it can never be mistaken for a sleeping or killed machine.
        if self.excused_seconds > 0:
            excused = min(dt, self.excused_seconds)
            dt -= excused
            self.excused_seconds = 0.0
            self.totals.interrupted_seconds += excused

        # A gap much larger than the sampling interval means the machine slept,
        # or the tracker was killed. Neither earns credit.
        max_dt = float(self.thr("sample_seconds")) * 4
        if dt > max_dt:
            self.gap_seconds += dt
            gap_signal = avoidance.desertion_signal(
                dt, float(self.thr("heartbeat_gap_seconds")))
            if gap_signal:
                self.signals.append(gap_signal)
                actions.append(Action(ActionKind.LOG, sample.ts, signal=gap_signal,
                                      message=gap_signal.detail))
            dt = 0.0
        dt = max(0.0, dt)

        verdict = self.classifier.classify(sample)
        idle = sample.idle_seconds >= float(self.thr("idle_seconds"))

        # -- clock ---------------------------------------------------------
        if idle:
            self.totals.idle_seconds += dt
            if not self.paused:
                self.paused = True
                actions.append(Action(ActionKind.PAUSE_CLOCK, sample.ts,
                                      message="clock paused — no input"))
        else:
            if self.paused:
                self.paused = False
                actions.append(Action(ActionKind.RESUME_CLOCK, sample.ts,
                                      message="clock resumed"))
            self._credit(verdict.category, dt)

        key = f"{sample.app}|{sample.title}"
        if self.last_window is not None and key != self.last_window:
            self.totals.switches += 1
        self.last_window = key
        if verdict.category is Category.FOCUS and not idle:
            self.last_focus_window = sample

        # -- detectors ------------------------------------------------------
        tick_ctx = avoidance.Tick(
            ts=sample.ts, dt=dt, sample=sample, verdict=verdict, totals=self.totals,
            elapsed=self.elapsed, contract=self.contract, cfg=self.cfg,
            classifier=self.classifier,
        )
        found: list[Signal] = []
        for detector in self.detectors:
            found.extend(detector.observe(tick_ctx))
        self.signals.extend(found)

        # -- judgement -------------------------------------------------------
        self.last_ts = sample.ts
        if self.pending is not None:
            return actions  # one overlay at a time; the rest waits

        signal = penalties.worst(found)
        intervention = (self.escalator.judge(signal, sample.ts)
                        if signal is not None else None)

        # A probe only fires when nothing more serious is happening. Asking
        # "what are you doing right now?" while you are on YouTube, instead of
        # stopping you, is the one outcome that makes Ana look broken.
        if intervention is None or intervention.level == 0:
            actions.extend(self._maybe_probe(sample, verdict))

        if signal is None or intervention is None:
            return actions

        if intervention.level == 0:
            actions.append(Action(ActionKind.NUDGE, sample.ts, intervention=intervention,
                                  signal=signal, message=intervention.body))
            return actions

        self.totals.interventions += 1
        self.pending = PendingIntervention(intervention, sample.ts)

        # Before the overlay, try simply putting the work back in front of you.
        if (verdict.category is Category.BLOCK and intervention.level <= 2
                and self.last_focus_window is not None
                and self.raise_attempts < int(self.thr("raise_window_attempts"))):
            self.raise_attempts += 1
            actions.append(Action(ActionKind.RAISE_WORK_WINDOW, sample.ts,
                                  payload={"app": self.last_focus_window.app,
                                           "title": self.last_focus_window.title}))

        actions.append(Action(ActionKind.INTERVENE, sample.ts,
                              intervention=intervention, signal=signal))
        if intervention.lockout_minutes:
            actions.append(Action(ActionKind.LOCKOUT, sample.ts,
                                  payload={"minutes": intervention.lockout_minutes}))
        return actions

    def _credit(self, category: Category, dt: float) -> None:
        if category is Category.FOCUS:
            self.totals.focus_seconds += dt
        elif category is Category.GREY:
            self.totals.grey_seconds += dt
        elif category is Category.BLOCK:
            self.totals.block_seconds += dt
        else:
            self.totals.neutral_seconds += dt

    def _maybe_probe(self, sample: Sample, verdict: Verdict) -> list[Action]:
        if self.awaiting_probe is not None or self.pending is not None:
            return []
        # Never probe someone who is visibly on a distraction. There is nothing
        # to check — you can see what they are doing — and asking "what are you
        # doing right now?" instead of stopping them reads as a broken app. The
        # slot is held, not spent, so the probe lands once they are back on task.
        if verdict.category is Category.BLOCK:
            return []
        for slot in self.probe_slots:
            if not slot.fired and sample.ts >= slot.at:
                slot.fired = True
                self.awaiting_probe = sample.ts
                return [Action(ActionKind.PROBE, sample.ts,
                               message=PROBE_QUESTION,
                               payload={"window": f"{sample.app} — {sample.title}",
                                        "seconds": self.thr("probe_answer_seconds")})]
        return []

    # -- responses ---------------------------------------------------------
    def resolve_intervention(self, response: str, waited_seconds: float = 0.0,
                             ts: Optional[float] = None) -> tuple[bool, str, Intervention]:
        """Validate the user's answer to the overlay. Returns (accepted, hint, iv)."""
        if self.pending is None:
            raise RuntimeError("no intervention pending")
        pending = self.pending
        iv = pending.intervention
        if iv.wait_seconds and waited_seconds < iv.wait_seconds:
            pending.attempts += 1
            return False, f"wait {iv.wait_seconds - int(waited_seconds)}s more", iv
        ok, hint = penalties.validate_response(iv, response, self.contract, self.cfg)
        pending.attempts += 1
        if not ok:
            return False, hint, iv
        if iv.challenge is Challenge.REFLECT and response.strip():
            self.escalator.recent_reflections = (
                [response.strip()] + self.escalator.recent_reflections)[:3]
        if iv.reset_streak:
            self.status = SessionStatus.BROKEN
        self.pending = None
        self.raise_attempts = 0
        return True, "", iv

    def break_glass(self, ts: float) -> Action:
        """The always-available exit. It works; it is also written down."""
        self.pending = None
        signal = Signal("break_glass", Severity.CRITICAL,
                        "break-glass used — overlay dismissed without answering", "E1")
        self.signals.append(signal)
        self.status = SessionStatus.DESERTED
        return Action(ActionKind.LOG, ts, signal=signal, message=signal.detail)

    def answer_probe(self, answer: str, ts: float, window: str = "") -> bool:
        """A probe is passed by answering it inside the time limit, with words."""
        if self.awaiting_probe is None:
            return False
        limit = float(self.thr("probe_answer_seconds"))
        in_time = (ts - self.awaiting_probe) <= limit
        substantive = len(answer.strip()) >= int(self.thr("probe_min_chars"))
        passed = bool(in_time and substantive)
        self.awaiting_probe = None
        if passed:
            self.totals.probes_passed += 1
        else:
            self.totals.probes_failed += 1
            self.signals.append(Signal(
                "probe_failed", Severity.MEDIUM,
                "did not answer 'what are you doing right now?' in time"
                if not in_time else "probe answered with nothing", "E4", ts))
        return passed

    def expire_probe(self, ts: float) -> None:
        if self.awaiting_probe is not None:
            self.answer_probe("", ts)

    # -- close --------------------------------------------------------------
    def close(self, ts: float, evidence: str = "",
              deserted: bool = False) -> tuple[SessionStatus, float, list[Signal]]:
        """End the session and hand back the verdict. This is where D3/E5 land."""
        self.closed = True
        self.last_ts = ts
        extra: list[Signal] = []

        micro = avoidance.micro_session_signal(
            self.elapsed, self.contract.planned_minutes,
            float(self.thr("micro_session_ratio")))
        if micro:
            extra.append(micro)

        if deserted or self.status is SessionStatus.DESERTED:
            status = SessionStatus.DESERTED
        elif micro:
            status = SessionStatus.MICRO
        elif self.status is SessionStatus.BROKEN:
            status = SessionStatus.BROKEN
        elif not evidence.strip():
            status = SessionStatus.UNVERIFIED
            extra.append(Signal("no_evidence", Severity.HIGH,
                                "session closed with nothing to show for it — "
                                "it does not count toward the streak", "E5", ts))
        else:
            status = SessionStatus.COMPLETE

        self.signals.extend(extra)
        self.status = status
        score = scoring.session_score(
            self.totals, self.contract.planned_minutes,
            interventions=self.totals.interventions,
            probes_failed=self.totals.probes_failed, status=status)
        return status, score, extra

    # -- introspection for the HUD ------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        planned = self.contract.planned_minutes * MINUTE
        return {
            "intent": self.contract.intent,
            "elapsed_minutes": round(self.elapsed / MINUTE, 1),
            "planned_minutes": self.contract.planned_minutes,
            "focus_minutes": round(self.totals.focus_seconds / MINUTE, 1),
            "grey_minutes": round(self.totals.grey_seconds / MINUTE, 1),
            "block_minutes": round(self.totals.block_seconds / MINUTE, 1),
            "idle_minutes": round(self.totals.idle_seconds / MINUTE, 1),
            "interventions": self.totals.interventions,
            "progress": round(min(1.0, self.totals.focus_seconds / max(planned, 1)), 3),
            "paused": self.paused,
            "status": self.status.value,
            "level": self.escalator.count,
        }
