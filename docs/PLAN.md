# Ana — Focus Enforcement & Accountability System

**Design plan. Read this before the code.**

Ana is a PC application that makes focused work the path of least resistance and
makes distraction and avoidance *expensive, visible and on the record*.

Three design commitments drive everything below:

1. **Detection before punishment.** You cannot punish what you cannot see. Most focus
   apps only see "you opened YouTube". The hard cases — avoidance — look like work.
2. **Punishment means friction, forfeit and exposure. Never destruction.** Ana will never
   delete your files, take your money, post on your behalf, or make itself unquittable.
   Its teeth are: time cost, loss of streak/debt, and an honest record you agreed in
   advance to be judged by.
3. **Accountability is pre-committed, not retrofitted.** You sign the terms while you are
   still the sober version of yourself. The distracted version of you does not get a vote.

---

## 1. Taxonomy: every form of distraction and avoidance

Distraction is the easy half. Avoidance is the half that beats every other tool, because
it is disguised as work. Ana is built around the full list.

### Tier A — Overt distraction (easy to see)

| # | Form | What it looks like |
|---|------|--------------------|
| A1 | Entertainment switch | YouTube, Netflix, Reddit, X, TikTok, Steam, news |
| A2 | Social/chat pull | WhatsApp Web, Discord, Telegram, Instagram |
| A3 | Alt-tab flicker | sub-5s peeks at a distracting window, dozens per hour |
| A4 | Background bleed | distracting window visible on a second monitor, audio playing |
| A5 | External interruption | notification, person, phone — genuinely not your fault, still costs |

### Tier B — Productive procrastination (looks like work, isn't *the* work)

| # | Form | What it looks like |
|---|------|--------------------|
| B1 | Inbox/Slack grazing | "just clearing comms" for 40 minutes |
| B2 | Yak-shaving | fixing your editor config, dotfiles, tooling, wallpaper |
| B3 | Research spiral | 9 tabs of legitimate docs, drifting further from the task each tab |
| B4 | Task hopping | switching to a different *real* task whenever the hard one bites |
| B5 | Preparation loop | planning, re-planning, reorganising the todo list, making the plan pretty |
| B6 | Perfection stall | rewriting the same paragraph/function, cosmetic tweaks, renaming things |
| B7 | Meta-work | tuning the focus app itself instead of working (yes, this one too) |

### Tier C — Absence and passivity

| # | Form | What it looks like |
|---|------|--------------------|
| C1 | Away from keyboard | session running, no input for minutes |
| C2 | Phone-in-hand | correct window on screen, zero input, zero output |
| C3 | Passive consumption | reading endlessly on-topic, producing nothing |
| C4 | Fatigue drift | long session, output collapses, you keep "sitting there" |

### Tier D — Session avoidance (the work never starts)

| # | Form | What it looks like |
|---|------|--------------------|
| D1 | No-show | scheduled block arrives, no session started |
| D2 | Snooze abuse | "5 more minutes", repeatedly |
| D3 | Micro-session | start a 90-minute block, quit at 11 minutes |
| D4 | Late-start drift | always starting 2 hours after the declared time |
| D5 | Vague intent | declaring "work on project" so nothing can ever count as failure |

### Tier E — Gaming the system (the user attacking the referee)

| # | Form | What it looks like |
|---|------|--------------------|
| E1 | Killing the tracker | quitting Ana mid-session, task-manager kill, reboot |
| E2 | Editing the record | hand-editing the log/database to erase a bad day |
| E3 | Whitelist inflation | adding YouTube to the allow-list "because it's research" |
| E4 | Misdeclaring | marking a distraction as work when challenged |
| E5 | Fake completion | declaring the session a success with nothing to show |
| E6 | Rule-shopping | loosening thresholds right before a hard session |

**A tool that only handles Tier A is a browser extension. Ana handles A–E.**

---

## 2. How each one is tracked

### 2.1 Raw signals sampled every 2 seconds

| Signal | Source (per OS) |
|--------|-----------------|
| Foreground app + window title | Win32 `GetForegroundWindow`; macOS AppleScript/Quartz; Linux `xdotool`/`xprop` |
| Input idle seconds | Win32 `GetLastInputInfo`; macOS `HIDIdleTime`; Linux `XScreenSaver` |
| Window-switch events | derived from foreground changes |
| Process list (optional) | `psutil`, for background media/games |
| Session heartbeat | written to disk every tick |

All sampling is **local**. Nothing leaves the machine unless you explicitly export a report.
Ana records window *titles*, which is sensitive — titles can be redacted per-rule in config.

### 2.2 Derived detectors (one per taxonomy entry)

