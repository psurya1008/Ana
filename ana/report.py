"""The mirror.

The daily report leads with the things you would rather it didn't. That is the
entire point: a report you enjoy reading is a report that has stopped working.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from . import config, scoring
from .accountability import standing
from .ledger import Ledger
from .storage import Store, day_key

MINUTE = 60.0


def _fmt_time(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _contract_of(row: Any) -> dict[str, Any]:
    try:
        return json.loads(row["contract"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


def daily_report(store: Store, cfg: dict[str, Any], ledger: Optional[Ledger] = None,
                 day: Optional[str] = None, now: Optional[float] = None) -> str:
    now = now if now is not None else time.time()
    day = day or day_key(now)
    rows = store.sessions_for_day(day)
    target = float(cfg.get("daily_focus_target_minutes", 180))
    summary = scoring.summarise_day(day, rows, target)
    st = standing(store, cfg, ledger, now)
    user = cfg.get("user") or "you"
    partner = cfg.get("partner", "")

    out: list[str] = [f"# Ana — {day}", ""]

    # --- the uncomfortable part, first -----------------------------------
    problems: list[str] = []
    if not st.integrity_ok:
        problems.append(f"**{st.integrity_note}** — the record was altered. "
                        f"Nothing below can be trusted until this is explained.")
    if summary.deserted:
        problems.append(f"**{summary.deserted} deserted session(s)** — "
                        f"the tracker was stopped mid-session.")
    if summary.broken:
        problems.append(f"**{summary.broken} broken session(s)** — escalated to L3+.")
    if summary.unverified:
        problems.append(f"**{summary.unverified} unverified session(s)** — "
                        f"time logged, nothing shown for it.")
    if summary.micro:
        problems.append(f"**{summary.micro} micro-session(s)** — signed for far "
                        f"longer than you sat for.")
    for missed in st.missed_blocks:
        problems.append(f"**Missed block**: \"{missed}\" — scheduled, never started.")
    if st.debt_minutes > 0:
        problems.append(f"**{st.debt_minutes:.0f} min of focus debt** outstanding. "
                        f"Recreation stays locked until it is worked off.")
    if not summary.met_target:
        problems.append(f"Target missed: {summary.focus_minutes:.0f} of "
                        f"{target:.0f} min of real focus.")

    if problems:
        out.append("## What went wrong")
        out.extend(f"- {p}" for p in problems)
    else:
        out.append("## What went wrong")
        out.append("- Nothing. Target met, every session evidenced, record intact.")
    out.append("")

    # --- your own words --------------------------------------------------
    reflections = []
    for row in rows:
        for iv in store.interventions_for(row["id"]):
            if iv["challenge"] == "reflect" and iv["response"]:
                reflections.append((_fmt_time(iv["ts"]), iv["signal_kind"],
                                    iv["response"]))
    if reflections:
        out.append("## What you said you were avoiding")
        out.extend(f'- `{t}` ({kind}) — "{text}"' for t, kind, text in reflections)
        out.append("")

    # --- the numbers ------------------------------------------------------
    out.append("## The numbers")
    out.append("")
    out.append("| | minutes |")
    out.append("|---|---|")
    out.append(f"| focus (on the declared task) | **{summary.focus_minutes:.0f}** |")
    out.append(f"| grey (allowed but budgeted) | {summary.grey_minutes:.0f} |")
    out.append(f"| blocked (distraction) | {summary.block_minutes:.0f} |")
    out.append(f"| idle (away / not touching it) | {summary.idle_minutes:.0f} |")
    out.append("")
    out.append(f"Sessions: {summary.sessions} · completed {summary.completed} · "
               f"mean score {summary.score} · streak **{st.streak}** · "
               f"interventions {_intervention_count(store, rows)}")
    out.append("")

    # --- session by session ----------------------------------------------
    if rows:
        out.append("## Sessions")
        out.append("")
        for row in rows:
            contract = _contract_of(row)
            totals = scoring._totals_of(row)
            status = row["status"]
            mark = {"complete": "✔", "unverified": "?", "broken": "✗",
                    "micro": "▁", "deserted": "☠", "active": "…"}.get(status, "·")
            out.append(f"### {mark} {_fmt_time(row['started_at'])} — "
                       f"{contract.get('intent', '(unknown)')}")
            out.append(f"- signed for {contract.get('planned_minutes', '?')} min · "
                       f"focus {totals.focus_seconds / MINUTE:.0f} min · "
                       f"score {row['score'] if row['score'] is not None else '—'} · "
                       f"**{status}**")
            out.append(f"- done when: {contract.get('done_criteria', '—')}")
            out.append(f"- evidence: {row['evidence'] or '**none supplied**'}")
            ivs = store.interventions_for(row["id"])
            if ivs:
                out.append("- interventions: " + ", ".join(
                    f"L{i['level']} {i['signal_kind']}" for i in ivs))
            probes = store.probes_for(row["id"])
            for p in probes:
                verdict = "answered" if p["passed"] else "**ignored**"
                out.append(f"- probe `{_fmt_time(p['ts'])}` {verdict}"
                           + (f': "{p["answer"]}" while looking at {p["window_at_probe"]}'
                              if p["answer"] else f" (window: {p['window_at_probe']})"))
            out.append("")

    # --- what it cost -----------------------------------------------------
    debts = [d for d in store.debt_entries(20)
             if day_key(d["ts"]) == day and d["seconds"] > 0]
    if debts:
        out.append("## What it cost")
        out.extend(f"- {d['seconds'] / MINUTE:.1f} min — {d['reason']}" for d in debts)
        out.append("")

    out.append("---")
    signature = f"Signed by {user}."
    if partner:
        signature += f" Copy to {partner}."
    out.append(f"_{signature}_")
    out.append("_Ana keeps this record locally. It is only as honest as you let it be._")
    return "\n".join(out)


def _intervention_count(store: Store, rows) -> int:
    return sum(len(store.interventions_for(r["id"])) for r in rows)


def weekly_report(store: Store, cfg: dict[str, Any], ledger: Optional[Ledger] = None,
                  now: Optional[float] = None) -> str:
    """Patterns, not events. This is where the useful truth usually is."""
    now = now if now is not None else time.time()
    target = float(cfg.get("daily_focus_target_minutes", 180))
    days = store.days_with_sessions(7)
    summaries = [scoring.summarise_day(d, store.sessions_for_day(d), target)
                 for d in days]

    out = ["# Ana — last 7 recorded days", ""]
    out.append("| day | focus | blocked | idle | sessions | broken | clean |")
    out.append("|---|---|---|---|---|---|---|")
    for s in summaries:
        out.append(f"| {s.day} | {s.focus_minutes:.0f} | {s.block_minutes:.0f} | "
                   f"{s.idle_minutes:.0f} | {s.sessions} | {s.broken} | "
                   f"{'yes' if s.clean else 'no'} |")
    out.append("")

    patterns = find_patterns(store, cfg, days)
    out.append("## Patterns")
    if patterns:
        out.extend(f"- {p}" for p in patterns)
    else:
        out.append("- Not enough data yet.")
    out.append("")

    if _chronic_stall(summaries):
        out.append("## A note")
        out.append("Several days in a row with no completed session is not a "
                   "discipline problem to be solved by tightening thresholds. "
                   "If starting is consistently impossible rather than merely "
                   "unpleasant, that is worth raising with a person, not an app.")
        out.append("")
    return "\n".join(out)


def find_patterns(store: Store, cfg: dict[str, Any], days: list[str]) -> list[str]:
    patterns: list[str] = []
    break_points: list[float] = []
    triggers: Counter = Counter()
    reflections: Counter = Counter()
    weekday_focus: dict[str, list[float]] = {}

    for day in days:
        for row in store.sessions_for_day(day):
            totals = scoring._totals_of(row)
            weekday = time.strftime("%A", time.localtime(row["started_at"]))
            weekday_focus.setdefault(weekday, []).append(totals.focus_seconds / MINUTE)
            for iv in store.interventions_for(row["id"]):
                triggers[iv["signal_kind"]] += 1
                if iv["ts"] and row["started_at"]:
                    break_points.append((iv["ts"] - row["started_at"]) / MINUTE)
                if iv["challenge"] == "reflect" and iv["response"]:
                    reflections[iv["response"].strip().lower()] += 1

    if len(break_points) >= 3:
        break_points.sort()
        median = break_points[len(break_points) // 2]
        patterns.append(f"You tend to break at about **{median:.0f} minutes** in. "
                        f"Consider signing for {max(15, int(median * 0.8) // 5 * 5)}-minute "
                        f"sessions you finish, instead of long ones you don't.")
    if triggers:
        kind, count = triggers.most_common(1)[0]
        patterns.append(f"Your most common trigger is **{kind}** ({count}×).")
    repeated = [(text, n) for text, n in reflections.most_common(3) if n >= 2]
    for text, n in repeated:
        patterns.append(f'You have written "{text}" **{n} times**. '
                        f"That is not a mood, that is a blocker. Name the next "
                        f"concrete 10-minute step and do only that.")
    thin = [d for d, mins in weekday_focus.items() if sum(mins) < 30]
    if thin:
        patterns.append(f"Almost nothing gets done on: {', '.join(sorted(set(thin)))}.")
    return patterns


def _chronic_stall(summaries: list[Any]) -> bool:
    recent = summaries[:4]
    return len(recent) >= 3 and all(s.completed == 0 for s in recent)


def write_report(text: str, day: Optional[str] = None,
                 kind: str = "daily") -> Path:
    home = config.ensure_home()
    name = f"{kind}-{day or day_key()}.md"
    path = home / "reports" / name
    path.write_text(text, encoding="utf-8")
    return path
