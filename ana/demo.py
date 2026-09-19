"""`ana demo` — a whole session against a scripted machine, in a few seconds.

Two reasons this exists. One: nobody should have to waste a real hour to find
out what the app does when it catches you. Two: it is the end-to-end test —
real Supervisor, real store, real ledger, real reports, fake clock and fake
machine.

It writes to ``~/.ana/demo`` and never touches your real record.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import config, report
from .accountability import sign_contract, standing
from .detect import WindowInfo
from .ledger import Ledger
from .models import Contract, Intervention, SessionStatus, Stake, StakeKind
from .runtime import Supervisor
from .storage import Store

MINUTE = 60.0

# (virtual seconds, app, title, idle_seconds)
SCENARIOS: dict[str, list[tuple[float, str, str, float]]] = {
    "distracted": [
        (6 * MINUTE, "code", "parser.py — billing — VS Code", 0),
        (3 * MINUTE, "chrome", "YouTube — lofi beats to focus to", 0),
        (4 * MINUTE, "code", "parser.py — billing — VS Code", 0),
        (3 * MINUTE, "chrome", "Reddit — r/programming", 0),
        (4 * MINUTE, "code", "parser.py — billing — VS Code", 0),
        (4 * MINUTE, "chrome", "YouTube — just one more", 0),
        (6 * MINUTE, "code", "parser.py — billing — VS Code", 0),
    ],
    "avoidant": [
        (9 * MINUTE, "notion", "Q3 plan — Notion", 0),                   # B5 prep loop
        (14 * MINUTE, "chrome", "wikipedia — history of parsing", 0),    # B3 spiral
        (4 * MINUTE, "code", "unrelated_admin.py — VS Code", 0),         # B4 task hop
        (12 * MINUTE, "code", "unrelated_admin.py — VS Code", 0),
        (6 * MINUTE, "code", "parser.py — billing — VS Code", 300),      # C1 absent
    ],
    "honest": [
        (12 * MINUTE, "code", "parser.py — billing — VS Code", 0),
        (2 * MINUTE, "terminal", "pytest — billing parser", 0),
        (10 * MINUTE, "code", "parser_test.py — billing — VS Code", 0),
        (2 * MINUTE, "chrome", "python docs — re module", 0),
        (4 * MINUTE, "code", "parser.py — billing — VS Code", 0),
    ],
}


class FakeClock:
    """Virtual time. `sleep` advances it by a whole sampling interval."""

    def __init__(self, start: Optional[float] = None, step: float = 30.0):
        self.now = start if start is not None else time.time()
        self.step = step

    def __call__(self) -> float:
        return self.now

    def sleep(self, _seconds: float) -> None:
        self.now += self.step


class TimelineProbe:
    """A machine that does exactly what the scenario says."""

    name = "timeline"
    note = "scripted machine (demo)"
    available = True

    def __init__(self, clock: FakeClock, timeline: list[tuple[float, str, str, float]],
                 background: tuple[str, ...] = ()):
        self.clock = clock
        self.timeline = timeline
        self.background = background
        self.start = clock()
        self.raised: list[tuple[str, str]] = []

    def _at_now(self) -> tuple[str, str, float]:
        elapsed = self.clock() - self.start
        acc = 0.0
        for duration, app, title, idle in self.timeline:
            acc += duration
            if elapsed < acc:
                return app, title, idle
        _, app, title, idle = self.timeline[-1]
        return app, title, idle

    def window(self) -> WindowInfo:
        app, title, _ = self._at_now()
        return WindowInfo(app, title)

    def idle_seconds(self) -> float:
        return self._at_now()[2]

    def background_processes(self) -> tuple[str, ...]:
        return self.background

    def raise_window(self, app: str, title: str) -> bool:
        self.raised.append((app, title))
        return True


class AutoUI:
    """A compliant user, for unattended runs. Answers everything correctly."""

    def __init__(self, cfg: dict[str, Any], verbose: bool = True):
        self.cfg = cfg
        self.verbose = verbose
        self.log: list[str] = []
        self.interventions: list[Intervention] = []

    def _say(self, text: str) -> None:
        self.log.append(text)
        if self.verbose:
            print(text)

    def notice(self, text: str) -> None:
        self._say(f"  · {text}")

    def hud(self, snapshot: dict[str, Any]) -> None:
        pass

    def nudge(self, intervention: Intervention) -> None:
        self._say(f"  ▲ nudge: {intervention.body.splitlines()[0]}")

    def intervene(self, intervention: Intervention, contract: Contract,
                  validate: Callable[[str, float], tuple[bool, str]]
                  ) -> tuple[str, float, bool]:
        self.interventions.append(intervention)
        self._say("")
        self._say(f"  ┏━ L{intervention.level}  {intervention.headline}")
        for line in intervention.body.splitlines():
            self._say(f"  ┃ {line}")
        for c in intervention.consequences:
            self._say(f"  ┃ costs you: {c}")
        answer = (contract.intent if intervention.challenge.value.endswith("transcribe")
                  or intervention.challenge.value == "transcribe_intent"
                  else "I am avoiding the legacy-rows branch because I do not know how "
                       "the old schema maps and I suspect it is a rewrite")
        waited = float(intervention.wait_seconds)
        ok, hint = validate(answer, waited)
        self._say(f"  ┃ > {answer}")
        self._say(f"  ┗━ {'accepted' if ok else 'rejected: ' + hint}")
        return answer, waited, False

    def probe(self, question: str, seconds: float) -> tuple[str, float]:
        self._say(f"  ? {question}")
        self._say("  > writing the legacy-row branch of the parser")
        return "writing the legacy-row branch of the parser", 4.0

    def ask_evidence(self, contract: Contract) -> str:
        return "parser.py rewritten, 14 tests green"

    def closing(self, status: SessionStatus, score: float, lines: list[str]) -> None:
        self._say("")
        self._say(f"  {status.value.upper()} · score {score}")
        for line in lines:
            self._say(f"    {line}")


def demo_paths() -> tuple[Path, Path]:
    home = config.ensure_home() / "demo"
    home.mkdir(parents=True, exist_ok=True)
    return home / "ana.db", home / "ledger.jsonl"


def run_demo(args: Any) -> int:
    scenario = getattr(args, "scenario", "distracted")
    auto = getattr(args, "auto", False)
    timeline = SCENARIOS[scenario]

    db_path, ledger_path = demo_paths()
    for p in (db_path, ledger_path):
        if p.exists():
            p.unlink()

    cfg = config.load()
    cfg["user"] = cfg.get("user") or "demo"
    # Virtual time runs in 10-second steps, and the engine is told that *is* the
    # sampling interval — otherwise every step looks like a machine that slept
    # and, quite correctly, earns no credit.
    step = 10.0
    cfg["thresholds"]["sample_seconds"] = step
    clock = FakeClock(step=step)
    store, ledger = Store(db_path), Ledger(ledger_path)
    probe = TimelineProbe(clock, timeline,
                          background=("spotify.exe",) if scenario == "distracted" else ())

    if auto:
        ui: Any = AutoUI(cfg)
    else:
        from .ui.console import ConsoleUI
        ui = ConsoleUI(cfg)

    contract = sign_contract(
        intent="refactor the billing parser in ana",
        done_criteria="the parser test suite passes green",
        planned_minutes=30, signed_by=cfg["user"], cfg=cfg,
        grey_budget_minutes=3.0, stake=Stake(StakeKind.DEBT, 20.0))

    print(f"\n  demo — scenario '{scenario}', virtual time, "
          f"writing to {db_path.parent}")
    print(f'  SIGNED: "{contract.intent}" for {contract.planned_minutes} min '
          f"— stake {contract.stake.describe()}\n")

    sup = Supervisor(ui, cfg, store, ledger, probe,
                     clock=clock, sleep=clock.sleep)
    total = sum(step[0] for step in timeline)
    sup.run_session(contract, max_seconds=total)

    print("\n" + "─" * 64)
    print(report.daily_report(store, cfg, ledger,
                              day=time.strftime("%Y-%m-%d", time.localtime(clock()))))
    print("─" * 64)
    print(f"\n  {ledger.verify().describe()}")
    print(f"  standing: {standing(store, cfg, ledger, clock()).headline()}")
    return 0