| Detector | Fires when | Catches |
|----------|-----------|---------|
| `CategoryDetector` | foreground app matches a BLOCK rule | A1, A2 |
| `FlickerDetector` | ≥3 visits to BLOCK/GREY windows under 5s each, within 3 min | A3 |
| `BackgroundMediaDetector` | media/game process alive while session active | A4 |
| `GreyBudgetDetector` | cumulative GREY time exceeds the session's grey budget | B1 |
| `YakShaveDetector` | config/settings/dotfile windows exceed a cap | B2, B7 |
| `ResearchSpiralDetector` | browser-only for N minutes with no return to a produce-app | B3 |
| `TaskHopDetector` | no window title has matched the declared intent keywords for N min | B4 |
| `PrepLoopDetector` | first N minutes spent only in planning/notes apps | B5 |
| `StallDetector` | same window title, high input, no new titles, for N minutes | B6 |
| `IdleDetector` | input idle > threshold during an active session | C1 |
| `PassivePresenceDetector` | on-task window but idle beyond a shorter threshold | C2, C3 |
| `FatigueDetector` | focus ratio in the last 15 min is under half the session's average | C4 |
| `ScheduleDetector` | a planned block's start time passes with no session | D1, D4 |
| `SnoozeDetector` | ≥2 snoozes on one block | D2 |
| `MicroSessionDetector` | session ends under 40% of planned duration | D3 |
| `IntentQualityGate` | intent is too short/vague or has no verifiable done-criteria | D5 |
| `DesertionDetector` | heartbeat gap > 60s, or a session found open at next launch | E1 |
| `LedgerIntegrityCheck` | hash chain does not verify | E2 |
| `RuleChangeAudit` | allow-list/threshold edits, especially during or just before a session | E3, E6 |
| `ProbeDetector` | random mid-session "what are you doing?" probe unanswered or contradicted | E4 |
| `EvidenceGate` | session closed as "done" with no evidence supplied | E5 |

### 2.3 The classifier

Every window resolves to one of four categories:

- **FOCUS** — matches the session's declared intent (keywords, project paths, allow-rules)
- **NEUTRAL** — OS chrome, file manager, password prompt: free, untimed
- **GREY** — mail, chat, browser, notes: *allowed but budgeted* (default 5 min/hour)
- **BLOCK** — known distraction: costs from the first second

GREY is the key idea. Blanket-blocking email breaks real work; letting email run free
is how a day dies. A budget forces a choice, and the choice is recorded.

---

## 3. How each one is tackled — the escalation ladder

Ana never punishes silently and never jumps straight to the maximum.

| Level | Trigger | Response |
|-------|---------|----------|
| **L0 Nudge** | first 10s on GREY-over-budget or BLOCK | HUD turns amber, soft chime. Free exit. |
| **L1 Interrupt** | 20s continued | Full-screen overlay. Dismiss by **typing your declared intent verbatim**. |
| **L2 Toll** | 2nd interrupt this session | Overlay + **30s forced wait** before the input unlocks, then transcription. |
| **L3 Reckoning** | 3rd interrupt, or any avoidance signal of severity ≥ high | Overlay demands a written answer to *"What are you avoiding right now?"* (min 40 chars), shows your **last three answers** to the same question. Session flagged `BROKEN`. Streak resets. Debt ×2. |
| **L4 Lockout** | 4th+, or chronic pattern across days | Recreation lockout: every GREY/BLOCK window triggers an instant overlay for the next N minutes. Logged to the report in bold. |

**Break-glass.** A single always-available exit (`Ctrl+Shift+Q`, hold 5 seconds) ends any
overlay and any session. It cannot be disabled. Using it writes a `BREAK_GLASS` entry to the
ledger and the daily report, and counts as a desertion. *An app you cannot quit is malware;
an app you can quit silently is a toy. Ana is neither.*

### 3.1 Counter-measures per tier

- **Tier A** → the ladder above, plus optional window de-focus (Ana raises your work window
  back to the front, twice, before escalating).
- **Tier B** → budgets and intent-matching, not blocking. The overlay asks
  *"Is this the task you declared?"* and your answer is recorded next to the evidence you
  produce at the end. B-tier is beaten by being **named**, in the moment, in your own words.
- **Tier C** → idle pauses the clock automatically (no credit for sitting there) and a
  return prompt asks where you went. Passive presence ≥ 4 min triggers an L1.
- **Tier D** → sessions are scheduled in advance as *commitments*; a missed block is a
  logged failure with the same weight as a broken session. Micro-sessions don't count toward
  the streak. Intent must pass a quality gate (≥ 6 words, a verb, a checkable done-criterion).
- **Tier E** → see §4. This is the part almost every tool omits.

---

## 4. Accountability: how the user carries the weight

The user is the one under the obligation. Ana is only the witness and the scorekeeper.

### 4.1 The pre-commitment contract
Before any session starts, you sign (typed name + enter):
- **Intent** — one sentence, must pass the quality gate
- **Done-criteria** — what will exist at the end that does not exist now
- **Duration** and **grey budget**
- **Stake** — what you forfeit if you break it

The contract is hashed into the ledger *before* the timer starts. It cannot be edited
mid-session — only abandoned, which is itself recorded.

