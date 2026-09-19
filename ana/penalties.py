"""Signals in, consequences out. The escalation ladder from docs/PLAN.md §3.

Ana's punishments are time, forfeit and exposure — never destruction. It will
not delete your work, take your money, post anything, or refuse to quit. What it
does is make the distracted choice cost something you already agreed it should
cost, while you were still the version of yourself who wanted to work.
"""

from __future__ import annotations

from typing import Any, Optional

from .models import (
    Challenge,
    Contract,
    Intervention,
    Severity,
    SEVERITY_RANK,
    Signal,
)

MINUTE = 60.0

# Signals that skip the polite rungs. These are the avoidance patterns that a
# nudge has never once fixed.
STRAIGHT_TO_RECKONING = {"task_hop", "passive_presence", "desertion", "integrity_breach"}

# Signals that are information, not accusation — Ana says it once and moves on.
ADVISORY = {"fatigue", "background_media", "idle", "stall"}


def _headline(signal: Signal, level: int) -> str:
    return {
        0: "Drifting.",
        1: "Stop.",
        2: "Second time.",
        3: "This is avoidance.",
        4: "Locked out.",
    }.get(level, "Stop.")


def _body(signal: Signal, contract: Contract, level: int,
          reflections: list[str] | None = None) -> str:
    lines = [signal.detail, ""]
    lines.append(f'You signed: "{contract.intent}"')
    lines.append(f"Done when: {contract.done_criteria}")
    if level >= 3:
        lines.append("")
        lines.append("Not 'why did you get distracted'. What are you avoiding?")
        if reflections:
            lines.append("")
            lines.append("The last times you were asked, you said:")
            lines.extend(f"  · {r}" for r in reflections)
    return "\n".join(lines)


class Escalator:
    """Tracks how far up the ladder this session has gone."""

    def __init__(self, cfg: dict[str, Any], contract: Contract):
        self.cfg = cfg
        self.contract = contract
        self.count = 0          # interventions issued this session
        self.lockout_until: float = 0.0
        self.recent_reflections: list[str] = []

    def thr(self, key: str) -> Any:
        from .config import threshold
        return threshold(self.cfg, key)

    def locked_out(self, ts: float) -> bool:
        return ts < self.lockout_until

    # -- the decision ------------------------------------------------------
    def level_for(self, signal: Signal, ts: float) -> int:
        # A nudge is a nudge even during a lockout. Turning the quiet warning
        # into a full overlay is how the ladder loses its meaning.
        if (signal.kind in ADVISORY or signal.kind.endswith("_nudge")
                or signal.severity is Severity.LOW):
            return 0
        if self.locked_out(ts):
            return 4
        if signal.kind in STRAIGHT_TO_RECKONING or signal.severity is Severity.CRITICAL:
            return max(3, min(4, self.count + 1))
        # ordinary distraction: one rung per prior intervention
        return min(4, self.count + 1)

    def judge(self, signal: Signal, ts: float) -> Optional[Intervention]:
        """Turn a Signal into an Intervention, or None if it isn't worth stopping you."""
        level = self.level_for(signal, ts)
        mult = float(self.thr("debt_multiplier"))

        if level == 0:
            return Intervention(
                level=0,
                challenge=Challenge.NONE,
                wait_seconds=0,
                headline=_headline(signal, 0),
                body=signal.detail,
                consequences=[],
                signal_kind=signal.kind,
            )

        self.count += 1
        consequences: list[str] = []
        debt = 0.0
        reset_streak = False
        lockout = 0.0

        if level == 1:
            challenge = Challenge.TRANSCRIBE_INTENT
            wait = 0
            debt = 30.0 * mult
            consequences.append(f"+{debt / MINUTE:.1f} min focus debt")
        elif level == 2:
            challenge = Challenge.WAIT_THEN_TRANSCRIBE
            wait = int(self.thr("l2_wait_seconds"))
            debt = 90.0 * mult
            consequences.append(f"{wait}s you do not get back")
            consequences.append(f"+{debt / MINUTE:.1f} min focus debt")
        elif level == 3:
            challenge = Challenge.REFLECT
            wait = int(self.thr("l3_wait_seconds"))
            debt = 240.0 * mult
            reset_streak = True
            consequences.append(f"{wait}s wait")
            consequences.append(f"+{debt / MINUTE:.1f} min focus debt (doubled)")
            consequences.append("session marked BROKEN — it will not count")
            consequences.append(f"streak reset · stake forfeit: {self.contract.stake.describe()}")
        else:
            challenge = Challenge.REFLECT
            wait = int(self.thr("l3_wait_seconds"))
            reset_streak = True
            lockout = float(self.thr("lockout_minutes"))
            first_l4 = self.lockout_until <= ts
            # The first L4 is the expensive one. Repeats inside the same lockout
            # still stop you, but they do not compound the charge — the time
            # itself is already billed as distracted time at session close, and
            # a debt that runs away stops being a number anyone acts on.
            debt = (300.0 if first_l4 else 90.0) * mult
            self.lockout_until = max(self.lockout_until, ts + lockout * MINUTE)
            if first_l4:
                consequences.append(f"recreation lockout for {lockout:.0f} min")
            else:
                consequences.append(
                    f"still locked out for "
                    f"{(self.lockout_until - ts) / MINUTE:.0f} more min")
            consequences.append(f"+{debt / MINUTE:.1f} min focus debt")
            consequences.append("flagged in bold in today's report")

        return Intervention(
            level=level,
            challenge=challenge,
            wait_seconds=wait,
            headline=_headline(signal, level),
            body=_body(signal, self.contract, level, self.recent_reflections),
            consequences=consequences,
            debt_seconds=debt,
            reset_streak=reset_streak,
            lockout_minutes=lockout,
            signal_kind=signal.kind,
        )


def worst(signals: list[Signal]) -> Optional[Signal]:
    """When several detectors fire at once, answer the most serious one only.

    Stacking four overlays on someone is how an accountability tool becomes an
    app you uninstall on a Tuesday.
    """
    if not signals:
        return None
    return max(signals, key=lambda s: (SEVERITY_RANK[s.severity], s.ts))


def validate_response(intervention: Intervention, response: str, contract: Contract,
                      cfg: dict[str, Any]) -> tuple[bool, str]:
    """Did the user actually do what the overlay demanded?"""
    from .config import threshold
    text = (response or "").strip()
    if intervention.challenge is Challenge.NONE:
        return True, ""
    if intervention.challenge is Challenge.ACKNOWLEDGE:
        return bool(text), "type something"
    if intervention.challenge in (Challenge.TRANSCRIBE_INTENT,
                                  Challenge.WAIT_THEN_TRANSCRIBE):
        if _normalise(text) == _normalise(contract.intent):
            return True, ""
        return False, "type your declared intent, exactly as you wrote it"
    if intervention.challenge is Challenge.REFLECT:
        need = int(threshold(cfg, "reflect_min_chars"))
        if len(text) < need:
            return False, f"at least {need} characters — you are {need - len(text)} short"
        if len(set(text.lower().split())) < 4:
            return False, "that is keyboard mashing, not an answer"
        return True, ""
    return True, ""


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())
