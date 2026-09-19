"""Tamper-evident append-only ledger.

Every consequential event is one JSON line, hash-chained to the line before it:

    hash_n = sha256(hash_{n-1} + canonical_json(payload_n))

You can still edit the file — it's your machine, and an app that pretends
otherwise is lying to you. What you cannot do is edit it *quietly*: the chain
stops verifying, and ``INTEGRITY BREACH`` then rides on the dashboard and every
report until you write a signed explanation (which is itself a ledger entry).

That is the whole design goal: make dishonesty deliberate and on the record.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

GENESIS = "0" * 64


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _digest(prev_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + _canonical(payload)).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Entry:
    seq: int
    ts: float
    kind: str
    payload: dict[str, Any]
    prev: str
    hash: str

    @property
    def summary(self) -> str:
        return self.payload.get("summary") or self.kind


@dataclass(frozen=True)
class Verification:
    ok: bool
    entries: int
    first_bad_seq: int | None = None
    reason: str = ""

    def describe(self) -> str:
        if self.ok:
            return f"ledger intact — {self.entries} entries verified"
        return f"INTEGRITY BREACH at entry #{self.first_bad_seq}: {self.reason}"


class Ledger:
    """Append-only, hash-chained event log."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # append() reads the chain head and then writes; two threads doing that
        # at once would fork the chain and break verification. The tk UI runs
        # the session on a worker while the dashboard reads on the main thread,
        # so this is a real race, not a theoretical one.
        self._lock = threading.RLock()

    # -- reading ----------------------------------------------------------
    def __iter__(self) -> Iterator[Entry]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield Entry(
                    seq=d.get("seq", -1),
                    ts=d.get("ts", 0.0),
                    kind=d.get("kind", "?"),
                    payload=d.get("payload", {}),
                    prev=d.get("prev", ""),
                    hash=d.get("hash", ""),
                )

    def entries(self) -> list[Entry]:
        return list(self)

    def tail(self, n: int = 20) -> list[Entry]:
        return self.entries()[-n:]

    def of_kind(self, *kinds: str) -> list[Entry]:
        wanted = set(kinds)
        return [e for e in self if e.kind in wanted]

    def head(self) -> tuple[int, str]:
        """(next sequence number, hash of the last entry)."""
        seq, last = 0, GENESIS
        for entry in self:
            seq, last = entry.seq + 1, entry.hash
        return seq, last

    # -- writing ----------------------------------------------------------
    def append(self, kind: str, payload: dict[str, Any] | None = None) -> Entry:
        with self._lock:
            return self._append_locked(kind, payload)

    def _append_locked(self, kind: str, payload: dict[str, Any] | None = None) -> Entry:
        payload = dict(payload or {})
        seq, prev = self.head()
        record = {
            "seq": seq,
            "ts": time.time(),
            "kind": kind,
            "payload": payload,
            "prev": prev,
        }
        record["hash"] = _digest(prev, {k: record[k] for k in ("seq", "ts", "kind", "payload")})
        # append + flush + fsync: a crash mid-session must not lose the entry that
        # says a session was running (that is exactly the entry a deserter wants gone).
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return Entry(seq, record["ts"], kind, payload, prev, record["hash"])

    # -- integrity --------------------------------------------------------
    def verify(self) -> Verification:
        prev, count = GENESIS, 0
        for expected_seq, entry in enumerate(self):
            body = {"seq": entry.seq, "ts": entry.ts, "kind": entry.kind,
                    "payload": entry.payload}
            if entry.seq != expected_seq:
                return Verification(False, count, entry.seq,
                                    f"sequence jumped (expected #{expected_seq}) — "
                                    "an entry was deleted")
            if entry.prev != prev:
                return Verification(False, count, entry.seq,
                                    "chain link does not match the previous entry")
            if _digest(prev, body) != entry.hash:
                return Verification(False, count, entry.seq,
                                    "contents were altered after the fact")
            prev = entry.hash
            count += 1
        return Verification(True, count)


def open_ledger(path: Path | None = None) -> Ledger:
    from . import config
    return Ledger(path or config.ledger_path())
