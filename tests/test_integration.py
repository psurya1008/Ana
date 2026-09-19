"""End-to-end: real Supervisor, real store, real ledger, fake clock and machine."""

import tempfile
import unittest
from pathlib import Path

from ana import config, report
from ana.accountability import sign_contract, standing
from ana.demo import SCENARIOS, AutoUI, FakeClock, TimelineProbe
from ana.ledger import Ledger
from ana.models import SessionStatus, Stake, StakeKind
from ana.runtime import Supervisor, recover_orphans
from ana.storage import Store


class _Harness:
    def __init__(self, scenario: str, minutes: int = 30, **thresholds):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.cfg = config.load()
        self.cfg["user"] = "tester"
        self.cfg["thresholds"]["sample_seconds"] = 10.0
        self.cfg["thresholds"].update(thresholds)
        self.clock = FakeClock(start=1_700_000_000.0, step=10.0)
        self.store = Store(root / "ana.db")
        self.ledger = Ledger(root / "ledger.jsonl")
        self.timeline = SCENARIOS[scenario]
        self.probe = TimelineProbe(self.clock, self.timeline)
        self.ui = AutoUI(self.cfg, verbose=False)
        self.sup = Supervisor(self.ui, self.cfg, self.store, self.ledger, self.probe,
                              clock=self.clock, sleep=self.clock.sleep)
        self.contract = sign_contract(
            "refactor the billing parser in ana",
            "the parser test suite passes green", minutes, "tester", self.cfg,
            grey_budget_minutes=3.0, stake=Stake(StakeKind.DEBT, 20.0))

    def run(self):
        total = sum(step[0] for step in self.timeline)
        return self.sup.run_session(self.contract, max_seconds=total)

    def close(self):
        self.dir.cleanup()


class TestHonestSession(unittest.TestCase):
    def setUp(self):
        self.h = _Harness("honest")
        self.result = self.h.run()

    def tearDown(self):
        self.h.close()

    def test_completes_with_a_good_score(self):
        self.assertIs(self.result.status, SessionStatus.COMPLETE)
        self.assertGreater(self.result.score, 75)

    def test_no_interventions(self):
        self.assertEqual(self.h.ui.interventions, [])

    def test_ledger_records_the_whole_session_and_verifies(self):
        kinds = [e.kind for e in self.h.ledger.entries()]
        self.assertIn("session_start", kinds)
        self.assertIn("session_end", kinds)
        self.assertTrue(self.h.ledger.verify().ok)

    def test_report_renders(self):
        text = report.daily_report(self.h.store, self.h.cfg, self.h.ledger,
                                   now=self.h.clock())
        self.assertIn("refactor the billing parser", text)
        self.assertIn("## The numbers", text)


class TestDistractedSession(unittest.TestCase):
    def setUp(self):
        self.h = _Harness("distracted")
        self.result = self.h.run()

    def tearDown(self):
        self.h.close()

    def test_session_is_broken_and_scored_down(self):
        self.assertIs(self.result.status, SessionStatus.BROKEN)
        self.assertLess(self.result.score, 40)

    def test_the_ladder_was_climbed_in_order(self):
        levels = [iv.level for iv in self.h.ui.interventions]
        self.assertEqual(levels[:4], [1, 2, 3, 4])

    def test_debt_was_charged(self):
        self.assertGreater(self.h.store.debt_seconds(), 0)

    def test_recreation_is_locked(self):
        st = standing(self.h.store, self.h.cfg, self.h.ledger, self.h.clock())
        self.assertFalse(st.recreation_unlocked)

    def test_report_leads_with_what_went_wrong(self):
        text = report.daily_report(self.h.store, self.h.cfg, self.h.ledger,
                                   now=self.h.clock())
        self.assertIn("## What went wrong", text.split("## The numbers")[0])
        self.assertIn("broken session", text)
        self.assertIn("What you said you were avoiding", text)


class TestAvoidantSession(unittest.TestCase):
    """The tier-B cases: nothing here looks like slacking off."""

    def setUp(self):
        self.h = _Harness("avoidant", minutes=45)
        self.result = self.h.run()

    def tearDown(self):
        self.h.close()

    def test_avoidance_is_caught_even_though_it_looks_like_work(self):
        caught = {s.kind for s in self.result.signals}
        self.assertIn("prep_loop", caught)
        self.assertIn("research_spiral", caught)
        self.assertTrue(self.h.ui.interventions, "avoidance must be interrupted")

    def test_no_block_dwell_was_needed_to_catch_it(self):
        kinds = [iv.signal_kind for iv in self.h.ui.interventions]
        self.assertTrue(any(k != "block_dwell" for k in kinds),
                        "the tier-B detectors, not the blocklist, must do the work")


