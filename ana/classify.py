"""Window -> Category. The eyes of the system.

The important idea here is **GREY**. Blanket-blocking email breaks real work;
leaving it unbudgeted is how a day quietly dies. Grey windows are allowed but
metered, and going over budget promotes them to a distraction.

FOCUS is decided *relative to the session's contract*: the same browser window is
focus if the title matches what you declared, and grey if it doesn't.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .models import Category, Contract, Sample

# Words too common to be evidence that a window is on-task.
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "for", "on", "in", "at", "by", "with",
    "my", "our", "this", "that", "it", "is", "be", "do", "doing", "work", "working",
    "task", "project", "stuff", "thing", "things", "some", "make", "get", "finish",
    "up", "out", "into", "from", "about", "write", "new", "all", "more", "just",
}

VERB_HINTS = {
    "write", "draft", "build", "fix", "implement", "refactor", "review", "read",
    "study", "design", "test", "debug", "ship", "send", "call", "plan", "analyse",
    "analyze", "edit", "record", "prepare", "revise", "solve", "learn", "outline",
    "migrate", "document", "reply", "clean", "sketch", "model", "train", "measure",
}


def keywords_from(text: str, extra: Iterable[str] = ()) -> tuple[str, ...]:
    """Pull the load-bearing words out of an intent sentence."""
    words = re.findall(r"[a-z0-9][a-z0-9\-_\.]{2,}", text.lower())
    kws = [w for w in words if w not in STOPWORDS]
    for e in extra:
        e = e.strip().lower()
        if e and e not in kws:
            kws.append(e)
    # de-duplicate, keep order
    seen, out = set(), []
    for k in kws:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return tuple(out)


@dataclass(frozen=True)
class Verdict:
    category: Category
    label: str = ""
    reason: str = ""
    matched_keyword: str = ""


class Classifier:
    def __init__(self, cfg: dict[str, Any], contract: Optional[Contract] = None):
        self.cfg = cfg
        self.contract = contract
        self._rules = [
            (Category(r["category"]), r.get("label", ""), re.compile(r["match"], re.I))
            for r in cfg.get("rules", [])
            if r.get("match")
        ]
        self._produce = [re.compile(p, re.I) for p in cfg.get("produce_apps", [])]
        self._planning = [re.compile(p, re.I) for p in cfg.get("planning_apps", [])]
        self._redact = [re.compile(p, re.I) for p in cfg.get("redact_titles_matching", [])]
        self._keywords = tuple(contract.keywords) if contract and contract.keywords else ()
        if contract and not self._keywords:
            self._keywords = keywords_from(contract.intent + " " + contract.done_criteria)

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _haystack(sample: Sample) -> str:
        return f"{sample.app} | {sample.title}".lower()

    def redact(self, title: str) -> str:
        return "[redacted]" if any(p.search(title) for p in self._redact) else title

    def is_produce_app(self, sample: Sample) -> bool:
        return any(p.search(self._haystack(sample)) for p in self._produce)

    def is_planning_app(self, sample: Sample) -> bool:
        return any(p.search(self._haystack(sample)) for p in self._planning)

    def intent_match(self, sample: Sample) -> str:
        """Return the declared keyword found in this window, if any."""
        hay = self._haystack(sample)
        for kw in self._keywords:
            if len(kw) >= 3 and kw in hay:
                return kw
        return ""

    # -- the call that matters --------------------------------------------
    def classify(self, sample: Sample) -> Verdict:
        hay = self._haystack(sample)

        # 1. Explicit rules first, so BLOCK beats a keyword coincidence.
        #    ("youtube — how to write the report" is not the report.)
        for category, label, pattern in self._rules:
            if pattern.search(hay):
                if category is Category.BLOCK:
                    return Verdict(Category.BLOCK, label, f"matches block rule '{label}'")
                if category is Category.NEUTRAL:
                    return Verdict(Category.NEUTRAL, label, "os chrome")
                # grey rule: a keyword match can still promote it to focus
                kw = self.intent_match(sample)
                if kw:
                    return Verdict(Category.FOCUS, label,
                                   f"matches your declared intent ('{kw}')", kw)
                return Verdict(Category.GREY, label, f"budgeted: '{label}'")

        # 2. No rule matched. Declared keyword in the title -> on task.
        kw = self.intent_match(sample)
        if kw:
            return Verdict(Category.FOCUS, "intent",
                           f"matches your declared intent ('{kw}')", kw)

        # 3. A producing app with no keyword match is still probably the work.
        if self.is_produce_app(sample):
            return Verdict(Category.FOCUS, "produce", "a tool you make things with")

        # 4. Anything unrecognised is grey, not blocked: Ana does not guess that
        #    an unknown window is a distraction, it just puts it on the meter.
        return Verdict(Category.GREY, "unknown", "unrecognised window — budgeted")


# --------------------------------------------------------------------------
# D5: the intent quality gate. A goal you cannot fail is not a goal.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GateResult:
    ok: bool
    problems: tuple[str, ...] = ()

    def describe(self) -> str:
        return "ok" if self.ok else "; ".join(self.problems)


VAGUE_INTENTS = re.compile(
    r"^\s*(work( on)?|do( some)?( work)?|study|be productive|focus|stuff|things|"
    r"catch up|get started|make progress|be good)\b[\s\w]{0,12}$", re.I)


def check_intent(intent: str, done_criteria: str, min_words: int = 6) -> GateResult:
    problems: list[str] = []
    words = intent.split()
    if len(words) < min_words:
        problems.append(f"intent is {len(words)} words; say at least {min_words}")
    if VAGUE_INTENTS.match(intent.strip()):
        problems.append("intent is unfalsifiable — name the specific thing")
    if not any(w.strip(".,").lower() in VERB_HINTS for w in words):
        problems.append("intent has no action verb — what will you actually do?")
    if len(done_criteria.split()) < 4:
        problems.append("done-criteria too thin — what will exist at the end "
                        "that does not exist now?")
    if not keywords_from(intent):
        problems.append("intent has no distinctive words to match windows against")
    return GateResult(not problems, tuple(problems))
