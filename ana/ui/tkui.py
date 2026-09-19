"""The PC front end: an always-on-top HUD, a full-screen interrupt, a dashboard.

Threading model: tkinter owns the main thread, the supervisor runs in a worker.
Every UI call from the worker is marshalled onto the tk thread through a queue
and waits on an Event, so the blocking calls in the UI protocol (the overlay,
the probe, the evidence prompt) block the *worker*, never the event loop.
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Optional

from ..models import Contract, Intervention, SessionStatus

BG = "#12141a"
FG = "#e8eaed"
DIM = "#8b93a1"
GREEN = "#3ddc97"
AMBER = "#f5a623"
RED = "#ff5c5c"
PANEL = "#1b1e26"

BREAK_GLASS_HOLD_SECONDS = 5


# ---------------------------------------------------------------------------
# thread marshalling
# ---------------------------------------------------------------------------

class _Request:
    def __init__(self, fn: Callable[["_Request"], None]):
        self.fn = fn
        self.done = threading.Event()
        self.result: Any = None

    def finish(self, value: Any = None) -> None:
        self.result = value
        self.done.set()


class TkUI:
    """Implements the UI protocol from runtime.py, on top of tkinter."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.queue: "queue.Queue[_Request]" = queue.Queue()
        self.root: Optional[tk.Tk] = None
        self.hud_window: Optional[HudWindow] = None
        self._stopped = threading.Event()

    # -- lifecycle ---------------------------------------------------------
    def run(self, target: Callable[[], Any]) -> Any:
        """Run `target` on a worker thread while tk owns the main thread."""
        self.root = tk.Tk()
        self.root.withdraw()
        self.hud_window = HudWindow(self.root)
        result: dict[str, Any] = {}

        def worker() -> None:
            try:
                result["value"] = target()
            except BaseException as exc:  # noqa: BLE001 - surfaced after mainloop
                result["error"] = exc
            finally:
                self._stopped.set()
                try:
                    self.root.after(0, self.root.quit)  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=worker, daemon=True, name="ana-supervisor").start()
        self._pump()
        self.root.mainloop()
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def _pump(self) -> None:
        assert self.root is not None
        try:
            while True:
                request = self.queue.get_nowait()
                try:
                    request.fn(request)
                except Exception:  # noqa: BLE001 - a UI slip must not hang the worker
                    request.finish(None)
        except queue.Empty:
            pass
        self.root.after(40, self._pump)

    def _call(self, fn: Callable[[_Request], None], block: bool = True) -> Any:
        if self.root is None:            # no GUI running: behave like a no-op
            return None
        request = _Request(fn)
        self.queue.put(request)
        if not block:
            return None
        request.done.wait()
        return request.result

    # -- UI protocol -------------------------------------------------------
    def notice(self, text: str) -> None:
        self._call(lambda r: (self.hud_window.flash(text), r.finish()), block=False)

    def hud(self, snapshot: dict[str, Any]) -> None:
        self._call(lambda r: (self.hud_window.update_snapshot(snapshot), r.finish()),
                   block=False)

    def nudge(self, intervention: Intervention) -> None:
        line = intervention.body.splitlines()[0]
        self._call(lambda r: (self.hud_window.warn(line), r.finish()), block=False)

    def intervene(self, intervention: Intervention, contract: Contract,
                  validate: Callable[[str, float], tuple[bool, str]]
                  ) -> tuple[str, float, bool]:
        def build(request: _Request) -> None:
            InterruptOverlay(self.root, intervention, contract, validate, request)
        result = self._call(build)
        return result or ("", 0.0, True)

    def probe(self, question: str, seconds: float) -> tuple[str, float]:
        def build(request: _Request) -> None:
            ProbeDialog(self.root, question, seconds, request)
        return self._call(build) or ("", seconds + 1)

    def ask_evidence(self, contract: Contract) -> str:
        def build(request: _Request) -> None:
            EvidenceDialog(self.root, contract, request)
        return self._call(build) or ""

    def closing(self, status: SessionStatus, score: float, lines: list[str]) -> None:
        def build(request: _Request) -> None:
            ClosingDialog(self.root, status, score, lines, request)
        self._call(build)


