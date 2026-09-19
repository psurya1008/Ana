"""SQLite store for sessions, events, interventions, probes, commitments and debt.

The ledger is the tamper-evident record of *what happened*; this database is the
queryable index used for scoring and reports. If the two ever disagree, the
ledger wins — see :meth:`Store.reconcile`.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import Contract, SessionStatus, SessionTotals, new_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    ended_at REAL,
    day TEXT NOT NULL,
    contract TEXT NOT NULL,
    status TEXT NOT NULL,
    totals TEXT NOT NULL DEFAULT '{}',
    evidence TEXT DEFAULT '',
    score REAL,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    app TEXT DEFAULT '',
    title TEXT DEFAULT '',
    category TEXT DEFAULT '',
    detail TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    ts REAL NOT NULL,
    level INTEGER NOT NULL,
    signal_kind TEXT DEFAULT '',
    challenge TEXT DEFAULT '',
    prompt TEXT DEFAULT '',
    response TEXT DEFAULT '',
    honored INTEGER DEFAULT 1,
    waited_seconds REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    ts REAL NOT NULL,
    question TEXT,
    answer TEXT DEFAULT '',
    window_at_probe TEXT DEFAULT '',
    passed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS commitments (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    day TEXT NOT NULL,
    start_hhmm TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    intent TEXT NOT NULL,
    stake TEXT DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    snoozes INTEGER DEFAULT 0,
    session_id TEXT,
    settled_at REAL
);
CREATE TABLE IF NOT EXISTS debt (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    seconds REAL NOT NULL,
    reason TEXT DEFAULT '',
    session_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_day ON sessions(day);
"""


