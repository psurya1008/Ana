import unittest

from ana.models import Challenge, SessionStatus

from .helpers import (comply, interventions, kinds, make_contract, make_engine,
                      run)


class TestEscalationLadder(unittest.TestCase):
    def test_ladder_climbs_one_rung_at_a_time(self):
        e = make_engine()
        levels = []
        for _ in range(4):
            acts = run(e, [(40, "chrome", "YouTube — lofi beats")])
            levels.extend(comply(e, acts))
            run(e, [(120, "code", "billing parser — VS Code")])
        self.assertEqual(levels[:4], [1, 2, 3, 4])

    def test_l1_demands_the_intent_typed_verbatim(self):
        e = make_engine()
        acts = run(e, [(40, "chrome", "YouTube — lofi beats")])
        iv = interventions(acts)[0]
        self.assertIs(iv.challenge, Challenge.TRANSCRIBE_INTENT)
        ok, hint, _ = e.resolve_intervention("nah")
        self.assertFalse(ok)
        self.assertIn("declared intent", hint)
        ok, _, _ = e.resolve_intervention("  Refactor The Billing Parser In Ana  ")
        self.assertTrue(ok, "case and spacing should not matter")

    def test_l2_wait_cannot_be_skipped(self):
        e = make_engine()
        for _ in range(2):
            acts = run(e, [(40, "chrome", "YouTube — lofi beats")])
            ivs = interventions(acts)
            if ivs and ivs[0].level == 2:
                ok, hint, _ = e.resolve_intervention(e.contract.intent, waited_seconds=3)
                self.assertFalse(ok)
                self.assertIn("wait", hint)
                ok, _, _ = e.resolve_intervention(e.contract.intent, waited_seconds=30)
                self.assertTrue(ok)
                return
            comply(e, acts)
            run(e, [(120, "code", "billing parser — VS Code")])
        self.fail("never reached L2")

    def test_l3_demands_a_real_answer_and_breaks_the_session(self):
        e = make_engine()
        # task_hop goes straight to the reckoning
        acts = run(e, [(60, "code", "billing parser — VS Code"),
                       (60 * 15, "code", "other_service.py — VS Code")])
        iv = interventions(acts)[0]
        self.assertGreaterEqual(iv.level, 3)
        self.assertIs(iv.challenge, Challenge.REFLECT)
        ok, hint, _ = e.resolve_intervention("dunno", waited_seconds=60)
        self.assertFalse(ok)
        ok, hint, _ = e.resolve_intervention("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                             waited_seconds=60)
        self.assertFalse(ok, "keyboard mashing should not pass")
        ok, _, _ = e.resolve_intervention(
            "I am avoiding the parser because I do not know how to handle the "
            "legacy rows and I am scared it is a rewrite", waited_seconds=60)
        self.assertTrue(ok)
        self.assertIs(e.status, SessionStatus.BROKEN)

    def test_reflection_answers_are_shown_back_next_time(self):
        e = make_engine()
        acts = run(e, [(60, "code", "billing parser — VS Code"),
                       (60 * 15, "code", "other_service.py — VS Code")])
        iv = interventions(acts)[0]
        answer = ("I do not know where to start with the legacy rows so I keep "
                  "doing the easy service instead")
        ok, hint, _ = e.resolve_intervention(answer, waited_seconds=iv.wait_seconds)
        self.assertTrue(ok, hint)
        self.assertIn(answer, e.escalator.recent_reflections)

    def test_only_one_overlay_at_a_time(self):
        e = make_engine()
        acts = run(e, [(60 * 10, "chrome", "YouTube — lofi beats")])
        self.assertEqual(len(interventions(acts)), 1,
                         "must not stack overlays while one is unanswered")

    def test_advisory_signals_only_nudge(self):
        e = make_engine()
        acts = run(e, [(60 * 5, "code", "billing parser — VS Code")],
                   background=("spotify.exe",))
        self.assertIn("nudge", kinds(acts))
        self.assertNotIn("intervene", kinds(acts))

    def test_work_window_is_raised_before_the_overlay(self):
        e = make_engine()
        acts = run(e, [(60 * 2, "code", "billing parser — VS Code"),
                       (40, "chrome", "YouTube — lofi beats")])
        self.assertIn("raise", kinds(acts))

    def test_lockout_action_at_l4(self):
        e = make_engine()
        for _ in range(4):
            acts = run(e, [(40, "chrome", "YouTube — lofi beats")])
            comply(e, acts)
            if "lockout" in kinds(acts):
                return
            run(e, [(120, "code", "billing parser — VS Code")])
        self.fail("never locked out")


class TestProbes(unittest.TestCase):
    def test_a_probe_fires_mid_session(self):
        e = make_engine(make_contract(minutes=30))
        acts = run(e, [(60 * 30, "code", "billing parser — VS Code")])
        self.assertIn("probe", kinds(acts))

    def test_ignored_probe_counts_against_you(self):
        e = make_engine(make_contract(minutes=30))
        acts = run(e, [(60 * 30, "code", "billing parser — VS Code")])
        probe = next(a for a in acts if a.kind.value == "probe")
        e.answer_probe("", probe.ts + 60)
        self.assertEqual(e.totals.probes_failed, 1)
        self.assertIn("probe_failed", [s.kind for s in e.signals])

    def test_prompt_answer_passes(self):
        e = make_engine(make_contract(minutes=30))
        acts = run(e, [(60 * 30, "code", "billing parser — VS Code")])
        probe = next(a for a in acts if a.kind.value == "probe")
        self.assertTrue(e.answer_probe("writing the legacy row branch", probe.ts + 4))
        self.assertEqual(e.totals.probes_passed, 1)


class TestSessionClose(unittest.TestCase):
    def test_no_evidence_means_unverified(self):
        e = make_engine(make_contract(minutes=30))
        run(e, [(60 * 30, "code", "billing parser — VS Code")])
        status, score, extra = e.close(e.last_ts, evidence="")
        self.assertIs(status, SessionStatus.UNVERIFIED)
        self.assertIn("no_evidence", [s.kind for s in extra])

    def test_evidence_completes_the_session(self):
        e = make_engine(make_contract(minutes=30))
        run(e, [(60 * 30, "code", "billing parser — VS Code")])
        status, score, _ = e.close(e.last_ts, evidence="parser.py rewritten, 14 tests green")
        self.assertIs(status, SessionStatus.COMPLETE)
        self.assertGreater(score, 70)

    def test_quitting_early_is_a_micro_session(self):
        e = make_engine(make_contract(minutes=90))
        run(e, [(60 * 11, "code", "billing parser — VS Code")])
        status, score, _ = e.close(e.last_ts, evidence="a bit of it")
        self.assertIs(status, SessionStatus.MICRO)

    def test_break_glass_always_works_and_is_recorded(self):
        e = make_engine()
        run(e, [(60, "chrome", "YouTube — lofi beats")])
        e.break_glass(e.last_ts)
        self.assertIsNone(e.pending)
        self.assertIn("break_glass", [s.kind for s in e.signals])
        status, _, _ = e.close(e.last_ts, evidence="none")
        self.assertIs(status, SessionStatus.DESERTED)

    def test_a_sleeping_machine_earns_no_credit(self):
        from ana.models import Sample
        e = make_engine()
        run(e, [(60 * 5, "code", "billing parser — VS Code")])
        before = e.totals.focus_seconds
        e.tick(Sample(e.last_ts + 3600, "code", "billing parser — VS Code"))
        self.assertAlmostEqual(e.totals.focus_seconds, before, delta=1.0)
        self.assertIn("desertion", [s.kind for s in e.signals])


class TestSnapshot(unittest.TestCase):
    def test_snapshot_shape(self):
        e = make_engine(make_contract(minutes=30))
        run(e, [(60 * 15, "code", "billing parser — VS Code")])
        snap = e.snapshot()
        self.assertAlmostEqual(snap["focus_minutes"], 15.0, delta=0.5)
        self.assertAlmostEqual(snap["progress"], 0.5, delta=0.05)


if __name__ == "__main__":
    unittest.main()


class TestYouCannotTalkYourWayPastL3(unittest.TestCase):
    def test_typing_the_intent_does_not_satisfy_a_reflect_challenge(self):
        """Regression: the L1 trick must not work on the L3 demand."""
        e = make_engine()
        acts = run(e, [(60, "code", "billing parser — VS Code"),
                       (60 * 15, "code", "other_service.py — VS Code")])
        iv = interventions(acts)[0]
        ok, _, _ = e.resolve_intervention(e.contract.intent, waited_seconds=iv.wait_seconds)
        self.assertFalse(ok)
        self.assertIsNotNone(e.pending, "the overlay must stay up")


class TestLockoutBehaviour(unittest.TestCase):
    def test_a_nudge_stays_a_nudge_during_a_lockout(self):
        """Regression: lockout must not promote advisory signals into overlays."""
        from ana.models import Severity, Signal
        e = make_engine()
        e.escalator.lockout_until = 10_000
        for kind in ("block_dwell_nudge", "fatigue", "background_media"):
            iv = e.escalator.judge(Signal(kind, Severity.LOW, "x"), 100.0)
            self.assertEqual(iv.level, 0, f"{kind} should not become an overlay")

    def test_repeat_l4_does_not_compound_the_charge(self):
        from ana.models import Severity, Signal
        e = make_engine()
        first = None
        for i in range(6):
            iv = e.escalator.judge(Signal("block_dwell", Severity.MEDIUM, "x"), 100.0 * i)
            if iv.level == 4 and first is None:
                first = iv
            elif iv.level == 4 and first is not None:
                self.assertLess(iv.debt_seconds, first.debt_seconds)
                return
        self.fail("never saw two L4s")