# ---------------------------------------------------------------------------
# HUD — small, always on top, unignorable but not in the way
# ---------------------------------------------------------------------------

class HudWindow:
    WIDTH, HEIGHT = 300, 104

    def __init__(self, root: tk.Tk):
        self.win = tk.Toplevel(root)
        self.win.title("ana ⟶ focus")
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-alpha", 0.94)
        except tk.TclError:
            pass
        screen_w = self.win.winfo_screenwidth()
        self.win.geometry(f"{self.WIDTH}x{self.HEIGHT}+{screen_w - self.WIDTH - 24}+24")
        self.win.configure(bg=PANEL)

        self.intent = tk.Label(self.win, text="", bg=PANEL, fg=FG,
                               font=("Segoe UI", 10, "bold"), anchor="w",
                               wraplength=self.WIDTH - 24, justify="left")
        self.intent.place(x=12, y=8, width=self.WIDTH - 24)

        self.canvas = tk.Canvas(self.win, height=8, bg="#2a2f3a",
                                highlightthickness=0)
        self.canvas.place(x=12, y=46, width=self.WIDTH - 24, height=8)
        self.bar = self.canvas.create_rectangle(0, 0, 0, 8, fill=GREEN, width=0)

        self.stats = tk.Label(self.win, text="", bg=PANEL, fg=DIM,
                              font=("Consolas", 9), anchor="w")
        self.stats.place(x=12, y=60, width=self.WIDTH - 24)

        self.message = tk.Label(self.win, text="", bg=PANEL, fg=AMBER,
                                font=("Segoe UI", 9), anchor="w",
                                wraplength=self.WIDTH - 24, justify="left")
        self.message.place(x=12, y=80, width=self.WIDTH - 24)

        # draggable, because a HUD you cannot move is a HUD you will kill
        self._drag = (0, 0)
        for widget in (self.win, self.intent, self.stats, self.message):
            widget.bind("<Button-1>", self._grab)
            widget.bind("<B1-Motion>", self._drag_to)

    def _grab(self, event) -> None:
        self._drag = (event.x_root - self.win.winfo_x(),
                      event.y_root - self.win.winfo_y())

    def _drag_to(self, event) -> None:
        self.win.geometry(f"+{event.x_root - self._drag[0]}+{event.y_root - self._drag[1]}")

    def update_snapshot(self, snap: dict[str, Any]) -> None:
        self.intent.config(text=snap.get("intent", ""))
        width = max(1, self.canvas.winfo_width())
        self.canvas.coords(self.bar, 0, 0, width * float(snap.get("progress", 0)), 8)
        colour = GREEN
        if snap.get("block_minutes", 0) or snap.get("interventions", 0):
            colour = AMBER
        if snap.get("level", 0) >= 3:
            colour = RED
        self.canvas.itemconfig(self.bar, fill=colour)
        self.stats.config(text=(
            f"{snap.get('focus_minutes', 0):.0f}/{snap.get('planned_minutes', 0)}m"
            f"  grey {snap.get('grey_minutes', 0):.0f}"
            f"  blk {snap.get('block_minutes', 0):.0f}"
            f"  idle {snap.get('idle_minutes', 0):.0f}"
            + ("  PAUSED" if snap.get("paused") else "")))

    def flash(self, text: str) -> None:
        self.message.config(text=text, fg=DIM)
        self.win.after(4000, lambda: self.message.config(text=""))

    def warn(self, text: str) -> None:
        self.message.config(text="▲ " + text, fg=AMBER)


# ---------------------------------------------------------------------------
# The interrupt — full screen, topmost, and it wants an answer
# ---------------------------------------------------------------------------