class TestDesertion(unittest.TestCase):
    def test_a_session_left_open_is_recorded_as_desertion(self):
        d = tempfile.TemporaryDirectory()
        root = Path(d.name)
        cfg = config.load()
        store, ledger = Store(root / "ana.db"), Ledger(root / "ledger.jsonl")
        contract = sign_contract("refactor the billing parser in ana",
                                 "the parser test suite passes green", 60,
                                 "tester", cfg)
        sid = store.create_session(contract, 1_700_000_000.0)
        ledger.append("session_start", {"session_id": sid})

        notes = recover_orphans(store, ledger, cfg, now=1_700_000_000.0 + 3600)
        self.assertTrue(notes)
        self.assertEqual(store.get_session(sid)["status"], "deserted")
        self.assertIn("session_deserted", [e.kind for e in ledger.entries()])
        self.assertGreater(store.debt_seconds(), 0)
        d.cleanup()


class TestPreflightSurfacesTampering(unittest.TestCase):
    def test_an_edited_ledger_is_reported_at_startup(self):
        d = tempfile.TemporaryDirectory()
        root = Path(d.name)
        cfg = config.load()
        store, ledger = Store(root / "ana.db"), Ledger(root / "ledger.jsonl")
        ledger.append("session_start", {"session_id": "ses_x", "intent": "a"})
        ledger.append("signal", {"summary": "40s on YouTube"})
        lines = ledger.path.read_text().splitlines()
        lines[1] = lines[1].replace("40s on YouTube", "0s on YouTube")
        ledger.path.write_text("\n".join(lines) + "\n")

        sup = Supervisor(AutoUI(cfg, verbose=False), cfg, store, ledger,
                         clock=lambda: 1_700_000_000.0, sleep=lambda _s: None)
        notes = sup.preflight()
        self.assertTrue(any("INTEGRITY BREACH" in n for n in notes))
        self.assertTrue(any("ses_x" in n for n in notes))
        d.cleanup()


if __name__ == "__main__":
    unittest.main()


class TestStreakAcrossDays(unittest.TestCase):
    """A streak you lose every morning at 00:01 is not a streak."""

    def setUp(self):
        import time as _time
        from ana.models import SessionTotals
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.cfg = config.load()
        self.cfg["daily_focus_target_minutes"] = 60
        self.store = Store(root / "ana.db")
        self.ledger = Ledger(root / "ledger.jsonl")
        self.now = _time.time()

        def add_day(days_ago: int, minutes: float, status: str):
            ts = self.now - days_ago * 86400
            contract = sign_contract("refactor the billing parser in ana",
                                     "the parser test suite passes green", 60,
                                     "tester", self.cfg)
            sid = self.store.create_session(contract, ts)
            totals = SessionTotals(focus_seconds=minutes * 60)
            self.store.finish_session(sid, SessionStatus(status), totals, 90.0,
                                      "evidence", ended_at=ts)
        self.add_day = add_day

    def tearDown(self):
        self.dir.cleanup()

    def test_an_unfinished_today_does_not_zero_a_real_streak(self):
        self.add_day(2, 90, "complete")
        self.add_day(1, 90, "complete")
        self.add_day(0, 5, "complete")          # today, barely started
        st = standing(self.store, self.cfg, self.ledger, self.now)
        self.assertEqual(st.streak, 2)

    def test_a_clean_today_extends_the_streak_immediately(self):
        self.add_day(1, 90, "complete")
        self.add_day(0, 90, "complete")
        st = standing(self.store, self.cfg, self.ledger, self.now)
        self.assertEqual(st.streak, 2)

    def test_a_broken_yesterday_still_breaks_the_streak(self):
        self.add_day(2, 90, "complete")
        self.add_day(1, 90, "broken")
        self.add_day(0, 90, "complete")
        st = standing(self.store, self.cfg, self.ledger, self.now)
        self.assertEqual(st.streak, 1)
