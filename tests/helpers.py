"""Synthetic-day driver: feed the engine a script instead of a real machine."""

from __future__ import annotations

import random
from typing import Iterable, Sequence

from ana.config import load
from ana.engine import FocusEngine
from ana.models import Action, Contract, Sample, Stake, StakeKind

STEP = 2.0


def make_contract(intent: str = "refactor the billing parser in ana",
                  done: str = "the parser test suite passes green",
                  minutes: int = 60, grey_budget: float = 5.0) -> Contract:
    return Contract(intent=intent, done_criteria=done, planned_minutes=minutes,
                    signed_by="tester", grey_budget_minutes=grey_budget,
                    stake=Stake(StakeKind.DEBT, 30.0))


def make_engine(contract: Contract | None = None, cfg: dict | None = None,
                seed: int = 7, **threshold_overrides) -> FocusEngine:
    cfg = cfg or load()
    cfg["thresholds"].update(threshold_overrides)
    return FocusEngine(contract or make_contract(), cfg, rng=random.Random(seed))


def run(engine: FocusEngine, script: Sequence[tuple[float, str, str]] | Iterable,
        start: float = 1000.0, idle: float = 0.0,
        background: tuple[str, ...] = ()) -> list[Action]:
    """script: (seconds, app, title) or (seconds, app, title, idle_seconds)."""
    actions: list[Action] = []
    if engine.started_at is None:
        ts = start
        actions.extend(engine.start(ts))
    else:
        # continue from where the engine already is, so successive run() calls
        # compose into one timeline instead of jumping backwards
        ts = engine.last_ts or start
    for item in script:
        seconds, app, title = item[0], item[1], item[2]
        item_idle = item[3] if len(item) > 3 else idle
        steps = max(1, int(seconds / STEP))
        for i in range(steps):
            ts += STEP
            # idle_seconds grows the longer the stretch runs, like a real machine
            eff_idle = 0.0 if not item_idle else min(item_idle, (i + 1) * STEP)
            actions.extend(engine.tick(
                Sample(ts=ts, app=app, title=title, idle_seconds=eff_idle,
                       background=background)))
    return actions


def kinds(actions: Iterable[Action]) -> list[str]:
    return [a.kind.value for a in actions]


def signals(engine: FocusEngine) -> list[str]:
    return [s.kind for s in engine.signals]


def interventions(actions: Iterable[Action]):
    return [a.intervention for a in actions if a.intervention and a.intervention.level > 0]


def answer_for(intervention, contract) -> str:
    """What a compliant user would type for this challenge."""
    from ana.models import Challenge
    if intervention.challenge is Challenge.REFLECT:
        return ("I am avoiding the legacy-rows branch because I do not know how to "
                "handle the old schema and I think it might be a rewrite")
    if intervention.challenge in (Challenge.TRANSCRIBE_INTENT,
                                  Challenge.WAIT_THEN_TRANSCRIBE):
        return contract.intent
    return "ok"


def comply(engine, actions) -> list:
    """Answer every pending overlay the way the overlay demands. Returns the levels."""
    levels = []
    for iv in interventions(actions):
        levels.append(iv.level)
        ok, hint, _ = engine.resolve_intervention(
            answer_for(iv, engine.contract), waited_seconds=iv.wait_seconds)
        assert ok, f"compliant answer rejected at L{iv.level}: {hint}"
    return levels
