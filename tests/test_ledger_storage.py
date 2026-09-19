import json
import tempfile
import unittest
from pathlib import Path

from ana.ledger import Ledger
from ana.models import Contract, SessionStatus, SessionTotals
from ana.scoring import DaySummary, counts_for_streak, debt_status, repay_debt, session_score, streak, summarise_day
from ana.storage import Store


class TestLedgerIntegrity(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.dir.name) / "ledger.jsonl")
        self.ledger.append("session_start", {"session_id": "ses_1", "intent": "parser"})
        self.ledger.append("distraction", {"app": "chrome", "title": "YouTube"})
        self.ledger.append("session_end", {"session_id": "ses_1", "status": "broken"})

    def tearDown(self):
        self.dir.cleanup()

    def _lines(self):
        return self.ledger.path.read_text(encoding="utf-8").splitlines()

    def _write(self, lines):
        self.ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_clean_chain_verifies(self):
        v = self.ledger.verify()
        self.assertTrue(v.ok)
        self.assertEqual(v.entries, 3)

    def test_editing_a_past_entry_is_caught(self):
        lines = self._lines()
        rec = json.loads(lines[1])
        rec["payload"]["title"] = "VS Code"      # rewriting history
        lines[1] = json.dumps(rec)
        self._write(lines)
        v = self.ledger.verify()
        self.assertFalse(v.ok)
        self.assertIn("altered", v.reason)

    def test_deleting_an_entry_is_caught(self):
        lines = self._lines()
        del lines[1]
        self._write(lines)
        self.assertFalse(self.ledger.verify().ok)

    def test_appending_a_forged_entry_is_caught(self):
        lines = self._lines()
        rec = json.loads(lines[-1])
        forged = dict(rec, seq=3, kind="session_end",
                      payload={"status": "complete"}, prev=rec["hash"])
        lines.append(json.dumps(forged))     # right shape, wrong hash
        self._write(lines)
        self.assertFalse(self.ledger.verify().ok)

    def test_truncating_the_tail_is_caught_by_the_next_append(self):
        # Chopping the end is the one edit the chain alone cannot see, so the
        # detection is that the next append renumbers and the day's totals no
        # longer match the database. We assert the chain still links correctly
        # and that the store reconciliation catches the missing session.
        lines = self._lines()
        self._write(lines[:1])
        self.assertTrue(self.ledger.verify().ok)
        store = Store()
        problems = store.reconcile(self.ledger.entries())
        self.assertTrue(any("ses_1" in p for p in problems))


class TestStore(unittest.TestCase):
    def setUp(self):
        self.store = Store()

    def test_session_roundtrip(self):
        sid = self.store.create_session(Contract("refactor the parser", "tests green", 60))
        self.store.finish_session(sid, SessionStatus.COMPLETE,
                                  SessionTotals(focus_seconds=3000), 88.0, "parser.py")
        row = self.store.get_session(sid)
        self.assertEqual(row["status"], "complete")
        self.assertEqual(json.loads(row["totals"])["focus_seconds"], 3000)

    def test_debt_accumulates_and_is_visible(self):
        self.store.add_debt(90, "L1 intervention")
        self.store.add_debt(240, "L3 intervention")
        self.assertEqual(self.store.debt_seconds(), 330)

    def test_reflections_come_back_newest_first(self):
        for i, text in enumerate(["first answer here", "second answer here",
                                  "third answer here", "fourth answer here"]):
            self.store.add_intervention("s", 3, "task_hop", "reflect", "q", text,
                                        ts=1000 + i)
        got = [r["response"] for r in self.store.recent_reflections(3)]
        self.assertEqual(got, ["fourth answer here", "third answer here",
                               "second answer here"])

    def test_commitment_snoozes_are_counted(self):
        cid = self.store.add_commitment("2026-09-19", "09:00", 90, "write the spec")
        self.assertEqual(self.store.snooze_commitment(cid), 1)
        self.assertEqual(self.store.snooze_commitment(cid), 2)


class TestScoring(unittest.TestCase):
    def test_focus_time_earns_and_distraction_docks(self):
        clean = SessionTotals(focus_seconds=3600)
        messy = SessionTotals(focus_seconds=1800, block_seconds=1200, idle_seconds=600)
        self.assertGreater(session_score(clean, 60), session_score(messy, 60))

    def test_broken_sessions_are_halved(self):
        t = SessionTotals(focus_seconds=3600)
        self.assertLess(session_score(t, 60, status=SessionStatus.BROKEN),
                        session_score(t, 60, status=SessionStatus.COMPLETE) * 0.75)

    def test_unverified_never_counts_for_the_streak(self):
        t = SessionTotals(focus_seconds=3600)
        self.assertFalse(counts_for_streak(SessionStatus.UNVERIFIED, t, 60))
        self.assertTrue(counts_for_streak(SessionStatus.COMPLETE, t, 60))

    def test_a_short_complete_session_does_not_count(self):
        t = SessionTotals(focus_seconds=600)
        self.assertFalse(counts_for_streak(SessionStatus.COMPLETE, t, 60))

    def test_day_summary_and_streak(self):
        rows = [
            {"totals": json.dumps(SessionTotals(focus_seconds=3600).to_dict()),
             "status": "complete", "score": 90},
            {"totals": json.dumps(SessionTotals(focus_seconds=1800,
                                                block_seconds=900).to_dict()),
             "status": "broken", "score": 30},
        ]
        s = summarise_day("2026-09-19", rows, target_minutes=60)
        self.assertEqual(s.sessions, 2)
        self.assertEqual(s.broken, 1)
        self.assertTrue(s.met_target)
        self.assertFalse(s.clean, "a broken session spoils the day")

        days = [DaySummary("d3", focus_minutes=200, target_minutes=180),
                DaySummary("d2", focus_minutes=200, target_minutes=180),
                DaySummary("d1", focus_minutes=10, target_minutes=180)]
        self.assertEqual(streak(days), 2)

    def test_debt_is_repaid_only_by_surplus_focus(self):
        self.assertFalse(debt_status(300)[0])
        self.assertTrue(debt_status(0)[0])
        # delivered exactly the target: debt untouched
        self.assertEqual(repay_debt(600, 3600, 3600), 600)
        # delivered 10 minutes over: debt paid down by 600s
        self.assertEqual(repay_debt(600, 4200, 3600), 0)


if __name__ == "__main__":
    unittest.main()