class InterruptOverlay:
    def __init__(self, root: tk.Tk, intervention: Intervention, contract: Contract,
                 validate: Callable[[str, float], tuple[bool, str]],
                 request: _Request):
        self.iv = intervention
        self.contract = contract
        self.validate = validate
        self.request = request
        self.started = time.time()
        self._hold_started: Optional[float] = None
        self._finished = False

        self.win = tk.Toplevel(root)
        self.win.title("ana ⟶ interrupt")
        self.win.configure(bg=BG)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-fullscreen", True)
        except tk.TclError:
            self.win.geometry("900x600")
        self.win.protocol("WM_DELETE_WINDOW", lambda: None)   # no quiet exit
        self.win.bind("<Escape>", lambda _e: "break")

        accent = RED if self.iv.level >= 3 else AMBER
        wrap = 820

        frame = tk.Frame(self.win, bg=BG)
        frame.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(frame, text=f"L{self.iv.level}   {self.iv.headline}", bg=BG,
                 fg=accent, font=("Segoe UI", 34, "bold")).pack(anchor="w", pady=(0, 18))
        tk.Label(frame, text=self.iv.body, bg=BG, fg=FG, justify="left",
                 wraplength=wrap, font=("Segoe UI", 14)).pack(anchor="w")

        if self.iv.consequences:
            box = tk.Frame(frame, bg=PANEL)
            box.pack(anchor="w", fill="x", pady=18)
            tk.Label(box, text="This costs you", bg=PANEL, fg=DIM,
                     font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14, pady=(10, 2))
            for c in self.iv.consequences:
                tk.Label(box, text=f"·  {c}", bg=PANEL, fg=FG,
                         font=("Segoe UI", 12)).pack(anchor="w", padx=14)
            tk.Label(box, text="", bg=PANEL).pack(pady=4)

        self.prompt = tk.Label(frame, text=self._prompt_text(), bg=BG, fg=DIM,
                               font=("Segoe UI", 12), wraplength=wrap, justify="left")
        self.prompt.pack(anchor="w", pady=(10, 6))

        self.entry = tk.Text(frame, height=3 if self._is_reflect() else 1, width=70,
                             bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                             font=("Consolas", 13), wrap="word")
        self.entry.pack(anchor="w", ipady=6)
        self.entry.bind("<Return>", self._on_return)

        self.hint = tk.Label(frame, text="", bg=BG, fg=RED, font=("Segoe UI", 11))
        self.hint.pack(anchor="w", pady=(8, 0))

        row = tk.Frame(frame, bg=BG)
        row.pack(anchor="w", pady=(18, 0), fill="x")
        self.submit = tk.Button(row, text="Back to it", command=self._submit,
                                bg=accent, fg="#10121a", relief="flat",
                                font=("Segoe UI", 12, "bold"), padx=22, pady=8,
                                activebackground=accent)
        self.submit.pack(side="left")

        self.glass = tk.Button(row, text=f"hold {BREAK_GLASS_HOLD_SECONDS}s to break glass",
                               bg=BG, fg=DIM, relief="flat", font=("Segoe UI", 10),
                               activebackground=BG, activeforeground=RED)
        self.glass.pack(side="left", padx=18)
        self.glass.bind("<ButtonPress-1>", self._glass_down)
        self.glass.bind("<ButtonRelease-1>", self._glass_up)

        tk.Label(frame, text="Break-glass always works. It ends the session, and it "
                             "goes in the record as a desertion.",
                 bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(anchor="w", pady=(10, 0))

        self.countdown = tk.Label(frame, text="", bg=BG, fg=DIM,
                                  font=("Consolas", 12))
        self.countdown.pack(anchor="w", pady=(10, 0))

        if self.iv.wait_seconds:
            self._lock_for_wait()
        else:
            self.entry.focus_force()
        self.win.after(120, self._raise)

    # -- helpers -----------------------------------------------------------
    def _is_reflect(self) -> bool:
        return self.iv.challenge.value == "reflect"

    def _prompt_text(self) -> str:
        return {
            "transcribe_intent": "Type your declared intent, exactly as you wrote it:",
            "wait_then_transcribe": "Type your declared intent, exactly as you wrote it:",
            "reflect": "Answer in your own words. Vague answers will not be accepted:",
        }.get(self.iv.challenge.value, "Type 'ok' to continue:")

    def _raise(self) -> None:
        if self._finished:
            return
        try:
            self.win.lift()
            self.win.attributes("-topmost", True)
        except tk.TclError:
            return
        self.win.after(1500, self._raise)

    def _lock_for_wait(self) -> None:
        self.entry.config(state="disabled")
        self.submit.config(state="disabled")

        def tick() -> None:
            if self._finished:
                return
            remaining = self.iv.wait_seconds - (time.time() - self.started)
            if remaining <= 0:
                self.countdown.config(text="")
                self.entry.config(state="normal")
                self.submit.config(state="normal")
                self.entry.focus_force()
                return
            self.countdown.config(text=f"wait  {remaining:4.0f}s")
            self.win.after(200, tick)

        tick()

    def _on_return(self, _event) -> str:
        if not self._is_reflect():
            self._submit()
            return "break"
        return ""

    def _submit(self) -> None:
        text = self.entry.get("1.0", "end").strip()
        waited = time.time() - self.started
        ok, hint = self.validate(text, waited)
        if not ok:
            self.hint.config(text=hint)
            return
        self._close((text, waited, False))

    def _glass_down(self, _event) -> None:
        self._hold_started = time.time()
        self._glass_tick()

    def _glass_up(self, _event) -> None:
        self._hold_started = None
        self.glass.config(text=f"hold {BREAK_GLASS_HOLD_SECONDS}s to break glass")

    def _glass_tick(self) -> None:
        if self._finished or self._hold_started is None:
            return
        held = time.time() - self._hold_started
        if held >= BREAK_GLASS_HOLD_SECONDS:
            self._close(("", time.time() - self.started, True))
            return
        self.glass.config(
            text=f"keep holding… {BREAK_GLASS_HOLD_SECONDS - held:.0f}s")
        self.win.after(100, self._glass_tick)

    def _close(self, value) -> None:
        self._finished = True
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        self.request.finish(value)


# ---------------------------------------------------------------------------
# Small dialogs
# ---------------------------------------------------------------------------

class _CentredDialog:
    def __init__(self, root: tk.Tk, title: str, width: int = 620, height: int = 260):
        self.win = tk.Toplevel(root)
        self.win.title(title)
        self.win.configure(bg=BG)
        self.win.attributes("-topmost", True)
        self.win.protocol("WM_DELETE_WINDOW", lambda: None)
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"{width}x{height}+{(sw - width) // 2}+{(sh - height) // 3}")


