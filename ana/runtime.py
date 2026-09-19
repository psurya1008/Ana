"""Supervisor: the only module that runs a real clock against a real machine.

It polls a platform probe, feeds the (pure) engine, and carries out whatever
Actions come back — the overlay, the nudge, the lockout, the ledger entry. Swap
the UI or the probe and the rulebook does not change.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

from . import accountability, avoidance, config, scoring
from .detect import Probe, WindowInfo, get_probe
from .engine import FocusEngine, PROBE_QUESTION
from .ledger import Ledger, open_ledger
from .models import (
    Action,
    ActionKind,
    Contract,
    Intervention,
    Sample,
    SessionStatus,
    Signal,
)
from .storage import Store, open_store

MINUTE = 60.0


class UI(Protocol):
    """Everything the supervisor needs a user interface to be able to do."""

    def notice(self, text: str) -> None: ...
    def hud(self, snapshot: dict[str, Any]) -> None: ...
    def nudge(self, intervention: Intervention) -> None: ...
    def intervene(self, intervention: Intervention, contract: Contract,
                  validate) -> tuple[str, float, bool]:
        """Block until answered. Returns (response, seconds_waited, broke_glass)."""
        ...
    def probe(self, question: str, seconds: float) -> tuple[str, float]: ...
    def ask_evidence(self, contract: Contract) -> str: ...
    def closing(self, status: SessionStatus, score: float, lines: list[str]) -> None: ...


# ---------------------------------------------------------------------------
# Heartbeat — how Ana notices it was killed (E1)
# ---------------------------------------------------------------------------

def write_heartbeat(session_id: str, path: Optional[Path] = None) -> None:
    path = path or config.heartbeat_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session_id": session_id, "ts": time.time()}),
                    encoding="utf-8")


def clear_heartbeat(path: Optional[Path] = None) -> None:
    path = path or config.heartbeat_path()
    try:
        path.unlink()
    except OSError:
        pass


def read_heartbeat(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    path = path or config.heartbeat_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def recover_orphans(store: Store, ledger: Ledger, cfg: dict[str, Any],
                    now: Optional[float] = None) -> list[str]:
    """A session still open at launch was deserted. Say so, book it, move on.

    This is the answer to 'I'll just kill the tracker': the kill is what gets
    recorded, and it is worse than the distraction would have been.
    """
    now = now if now is not None else time.time()
    beat = read_heartbeat()
    notes: list[str] = []
    for row in store.active_sessions():
        contract = Contract.from_dict(json.loads(row["contract"]))
        last_seen = beat["ts"] if beat and beat.get("session_id") == row["id"] \
            else row["started_at"]
        gap = now - last_seen
        totals = scoring._totals_of(row)
        score = scoring.session_score(totals, contract.planned_minutes,
                                      status=SessionStatus.DESERTED)
        store.finish_session(row["id"], SessionStatus.DESERTED, totals, score,
                             evidence="", ended_at=last_seen,
                             note="tracker stopped while the session was open")
        signal = avoidance.desertion_signal(
            gap, float(config.threshold(cfg, "heartbeat_gap_seconds")))
        ledger.append("session_deserted", {
            "session_id": row["id"], "intent": contract.intent,
            "gap_seconds": round(gap, 1),
            "detail": signal.detail if signal else "",
            "summary": f'DESERTED: "{contract.intent}" — tracker stopped for '
                       f"{gap / MINUTE:.0f} min"})
        accountability.apply_session_outcome(store, cfg, row["id"],
                                             SessionStatus.DESERTED, totals, contract, now)
        notes.append(f'"{contract.intent}" was left open — recorded as desertion '
                     f"({gap / MINUTE:.0f} min unaccounted)")
    clear_heartbeat()
    return notes


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

@dataclass
class SessionResult:
    session_id: str
    status: SessionStatus
    score: float
    totals: Any
    charged: list[str]
    signals: list[Signal]


class Supervisor:
    def __init__(self, ui: UI, cfg: Optional[dict[str, Any]] = None,
                 store: Optional[Store] = None, ledger: Optional[Ledger] = None,
                 probe: Optional[Probe] = None, clock=time.time,
                 sleep=time.sleep):
        config.ensure_home()
        self.cfg = cfg or config.load()
        self.store = store or open_store()
        self.ledger = ledger or open_ledger()
        self.probe = probe or get_probe()
        self.ui = ui
        self.clock = clock
        self.sleep = sleep
        self.engine: Optional[FocusEngine] = None
        self.session_id: Optional[str] = None
        self.contract: Optional[Contract] = None
        self._stop = False
        self._lockout_until = 0.0

    # -- startup checks ----------------------------------------------------
    def preflight(self) -> list[str]:
        notes: list[str] = []
        verification = self.ledger.verify()
        if not verification.ok:
            notes.append(verification.describe())
            notes.append("Until you write a signed explanation (`ana explain`), this "
                         "sits at the top of every report.")
        notes.extend(recover_orphans(self.store, self.ledger, self.cfg, self.clock()))
        notes.extend(p for p in self.store.reconcile(self.ledger.entries()))
        for signal in accountability.schedule_signals(self.store, self.cfg, self.clock()):
            notes.append(signal.detail)
            self.ledger.append("schedule_miss", {"summary": signal.detail,
                                                 "taxonomy": signal.taxonomy})
        return notes

    # -- the session -------------------------------------------------------
    def run_session(self, contract: Contract, project_path: Optional[str] = None,
                    max_seconds: Optional[float] = None) -> SessionResult:
        self.contract = contract
        started = self.clock()
        self.session_id = self.store.create_session(contract, started)
        reflections = [r["response"] for r in self.store.recent_reflections(3)]
        self.engine = FocusEngine(contract, self.cfg, recent_reflections=reflections)
        self.engine.start(started)

        self.ledger.append("session_start", {
            "session_id": self.session_id,
            "contract": contract.to_dict(),
            "summary": f'SIGNED: "{contract.intent}" for {contract.planned_minutes} min '
                       f"— stake: {contract.stake.describe()}"})

        interval = float(config.threshold(self.cfg, "sample_seconds"))
        planned = contract.planned_minutes * MINUTE
        limit = max_seconds if max_seconds is not None else planned
        self._stop = False
        broke_glass = False

        try:
            while not self._stop:
                self.sleep(interval)
                now = self.clock()
                sample = self._take_sample(now)
                actions = self.engine.tick(sample)
                # _execute blocks for as long as an overlay or probe is up.
                # Hand that back to the engine so it is not read as a gap.
                before_ui = self.clock()
                broke_glass = self._execute(actions, sample) or broke_glass
                self.engine.excuse(self.clock() - before_ui)
                write_heartbeat(self.session_id)
                self.ui.hud(self.engine.snapshot())
                if self.engine.elapsed >= limit:
                    break
        except KeyboardInterrupt:
            # Ctrl-C during a session is a desertion, not a clean exit.
            self.ui.notice("\ninterrupted — recorded as a desertion")
            broke_glass = True

        return self._finish(project_path, deserted=broke_glass, started=started)

    def _take_sample(self, now: float) -> Sample:
        try:
            info: WindowInfo = self.probe.window()
            idle = self.probe.idle_seconds()
            background = self.probe.background_processes()
        except Exception:  # noqa: BLE001 - never let a probe crash a session
            info, idle, background = WindowInfo("unknown", ""), 0.0, ()
        title = info.title
        if self.engine is not None:
            title = self.engine.classifier.redact(title)
        return Sample(ts=now, app=info.app or "unknown", title=title,
                      idle_seconds=idle, background=background)

    def _execute(self, actions: list[Action], sample: Sample) -> bool:
        """Carry out the engine's orders. Returns True if break-glass was used."""
        assert self.engine is not None
        broke = False
        for action in actions:
            if action.kind is ActionKind.LOG:
                self._log(action, sample)
            elif action.kind is ActionKind.NUDGE:
                if action.intervention:
                    self.ui.nudge(action.intervention)
            elif action.kind is ActionKind.PAUSE_CLOCK:
                self.ui.notice("clock paused — no input detected")
            elif action.kind is ActionKind.RESUME_CLOCK:
                self.ui.notice("welcome back — clock running")
            elif action.kind is ActionKind.RAISE_WORK_WINDOW:
                try:
                    self.probe.raise_window(action.payload.get("app", ""),
                                            action.payload.get("title", ""))
                except Exception:  # noqa: BLE001
                    pass
            elif action.kind is ActionKind.LOCKOUT:
                minutes = float(action.payload.get("minutes", 0))
                self._lockout_until = self.clock() + minutes * MINUTE
                self.ledger.append("lockout", {
                    "minutes": minutes,
                    "summary": f"recreation lockout for {minutes:.0f} min"})
            elif action.kind is ActionKind.PROBE:
                self._run_probe(action, sample)
            elif action.kind is ActionKind.INTERVENE:
                broke = self._run_intervention(action, sample) or broke
            elif action.kind is ActionKind.END_SESSION:
                self._stop = True
        return broke

    def _log(self, action: Action, sample: Sample) -> None:
        if action.signal is not None:
            self.store.add_event(self.session_id, action.signal.kind, sample.app,
                                 sample.title, "", action.signal.to_dict(), sample.ts)
            self.ledger.append("signal", {"session_id": self.session_id,
                                          "summary": action.signal.detail,
                                          **action.signal.to_dict()})
        elif action.message:
            self.store.add_event(self.session_id, "note", sample.app, sample.title,
                                 "", {"message": action.message}, sample.ts)

    def _run_probe(self, action: Action, sample: Sample) -> None:
        assert self.engine is not None
        window = action.payload.get("window", "")
        seconds = float(action.payload.get("seconds", 15))
        answer, took = self.ui.probe(action.message or PROBE_QUESTION, seconds)
        passed = self.engine.answer_probe(answer, action.ts + took, window)
        self.store.add_probe(self.session_id, PROBE_QUESTION, answer, window, passed,
                             ts=action.ts)
        self.ledger.append("probe", {
            "session_id": self.session_id, "answer": answer, "window": window,
            "passed": passed,
            "summary": (f'probe: you said "{answer}" while looking at {window}'
                        if passed else
                        f"probe unanswered while looking at {window}")})

    def _run_intervention(self, action: Action, sample: Sample) -> bool:
        assert self.engine is not None and self.contract is not None
        iv = action.intervention
        if iv is None:
            return False
        signal_kind = action.signal.kind if action.signal else iv.signal_kind

        def validate(text: str, waited: float):
            from .penalties import validate_response
            if iv.wait_seconds and waited < iv.wait_seconds:
                return False, f"wait {iv.wait_seconds - int(waited)}s more"
            return validate_response(iv, text, self.contract, self.cfg)

        response, waited, broke = self.ui.intervene(iv, self.contract, validate)

        if broke:
            self.engine.break_glass(self.clock())
            self.ledger.append("break_glass", {
                "session_id": self.session_id, "level": iv.level,
                "summary": f"BREAK-GLASS at L{iv.level} ({signal_kind}) — "
                           f"overlay dismissed without answering"})
            self.store.add_intervention(self.session_id, iv.level, signal_kind,
                                        iv.challenge.value, iv.body, "", False, waited,
                                        ts=action.ts)
            self._stop = True
            return True

        ok, hint, _ = self.engine.resolve_intervention(response, waited, self.clock())
        self.store.add_intervention(self.session_id, iv.level, signal_kind,
                                    iv.challenge.value, iv.body, response, ok, waited,
                                    ts=action.ts)
        if iv.debt_seconds:
            self.store.add_debt(iv.debt_seconds, f"L{iv.level} {signal_kind}",
                                self.session_id, action.ts)
        self.ledger.append("intervention", {
            "session_id": self.session_id, "level": iv.level, "signal": signal_kind,
            "challenge": iv.challenge.value, "response": response,
            "consequences": iv.consequences,
            "summary": f"L{iv.level} {signal_kind}: {iv.body.splitlines()[0]}"
                       + (f' — you wrote: "{response}"' if response else "")})
        return False

    # -- close -------------------------------------------------------------
    def _finish(self, project_path: Optional[str], deserted: bool,
                started: float) -> SessionResult:
        assert self.engine is not None and self.contract is not None
        now = self.clock()
        statement = "" if deserted else self.ui.ask_evidence(self.contract)
        evidence = accountability.collect_evidence(statement, project_path, started)
        status, score, extra = self.engine.close(now, evidence.statement, deserted)

        self.store.finish_session(self.session_id, status, self.engine.totals, score,
                                  evidence.render(), ended_at=now)
        charged = accountability.apply_session_outcome(
            self.store, self.cfg, self.session_id, status, self.engine.totals,
            self.contract, now)
        clear_heartbeat()

        self.ledger.append("session_end", {
            "session_id": self.session_id,
            "status": status.value,
            "score": score,
            "totals": self.engine.totals.to_dict(),
            "evidence": evidence.render(),
            "charged": charged,
            "summary": f'{status.value.upper()}: "{self.contract.intent}" — '
                       f"score {score}, focus "
                       f"{self.engine.totals.focus_seconds / MINUTE:.0f} min"
                       + (f", evidence: {evidence.statement}" if evidence.statement
                          else ", NO EVIDENCE")})

        lines = [f"focus {self.engine.totals.focus_seconds / MINUTE:.0f} min · "
                 f"grey {self.engine.totals.grey_seconds / MINUTE:.0f} · "
                 f"blocked {self.engine.totals.block_seconds / MINUTE:.0f} · "
                 f"idle {self.engine.totals.idle_seconds / MINUTE:.0f}"]
        lines += [f"charged: {c}" for c in charged]
        lines += [s.detail for s in extra]
        self.ui.closing(status, score, lines)

        return SessionResult(self.session_id, status, score, self.engine.totals,
                             charged, list(self.engine.signals))

    def stop(self) -> None:
        self._stop = True