### 4.2 Tamper-evident ledger
Every consequential event is appended to `ledger.jsonl` as a **hash-chained** record
(`hash = sha256(prev_hash + payload)`). Editing or deleting any past line breaks the chain.
Ana verifies the chain on every launch, and a broken chain is displayed permanently as
`INTEGRITY BREACH` on the dashboard and in every report until you write a signed explanation.
You can still cheat — it is your machine — but **you cannot cheat quietly**, and quiet is
what cheating needs.

### 4.3 Stakes and forfeits (self-imposed, app-tracked only)
Ana tracks, it never executes. Supported stake types:
- `RECREATION` — lose N minutes of recreation-unlock time
- `DEBT` — focus debt in minutes, must be repaid by real focus before recreation unlocks
- `STREAK` — the streak you've been protecting
- `PLEDGE` — an IOU to a named person or cause, recorded and surfaced until you mark it paid
- `REPORT` — the failure appears verbatim in the report sent to your accountability partner

### 4.4 Focus debt
Distracted minutes accrue debt at 1.5×. Debt must reach zero before Ana marks the day
complete or unlocks recreation mode. Debt carries over. It is the mechanism that makes a
bad morning a problem for the same day, not for "tomorrow me".

### 4.5 Honesty probes
At 1–3 random points per session, a 15-second prompt: *"In one line: what are you doing
right now?"* No answer → recorded as a failed probe. Your answer is stored next to the actual
window title at that instant, so the record shows the gap between what you said and what was
on screen. Nobody has to catch you; the record does.

### 4.6 Evidence at close
Ending a session asks: *"What exists now that didn't before?"* Free text, plus optional
auto-evidence (files modified in the project path, git diff stat, word count delta).
A session closed with no evidence is `UNVERIFIED` — it does not count toward the streak,
no matter how clean the focus time looked.

### 4.7 The mirror
The daily report is not a congratulation. It leads with the things you would rather it
didn't: every intervention, every answer you typed to *"what are you avoiding?"*, every
missed block, every unverified session, every integrity breach. Weekly rollups surface
patterns ("you break at ~34 minutes", "Tuesdays have no completed blocks", "the same answer
appeared 6 times: *'I don't know where to start'*").

### 4.8 Optional external accountability
A partner can be named. The daily report is written to a file (and optionally emailed) in a
form fit to send. Ana does not send anything automatically without explicit setup. Knowing
the report exists and is sendable is most of the effect.

---

## 5. Safety and honesty about limits

- Ana is **local-first**: no telemetry, no account, no cloud. Data lives in `~/.ana`.
- Ana requires **no admin rights** and modifies nothing outside its own directory
  (no hosts file, no firewall, no driver, no kernel hooks).
- Ana is **always quittable** (break-glass), and uninstalling is deleting a folder.
- Ana records window titles; treat `~/.ana` as private. Redaction rules are supported.
- Ana is not a treatment for ADHD, depression, or burnout, and chronic inability to start
  is a signal to get help from a person, not to tighten the thresholds. The weekly report
  says so when the pattern shows up.
- **Ana cannot make you honest.** It can only make dishonesty deliberate, effortful and
  written down. That is the whole trick, and it is enough for most people most of the time.

---

## 6. Architecture

```
ana/
  models.py       dataclasses: Contract, Session, Sample, Signal, Intervention, Action
  config.py       paths, settings, rules, thresholds  (~/.ana/config.json)
  ledger.py       hash-chained append-only JSONL + verification
  storage.py      sqlite: sessions, events, interventions, probes, commitments, debt
  classify.py     window -> FOCUS / NEUTRAL / GREY / BLOCK
  avoidance.py    the detector suite (§2.2)  — pure, fed samples, emits Signals
  penalties.py    Signals -> escalation level -> Intervention + consequences
  scoring.py      focus score, debt, streaks, day/week rollups
  engine.py       FocusEngine: pure state machine. Sample in, Action out. Fully testable.
  runtime.py      Supervisor: real clock, real detectors, drives the engine, executes Actions
  detect/         per-OS foreground-window + idle-time probes (+ a mock for tests)
  ui/             console UI (works everywhere) and tkinter UI (HUD, overlay, dashboard)
  report.py       daily/weekly markdown reports
  cli.py          ana start | status | stop | report | contract | schedule | verify | gui
tests/            unit tests for the pure layer — no OS, no clock, no UI
```

The split that matters: **`engine.py` is pure**. It receives `Sample(ts, app, title,
idle_seconds)` and returns `Action`s. Every rule in §2 and §3 is therefore testable without
a screen, a clock or a human. `runtime.py` is the only part that touches the OS.

---

## 7. Build order

1. models, config, ledger, storage ✔ foundations
2. classify + avoidance detectors ✔ the eyes
3. penalties + scoring ✔ the teeth
4. engine (pure state machine) + tests ✔ the brain
5. detect/ per-OS probes + runtime supervisor ✔ the body
6. CLI + console UI ✔ usable everywhere, headless-safe
7. tkinter HUD / overlay / dashboard ✔ the PC experience
8. report generator ✔ the mirror
