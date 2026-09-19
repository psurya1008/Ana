"""One test per taxonomy row in docs/PLAN.md §1 — the whole point of the tool."""

import unittest

from ana.models import Severity

from .helpers import make_contract, make_engine, run, signals


class TestTierAOvertDistraction(unittest.TestCase):
    def test_a1_block_dwell_escalates(self):
        e = make_engine()
        run(e, [(600, "chrome", "YouTube — lofi beats")])
        self.assertIn("block_dwell", signals(e))
        self.assertGreater(e.totals.block_seconds, 500)

    def test_a1_nudge_precedes_intervention(self):
        e = make_engine()
        acts = run(e, [(14, "chrome", "YouTube — lofi beats")])
        self.assertIn("block_dwell_nudge", signals(e))
        self.assertEqual([a for a in acts if a.kind.value == "intervene"], [])

    def test_a3_flicker_catches_the_alt_tab_peek(self):
        e = make_engine()
        script = []
        for i in range(5):
            script.append((60, "code", f"module_{i}.py — VS Code"))
            script.append((4, "chrome", "Reddit — r/all"))   # a 4s peek
        run(e, script)
        self.assertIn("flicker", signals(e))

    def test_a3_flicker_ignores_long_honest_visits(self):
        e = make_engine()
        run(e, [(300, "code", "a.py — VS Code"), (300, "chrome", "Gmail — Inbox")])
        self.assertNotIn("flicker", signals(e))

    def test_a4_background_media_noticed(self):
        e = make_engine()
        run(e, [(120, "code", "a.py — VS Code")], background=("spotify.exe", "svchost"))
        self.assertIn("background_media", signals(e))


class TestTierBProductiveProcrastination(unittest.TestCase):
    def test_b1_grey_budget_overrun(self):
        e = make_engine(make_contract(minutes=60, grey_budget=5.0))
        run(e, [(60 * 12, "chrome", "Gmail — Inbox (42)")])
        self.assertIn("grey_over_budget", signals(e))

    def test_b1_grey_under_budget_is_silent(self):
        e = make_engine(make_contract(minutes=60, grey_budget=10.0))
        run(e, [(60 * 4, "chrome", "Gmail — Inbox (42)")])
        self.assertNotIn("grey_over_budget", signals(e))

    def test_b2_yak_shave(self):
        e = make_engine()
        run(e, [(60 * 9, "code", "settings.json — Preferences")])
        self.assertIn("yak_shave", signals(e))

    def test_b3_research_spiral(self):
        e = make_engine()
        run(e, [(60 * 4, "chrome", "docs: billing parser design")])
        self.assertNotIn("research_spiral", signals(e))
        run(e, [(60 * 12, "chrome", "wikipedia — history of parsers")])
        self.assertIn("research_spiral", signals(e))

    def test_b3_spiral_resets_when_you_go_back_to_making_things(self):
        e = make_engine()
        run(e, [(60 * 10, "chrome", "docs: billing parser"),
                (60 * 2, "code", "parser.py — VS Code"),
                (60 * 10, "chrome", "docs: billing parser")])
        self.assertNotIn("research_spiral", signals(e))

    def test_b4_task_hop_real_work_wrong_work(self):
        e = make_engine()
        run(e, [(60 * 2, "code", "billing parser — VS Code"),     # on the declared task
                (60 * 15, "code", "unrelated_service.py — VS Code")])  # real, but not it
        self.assertIn("task_hop", signals(e))

    def test_b4_no_false_positive_while_on_task(self):
        e = make_engine()
        run(e, [(60 * 30, "code", "billing parser — VS Code")])
        self.assertNotIn("task_hop", signals(e))

    def test_b5_prep_loop(self):
        e = make_engine()
        run(e, [(60 * 10, "notion", "Q3 plan — Notion")])
        self.assertIn("prep_loop", signals(e))

    def test_b6_stall_same_screen_still_typing(self):
        e = make_engine()
        run(e, [(60 * 20, "code", "billing parser — VS Code")])
        self.assertIn("stall", signals(e))


class TestTierCAbsence(unittest.TestCase):
    def test_c1_idle_pauses_the_clock_and_is_recorded(self):
        e = make_engine()
        acts = run(e, [(60 * 6, "code", "billing parser — VS Code", 400)])
        self.assertIn("idle", signals(e))
        self.assertIn("pause_clock", [a.kind.value for a in acts])
        self.assertGreater(e.totals.idle_seconds, 60)

    def test_c1_idle_time_is_never_credited_as_focus(self):
        e = make_engine()
        run(e, [(60 * 10, "code", "billing parser — VS Code", 600)])
        self.assertLess(e.totals.focus_seconds, 130)  # only the pre-threshold part

    def test_c2_passive_presence_right_window_no_input(self):
        e = make_engine()
        # idle above the soft threshold (45s) but below the away threshold (120s)
        run(e, [(60 * 10, "code", "billing parser — VS Code", 60)])
        self.assertIn("passive_presence", signals(e))

    def test_c4_fatigue_when_output_collapses(self):
        e = make_engine(make_contract(minutes=120))
        run(e, [(60 * 40, "code", "billing parser — VS Code"),
                (60 * 20, "chrome", "Gmail — Inbox")])
        self.assertIn("fatigue", signals(e))


class TestTierDSessionAvoidance(unittest.TestCase):
    def test_d3_micro_session(self):
        from ana.avoidance import micro_session_signal
        self.assertIsNotNone(micro_session_signal(11 * 60, 90))
        self.assertIsNone(micro_session_signal(80 * 60, 90))


class TestTierEGaming(unittest.TestCase):
    def test_e1_desertion_from_a_heartbeat_gap(self):
        from ana.avoidance import desertion_signal
        s = desertion_signal(400, 60)
        self.assertIsNotNone(s)
        self.assertIs(s.severity, Severity.CRITICAL)

    def test_e2_integrity_signal(self):
        from ana.avoidance import integrity_signal
        from ana.ledger import Verification
        self.assertIsNone(integrity_signal(Verification(True, 5)))
        self.assertIsNotNone(integrity_signal(Verification(False, 5, 2, "altered")))

    def test_e3_rule_change_is_harsher_mid_session(self):
        from ana.avoidance import rule_change_signal
        mid = rule_change_signal(["rule removed: block 'youtube'"], during_session=True)
        out = rule_change_signal(["rule removed: block 'youtube'"], during_session=False)
        self.assertIs(mid.severity, Severity.HIGH)
        self.assertIs(out.severity, Severity.MEDIUM)


class TestNoFalsePositivesOnAGoodSession(unittest.TestCase):
    def test_honest_work_produces_no_signals(self):
        e = make_engine(make_contract(minutes=50))
        run(e, [(60 * 12, "code", "billing parser — VS Code"),
                (60 * 3, "terminal", "pytest — billing parser"),
                (60 * 10, "code", "parser_test.py — billing — VS Code"),
                (60 * 2, "chrome", "python docs — re module"),
                (60 * 12, "code", "billing parser — VS Code")])
        self.assertEqual(signals(e), [], f"unexpected signals: {signals(e)}")


if __name__ == "__main__":
    unittest.main()
