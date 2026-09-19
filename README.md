# Ana

A focus application for PC that makes distraction and avoidance **expensive,
visible, and on the record**.

Most focus tools block YouTube and call it a day. That catches maybe a third of
how a working day actually dies. The rest is avoidance — reorganising the todo
list, a research spiral, "just clearing the inbox", switching to a different
*real* task the moment the hard one bites, sitting in front of the right window
doing nothing. All of it looks like work. Ana is built to catch all of it.

**Read [`docs/PLAN.md`](docs/PLAN.md) first.** It is the design: the full
taxonomy of distraction and avoidance, how each form is tracked, how each one is
tackled, and how accountability is made to stick.

---

## Three rules Ana holds to

1. **Detection before punishment.** You cannot punish what you cannot see.
2. **Punishment is friction, forfeit and exposure — never destruction.** Ana will
   not delete your files, take your money, post anything, or refuse to quit.
3. **Accountability is pre-committed.** You sign the terms while you are still
   the version of yourself that wants to work. The distracted version gets no vote.

## Try it in thirty seconds

```bash
python -m ana demo --scenario distracted --auto   # watch the ladder escalate
python -m ana demo --scenario avoidant  --auto    # the cases other tools miss
python -m ana demo --scenario honest    --auto    # a clean session: no interruptions
```

The demo runs a full session against a scripted machine on a virtual clock, then
prints the daily report. It writes to `~/.ana/demo` and never touches your record.

## Real use

```bash
pip install -e .

ana schedule --add "09:00 write the billing parser tests" --minutes 90
ana start --project ~/code/billing        # sign a contract, then work
ana status                                # where you stand
ana report                                # the mirror
ana report --weekly                       # the patterns
ana verify                                # has the record been altered?
ana gui                                   # dashboard + HUD (needs tkinter)
```

`ana start` will refuse a goal you cannot fail:

```
$ ana start --intent "work on stuff" --done "done"

  This contract is not signable:
    · intent is 3 words; say at least 6
    · intent is unfalsifiable — name the specific thing
    · intent has no action verb — what will you actually do?
    · done-criteria too thin — what will exist at the end that does not exist now?

  A goal you cannot fail is not a goal. Sharpen it and try again.
```

## What it catches

| Tier | Forms | Caught by |
|---|---|---|
| **A** Overt distraction | entertainment, social, alt-tab peeks, background media | blocklist, dwell timer, flicker detector |
| **B** Productive procrastination | inbox grazing, yak-shaving, research spirals, task hopping, prep loops, perfection stalls | grey budgets, intent matching, produce-app tracking |
| **C** Absence & passivity | away from keyboard, phone-in-hand, passive reading, fatigue drift | idle probe, passive-presence timer, focus-ratio collapse |
| **D** Session avoidance | no-shows, snooze abuse, micro-sessions, late starts, vague intents | scheduled commitments, intent quality gate |
| **E** Gaming the system | killing the tracker, editing the log, whitelist inflation, misdeclaring, fake completion | heartbeat, hash-chained ledger, rule-change audit, honesty probes, evidence gate |

The interesting column is **E**. Every other tool assumes you are on its side.

## The escalation ladder

| Level | Trigger | Response |
|---|---|---|
| L0 | drifting | HUD turns amber, quiet chime |
| L1 | 20s on a distraction | full-screen overlay; dismiss by typing your declared intent verbatim |
| L2 | second time | 30-second forced wait, then the same |
| L3 | third time, or any high-severity avoidance signal | *"What are you avoiding?"*, minimum 40 characters, **your last three answers shown back to you**; session marked BROKEN, streak reset, debt doubled |
| L4 | chronic | recreation lockout; flagged in bold in the report |

**Break-glass**: hold the button for 5 seconds (or type `BREAK GLASS` in the
console) and everything stops. It always works. It is recorded as a desertion.
An app you cannot quit is malware; an app you can quit silently is a toy.

## How accountability is made to stick

- **A signed contract** — intent, done-criteria, duration, grey budget, stake —
  hashed into the ledger *before* the timer starts, and not editable afterwards.
- **A tamper-evident ledger** — `~/.ana/ledger.jsonl`, hash-chained. Edit or
  delete any line and the chain stops verifying; `INTEGRITY BREACH` then rides on
  the dashboard and every report until you write a signed explanation. You can
  still cheat. You cannot cheat *quietly*, and quiet is what cheating needs.
- **Honesty probes** — 15 seconds to say what you are doing. Your answer is stored
  next to the window that was actually on screen.
- **An evidence gate** — a session closed with nothing to show is `UNVERIFIED` and
  does not count, however clean the focus time looked.
- **Focus debt** — distracted minutes accrue at 1.5×, and recreation stays locked
  until they are worked off. A bad morning is a problem for *this* day.
- **The mirror** — the daily report leads with what went wrong, quotes back every
  answer you gave to *"what are you avoiding?"*, and the weekly rollup finds the
  patterns ("you break at about 34 minutes", "you have written *'I don't know
  where to start'* six times").

## Privacy, safety, limits

- **Local only.** No account, no telemetry, no network. Everything is in `~/.ana`.
- **No admin rights, nothing global.** No hosts file, no firewall, no driver.
  Uninstalling is deleting a folder.
- Ana records **window titles** — treat `~/.ana` as private. Set
  `redact_titles_matching` in the config for anything that should never be stored.
- Ana is not a treatment for ADHD, depression or burnout. If starting is
  consistently *impossible* rather than merely unpleasant, that is worth raising
  with a person, not solved by tightening a threshold. The weekly report says so
  when the pattern appears.

## Platform support

| | foreground window | idle time | raise work window |
|---|---|---|---|
| **Windows** | Win32 `GetForegroundWindow` | `GetLastInputInfo` | `SetForegroundWindow` |
| **macOS** | System Events | Quartz `HIDIdleTime` | `osascript activate` |
| **Linux/X11** | `xdotool` | `xprintidle` | `xdotool windowactivate` |

ctypes and subprocess only — no compiled dependency. If a probe is unavailable,
Ana still times your sessions and says plainly that it cannot see your windows.
The GUI needs `tkinter`, which ships with the python.org installers on Windows
and macOS; on Debian/Ubuntu run `sudo apt install python3-tk`.

## Layout

```
ana/
  models.py       value types
  config.py       rules, thresholds, the rule-change audit
  ledger.py       hash-chained append-only record
  storage.py      sqlite index for scoring and reports
  classify.py     window -> FOCUS / NEUTRAL / GREY / BLOCK, + the intent gate
  avoidance.py    the detector suite — one per taxonomy row
  penalties.py    the escalation ladder
  scoring.py      score, debt, streaks
  engine.py       the pure state machine: Sample in, Action out
  runtime.py      the supervisor — the only module that touches your machine
  detect/         per-OS window + idle probes
  ui/             console UI, and the tk HUD / overlay / dashboard
  report.py       the mirror
  demo.py         a full session on a virtual clock
tests/            87 tests over the pure layer and end to end
```

`engine.py` is pure — samples in, actions out — which is why the entire rulebook
is tested against synthetic days in under a second:

```bash
python -m unittest discover -s tests -t .
```
