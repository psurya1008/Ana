"""Regression tests for the tk threading model.

The tk UI owns the main thread and runs the session on a worker, so the Store
and Ledger are created on one thread and used from another. Shipped once
without this and it crashed on the first `ana start --ui tk`:

    sqlite3.ProgrammingError: SQLite objects created in a thread can only be
    used in that same thread.

The existing integration tests all run the single-threaded console path, which
is exactly why they missed it.
"""

import tempfile
import threading
import unittest
from pathlib import Path

from ana import config
from ana.accountability import sign_contract, standing
from ana.demo import SCENARIOS, AutoUI, FakeClock, TimelineProbe
from ana.ledger import Ledger
from ana.models import SessionStatus
from ana.runtime import Supervisor
from ana.storage import Store


def _run_on_worker(fn):
    """Call fn() on another thread; re-raise whatever it raised, here."""
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=target)
    t.start()
    t.join(30)
    if t.is_alive():
        raise AssertionError("worker thread hung")
    if "error" in box:
        raise box["error"]
    return box.get("value")


class TestStoreAcrossThreads(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cfg = config.load()
        self.store = Store(Path(self.dir.name) / "ana.db")   # main thread
        self.contract = sign_contract("refactor the billing parser in ana",
                                      "the parser test suite passes green", 30,
                                      "tester", self.cfg)

    def tearDown(self):
        self.dir.cleanup()

    def test_a_session_can_be_created_from_another_thread(self):
        sid = _run_on_worker(lambda: self.store.create_session(self.contract))
        self.assertTrue(sid.startswith("ses_"))
        self.assertIsNotNone(self.store.get_session(sid))   # readable back here

    def test_writes_from_a_worker_are_visible_on_the_main_thread(self):
        sid = self.store.create_session(self.contract)
        _run_on_worker(lambda: self.store.add_debt(120, "L1", sid))
        self.assertEqual(self.store.debt_seconds(), 120)

    def test_concurrent_writers_do_not_lose_rows(self):
        sid = self.store.create_session(self.contract)

        def spam(n):
            for i in range(40):
                self.store.add_debt(1, f"w{n}-{i}", sid)

        threads = [threading.Thread(target=spam, args=(n,)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.store.debt_seconds(), 200)

    def test_snooze_increments_are_not_lost_under_contention(self):
        cid = self.store.add_commitment("2026-09-19", "09:00", 60, "write the spec")

        def spam():
            for _ in range(20):
                self.store.snooze_commitment(cid)

        threads = [threading.Thread(target=spam) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        row = self.store.commitments_for_day("2026-09-19")[0]
        self.assertEqual(row["snoozes"], 80)


class TestLedgerAcrossThreads(unittest.TestCase):
    def test_concurrent_appends_keep_the_chain_intact(self):
        d = tempfile.TemporaryDirectory()
        ledger = Ledger(Path(d.name) / "ledger.jsonl")

        def spam(n):
            for i in range(30):
                ledger.append("signal", {"summary": f"{n}-{i}"})

        threads = [threading.Thread(target=spam, args=(n,)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        v = ledger.verify()
        self.assertTrue(v.ok, v.describe())
        self.assertEqual(v.entries, 150)
        d.cleanup()


class TestWholeSessionOnAWorkerThread(unittest.TestCase):
    """The exact shape of the tk path: store/ledger here, run_session there."""

    def test_a_full_session_runs_from_another_thread(self):
        d = tempfile.TemporaryDirectory()
        root = Path(d.name)
        cfg = config.load()
        cfg["user"] = "tester"
        cfg["thresholds"]["sample_seconds"] = 10.0
        clock = FakeClock(start=1_700_000_000.0, step=10.0)

        store = Store(root / "ana.db")            # created on the main thread
        ledger = Ledger(root / "ledger.jsonl")    # ...as the dashboard does
        timeline = SCENARIOS["distracted"]
        sup = Supervisor(AutoUI(cfg, verbose=False), cfg, store, ledger,
                         TimelineProbe(clock, timeline),
                         clock=clock, sleep=clock.sleep)
        contract = sign_contract("refactor the billing parser in ana",
                                 "the parser test suite passes green", 30,
                                 "tester", cfg, grey_budget_minutes=3.0)

        total = sum(step[0] for step in timeline)
        result = _run_on_worker(
            lambda: sup.run_session(contract, max_seconds=total))

        self.assertIs(result.status, SessionStatus.BROKEN)
        self.assertTrue(ledger.verify().ok)
        # and the main thread can still read everything the worker wrote
        self.assertEqual(store.get_session(result.session_id)["status"], "broken")
        self.assertGreater(store.debt_seconds(), 0)
        self.assertFalse(standing(store, cfg, ledger, clock()).recreation_unlocked)
        d.cleanup()


if __name__ == "__main__":
    unittest.main()
