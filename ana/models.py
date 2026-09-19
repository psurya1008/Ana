"""Core value types. Everything here is plain data — no behaviour that needs a clock."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class Category(str, Enum):
    """What a window counts as, for the session currently running."""

    FOCUS = "focus"      # on the declared task
    NEUTRAL = "neutral"  # OS chrome; free, untimed
    GREY = "grey"        # allowed but budgeted (mail, chat, browser)
    BLOCK = "block"      # known distraction; costs from the first second


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_RANK = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


class SessionStatus(str, Enum):
    ACTIVE = "active"
    COMPLETE = "complete"        # ran its time, evidence supplied
    UNVERIFIED = "unverified"    # ran its time, no evidence -> does not count
    BROKEN = "broken"            # escalated to L3+
    MICRO = "micro"              # quit far short of the declared duration
    DESERTED = "deserted"        # tracker killed / break-glass / heartbeat gap


class StakeKind(str, Enum):
    RECREATION = "recreation"
    DEBT = "debt"
    STREAK = "streak"
    PLEDGE = "pledge"
    REPORT = "report"


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class Stake:
    """What you forfeit if you break the contract. Ana tracks; Ana never executes."""

    kind: StakeKind = StakeKind.DEBT
    amount: float = 0.0          # minutes, for RECREATION/DEBT
    detail: str = ""             # e.g. "£20 to Dan" for PLEDGE

    def describe(self) -> str:
        if self.kind is StakeKind.PLEDGE:
            return f"pledge: {self.detail or 'unspecified'}"
        if self.kind in (StakeKind.RECREATION, StakeKind.DEBT):
            return f"{self.kind.value}: {self.amount:g} min"
        if self.kind is StakeKind.STREAK:
            return "streak reset"
        return f"reported: {self.detail or 'in the daily report'}"


@dataclass
class Contract:
    """Signed before the timer starts. Immutable for the life of the session."""

    intent: str
    done_criteria: str
    planned_minutes: int
    signed_by: str = ""
    grey_budget_minutes: float = 5.0
    stake: Stake = field(default_factory=Stake)
    signed_at: float = field(default_factory=now)
    keywords: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["stake"]["kind"] = self.stake.kind.value
        d["keywords"] = list(self.keywords)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Contract":
        d = dict(d)
        stake = d.pop("stake", {}) or {}
        return cls(
            stake=Stake(
                kind=StakeKind(stake.get("kind", "debt")),
                amount=float(stake.get("amount", 0.0)),
                detail=stake.get("detail", ""),
            ),
            keywords=tuple(d.pop("keywords", []) or ()),
            **d,
        )


@dataclass(frozen=True)
class Sample:
    """One observation of the machine. The only input the engine ever gets."""

    ts: float
    app: str
    title: str
    idle_seconds: float = 0.0
    background: tuple[str, ...] = ()  # notable background processes (media, games)


@dataclass(frozen=True)
class Signal:
    """A detector's finding. Detectors emit these; penalties.py turns them into teeth."""

    kind: str
    severity: Severity
    detail: str
    taxonomy: str = ""     # the row in docs/PLAN.md §1 this corresponds to
    ts: float = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


class ActionKind(str, Enum):
    NUDGE = "nudge"               # HUD colour change + chime
    INTERVENE = "intervene"       # full-screen overlay
    PROBE = "probe"               # "what are you doing right now?"
    RAISE_WORK_WINDOW = "raise"   # pull the work window back to the front
    PAUSE_CLOCK = "pause_clock"   # idle: stop crediting time
    RESUME_CLOCK = "resume_clock"
    LOCKOUT = "lockout"
    END_SESSION = "end_session"
    LOG = "log"


class Challenge(str, Enum):
    ACKNOWLEDGE = "acknowledge"
    TRANSCRIBE_INTENT = "transcribe_intent"
    WAIT_THEN_TRANSCRIBE = "wait_then_transcribe"
    REFLECT = "reflect"           # "what are you avoiding?", min length enforced
    NONE = "none"


@dataclass
class Intervention:
    """What the overlay will demand. Produced by penalties.py."""

    level: int
    challenge: Challenge
    wait_seconds: int
    headline: str
    body: str
    consequences: list[str] = field(default_factory=list)
    debt_seconds: float = 0.0
    reset_streak: bool = False
    lockout_minutes: float = 0.0
    signal_kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["challenge"] = self.challenge.value
        return d


@dataclass
class Action:
    """What the engine tells the runtime to do. The runtime is dumb; this is the order."""

    kind: ActionKind
    ts: float = field(default_factory=now)
    intervention: Optional[Intervention] = None
    signal: Optional[Signal] = None
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionTotals:
    """Running clock for a session. Idle time is never credited as focus."""

    focus_seconds: float = 0.0
    neutral_seconds: float = 0.0
    grey_seconds: float = 0.0
    block_seconds: float = 0.0
    idle_seconds: float = 0.0
    interrupted_seconds: float = 0.0   # time spent answering Ana's own prompts
    switches: int = 0
    interventions: int = 0
    probes_passed: int = 0
    probes_failed: int = 0

    @property
    def tracked_seconds(self) -> float:
        return (
            self.focus_seconds
            + self.neutral_seconds
            + self.grey_seconds
            + self.block_seconds
            + self.idle_seconds
            + self.interrupted_seconds
        )

    @property
    def distracted_seconds(self) -> float:
        return self.block_seconds + self.idle_seconds

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
