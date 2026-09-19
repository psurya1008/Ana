import unittest

from ana.classify import Classifier, check_intent, keywords_from
from ana.config import load
from ana.models import Category, Sample

from .helpers import make_contract


class TestClassifier(unittest.TestCase):
    def setUp(self):
        self.cl = Classifier(load(), make_contract())

    def cat(self, app, title, idle=0.0):
        return self.cl.classify(Sample(0.0, app, title, idle)).category

    def test_block_rules_win_over_keyword_coincidence(self):
        # "youtube — how to refactor a parser" is not the parser.
        self.assertIs(self.cat("chrome", "YouTube — how to refactor a parser in ana"),
                      Category.BLOCK)

    def test_declared_keyword_promotes_grey_to_focus(self):
        self.assertIs(self.cat("chrome", "stackoverflow — billing parser"), Category.FOCUS)
        self.assertIs(self.cat("chrome", "holiday cottages in wales"), Category.GREY)

    def test_produce_app_is_focus_even_without_keyword(self):
        self.assertIs(self.cat("code", "utils.py — VS Code"), Category.FOCUS)

    def test_unknown_window_is_grey_not_blocked(self):
        self.assertIs(self.cat("some-internal-tool", "dashboard"), Category.GREY)

    def test_mail_and_chat_are_grey(self):
        self.assertIs(self.cat("chrome", "Gmail — Inbox (42)"), Category.GREY)
        self.assertIs(self.cat("slack", "#random"), Category.GREY)

    def test_os_chrome_is_neutral_and_free(self):
        self.assertIs(self.cat("explorer.exe", "Downloads"), Category.NEUTRAL)

    def test_redaction(self):
        cfg = load()
        cfg["redact_titles_matching"] = [r"bank|medical"]
        cl = Classifier(cfg, make_contract())
        self.assertEqual(cl.redact("Barclays bank statement"), "[redacted]")
        self.assertEqual(cl.redact("notes.md"), "notes.md")


class TestIntentGate(unittest.TestCase):
    def test_vague_intent_rejected(self):
        r = check_intent("work on project", "done")
        self.assertFalse(r.ok)
        self.assertTrue(any("unfalsifiable" in p for p in r.problems))

    def test_missing_verb_rejected(self):
        r = check_intent("the billing parser thing for the new client", "a file")
        self.assertFalse(r.ok)
        self.assertTrue(any("action verb" in p for p in r.problems))

    def test_thin_done_criteria_rejected(self):
        r = check_intent("refactor the billing parser in ana today", "done")
        self.assertFalse(r.ok)
        self.assertTrue(any("done-criteria" in p for p in r.problems))

    def test_good_intent_passes(self):
        r = check_intent("refactor the billing parser in ana",
                         "the parser test suite passes green")
        self.assertTrue(r.ok, r.problems)

    def test_keywords_drop_stopwords(self):
        kws = keywords_from("write the report about the thing for work")
        self.assertIn("report", kws)
        self.assertNotIn("the", kws)
        self.assertNotIn("work", kws)


if __name__ == "__main__":
    unittest.main()