class ProbeDialog(_CentredDialog):
    """15 seconds to say what you are doing. Silence is an answer too."""

    def __init__(self, root: tk.Tk, question: str, seconds: float, request: _Request):
        super().__init__(root, "ana ⟶ check", 620, 200)
        self.request = request
        self.started = time.time()
        self.limit = seconds
        self._finished = False

        tk.Label(self.win, text=question, bg=BG, fg=FG, font=("Segoe UI", 15, "bold"),
                 wraplength=560, justify="left").pack(anchor="w", padx=28, pady=(26, 6))
        tk.Label(self.win, text="No answer counts as a failed check.", bg=BG, fg=DIM,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=28)
        self.entry = tk.Entry(self.win, bg=PANEL, fg=FG, insertbackground=FG,
                              relief="flat", font=("Consolas", 13))
        self.entry.pack(fill="x", padx=28, pady=14, ipady=7)
        self.entry.bind("<Return>", lambda _e: self._submit())
        self.entry.focus_force()
        self.timer = tk.Label(self.win, text="", bg=BG, fg=AMBER, font=("Consolas", 12))
        self.timer.pack(anchor="w", padx=28)
        self._tick()

    def _tick(self) -> None:
        if self._finished:
            return
        remaining = self.limit - (time.time() - self.started)
        if remaining <= 0:
            self._submit(timed_out=True)
            return
        self.timer.config(text=f"{remaining:4.0f}s")
        self.win.after(200, self._tick)

    def _submit(self, timed_out: bool = False) -> None:
        self._finished = True
        text = "" if timed_out else self.entry.get().strip()
        took = time.time() - self.started
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        self.request.finish((text, took))