def day_key(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


class Store:
    def __init__(self, path: Path | str = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- sessions ---------------------------------------------------------
    def create_session(self, contract: Contract, started_at: float | None = None) -> str:
        sid = new_id("ses")
        started = started_at if started_at is not None else time.time()
        self.db.execute(
            "INSERT INTO sessions (id, started_at, day, contract, status, totals)"
            " VALUES (?,?,?,?,?,?)",
            (sid, started, day_key(started), json.dumps(contract.to_dict()),
             SessionStatus.ACTIVE.value, "{}"),
        )
        self.db.commit()
        return sid

    def finish_session(
        self,
        sid: str,
        status: SessionStatus,
        totals: SessionTotals,
        score: float,
        evidence: str = "",
        ended_at: float | None = None,
        note: str = "",
    ) -> None:
        self.db.execute(
            "UPDATE sessions SET ended_at=?, status=?, totals=?, score=?, evidence=?, note=?"
            " WHERE id=?",
            (ended_at if ended_at is not None else time.time(), status.value,
             json.dumps(totals.to_dict()), score, evidence, note, sid),
        )
        self.db.commit()

    def get_session(self, sid: str) -> Optional[sqlite3.Row]:
        cur = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,))
        return cur.fetchone()

    def active_sessions(self) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM sessions WHERE status=? ORDER BY started_at",
            (SessionStatus.ACTIVE.value,)))

    def sessions_for_day(self, day: str) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM sessions WHERE day=? ORDER BY started_at", (day,)))

    def sessions_since(self, ts: float) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM sessions WHERE started_at>=? ORDER BY started_at", (ts,)))

    def days_with_sessions(self, limit: int = 60) -> list[str]:
        return [r["day"] for r in self.db.execute(
            "SELECT DISTINCT day FROM sessions ORDER BY day DESC LIMIT ?", (limit,))]

    # -- events -----------------------------------------------------------
    def add_event(self, session_id: str | None, kind: str, app: str = "", title: str = "",
                  category: str = "", detail: dict[str, Any] | None = None,
                  ts: float | None = None) -> None:
        self.db.execute(
            "INSERT INTO events (session_id, ts, kind, app, title, category, detail)"
            " VALUES (?,?,?,?,?,?,?)",
            (session_id, ts if ts is not None else time.time(), kind, app, title,
             category, json.dumps(detail or {})),
        )
        self.db.commit()

    def events_for(self, session_id: str, kind: str | None = None) -> list[sqlite3.Row]:
        if kind:
            return list(self.db.execute(
                "SELECT * FROM events WHERE session_id=? AND kind=? ORDER BY ts",
                (session_id, kind)))
        return list(self.db.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY ts", (session_id,)))

    # -- interventions ----------------------------------------------------
    def add_intervention(self, session_id: str | None, level: int, signal_kind: str,
                         challenge: str, prompt: str, response: str = "",
                         honored: bool = True, waited: float = 0.0,
                         ts: float | None = None) -> None:
        self.db.execute(
            "INSERT INTO interventions (session_id, ts, level, signal_kind, challenge,"
            " prompt, response, honored, waited_seconds) VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, ts if ts is not None else time.time(), level, signal_kind,
             challenge, prompt, response, int(honored), waited),
        )
        self.db.commit()

    def interventions_for(self, session_id: str) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM interventions WHERE session_id=? ORDER BY ts", (session_id,)))

    def recent_reflections(self, limit: int = 3) -> list[sqlite3.Row]:
        """The last few answers to 'what are you avoiding?' — shown back to you at L3."""
        return list(self.db.execute(
            "SELECT * FROM interventions WHERE challenge='reflect' AND response<>''"
            " ORDER BY ts DESC LIMIT ?", (limit,)))

    # -- probes -----------------------------------------------------------
    def add_probe(self, session_id: str | None, question: str, answer: str,
                  window_at_probe: str, passed: bool, ts: float | None = None) -> None:
        self.db.execute(
            "INSERT INTO probes (session_id, ts, question, answer, window_at_probe, passed)"
            " VALUES (?,?,?,?,?,?)",
            (session_id, ts if ts is not None else time.time(), question, answer,
             window_at_probe, int(passed)),
        )
        self.db.commit()

    def probes_for(self, session_id: str) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM probes WHERE session_id=? ORDER BY ts", (session_id,)))

    # -- commitments (scheduled blocks) -----------------------------------
    def add_commitment(self, day: str, start_hhmm: str, minutes: int, intent: str,
                       stake: dict[str, Any] | None = None) -> str:
        cid = new_id("cmt")
        self.db.execute(
            "INSERT INTO commitments (id, created_at, day, start_hhmm, minutes, intent, stake)"
            " VALUES (?,?,?,?,?,?,?)",
            (cid, time.time(), day, start_hhmm, minutes, intent, json.dumps(stake or {})),
        )
        self.db.commit()
        return cid

    def commitments_for_day(self, day: str) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM commitments WHERE day=? ORDER BY start_hhmm", (day,)))

    def pending_commitments(self, day: str) -> list[sqlite3.Row]:
        return [r for r in self.commitments_for_day(day) if r["status"] == "pending"]

    def settle_commitment(self, cid: str, status: str, session_id: str | None = None) -> None:
        self.db.execute(
            "UPDATE commitments SET status=?, settled_at=?, session_id=? WHERE id=?",
            (status, time.time(), session_id, cid))
        self.db.commit()

    def snooze_commitment(self, cid: str) -> int:
        self.db.execute("UPDATE commitments SET snoozes=snoozes+1 WHERE id=?", (cid,))
        self.db.commit()
        row = self.db.execute("SELECT snoozes FROM commitments WHERE id=?", (cid,)).fetchone()
        return row["snoozes"] if row else 0

    # -- debt -------------------------------------------------------------
    def add_debt(self, seconds: float, reason: str, session_id: str | None = None,
                 ts: float | None = None) -> None:
        if not seconds:
            return
        self.db.execute(
            "INSERT INTO debt (ts, seconds, reason, session_id) VALUES (?,?,?,?)",
            (ts if ts is not None else time.time(), seconds, reason, session_id))
        self.db.commit()

    def debt_seconds(self) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(seconds),0) AS s FROM debt").fetchone()
        return float(row["s"])

    def debt_entries(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM debt ORDER BY ts DESC LIMIT ?", (limit,)))

    # -- maintenance ------------------------------------------------------
    def reconcile(self, ledger_entries: Iterable[Any]) -> list[str]:
        """Compare the DB against the ledger; the ledger is the source of truth.

        Catches the crude version of E2: deleting rows from ana.db and hoping the
        report forgets. Any session the ledger opened but the DB has lost is
        reported here and shows up in the daily report as a discrepancy.
        """
        problems: list[str] = []
        ledger_sessions = {
            e.payload.get("session_id")
            for e in ledger_entries
            if e.kind == "session_start" and e.payload.get("session_id")
        }
        known = {r["id"] for r in self.db.execute("SELECT id FROM sessions")}
        for missing in sorted(ledger_sessions - known):
            problems.append(f"session {missing} exists in the ledger but not in the database")
        return problems


def open_store(path: Path | None = None) -> Store:
    from . import config
    return Store(path or config.db_path())
