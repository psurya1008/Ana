"""Console UI. No dependencies, works over SSH, and is never not available.

The overlay becomes a blocking prompt that will not go away until you answer it
or break the glass. Less dramatic than the full-screen version, same demand.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Callable

from ..models import Contract, Intervention, SessionStatus

BREAK_GLASS_PHRASE = "BREAK GLASS"

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
AMBER = "\033[33m"
GREEN = "\033[32m"


def _supports_colour() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


class ConsoleUI:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.colour = _supports_colour()
        self._last_hud = 0.0

    def run(self, target):
        """The console UI has no event loop of its own — just run the session."""
        return target()

    # -- plumbing ----------------------------------------------------------
    def _c(self, text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if self.colour else text

    def notice(self, text: str) -> None:
        print(self._c(f"  · {text}", DIM))

    def hud(self, snapshot: dict[str, Any]) -> None:
        # Repaint at most once a second, on one line.
        now = time.time()
        if now - self._last_hud < 1.0:
            return
        self._last_hud = now
        width = 24
        filled = int(snapshot["progress"] * width)
        bar = "█" * filled + "·" * (width - filled)
        colour = GREEN
        if snapshot["block_minutes"] > 0 or snapshot["interventions"]:
            colour = AMBER
        if snapshot["interventions"] >= 3:
            colour = RED
        line = (f"\r{self._c(bar, colour)} "
                f"{snapshot['focus_minutes']:.0f}/{snapshot['planned_minutes']} min"
                f"  grey {snapshot['grey_minutes']:.0f}"
                f"  blocked {snapshot['block_minutes']:.0f}"
                f"  idle {snapshot['idle_minutes']:.0f}"
                f"  L{snapshot['level']}"
                + ("  [PAUSED]" if snapshot["paused"] else "        "))
        sys.stdout.write(line)
        sys.stdout.flush()

    # -- the ladder --------------------------------------------------------
    def nudge(self, intervention: Intervention) -> None:
        print("\n" + self._c(f"  ▲ {intervention.body.splitlines()[0]}", AMBER))

    def intervene(self, intervention: Intervention, contract: Contract,
                  validate: Callable[[str, float], tuple[bool, str]]
                  ) -> tuple[str, float, bool]:
        iv = intervention
        rule = "═" * 64
        print("\n" + self._c(rule, RED))
        print(self._c(f"  L{iv.level}  {iv.headline}", BOLD + RED))
        print()
        for line in iv.body.splitlines():
            print(f"  {line}")
        if iv.consequences:
            print()
            print(self._c("  This costs you:", BOLD))
            for c in iv.consequences:
                print(f"    · {c}")
        print(self._c(rule, RED))

        started = time.time()
        if iv.wait_seconds:
            self._countdown(iv.wait_seconds)

        prompt = {
            "transcribe_intent": "Type your declared intent, exactly: ",
            "wait_then_transcribe": "Type your declared intent, exactly: ",
            "reflect": "What are you avoiding? (be specific) ",
        }.get(iv.challenge.value, "Type 'ok' to continue: ")

        while True:
            try:
                answer = input("  " + prompt)
            except (EOFError, KeyboardInterrupt):
                print()
                return "", time.time() - started, True
            waited = time.time() - started
            if answer.strip() == BREAK_GLASS_PHRASE:
                print(self._c("  break-glass used — this is going on the record.", RED))
                return "", waited, True
            ok, hint = validate(answer, waited)
            if ok:
                print(self._c("  back to it.", GREEN))
                return answer, waited, False
            print(self._c(f"  {hint}   (or type {BREAK_GLASS_PHRASE} to quit and "
                          f"have it recorded)", DIM))

    def _countdown(self, seconds: int) -> None:
        print()
        for remaining in range(int(seconds), 0, -1):
            sys.stdout.write(f"\r  {self._c('wait', DIM)} {remaining:3d}s   ")
            sys.stdout.flush()
            time.sleep(1)
        sys.stdout.write("\r" + " " * 24 + "\r")
        sys.stdout.flush()

    # -- probe --------------------------------------------------------------
    def probe(self, question: str, seconds: float) -> tuple[str, float]:
        print("\n" + self._c(f"  ? {question} ({int(seconds)}s)", BOLD))
        started = time.time()
        try:
            answer = input("  > ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        return answer, time.time() - started

    # -- close --------------------------------------------------------------
    def ask_evidence(self, contract: Contract) -> str:
        print("\n")
        print(self._c("  What exists now that didn't when you started?", BOLD))
        print(self._c(f"  (you signed: {contract.done_criteria})", DIM))
        print(self._c("  An empty answer marks the session UNVERIFIED — it will not "
                      "count toward your streak.", DIM))
        try:
            return input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    def closing(self, status: SessionStatus, score: float, lines: list[str]) -> None:
        colour = {SessionStatus.COMPLETE: GREEN}.get(status, RED)
        print()
        print(self._c(f"  {status.value.upper()}  ·  score {score}", BOLD + colour))
        for line in lines:
            print(f"    {line}")
        print()