class EvidenceDialog(_CentredDialog):
    """E5: what exists now that didn't before?"""

    def __init__(self, root: tk.Tk, contract: Contract, request: _Request):
        super().__init__(root, "ana ⟶ evidence", 680, 330)
        self.request = request
        tk.Label(self.win, text="What exists now that didn't when you started?",
                 bg=BG, fg=FG, font=("Segoe UI", 16, "bold"), wraplength=600,
                 justify="left").pack(anchor="w", padx=28, pady=(26, 4))
        tk.Label(self.win, text=f"You signed: {contract.done_criteria}", bg=BG, fg=DIM,
                 font=("Segoe UI", 11), wraplength=600, justify="left"
                 ).pack(anchor="w", padx=28)
        tk.Label(self.win, text="An empty answer marks this session UNVERIFIED. "
                               "It will not count toward your streak.",
                 bg=BG, fg=AMBER, font=("Segoe UI", 10), wraplength=600,
                 justify="left").pack(anchor="w", padx=28, pady=(8, 0))
        self.entry = tk.Text(self.win, height=4, bg=PANEL, fg=FG, insertbackground=FG,
                             relief="flat", font=("Consolas", 12), wrap="word")
        self.entry.pack(fill="x", padx=28, pady=16, ipady=4)
        self.entry.focus_force()
        tk.Button(self.win, text="Close the session", command=self._submit,
                  bg=GREEN, fg="#10121a", relief="flat",
                  font=("Segoe UI", 12, "bold"), padx=20, pady=7).pack(anchor="e",
                                                                       padx=28)

    def _submit(self) -> None:
        text = self.entry.get("1.0", "end").strip()
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        self.request.finish(text)


class ClosingDialog(_CentredDialog):
    def __init__(self, root: tk.Tk, status: SessionStatus, score: float,
                 lines: list[str], request: _Request):
        super().__init__(root, "ana ⟶ result", 620, 300)
        colour = GREEN if status is SessionStatus.COMPLETE else RED
        tk.Label(self.win, text=f"{status.value.upper()}", bg=BG, fg=colour,
                 font=("Segoe UI", 26, "bold")).pack(anchor="w", padx=28, pady=(24, 0))
        tk.Label(self.win, text=f"score {score}", bg=BG, fg=DIM,
                 font=("Segoe UI", 13)).pack(anchor="w", padx=28)
        for line in lines:
            tk.Label(self.win, text=line, bg=BG, fg=FG, font=("Segoe UI", 11),
                     wraplength=560, justify="left").pack(anchor="w", padx=28, pady=2)
        tk.Button(self.win, text="Done", command=lambda: self._close(request),
                  bg=PANEL, fg=FG, relief="flat", font=("Segoe UI", 11),
                  padx=18, pady=6).pack(anchor="e", padx=28, pady=14)

    def _close(self, request: _Request) -> None:
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        request.finish(None)


# ---------------------------------------------------------------------------
# Dashboard — `ana gui`
# ---------------------------------------------------------------------------

