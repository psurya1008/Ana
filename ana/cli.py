"""``ana`` — the command line. Everything the app does is reachable from here."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Optional

from . import accountability, config, report
from .accountability import ContractRejected, sign_contract, standing
from .ledger import open_ledger
from .models import Stake, StakeKind
from .runtime import Supervisor
from .storage import day_key, open_store
from .ui import pick_ui

MINUTE = 60.0


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        got = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(1)
    return got or default


def _parse_stake(text: str) -> Stake:
    """--stake debt:30 | recreation:45 | pledge:'£20 to Dan' | streak | report:..."""
    kind, _, rest = text.partition(":")
    kind = kind.strip().lower()
    try:
        stake_kind = StakeKind(kind)
    except ValueError:
        raise SystemExit(f"unknown stake kind '{kind}'. "
                         f"one of: {', '.join(k.value for k in StakeKind)}")
    if stake_kind in (StakeKind.DEBT, StakeKind.RECREATION):
        try:
            return Stake(stake_kind, float(rest or 15))
        except ValueError:
            raise SystemExit(f"stake '{text}' needs minutes, e.g. {kind}:30")
    return Stake(stake_kind, 0.0, rest.strip())


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> int:
    cfg = config.load()
    store, ledger = open_store(), open_ledger()
    ui = pick_ui(cfg, args.ui)
    sup = Supervisor(ui, cfg, store, ledger)

    for note in sup.preflight():
        print(f"  ! {note}")

    intent = args.intent or _ask("What exactly will you do")
    done = args.done or _ask("Done when")
    minutes = args.minutes or int(_ask("For how many minutes", "50"))
    signer = args.sign or cfg.get("user") or _ask("Sign it — your name")
    stake = _parse_stake(args.stake) if args.stake else Stake(StakeKind.DEBT, 15.0)

    try:
        contract = sign_contract(intent, done, minutes, signer, cfg,
                                 grey_budget_minutes=args.grey, stake=stake,
                                 extra_keywords=args.keyword or ())
    except ContractRejected as exc:
        print("\n  This contract is not signable:")
        for problem in exc.problems:
            print(f"    · {problem}")
        print("\n  A goal you cannot fail is not a goal. Sharpen it and try again.")
        return 2

    if not cfg.get("user"):
        cfg["user"] = signer
        config.save(cfg)

    print()
    print(f'  SIGNED: "{contract.intent}"')
    print(f"  done when: {contract.done_criteria}")
    print(f"  {contract.planned_minutes} min · grey budget "
          f"{contract.grey_budget_minutes:g} min/h · stake: {contract.stake.describe()}")
    st = standing(store, cfg, ledger)
    print(f"  standing: {st.headline()}")
    print("  (break-glass: type 'BREAK GLASS' at any prompt — it works, "
          "and it is recorded)")
    print()

    # The tk UI owns the main thread and runs the session on a worker; the
    # console UI just calls it. Both go through run() so this stays one line.
    result = ui.run(lambda: sup.run_session(contract, project_path=args.project))
    return 0 if result and result.status.value == "complete" else 1


def cmd_status(args: argparse.Namespace) -> int:
    cfg = config.load()
    store, ledger = open_store(), open_ledger()
    st = standing(store, cfg, ledger)
    print(f"  {st.day}")
    print(f"  {st.headline()}")
    if not st.integrity_ok:
        print(f"  ! {st.integrity_note}")
    if st.open_sessions:
        print(f"  ! {len(st.open_sessions)} session(s) still open — run `ana start` "
              f"or `ana verify` to settle them")
    for missed in st.missed_blocks:
        print(f"  ! missed block: {missed}")
    for signal in accountability.schedule_signals(store, cfg):
        print(f"  ! {signal.detail}")
    pending = store.pending_commitments(day_key())
    for row in pending:
        print(f"  · {row['start_hhmm']} — {row['intent']} ({row['minutes']} min)"
              + (f" [snoozed {row['snoozes']}×]" if row["snoozes"] else ""))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    cfg = config.load()
    store, ledger = open_store(), open_ledger()
    if args.weekly:
        text = report.weekly_report(store, cfg, ledger)
        kind, day = "weekly", day_key()
    else:
        day = args.day or day_key()
        text = report.daily_report(store, cfg, ledger, day)
        kind = "daily"
    print(text)
    if args.write:
        path = report.write_report(text, day, kind)
        print(f"\n  written to {path}")
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    cfg = config.load()
    store, ledger = open_store(), open_ledger()
    if args.add:
        at, _, rest = args.add.partition(" ")
        minutes = args.minutes or 50
        intent = rest.strip() or _ask("What will you do in that block")
        cid = store.add_commitment(args.day or day_key(), at, minutes, intent)
        ledger.append("commitment", {
            "id": cid, "day": args.day or day_key(), "at": at, "minutes": minutes,
            "intent": intent,
            "summary": f'COMMITTED: {at} — "{intent}" ({minutes} min)'})
        print(f"  committed: {at} — {intent} ({minutes} min)")
        return 0
    if args.miss:
        missed = accountability.settle_past_commitments(store, cfg)
        for m in missed:
            ledger.append("block_missed", {"intent": m,
                                           "summary": f'MISSED: "{m}"'})
            print(f"  recorded as missed: {m}")
        if not missed:
            print("  nothing outstanding")
        return 0
    rows = store.commitments_for_day(args.day or day_key())
    if not rows:
        print("  nothing scheduled")
    for row in rows:
        print(f"  {row['start_hhmm']}  {row['minutes']:>3} min  "
              f"[{row['status']}]  {row['intent']}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    cfg = config.load()
    store, ledger = open_store(), open_ledger()
    v = ledger.verify()
    print(f"  {v.describe()}")
    problems = store.reconcile(ledger.entries())
    for p in problems:
        print(f"  ! {p}")
    from .runtime import recover_orphans
    for note in recover_orphans(store, ledger, cfg):
        print(f"  ! {note}")
    return 0 if (v.ok and not problems) else 1


def cmd_explain(args: argparse.Namespace) -> int:
    """Write a signed explanation for an integrity breach. It does not erase it."""
    ledger = open_ledger()
    cfg = config.load()
    v = ledger.verify()
    if v.ok:
        print("  the ledger verifies — nothing to explain")
        return 0
    print(f"  {v.describe()}")
    text = args.text or _ask("Explain, in your own words, what you changed and why")
    if len(text.strip()) < 40:
        print("  that is not an explanation. At least 40 characters.")
        return 2
    ledger.append("integrity_explanation", {
        "signed_by": cfg.get("user", ""), "text": text,
        "summary": f"INTEGRITY BREACH explained: {text}"})
    print("  recorded. The breach stays in the record; so does your explanation.")
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    ledger = open_ledger()
    for entry in ledger.tail(args.number):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.ts))
        print(f"  #{entry.seq:<4} {when}  {entry.kind:<22} {entry.summary}")
    print(f"\n  {ledger.verify().describe()}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    cfg = config.load()
    ledger = open_ledger()
    store = open_store()
    if not args.set:
        print(json.dumps(cfg, indent=2, sort_keys=True))
        return 0
    before = config.load()
    for pair in args.set:
        key, _, value = pair.partition("=")
        key = key.strip()
        try:
            parsed: Any = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        if key in config.DEFAULT_THRESHOLDS:
            cfg["thresholds"][key] = parsed
        else:
            cfg[key] = parsed
    changes = config.diff_for_audit(before, cfg)
    during = bool(store.active_sessions())
    if changes:
        from .avoidance import rule_change_signal
        signal = rule_change_signal(changes, during)
        ledger.append("rule_change", {
            "changes": changes, "during_session": during,
            "summary": signal.detail if signal else "; ".join(changes)})
        print("  recorded in the ledger:")
        for c in changes:
            print(f"    · {c}")
        if during:
            print("  ! changing the rules mid-session is logged as such.")
    config.save(cfg)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Run a whole session against a scripted machine in a few seconds.

    The point is to see the ladder work without waiting an hour to be caught.
    """
    from .demo import run_demo
    return run_demo(args)


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from .ui.tkui import run_dashboard
    except Exception as exc:  # noqa: BLE001
        print(f"  the graphical UI needs tkinter, which is not available here ({exc}).")
        print("  On Windows/macOS it ships with python.org's installer; on Debian/Ubuntu "
              "run: sudo apt install python3-tk")
        return 2
    return run_dashboard()


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ana",
        description="Ana — focus enforcement and accountability. "
                    "Local, quittable, and on the record.")
    p.add_argument("--version", action="store_true")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("start", help="sign a contract and run a focus session")
    s.add_argument("--intent")
    s.add_argument("--done")
    s.add_argument("--minutes", type=int)
    s.add_argument("--grey", type=float, help="grey budget, minutes per hour")
    s.add_argument("--stake", help="debt:30 | recreation:45 | streak | pledge:'£20 to Dan'")
    s.add_argument("--sign", help="your name")
    s.add_argument("--project", help="path whose file changes count as evidence")
    s.add_argument("--keyword", action="append", help="extra on-task keyword")
    s.add_argument("--ui", choices=["tk", "console"])
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("status", help="where you stand right now")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("report", help="the mirror")
    s.add_argument("--weekly", action="store_true")
    s.add_argument("--day")
    s.add_argument("--write", action="store_true", help="also save to ~/.ana/reports")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("schedule", help="commit to blocks in advance")
    s.add_argument("--add", metavar="'HH:MM intent'")
    s.add_argument("--minutes", type=int)
    s.add_argument("--day")
    s.add_argument("--miss", action="store_true",
                   help="settle every unstarted block for today as missed")
    s.set_defaults(func=cmd_schedule)

    s = sub.add_parser("verify", help="check the record has not been altered")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("explain", help="sign an explanation for an integrity breach")
    s.add_argument("--text")
    s.set_defaults(func=cmd_explain)

    s = sub.add_parser("ledger", help="show the tail of the record")
    s.add_argument("-n", "--number", type=int, default=20)
    s.set_defaults(func=cmd_ledger)

    s = sub.add_parser("config", help="show or change settings (changes are logged)")
    s.add_argument("--set", action="append", metavar="key=value")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("demo", help="run a fast simulated session to see the ladder")
    s.add_argument("--scenario", default="distracted",
                   choices=["distracted", "avoidant", "honest"])
    s.add_argument("--auto", action="store_true",
                   help="answer the prompts automatically")
    s.set_defaults(func=cmd_demo)

    s = sub.add_parser("gui", help="open the dashboard (needs tkinter)")
    s.set_defaults(func=cmd_gui)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "version", False):
        from . import __version__
        print(f"ana {__version__}")
        return 0
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    config.ensure_home()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