class Dashboard:
    def __init__(self) -> None:
        from .. import accountability, config, report
        from ..ledger import open_ledger
        from ..storage import day_key, open_store

        self.cfg = config.load()
        self.store = open_store()
        self.ledger = open_ledger()
        self.accountability = accountability
        self.report = report
        self.day_key = day_key

        self.root = tk.Tk()
        self.root.title("ana ⟶ dashboard")
        self.root.configure(bg=BG)
        self.root.geometry("980x680")

        self._build()
        self.refresh()

    def _build(self) -> None:
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=24, pady=(20, 6))
        tk.Label(header, text="ana", bg=BG, fg=FG,
                 font=("Segoe UI", 24, "bold")).pack(side="left")
        self.standing_label = tk.Label(header, text="", bg=BG, fg=DIM,
                                       font=("Consolas", 12))
        self.standing_label.pack(side="left", padx=18)
        tk.Button(header, text="Refresh", command=self.refresh, bg=PANEL, fg=FG,
                  relief="flat", padx=14, pady=5).pack(side="right")

        self.breach = tk.Label(self.root, text="", bg=RED, fg="#10121a",
                               font=("Segoe UI", 11, "bold"))

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=24, pady=16)

        self.start_tab = tk.Frame(notebook, bg=BG)
        self.today_tab = tk.Frame(notebook, bg=BG)
        self.record_tab = tk.Frame(notebook, bg=BG)
        notebook.add(self.start_tab, text="Start a session")
        notebook.add(self.today_tab, text="Today")
        notebook.add(self.record_tab, text="The record")

        self._build_start(self.start_tab)
        self.today_text = self._text_area(self.today_tab)
        self.record_text = self._text_area(self.record_tab)

    def _text_area(self, parent: tk.Frame) -> tk.Text:
        text = tk.Text(parent, bg=PANEL, fg=FG, relief="flat", wrap="word",
                       font=("Consolas", 11), insertbackground=FG)
        text.pack(fill="both", expand=True, padx=8, pady=8)
        return text

    def _build_start(self, parent: tk.Frame) -> None:
        self.fields: dict[str, tk.Entry] = {}
        rows = [("intent", "What exactly will you do?", ""),
                ("done", "Done when?", ""),
                ("minutes", "Minutes", "50"),
                ("stake", "Stake (debt:30 | recreation:45 | pledge:…)", "debt:15"),
                ("project", "Project folder (optional, for evidence)", "")]
        for key, label, default in rows:
            tk.Label(parent, text=label, bg=BG, fg=DIM,
                     font=("Segoe UI", 10)).pack(anchor="w", padx=14, pady=(12, 2))
            entry = tk.Entry(parent, bg=PANEL, fg=FG, insertbackground=FG,
                             relief="flat", font=("Consolas", 12))
            entry.insert(0, default)
            entry.pack(fill="x", padx=14, ipady=6)
            self.fields[key] = entry

        self.start_hint = tk.Label(parent, text="", bg=BG, fg=AMBER,
                                   font=("Segoe UI", 10), justify="left",
                                   wraplength=880)
        self.start_hint.pack(anchor="w", padx=14, pady=10)
        tk.Button(parent, text="Sign and start", command=self._start,
                  bg=GREEN, fg="#10121a", relief="flat",
                  font=("Segoe UI", 12, "bold"), padx=20,
                  pady=8).pack(anchor="w", padx=14, pady=6)

    def _start(self) -> None:
        from ..accountability import ContractRejected, sign_contract
        from ..cli import _parse_stake
        from ..runtime import Supervisor

        try:
            minutes = int(self.fields["minutes"].get() or 50)
        except ValueError:
            self.start_hint.config(text="minutes must be a number")
            return
        try:
            contract = sign_contract(
                self.fields["intent"].get(), self.fields["done"].get(), minutes,
                self.cfg.get("user") or "you", self.cfg,
                stake=_parse_stake(self.fields["stake"].get() or "debt:15"))
        except ContractRejected as exc:
            self.start_hint.config(
                text="This contract is not signable:\n· "
                     + "\n· ".join(exc.problems)
                     + "\nA goal you cannot fail is not a goal.")
            return

        project = self.fields["project"].get() or None
        self.root.destroy()
        ui = TkUI(self.cfg)
        sup = Supervisor(ui, self.cfg, self.store, self.ledger)
        notes = sup.preflight()
        if notes:
            print("\n".join(f"  ! {n}" for n in notes))
        ui.run(lambda: sup.run_session(contract, project_path=project))

    def refresh(self) -> None:
        st = self.accountability.standing(self.store, self.cfg, self.ledger)
        self.standing_label.config(text=st.headline())
        if not st.integrity_ok:
            self.breach.config(text="  " + st.integrity_note +
                                    "  —  run `ana explain`  ")
            self.breach.pack(fill="x", padx=24, pady=4)
        else:
            self.breach.pack_forget()

        today = self.report.daily_report(self.store, self.cfg, self.ledger)
        self.today_text.delete("1.0", "end")
        self.today_text.insert("1.0", today)

        lines = []
        for entry in self.ledger.tail(80):
            when = time.strftime("%m-%d %H:%M", time.localtime(entry.ts))
            lines.append(f"#{entry.seq:<4} {when}  {entry.kind:<20} {entry.summary}")
        lines.append("")
        lines.append(self.ledger.verify().describe())
        self.record_text.delete("1.0", "end")
        self.record_text.insert("1.0", "\n".join(lines))

    def run(self) -> int:
        self.root.mainloop()
        return 0


def run_dashboard() -> int:
    return Dashboard().run()
