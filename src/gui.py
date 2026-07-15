"""Native-tkinter GUI for the screen-automation AI assistant.

The UI uses a CustomTkinter-compatible shim (``ctk_compat``) built on classic
tkinter/ttk widgets.  CustomTkinter draws everything on a tkinter.Canvas which
does not paint reliably over remote-desktop sessions (RDP / AnyDesk); native
widgets do, so the shim keeps the same API while rendering everywhere.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Optional

from . import ctk_compat as ctk
from PIL import Image

from . import ai_client, automation, screen, workflow
from .config import AppConfig, HISTORY_PATH, Point, Region
from .workflow import STEP_KINDS, RunContext, RunRecord, Step, WorkflowRunner

PREVIEW_MAX = (460, 300)

COMMON_KEYS = [
    "enter", "tab", "esc", "space", "backspace", "delete",
    "up", "down", "left", "right",
    "home", "end", "pageup", "pagedown",
    "ctrl+a", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+s", "ctrl+f",
    "alt+tab", "f5",
]


class _StepRow:
    """Widget handles for one workflow step row (avoids full list rebuilds)."""

    __slots__ = ("frame", "pos_var", "toggle_btn", "summary_label")

    def __init__(
        self,
        frame: ctk.CTkFrame,
        pos_var: ctk.StringVar,
        toggle_btn: ctk.CTkButton,
        summary_label: ctk.CTkLabel,
    ) -> None:
        self.frame = frame
        self.pos_var = pos_var
        self.toggle_btn = toggle_btn
        self.summary_label = summary_label


class _StepListController:
    """Editable step list shared by the Workflow and Restart tabs."""

    def __init__(
        self,
        app: "App",
        scroll_frame: ctk.CTkScrollableFrame,
        steps: list[Step],
        *,
        empty_text: str,
        on_run_from,
        on_run_only,
    ) -> None:
        self.app = app
        self.frame = scroll_frame
        self.steps = steps
        self.empty_text = empty_text
        self.on_run_from = on_run_from
        self.on_run_only = on_run_only
        self.rows: list[_StepRow] = []

    def refresh(self) -> None:
        self.rows.clear()
        for child in self.frame.winfo_children():
            child.destroy()

        if not self.steps:
            ctk.CTkLabel(
                self.frame, text=self.empty_text,
                text_color="gray", justify="left",
            ).grid(row=0, column=0, sticky="w", padx=12, pady=20)
            return

        # Build rows in small batches so the event loop stays responsive.
        # With 100+ steps (≈900 widgets), a synchronous build freezes the UI.
        self._build_batch(0)

    _BATCH_SIZE = 12   # rows to create per event-loop tick

    def _build_batch(self, start: int) -> None:
        try:
            if not self.frame.winfo_exists():
                return
            end = min(start + self._BATCH_SIZE, len(self.steps))
            for i in range(start, end):
                self.rows.append(self._create_row(i, self.steps[i]))
            if end < len(self.steps):
                self.frame.after(0, lambda s=end: self._build_batch(s))
        except Exception:
            pass

    def _create_row(self, index: int, step: Step) -> _StepRow:
        row = ctk.CTkFrame(self.frame)
        row.grid(row=index, column=0, sticky="ew", padx=4, pady=3)
        row.grid_columnconfigure(2, weight=1)

        pos_var = ctk.StringVar(value=str(index + 1))
        pos_entry = ctk.CTkEntry(row, width=42, textvariable=pos_var, justify="center")
        pos_entry.grid(row=0, column=0, padx=(8, 2), pady=6)
        pos_entry.bind(
            "<Return>",
            lambda _e, idx=index, v=pos_var: self.move_to_position(idx, v.get()),
        )
        pos_entry.bind("<FocusIn>", lambda _e, w=pos_entry: w.select_range(0, "end"))

        toggle_btn = ctk.CTkButton(
            row,
            text="On" if step.enabled else "Off",
            width=44,
            fg_color="#2f6f43" if step.enabled else "#5a5a5a",
            hover_color="#27583a" if step.enabled else "#4a4a4a",
            command=lambda idx=index: self.toggle(idx),
        )
        toggle_btn.grid(row=0, column=1, padx=2)

        text, text_color = App._step_row_summary(step)
        summary_label = ctk.CTkLabel(
            row, text=text, anchor="w", text_color=text_color,
            font=("Segoe UI", 12),
        )
        summary_label.grid(row=0, column=2, sticky="ew", padx=6)

        ctk.CTkButton(
            row, text="▶ here", width=56, fg_color="#2f6f43", hover_color="#27583a",
            command=lambda idx=index: self.on_run_from(idx),
        ).grid(row=0, column=3, padx=(2, 1), pady=4)
        ctk.CTkButton(
            row, text="▶ one", width=52, fg_color="#2f6f43", hover_color="#27583a",
            command=lambda idx=index: self.on_run_only(idx),
        ).grid(row=0, column=4, padx=(1, 6))

        ctk.CTkButton(row, text="✎", width=30, command=lambda idx=index: self.edit(idx)).grid(
            row=0, column=5, padx=2, pady=4)
        ctk.CTkButton(row, text="▲", width=30, command=lambda idx=index: self.move(idx, -1)).grid(
            row=0, column=6, padx=2)
        ctk.CTkButton(row, text="▼", width=30, command=lambda idx=index: self.move(idx, 1)).grid(
            row=0, column=7, padx=2)
        ctk.CTkButton(
            row, text="✕", width=30, fg_color="#a13c3c", hover_color="#7d2e2e",
            command=lambda idx=index: self.delete(idx),
        ).grid(row=0, column=8, padx=(2, 8))

        return _StepRow(row, pos_var, toggle_btn, summary_label)

    def update_row(self, index: int) -> None:
        if index >= len(self.rows) or index >= len(self.steps):
            self.refresh()
            return
        step = self.steps[index]
        widgets = self.rows[index]
        widgets.pos_var.set(str(index + 1))
        widgets.toggle_btn.configure(
            text="On" if step.enabled else "Off",
            fg_color="#2f6f43" if step.enabled else "#5a5a5a",
            hover_color="#27583a" if step.enabled else "#4a4a4a",
        )
        text, text_color = App._step_row_summary(step)
        widgets.summary_label.configure(text=text, text_color=text_color)

    def sync_positions(self) -> None:
        for i, widgets in enumerate(self.rows):
            if i < len(self.steps):
                widgets.pos_var.set(str(i + 1))

    def add_by_kind(self, kind: str) -> None:
        """Add a new step of the given kind (called from the picker popup)."""
        step = Step(kind=kind)
        if kind in ("move", "ai_paste_macro", "image_paste"):
            step.use_point = True
        StepEditor(self.app, step, on_save=self.append)

    def on_add(self, label: str, menu=None) -> None:
        kind = None
        for k, name in STEP_KINDS.items():
            if name == label:
                kind = k
                break
        menu.set("➕  Add step…")
        if kind is None:
            return
        step = Step(kind=kind)
        if kind in ("move", "ai_paste_macro", "image_paste"):
            step.use_point = True
        StepEditor(self.app, step, on_save=lambda s: self.append(s))

    def append(self, step: Step) -> None:
        self.steps.append(step)
        if not self.rows:
            self.refresh()
        else:
            index = len(self.steps) - 1
            self.rows.append(self._create_row(index, step))

    def edit(self, index: int) -> None:
        step = self.steps[index]
        StepEditor(self.app, step, on_save=lambda s, idx=index: self.replace(idx, s))

    def replace(self, index: int, step: Step) -> None:
        self.steps[index] = step
        self.update_row(index)

    def delete(self, index: int) -> None:
        del self.steps[index]
        self.refresh()

    def move(self, index: int, delta: int) -> None:
        new = index + delta
        if 0 <= new < len(self.steps):
            self.steps[index], self.steps[new] = self.steps[new], self.steps[index]
            self.refresh()

    def move_to(self, index: int, target: int) -> None:
        if index == target or not (0 <= target < len(self.steps)):
            return
        step = self.steps.pop(index)
        self.steps.insert(target, step)
        self.refresh()

    def move_to_position(self, index: int, value: str) -> None:
        try:
            target = int(float(value)) - 1
        except (ValueError, TypeError):
            self.sync_positions()
            return
        target = max(0, min(len(self.steps) - 1, target))
        if target == index:
            self.sync_positions()
        else:
            self.move_to(index, target)

    def toggle(self, index: int) -> None:
        self.steps[index].enabled = not self.steps[index].enabled
        self.update_row(index)


def _open_picker(
    parent: ctk.CTk,
    title: str,
    options: list[str],
    callback,
    *,
    current: str = "",
    width: int = 300,
    height: int = 380,
) -> None:
    """Open a floating list-picker that calls callback(selected) on pick.

    Uses only CTkButton widgets — no tkinter.Menu / HMENU handles at all.
    """
    popup = ctk.CTkToplevel(parent)
    popup.title(title)
    popup.geometry(f"{width}x{height}")
    popup.resizable(False, True)
    # -toolwindow removes the system-menu icon, avoiding HMENU allocation on RDP
    try:
        popup.attributes("-toolwindow", True)
    except Exception:
        pass

    # Defer the grab until the window is actually viewable. Calling grab_set()
    # on a not-yet-mapped Toplevel can raise "grab failed: window not viewable"
    # or leave a dangling global input grab — which over RDP shows up as the
    # app "locking up" whenever a picker window is created.
    def _activate() -> None:
        try:
            popup.grab_set()
            popup.lift()
            popup.focus_force()
        except Exception:
            pass

    popup.after(80, _activate)

    sf = ctk.CTkScrollableFrame(popup, label_text="")
    sf.pack(fill="both", expand=True, padx=8, pady=8)
    sf.grid_columnconfigure(0, weight=1)

    for i, opt in enumerate(options):
        is_cur = opt == current
        ctk.CTkButton(
            sf, text=opt, anchor="w",
            fg_color="#2f6f43" if is_cur else ("gray25" if not is_cur else None),
            hover_color="#27583a" if is_cur else "gray35",
            command=lambda o=opt: (popup.destroy(), callback(o)),
        ).grid(row=i, column=0, sticky="ew", pady=2, padx=2)


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        self.cfg = AppConfig.load()
        ctk.set_appearance_mode("Dark")

        automation.set_directinput(self.cfg.use_directinput)
        automation.set_typos(self.cfg.humanize_typos)
        automation.set_failsafe(not self.cfg.disable_failsafe)

        self.title("Screen AI Assistant")
        self.geometry("960x720")
        self.minsize(820, 620)

        self._last_image: Optional[Image.Image] = None
        self._last_answer: str = ""
        self._loop_stop = threading.Event()
        self._loop_thread: Optional[threading.Thread] = None
        self._busy = False

        # Workflow state
        self._steps: list[Step] = [Step.from_dict(d) for d in self.cfg.steps]
        self._restart_steps: list[Step] = [Step.from_dict(d) for d in self.cfg.restart_steps]
        self._wf_stop = threading.Event()
        self._wf_thread: Optional[threading.Thread] = None
        self._run_history: list[RunRecord] = self._load_history()
        self._log_boxes: list[ctk.CTkTextbox] = []

        # Test-tab state
        self._test_step: Optional[Step] = None
        self._test_stop = threading.Event()
        self._test_thread: Optional[threading.Thread] = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(self)
        self.tabs.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        self.tab_run = self.tabs.add("Test")
        self.tab_workflow = self.tabs.add("Workflow")
        self.tab_restart = self.tabs.add("Restart")
        self.tab_history = self.tabs.add("History")
        self.tab_settings = self.tabs.add("Settings")

        self._build_run_tab()
        self._build_workflow_tab()
        self._build_restart_tab()
        self._build_history_tab()
        self._build_settings_tab()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Suppress Tk's built-in right-click context menus (they can create
        # native menu windows that crash over RDP).
        try:
            for cls in ("Text", "Listbox", "Entry", "Scrollbar", "Canvas"):
                self.tk.eval(f"bind {cls} <Button-3> {{break}}")
                self.tk.eval(f"bind {cls} <Button-2> {{break}}")
            self.bind_all("<Button-3>", lambda e: "break")
        except Exception:
            pass

        self.log("Ready. Use the Test tab to try steps, then build a Workflow.")

    # ------------------------------------------------------------------
    # Test tab — try one step at a time before building a workflow
    # ------------------------------------------------------------------
    def _build_run_tab(self) -> None:
        tab = self.tab_run
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_rowconfigure(3, weight=1)

        # --- Capture area (top-left) ---
        cap = ctk.CTkFrame(tab)
        cap.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=6)
        cap.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(cap, text="Capture area", font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 4)
        )
        self.full_screen_var = ctk.BooleanVar(value=self.cfg.region.full_screen)
        ctk.CTkCheckBox(
            cap, text="Use full screen", variable=self.full_screen_var,
            command=self._on_fullscreen_toggle,
        ).grid(row=1, column=0, sticky="w", padx=10, pady=2)
        ctk.CTkButton(cap, text="Select region…", command=self._on_select_region).grid(
            row=2, column=0, sticky="ew", padx=10, pady=2
        )
        self.region_label = ctk.CTkLabel(cap, text=self._region_text(), text_color="gray")
        self.region_label.grid(row=3, column=0, sticky="w", padx=10, pady=(0, 8))
        ctk.CTkButton(cap, text="Capture now (preview)", command=self._on_capture).grid(
            row=4, column=0, sticky="ew", padx=10, pady=(0, 10)
        )

        # --- Global AI prompt (top-right) ---
        pr = ctk.CTkFrame(tab)
        pr.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=6)
        pr.grid_columnconfigure(0, weight=1)
        pr.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(pr, text="Default AI instruction", font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 4)
        )
        self.prompt_box = ctk.CTkTextbox(pr, height=90, wrap="word")
        self.prompt_box.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.prompt_box.insert("1.0", self.cfg.prompt)

        # --- Test a single step ---
        ts = ctk.CTkFrame(tab)
        ts.grid(row=1, column=0, columnspan=2, sticky="ew", pady=6)
        ts.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(ts, text="Test a step", font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=10, pady=(10, 4)
        )
        self._test_kind_btn = ctk.CTkButton(
            ts, text="Pick a step type…", width=200,
            command=self._open_test_kind_picker,
        )
        self._test_kind_btn.grid(row=1, column=0, padx=(10, 6), pady=(0, 10))
        self.test_step_label = ctk.CTkLabel(ts, text="No step configured.", text_color="gray", anchor="w")
        self.test_step_label.grid(row=1, column=1, sticky="ew", padx=6, pady=(0, 10))
        self.test_edit_btn = ctk.CTkButton(ts, text="✎ Edit", width=70, command=self._edit_test_step, state="disabled")
        self.test_edit_btn.grid(row=1, column=2, padx=4, pady=(0, 10))
        self.test_run_btn = ctk.CTkButton(ts, text="▶ Test this step", width=130, command=self._run_test_step, state="disabled")
        self.test_run_btn.grid(row=1, column=3, padx=4, pady=(0, 10))
        self.test_stop_btn = ctk.CTkButton(
            ts, text="⏹ Stop", width=70, command=self._stop_test,
            fg_color="#a13c3c", hover_color="#7d2e2e",
        )
        self.test_stop_btn.grid(row=1, column=4, padx=(4, 10), pady=(0, 10))

        ctk.CTkLabel(
            ts, text="Tip: configure a step and run it once to confirm it works "
                     "(click lands, key is sent, AI reads correctly) before adding "
                     "it to your workflow.",
            text_color="gray", wraplength=820, justify="left",
        ).grid(row=2, column=0, columnspan=5, sticky="w", padx=10, pady=(0, 8))

        # --- Preview + answer + log ---
        bottom = ctk.CTkFrame(tab, fg_color="transparent")
        bottom.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        bottom.grid_columnconfigure(0, weight=1)
        bottom.grid_columnconfigure(1, weight=1)
        bottom.grid_rowconfigure(0, weight=1)

        prev = ctk.CTkFrame(bottom)
        prev.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        prev.grid_columnconfigure(0, weight=1)
        prev.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(prev, text="Preview", font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, sticky="w", padx=10, pady=(8, 2)
        )
        self.preview_label = ctk.CTkLabel(prev, text="(no capture yet)", text_color="gray")
        self.preview_label.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)

        right = ctk.CTkFrame(bottom)
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        right.grid_rowconfigure(3, weight=1)
        ctk.CTkLabel(right, text="AI answer", font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, sticky="w", padx=10, pady=(8, 2)
        )
        self.answer_box = ctk.CTkTextbox(right, height=80, wrap="word")
        self.answer_box.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 6))
        ctk.CTkLabel(right, text="Log", font=("Segoe UI", 12, "bold")).grid(
            row=2, column=0, sticky="w", padx=10, pady=(4, 2)
        )
        self.log_box = ctk.CTkTextbox(right, height=120, wrap="word")
        self.log_box.grid(row=3, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.log_box.configure(state="disabled")
        self._log_boxes.append(self.log_box)

    # ------------------------------------------------------------------
    # Workflow tab
    # ------------------------------------------------------------------
    def _build_workflow_tab(self) -> None:
        tab = self.tab_workflow
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)   # step list expands

        # --- Toolbar (row 0): workflow selector + run controls ---
        bar = ctk.CTkFrame(tab)
        bar.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 4))
        bar.grid_columnconfigure(11, weight=1)  # spacer column pushes Save/Stop right

        # Workflow picker cluster (left)
        ctk.CTkLabel(bar, text="Workflow:", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, padx=(8, 2), pady=6)
        self._wf_btn = ctk.CTkButton(
            bar, text=self.cfg.active_workflow, width=130,
            command=self._open_wf_picker,
        )
        self._wf_btn.grid(row=0, column=1, padx=2, pady=6)
        ctk.CTkButton(bar, text="＋", width=30, command=self._new_workflow).grid(
            row=0, column=2, padx=1, pady=6)
        ctk.CTkButton(bar, text="✎", width=30, command=self._rename_workflow).grid(
            row=0, column=3, padx=1, pady=6)
        ctk.CTkButton(bar, text="📋", width=30, command=self._duplicate_workflow).grid(
            row=0, column=4, padx=1, pady=6)
        ctk.CTkButton(bar, text="✕", width=30, fg_color="#a13c3c", hover_color="#7d2e2e",
                      command=self._delete_workflow).grid(row=0, column=5, padx=(1, 4), pady=6)
        ctk.CTkLabel(bar, text="|", text_color="gray").grid(row=0, column=6, padx=4)

        # Add step + run cluster (middle)
        ctk.CTkButton(
            bar, text="➕ Add step", width=110,
            command=lambda: self._open_step_picker(self._wf_list.add_by_kind),
        ).grid(row=0, column=7, padx=4, pady=6)

        self.wf_run_btn = ctk.CTkButton(
            bar, text="▶ Run workflow", command=self._run_workflow, width=120)
        self.wf_run_btn.grid(row=0, column=8, padx=4, pady=6)

        ctk.CTkLabel(bar, text="×").grid(row=0, column=9, padx=(8, 2))
        self.repeat_var = ctk.StringVar(value=str(self.cfg.workflow_repeat))
        self.repeat_entry = ctk.CTkEntry(bar, textvariable=self.repeat_var, width=44)
        self.repeat_entry.grid(row=0, column=10, padx=2)
        self.repeat_entry.bind("<FocusOut>", lambda _e: self._persist_workflow_repeat())
        self.repeat_entry.bind("<Return>", lambda _e: self._persist_workflow_repeat())

        # Save / Stop (right cluster)
        ctk.CTkButton(bar, text="💾 Save", command=self._save_workflow, width=70).grid(
            row=0, column=12, padx=(4, 4), pady=6)
        self.wf_stop_btn = ctk.CTkButton(
            bar, text="⏹ Stop", command=self._stop_workflow, width=72,
            fg_color="#a13c3c", hover_color="#7d2e2e",
        )
        self.wf_stop_btn.grid(row=0, column=13, padx=(2, 8), pady=6)

        # --- Step list (row 1, expands) ---
        self.steps_frame = ctk.CTkScrollableFrame(
            tab, label_text="Steps  (tip: type a new number in a step's box + Enter to move it)")
        self.steps_frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self.steps_frame.grid_columnconfigure(0, weight=1)

        self._wf_list = _StepListController(
            self, self.steps_frame, self._steps,
            empty_text=(
                "No steps yet. Use “Add step…” to build your sequence:\n"
                "e.g. Click → Wait → Capture + ask AI → Type AI answer → Press Enter."
            ),
            on_run_from=self._run_workflow_from,
            on_run_only=self._run_only_step,
        )

        # --- Workflow log (row 2) ---
        logf = ctk.CTkFrame(tab)
        logf.grid(row=2, column=0, sticky="ew", padx=4, pady=(6, 4))
        logf.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(logf, text="Log", font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, sticky="w", padx=8, pady=(6, 0)
        )
        wf_log = ctk.CTkTextbox(logf, height=110, wrap="word")
        wf_log.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 8))
        wf_log.configure(state="disabled")
        self._log_boxes.append(wf_log)
        # Rows built shortly after startup so the toolbar always appears first
        self.after(50, self._wf_list.refresh)

    def _build_restart_tab(self) -> None:
        tab = self.tab_restart
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(tab)
        bar.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 6))
        bar.grid_columnconfigure(4, weight=1)

        ctk.CTkButton(
            bar, text="➕  Add step…", width=170,
            command=lambda: self._open_step_picker(self._restart_list.add_by_kind),
        ).grid(row=0, column=0, padx=(8, 6), pady=8)

        self.restart_run_btn = ctk.CTkButton(
            bar, text="▶  Run restart", command=self._run_restart_workflow, width=130,
        )
        self.restart_run_btn.grid(row=0, column=1, padx=4, pady=8)

        ctk.CTkButton(bar, text="💾 Save", command=self._save_restart_workflow, width=70).grid(
            row=0, column=2, padx=(4, 4), pady=8,
        )

        ctk.CTkLabel(
            bar,
            text="Runs when an AI check fails with “Run restart workflow” enabled, "
                 "then the main workflow starts again from step 1 (unlimited retries).",
            text_color="gray", wraplength=520, justify="left",
        ).grid(row=0, column=3, columnspan=2, sticky="w", padx=(12, 8), pady=8)

        self.restart_steps_frame = ctk.CTkScrollableFrame(
            tab,
            label_text="Restart steps  (recovery actions before the main workflow loops again)",
        )
        self.restart_steps_frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self.restart_steps_frame.grid_columnconfigure(0, weight=1)

        self._restart_list = _StepListController(
            self, self.restart_steps_frame, self._restart_steps,
            empty_text=(
                "No restart steps yet. Add steps here to recover when an AI check fails\n"
                "(e.g. close a dialog, navigate back, click Retry)."
            ),
            on_run_from=self._run_restart_from,
            on_run_only=self._run_restart_only,
        )
        self.after(80, self._restart_list.refresh)

    @staticmethod
    def _step_row_summary(step: Step) -> tuple[str, str | tuple[str, str]]:
        if step.enabled:
            return step.summary(), ctk.ThemeManager.theme["CTkLabel"]["text_color"]
        return f"⊘ {step.summary()}  (disabled)", "gray"

    # ------------------------------------------------------------------
    # History tab
    # ------------------------------------------------------------------
    @staticmethod
    def _load_history() -> list[RunRecord]:
        try:
            if HISTORY_PATH.exists():
                import json as _json
                raw = _json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
                return [RunRecord(**d) for d in raw if isinstance(d, dict)]
        except Exception:  # noqa: BLE001
            pass
        return []

    def _save_history(self) -> None:
        try:
            import json as _json
            HISTORY_PATH.write_text(
                _json.dumps([r.as_dict() for r in self._run_history], indent=2),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass

    def _build_history_tab(self) -> None:
        tab = self.tab_history
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        # --- Toolbar ---
        bar = ctk.CTkFrame(tab)
        bar.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 6))
        bar.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(bar, text="Workflow run history",
                     font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=8)
        ctk.CTkButton(bar, text="🗑 Clear", width=80,
                      command=self._clear_history).grid(
            row=0, column=3, padx=(4, 8), pady=8)

        # --- Header row ---
        hdr = ctk.CTkFrame(tab, fg_color="#1a3a52")
        hdr.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 1))
        for col, (label, w) in enumerate([
            ("#", 36), ("Started", 140), ("Type", 170),
            ("Steps", 52), ("Pass", 70), ("Restarts", 64),
            ("Duration", 76), ("Status", 80),
        ]):
            ctk.CTkLabel(hdr, text=label, font=("Segoe UI", 11, "bold"),
                         text_color="#ffffff", width=w, anchor="w").grid(
                row=0, column=col, padx=(6 if col == 0 else 2, 2),
                pady=6, sticky="w")

        # --- Scrollable body ---
        self.history_frame = ctk.CTkScrollableFrame(tab, label_text="")
        self.history_frame.grid(row=2, column=0, sticky="nsew", padx=4, pady=(0, 4))
        self._history_col_widths = [36, 140, 170, 52, 70, 64, 76, 80]
        self._history_row_count = 0

        # Render rows in small batches after startup. Building hundreds of rows
        # (each ~10 widgets) synchronously here freezes the whole app on launch.
        self.after(60, lambda: self._render_history_batch(0))

    _HISTORY_BATCH = 25

    def _render_history_batch(self, start: int) -> None:
        try:
            if not self.history_frame.winfo_exists():
                return
        except Exception:
            return
        end = min(start + self._HISTORY_BATCH, len(self._run_history))
        for i in range(start, end):
            self._render_history_row(self._run_history[i])
        if end < len(self._run_history):
            self.after(0, lambda: self._render_history_batch(end))

    _STATUS_COLOR = {
        "Finished": "#4caf78",
        "Stopped":  "#e0b030",
        "Failed":   "#e05555",
    }

    def _render_history_row(self, record: workflow.RunRecord) -> None:
        i = self._history_row_count
        self._history_row_count += 1
        bg = "#2e2e2e" if i % 2 == 0 else "#3a3a3a"
        row = ctk.CTkFrame(self.history_frame, fg_color=bg, corner_radius=4)
        row.grid(row=i, column=0, sticky="ew", padx=2, pady=2)

        duration_str = (
            f"{int(record.duration_s // 60)}m {int(record.duration_s % 60)}s"
            if record.duration_s >= 60 else f"{record.duration_s:.1f}s"
        )
        status_color = self._STATUS_COLOR.get(record.status, "#aaaaaa")
        pass_label = (f"{record.pass_number}/{record.total_passes}"
                      if record.total_passes > 1 else "1")
        values = [
            str(i + 1),
            record.started_at,
            record.run_type,
            str(record.total_steps),
            pass_label,
            str(record.restarts),
            duration_str,
            record.status,
        ]
        STATUS_COL = 7
        for col, (val, w) in enumerate(zip(values, self._history_col_widths)):
            is_status = col == STATUS_COL
            color = status_color if is_status else "#e0e0e0"
            font = ("Segoe UI", 11, "bold") if is_status else ("Segoe UI", 11)
            ctk.CTkLabel(row, text=val, width=w, anchor="w",
                         font=font, text_color=color).grid(
                row=0, column=col,
                padx=(6 if col == 0 else 2, 2), pady=5, sticky="w")

        if record.fail_reason:
            ctk.CTkLabel(row, text=f"  ⚠ {record.fail_reason}",
                         text_color="#e09040", font=("Segoe UI", 10),
                         anchor="w").grid(
                row=1, column=0, columnspan=8,
                sticky="ew", padx=8, pady=(0, 4))

    def _add_history_record(self, record: workflow.RunRecord) -> None:
        self._run_history.append(record)
        self._render_history_row(record)
        self._save_history()

    def _clear_history(self) -> None:
        self._run_history.clear()
        self._history_row_count = 0
        for child in self.history_frame.winfo_children():
            child.destroy()
        self._save_history()

    # -- run / stop ----------------------------------------------------
    def _collect_workflow_repeat(self) -> None:
        try:
            self.cfg.workflow_repeat = max(1, int(self.repeat_var.get()))
        except ValueError:
            self.cfg.workflow_repeat = 1

    def _persist_workflow_repeat(self) -> None:
        """Save the repeat count to config.json when the user edits it."""
        self._collect_workflow_repeat()
        try:
            self.cfg.save()
        except Exception as exc:  # noqa: BLE001
            self.log(f"Could not save repeat count: {exc}")

    # ---- multiple named workflows ------------------------------------
    def _workflow_names(self) -> list[str]:
        names = list(self.cfg.named_workflows.keys())
        return names if names else ["Default"]

    def _flush_active_workflow(self) -> None:
        """Write the current in-memory steps back into named_workflows."""
        self._collect_workflow_repeat()
        self.cfg.steps = [s.as_dict() for s in self._steps]
        self.cfg.restart_steps = [s.as_dict() for s in self._restart_steps]
        self.cfg.named_workflows[self.cfg.active_workflow] = {
            "steps": self.cfg.steps,
            "restart_steps": self.cfg.restart_steps,
            "repeat": self.cfg.workflow_repeat,
        }

    def _load_active_workflow(self) -> None:
        """Reload in-memory steps from the active named workflow."""
        wf = self.cfg.named_workflows.get(self.cfg.active_workflow, {})
        self._steps.clear()
        self._steps.extend(Step.from_dict(d) for d in wf.get("steps", []))
        self._restart_steps.clear()
        self._restart_steps.extend(Step.from_dict(d) for d in wf.get("restart_steps", []))
        try:
            self.repeat_var.set(str(max(1, wf.get("repeat", 1))))
        except Exception:
            pass
        if hasattr(self, "_wf_list"):
            self._wf_list.refresh()
        if hasattr(self, "_restart_list"):
            self._restart_list.refresh()

    def _open_step_picker(self, callback) -> None:
        """Open a step-kind picker popup and call callback(kind) on selection."""
        labels = list(STEP_KINDS.values())
        kinds = list(STEP_KINDS.keys())
        def on_label(label: str) -> None:
            callback(kinds[labels.index(label)])
        _open_picker(self, "Add step", labels, on_label, height=430)

    def _open_test_kind_picker(self) -> None:
        """Picker for the Test-tab step kind selector."""
        labels = list(STEP_KINDS.values())
        kinds = list(STEP_KINDS.keys())
        def on_label(label: str) -> None:
            self._test_kind_btn.configure(text=label)
            kind = kinds[labels.index(label)]
            self._on_pick_test_kind_by_kind(kind)
        _open_picker(self, "Pick step type", labels, on_label, height=430)

    def _open_wf_picker(self) -> None:
        """Picker for the workflow switcher button."""
        names = self._workflow_names()
        _open_picker(self, "Switch workflow", names, self._on_switch_workflow,
                     current=self.cfg.active_workflow, width=280, height=320)

    def _open_provider_picker(self) -> None:
        """Picker for the AI provider selector."""
        labels = ai_client.provider_labels()
        current = ai_client.label_for(self.cfg.provider)
        _open_picker(self, "Select AI provider", labels, self._on_provider_change,
                     current=current, width=300, height=280)

    def _refresh_wf_selector(self) -> None:
        self._wf_btn.configure(text=self.cfg.active_workflow)

    def _on_switch_workflow(self, name: str) -> None:
        if name == self.cfg.active_workflow:
            return
        self._flush_active_workflow()
        self.cfg.active_workflow = name
        self._load_active_workflow()
        self.cfg.save()
        self.log(f"Switched to workflow: {name}")

    def _new_workflow(self) -> None:
        import tkinter.simpledialog as sd
        name = sd.askstring("New workflow", "Enter a name for the new workflow:",
                            parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        if name in self.cfg.named_workflows:
            self.log(f"A workflow named '{name}' already exists.")
            return
        self._flush_active_workflow()
        self.cfg.named_workflows[name] = {"steps": [], "restart_steps": [], "repeat": 1}
        self.cfg.active_workflow = name
        self._load_active_workflow()
        self._refresh_wf_selector()
        self.cfg.save()
        self.log(f"Created new workflow: {name}")

    def _rename_workflow(self) -> None:
        import tkinter.simpledialog as sd
        old = self.cfg.active_workflow
        new = sd.askstring("Rename workflow", f"New name for '{old}':", parent=self)
        if not new or not new.strip():
            return
        new = new.strip()
        if new == old:
            return
        if new in self.cfg.named_workflows:
            self.log(f"A workflow named '{new}' already exists.")
            return
        self._flush_active_workflow()
        self.cfg.named_workflows[new] = self.cfg.named_workflows.pop(old)
        self.cfg.active_workflow = new
        self._refresh_wf_selector()
        self.cfg.save()
        self.log(f"Renamed '{old}' → '{new}'")

    def _duplicate_workflow(self) -> None:
        import tkinter.simpledialog as sd
        import copy
        src = self.cfg.active_workflow
        name = sd.askstring("Copy workflow", f"Name for the copy of '{src}':", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        if name in self.cfg.named_workflows:
            self.log(f"A workflow named '{name}' already exists.")
            return
        self._flush_active_workflow()
        self.cfg.named_workflows[name] = copy.deepcopy(self.cfg.named_workflows[src])
        self.cfg.active_workflow = name
        self._load_active_workflow()
        self._refresh_wf_selector()
        self.cfg.save()
        self.log(f"Duplicated '{src}' → '{name}'")

    def _delete_workflow(self) -> None:
        import tkinter.messagebox as mb
        name = self.cfg.active_workflow
        if len(self.cfg.named_workflows) <= 1:
            self.log("Cannot delete the last workflow.")
            return
        if not mb.askyesno("Delete workflow",
                           f"Delete workflow '{name}'? This cannot be undone.",
                           parent=self):
            return
        del self.cfg.named_workflows[name]
        self.cfg.active_workflow = next(iter(self.cfg.named_workflows))
        self._load_active_workflow()
        self._refresh_wf_selector()
        self.cfg.save()
        self.log(f"Deleted workflow: {name}")

    def _save_workflow(self) -> None:
        self._flush_active_workflow()
        self._save_settings()
        self.log(f"Workflow '{self.cfg.active_workflow}' saved ({len(self._steps)} steps).")

    def _save_restart_workflow(self) -> None:
        self.cfg.restart_steps = [s.as_dict() for s in self._restart_steps]
        if self.cfg.active_workflow in self.cfg.named_workflows:
            self.cfg.named_workflows[self.cfg.active_workflow]["restart_steps"] = self.cfg.restart_steps
        self._save_settings()
        self.log(f"Restart workflow saved ({len(self._restart_steps)} steps).")

    def _start_run(
        self,
        steps: list,
        repeat: int,
        header: str,
        start_number: int = 1,
        *,
        start_at: int = 0,
        end_at: Optional[int] = None,
        sub_workflow: bool = False,
        main_steps: Optional[list] = None,
    ) -> None:
        """Shared launcher for full / from-here / single-step runs."""
        if self._wf_thread and self._wf_thread.is_alive():
            self.log("Workflow already running.")
            return
        if end_at is None:
            end_at = len(steps)
        run_slice = steps[start_at:end_at]
        if not any(s.enabled for s in run_slice):
            self.log("No enabled steps to run.")
            return
        self._collect_runtime_settings()
        self._collect_workflow_repeat()
        self._wf_stop.clear()
        self.wf_run_btn.configure(state="disabled")
        self.log(header)
        if any(s.enabled and s.kind in ("capture_ai", "ai_find_click",
                                        "remember_screen", "image_paste", "ai_assert")
               for s in run_slice):
            self.log("Screen captures are local to this PC (invisible to an AnyDesk "
                     "remote); only clicks/keys are sent.")

        ctx = RunContext(
            cfg=self.cfg, log=self.log,
            on_image=self._show_preview, on_answer=self._set_answer,
            restart_steps=self._restart_steps,
            main_steps=main_steps if main_steps is not None else self._steps,
            on_pass_complete=lambda r: self.after(0, lambda rec=r: self._add_history_record(rec)),
        )
        runner = WorkflowRunner(steps, ctx, self._wf_stop, sub_workflow=sub_workflow)
        run_type = header.split("…")[0].strip()
        total_enabled = sum(1 for s in run_slice if s.enabled)

        def _run() -> None:
            guarded = self._rdp_clipboard_guard_begin()
            try:
                runner.run(
                    repeat=repeat,
                    start_number=start_number,
                    start_at=start_at,
                    end_at=end_at,
                    run_type=run_type,
                    total_steps=total_enabled,
                )
            finally:
                self._rdp_clipboard_guard_end(guarded)
                self.after(0, lambda: self.wf_run_btn.configure(state="normal"))

        self._wf_thread = threading.Thread(target=_run, daemon=True)
        self._wf_thread.start()

    def _rdp_clipboard_guard_begin(self) -> bool:
        """Pause RDP clipboard sync (kill rdpclip.exe) while automating so the
        client machine's clipboard can't overwrite ours mid-paste.  Returns
        True if it was suspended and must be resumed afterwards."""
        if not getattr(self.cfg, "manage_rdp_clipboard", False):
            return False
        try:
            if automation.suspend_rdp_clipboard():
                self.log("Paused RDP clipboard sync (rdpclip.exe) for reliable paste.")
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _rdp_clipboard_guard_end(self, suspended: bool) -> None:
        if not suspended:
            return
        try:
            automation.resume_rdp_clipboard()
            self.log("Restored RDP clipboard sync.")
        except Exception:  # noqa: BLE001
            pass

    def _run_workflow(self) -> None:
        self._collect_workflow_repeat()
        repeat = self.cfg.workflow_repeat
        n = sum(s.enabled for s in self._steps)
        self._persist_workflow_repeat()
        self._start_run(
            self._steps, repeat,
            f"Starting workflow ({n} steps × {repeat})…",
            start_number=1, start_at=0,
        )

    def _run_workflow_from(self, index: int) -> None:
        if not (0 <= index < len(self._steps)):
            return
        n = sum(s.enabled for s in self._steps[index:])
        self._start_run(
            self._steps, 1,
            f"Starting from step {index + 1} ({n} steps)…",
            start_number=index + 1, start_at=index,
        )

    def _run_only_step(self, index: int) -> None:
        if not (0 <= index < len(self._steps)):
            return
        step = self._steps[index]
        run_steps = [Step.from_dict(s.as_dict()) for s in self._steps]
        run_steps[index] = Step.from_dict({**step.as_dict(), "enabled": True})
        self._start_run(
            run_steps, 1,
            f"Running only step {index + 1}: {step.summary()}",
            start_number=index + 1, start_at=index, end_at=index + 1,
            main_steps=run_steps,
        )

    def _run_restart_workflow(self) -> None:
        n = sum(s.enabled for s in self._restart_steps)
        self._start_run(self._restart_steps, 1,
                        f"Starting restart workflow ({n} steps)…", start_number=1,
                        sub_workflow=True)

    def _run_restart_from(self, index: int) -> None:
        if not (0 <= index < len(self._restart_steps)):
            return
        steps = self._restart_steps[index:]
        n = sum(s.enabled for s in steps)
        self._start_run(steps, 1,
                        f"Starting restart from step {index + 1} ({n} steps)…",
                        start_number=index + 1, sub_workflow=True)

    def _run_restart_only(self, index: int) -> None:
        if not (0 <= index < len(self._restart_steps)):
            return
        step = self._restart_steps[index]
        run_step = Step.from_dict({**step.as_dict(), "enabled": True})
        self._start_run([run_step], 1,
                        f"Running only restart step {index + 1}: {step.summary()}",
                        start_number=index + 1, sub_workflow=True)

    def _stop_workflow(self) -> None:
        if self._wf_thread and self._wf_thread.is_alive():
            self._wf_stop.set()
            self.log("Stopping workflow…")

    # ------------------------------------------------------------------
    # Settings tab
    # ------------------------------------------------------------------
    def _build_settings_tab(self) -> None:
        tab = self.tab_settings
        tab.grid_columnconfigure(0, weight=1)

        frame = ctk.CTkScrollableFrame(tab, label_text="Configuration")
        frame.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        tab.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)

        self._row_counter = [0]

        def next_row() -> int:
            r = self._row_counter[0]
            self._row_counter[0] += 1
            return r

        def row_label(text: str) -> int:
            r = next_row()
            ctk.CTkLabel(frame, text=text).grid(row=r, column=0, sticky="w", padx=10, pady=8)
            return r

        # --- Provider selection ---
        # Seed per-provider key/model from legacy single values on first run.
        self._editing_provider = self.cfg.provider
        if self.cfg.provider not in self.cfg.api_keys and self.cfg.api_key:
            self.cfg.api_keys[self.cfg.provider] = self.cfg.api_key
        if self.cfg.provider not in self.cfg.models and self.cfg.model:
            self.cfg.models[self.cfg.provider] = self.cfg.model

        r = row_label("AI provider")
        self._provider_btn = ctk.CTkButton(
            frame, text=ai_client.label_for(self.cfg.provider),
            command=self._open_provider_picker, anchor="w",
        )
        self._provider_btn.grid(row=r, column=1, columnspan=2, sticky="ew", padx=10, pady=8)

        r = row_label("API key")
        self.api_key_entry = ctk.CTkEntry(frame, show="•", placeholder_text="paste key…")
        self.api_key_entry.grid(row=r, column=1, sticky="ew", padx=10, pady=8)
        self.api_key_entry.insert(0, self.cfg.api_keys.get(self.cfg.provider, self.cfg.api_key))
        self.show_key_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            frame, text="Show", variable=self.show_key_var, width=60,
            command=lambda: self.api_key_entry.configure(show="" if self.show_key_var.get() else "•"),
        ).grid(row=r, column=2, padx=6)

        r = row_label("Model")
        self.model_combo = ctk.CTkEntry(frame, placeholder_text="e.g. gpt-4o-mini")
        self.model_combo.grid(row=r, column=1, sticky="ew", padx=10, pady=8)
        self.model_combo.insert(0,
            self.cfg.models.get(self.cfg.provider) or self.cfg.model
            or ai_client.default_model(self.cfg.provider))
        self.fetch_btn = ctk.CTkButton(frame, text="↻ Fetch", width=72, command=self._fetch_models)
        self.fetch_btn.grid(row=r, column=2, padx=6)

        r = row_label("Base URL (optional)")
        self.base_url_entry = ctk.CTkEntry(frame)
        self.base_url_entry.grid(row=r, column=1, columnspan=2, sticky="ew", padx=10, pady=8)
        self.base_url_entry.insert(0, self.cfg.base_url)

        self.provider_hint = ctk.CTkLabel(frame, text="", text_color="gray",
                                          wraplength=560, justify="left")
        self.provider_hint.grid(row=next_row(), column=0, columnspan=3, sticky="w", padx=10, pady=(0, 6))
        self._update_provider_hints(self.cfg.provider)

        self.refine_var = ctk.BooleanVar(value=self.cfg.ai_locate_refine)
        ctk.CTkCheckBox(
            frame, text="High-accuracy “AI: find & click” (zoom-in 2nd pass — slower, 2 API calls)",
            variable=self.refine_var,
        ).grid(row=next_row(), column=0, columnspan=3, sticky="w", padx=10, pady=8)

        self.ocr_fallback_var = ctk.BooleanVar(
            value=getattr(self.cfg, "ocr_fallback", True))
        ctk.CTkCheckBox(
            frame,
            text="Fall back to on-device OCR when the AI is out of usage / unavailable",
            variable=self.ocr_fallback_var,
        ).grid(row=next_row(), column=0, columnspan=3, sticky="w", padx=10, pady=(8, 0))
        self.ocr_hint = ctk.CTkLabel(
            frame,
            text=self._ocr_status_text(),
            text_color="gray", wraplength=560, justify="left",
        )
        self.ocr_hint.grid(row=next_row(), column=0, columnspan=3, sticky="w", padx=10, pady=(0, 8))

        r = row_label("Typing speed (sec/char)")
        self.interval_entry = ctk.CTkEntry(frame, width=100)
        self.interval_entry.grid(row=r, column=1, sticky="w", padx=10, pady=8)
        self.interval_entry.insert(0, str(self.cfg.type_interval))
        ctk.CTkLabel(
            frame,
            text="Average delay per character. Higher = slower / more human. "
                 "Try 0.08–0.15 for natural typing; 0.02 is very fast. "
                 "(Needs “Human-like behaviour” on, below.)",
            text_color="gray", wraplength=520, justify="left",
        ).grid(row=next_row(), column=0, columnspan=3, sticky="w", padx=10, pady=(0, 6))

        self.clear_var = ctk.BooleanVar(value=self.cfg.clear_before_type)
        ctk.CTkCheckBox(
            frame, text="Clear input field before typing (Ctrl+A, Delete)",
            variable=self.clear_var,
        ).grid(row=next_row(), column=0, columnspan=3, sticky="w", padx=10, pady=8)

        self.humanize_var = ctk.BooleanVar(value=self.cfg.humanize)
        ctk.CTkCheckBox(
            frame, text="Human-like behaviour (random delays, mouse jitter, variable typing)",
            variable=self.humanize_var,
        ).grid(row=next_row(), column=0, columnspan=3, sticky="w", padx=10, pady=8)

        self.typos_var = ctk.BooleanVar(value=self.cfg.humanize_typos)
        ctk.CTkCheckBox(
            frame, text="Occasionally mistype and correct it (needs human-like behaviour)",
            variable=self.typos_var,
        ).grid(row=next_row(), column=0, columnspan=3, sticky="w", padx=10, pady=8)

        r = row_label("Delay between steps (sec)")
        delayfr = ctk.CTkFrame(frame, fg_color="transparent")
        delayfr.grid(row=r, column=1, columnspan=2, sticky="w", padx=10, pady=8)
        self.hmin_entry = ctk.CTkEntry(delayfr, width=70)
        self.hmin_entry.insert(0, str(self.cfg.humanize_min))
        self.hmin_entry.pack(side="left")
        ctk.CTkLabel(delayfr, text="to").pack(side="left", padx=6)
        self.hmax_entry = ctk.CTkEntry(delayfr, width=70)
        self.hmax_entry.insert(0, str(self.cfg.humanize_max))
        self.hmax_entry.pack(side="left")

        di_state = "normal" if automation.HAVE_DIRECTINPUT else "disabled"
        di_text = "Hardware scan-code input — required for AnyDesk / RDP / games"
        if not automation.HAVE_DIRECTINPUT:
            di_text += "  (pydirectinput not installed)"
        self.directinput_var = ctk.BooleanVar(
            value=self.cfg.use_directinput and automation.HAVE_DIRECTINPUT)
        ctk.CTkCheckBox(
            frame, text=di_text, variable=self.directinput_var,
            state=di_state, command=self._on_directinput_toggle,
        ).grid(row=next_row(), column=0, columnspan=3, sticky="w", padx=10, pady=8)

        self.failsafe_var = ctk.BooleanVar(value=self.cfg.disable_failsafe)
        ctk.CTkCheckBox(
            frame,
            text="Disable corner fail-safe (needed for minimized RDP / unattended VPS)",
            variable=self.failsafe_var, command=self._on_failsafe_toggle,
        ).grid(row=next_row(), column=0, columnspan=3, sticky="w", padx=10, pady=8)
        ctk.CTkLabel(
            frame,
            text="Turn this ON when running on a VPS over RDP. A minimized or "
                 "disconnected RDP session reports the cursor at (0,0), which "
                 "otherwise trips the fail-safe and aborts every step. Use the "
                 "Stop button to halt instead.",
            text_color="gray", wraplength=560, justify="left",
        ).grid(row=next_row(), column=0, columnspan=3, sticky="w", padx=10, pady=(0, 8))

        self.rdpclip_var = ctk.BooleanVar(
            value=getattr(self.cfg, "manage_rdp_clipboard", True))
        ctk.CTkCheckBox(
            frame,
            text="Lock clipboard during runs (fixes Ctrl+V / image paste over RDP)",
            variable=self.rdpclip_var, command=self._on_rdpclip_toggle,
        ).grid(row=next_row(), column=0, columnspan=3, sticky="w", padx=10, pady=8)
        ctk.CTkLabel(
            frame,
            text="Keep this ON when the bot runs in an RDP session. It pauses "
                 "RDP clipboard sharing (rdpclip.exe) while a workflow or test "
                 "runs so that copying something on your local laptop can't "
                 "overwrite what the bot puts on the clipboard. Sharing is "
                 "automatically restored when the run finishes.",
            text_color="gray", wraplength=560, justify="left",
        ).grid(row=next_row(), column=0, columnspan=3, sticky="w", padx=10, pady=(0, 8))

        r = row_label("Appearance")
        self.appearance_seg = ctk.CTkSegmentedButton(
            frame, values=["System", "Dark", "Light"], command=self._on_appearance
        )
        self.appearance_seg.grid(row=r, column=1, sticky="w", padx=10, pady=8)
        self.appearance_seg.set(self.cfg.appearance)

        ctk.CTkButton(frame, text="💾  Save settings", command=self._save_settings).grid(
            row=next_row(), column=0, columnspan=3, sticky="ew", padx=10, pady=16
        )

        ctk.CTkLabel(
            frame,
            text=(
                "Safety: move your mouse to a screen corner to instantly abort "
                "any automated click/typing (fail-safe).  Tip: for AnyDesk, keep "
                "the remote window focused with the cursor inside it while running."
            ),
            text_color="gray", wraplength=560, justify="left",
        ).grid(row=next_row(), column=0, columnspan=3, sticky="w", padx=10, pady=(0, 10))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _region_text(self) -> str:
        r = self.cfg.region
        if r.full_screen:
            return "Region: full screen"
        return f"Region: {r.width}×{r.height} at ({r.left}, {r.top})"

    @staticmethod
    def _point_text(p: Point) -> str:
        return f"Point: ({p.x}, {p.y})" if p.set else "Point: not set"

    def log(self, message: str) -> None:
        def _append() -> None:
            stamp = datetime.now().strftime("%H:%M:%S")
            for box in self._log_boxes:
                box.configure(state="normal")
                box.insert("end", f"[{stamp}] {message}\n")
                box.see("end")
                box.configure(state="disabled")
        self.after(0, _append)

    def _set_answer(self, text: str) -> None:
        def _do() -> None:
            self.answer_box.delete("1.0", "end")
            self.answer_box.insert("1.0", text)
        self.after(0, _do)

    def _show_preview(self, image: Image.Image) -> None:
        thumb = image.copy()
        thumb.thumbnail(PREVIEW_MAX)
        ctk_img = ctk.CTkImage(light_image=thumb, dark_image=thumb, size=thumb.size)

        def _do() -> None:
            self.preview_label.configure(image=ctk_img, text="")
            self.preview_label.image = ctk_img  # keep a reference
        self.after(0, _do)

    # ------------------------------------------------------------------
    # Region / capture handlers
    # ------------------------------------------------------------------
    def _on_fullscreen_toggle(self) -> None:
        self.cfg.region.full_screen = self.full_screen_var.get()
        self.region_label.configure(text=self._region_text())

    def _on_select_region(self) -> None:
        self.withdraw()
        self.after(250, self._do_select_region)

    def _do_select_region(self) -> None:
        try:
            region = screen.select_region(self)
        finally:
            self.deiconify()
            self.lift()
        if region is not None:
            self.cfg.region = region
            self.full_screen_var.set(False)
            self.region_label.configure(text=self._region_text())
            self.log(f"Region set to {region.width}×{region.height} at ({region.left}, {region.top}).")
        else:
            self.log("Region selection cancelled.")

    def _on_capture(self) -> None:
        try:
            image = screen.capture(self.cfg.region)
        except Exception as exc:  # noqa: BLE001
            self.log(f"Capture failed: {exc}")
            return
        self._last_image = image
        self._show_preview(image)
        self.log(f"Captured {image.width}×{image.height} image.")

    # ------------------------------------------------------------------
    # Test a single step
    # ------------------------------------------------------------------
    def _on_pick_test_kind(self, label: str) -> None:
        """Legacy label-based callback (kept for compatibility)."""
        kind = next((k for k, n in STEP_KINDS.items() if n == label), None)
        if kind:
            self._on_pick_test_kind_by_kind(kind)

    def _on_pick_test_kind_by_kind(self, kind: str) -> None:
        step = Step(kind=kind)
        if kind in ("move", "ai_paste_macro", "image_paste"):
            step.use_point = True
        StepEditor(self, step, on_save=self._set_test_step)

    def _set_test_step(self, step: Step) -> None:
        self._test_step = step
        default_text = ctk.ThemeManager.theme["CTkLabel"]["text_color"]
        self.test_step_label.configure(text=step.summary(), text_color=default_text)
        self.test_edit_btn.configure(state="normal")
        self.test_run_btn.configure(state="normal")

    def _edit_test_step(self) -> None:
        if self._test_step is None:
            return
        StepEditor(self, self._test_step, on_save=self._set_test_step)

    def _run_test_step(self) -> None:
        if self._test_step is None:
            self.log("Configure a step to test first.")
            return
        if self._test_thread and self._test_thread.is_alive():
            self.log("A test is already running.")
            return
        self._collect_runtime_settings()
        self._test_stop.clear()
        self.test_run_btn.configure(state="disabled")
        self.log(f"Testing step: {self._test_step.summary()}")

        ctx = RunContext(
            cfg=self.cfg, log=self.log,
            on_image=self._show_preview, on_answer=self._set_answer,
        )
        runner = WorkflowRunner([self._test_step], ctx, self._test_stop)

        def _run() -> None:
            guarded = self._rdp_clipboard_guard_begin()
            try:
                runner.run(repeat=1)
            finally:
                self._rdp_clipboard_guard_end(guarded)
                self.after(0, lambda: self.test_run_btn.configure(state="normal"))

        self._test_thread = threading.Thread(target=_run, daemon=True)
        self._test_thread.start()

    def _stop_test(self) -> None:
        if self._test_thread and self._test_thread.is_alive():
            self._test_stop.set()
            self.log("Stopping test…")

    # ------------------------------------------------------------------
    # Shared runtime settings
    # ------------------------------------------------------------------
    def _collect_runtime_settings(self) -> None:
        self.cfg.prompt = self.prompt_box.get("1.0", "end").strip()
        self.cfg.provider = self._editing_provider
        self._stash_current_provider()
        self.cfg.api_key = self.api_key_entry.get().strip()
        self.cfg.model = self.model_combo.get().strip() or ai_client.default_model(self.cfg.provider)
        self.cfg.base_url = self.base_url_entry.get().strip()
        self.cfg.clear_before_type = self.clear_var.get()
        try:
            self.cfg.type_interval = float(self.interval_entry.get())
        except ValueError:
            self.cfg.type_interval = 0.06
        self.cfg.humanize = self.humanize_var.get()
        self.cfg.humanize_typos = self.typos_var.get()
        automation.set_typos(self.cfg.humanize_typos)
        self.cfg.ai_locate_refine = self.refine_var.get()
        self.cfg.ocr_fallback = self.ocr_fallback_var.get()
        self.cfg.use_directinput = self.directinput_var.get()
        automation.set_directinput(self.cfg.use_directinput)
        self.cfg.disable_failsafe = self.failsafe_var.get()
        automation.set_failsafe(not self.cfg.disable_failsafe)
        self.cfg.manage_rdp_clipboard = self.rdpclip_var.get()
        try:
            self.cfg.humanize_min = float(self.hmin_entry.get())
        except ValueError:
            self.cfg.humanize_min = 0.4
        try:
            self.cfg.humanize_max = float(self.hmax_entry.get())
        except ValueError:
            self.cfg.humanize_max = 1.2
        if self.cfg.humanize_max < self.cfg.humanize_min:
            self.cfg.humanize_max = self.cfg.humanize_min

    # ------------------------------------------------------------------
    # Settings handlers
    # ------------------------------------------------------------------
    def _on_appearance(self, value: str) -> None:
        _mode = value if value in ("Dark", "Light") else "Dark"
        ctk.set_appearance_mode(_mode)
        self.cfg.appearance = value

    def _on_directinput_toggle(self) -> None:
        enabled = self.directinput_var.get()
        self.cfg.use_directinput = enabled
        automation.set_directinput(enabled)
        self.log(f"Input backend: {automation.backend_name()}.")

    def _on_failsafe_toggle(self) -> None:
        disabled = self.failsafe_var.get()
        self.cfg.disable_failsafe = disabled
        automation.set_failsafe(not disabled)
        self.log("Corner fail-safe " + ("DISABLED (use Stop to halt)." if disabled else "enabled."))

    def _on_rdpclip_toggle(self) -> None:
        enabled = self.rdpclip_var.get()
        self.cfg.manage_rdp_clipboard = enabled
        self.log("RDP clipboard lock during runs " + ("enabled." if enabled else "disabled."))

    @staticmethod
    def _ocr_status_text() -> str:
        """Describe the available OCR backend for the Settings hint."""
        try:
            from . import ocr
            backend = ocr.backend_name()
        except Exception:  # noqa: BLE001
            backend = None
        if backend:
            return (f"Reads the captured region's text locally when the AI can't "
                    f"be used (out of credits, rate-limited, or no key). "
                    f"Detected engine: {backend}. Keep the capture region tight "
                    f"around the target for the cleanest result.")
        return ("Reads the captured region's text locally when the AI can't be "
                "used. No OCR engine detected — on Windows run "
                "'pip install winocr', or install Tesseract-OCR + "
                "'pip install pytesseract'.")

    def _stash_current_provider(self) -> None:
        """Remember the key/model currently in the entry boxes for the provider
        being edited, so switching providers doesn't lose them."""
        p = self._editing_provider
        self.cfg.api_keys[p] = self.api_key_entry.get().strip()
        self.cfg.models[p] = self.model_combo.get().strip()

    def _update_provider_hints(self, provider: str) -> None:
        if provider == "custom":
            self.base_url_entry.configure(placeholder_text="required, e.g. http://localhost:1234/v1")
            hint = "Custom: any OpenAI-compatible endpoint. Set the Base URL and a model name."
        elif provider in ("gemini", "openrouter"):
            self.base_url_entry.configure(placeholder_text="preset — leave blank")
            hint = f"{ai_client.label_for(provider)}: endpoint is preset. Just add your API key (and model if needed)."
        elif provider == "anthropic":
            self.base_url_entry.configure(placeholder_text="leave blank")
            hint = "Anthropic: uses the native Claude API. Vision models like claude-3-5-sonnet work well."
        else:
            self.base_url_entry.configure(placeholder_text="leave blank for OpenAI")
            hint = "OpenAI: standard API. Use a vision model such as gpt-4o or gpt-4o-mini."
        self.provider_hint.configure(text=hint)

    def _on_provider_change(self, label: str) -> None:
        new_provider = ai_client.provider_from_label(label)
        if new_provider == self._editing_provider:
            return
        self._stash_current_provider()
        self._editing_provider = new_provider
        self.cfg.provider = new_provider

        key = self.cfg.api_keys.get(new_provider, "")
        model = self.cfg.models.get(new_provider) or ai_client.default_model(new_provider)
        self.api_key_entry.delete(0, "end")
        self.api_key_entry.insert(0, key)
        self.model_combo.delete(0, "end")
        self.model_combo.insert(0, model)
        self._update_provider_hints(new_provider)
        self._provider_btn.configure(text=label)
        self.log(f"AI provider: {label}.")

    def _fetch_models(self) -> None:
        provider = self._editing_provider
        key = self.api_key_entry.get().strip()
        base = self.base_url_entry.get().strip() or None
        if not key:
            self.log(f"Enter an API key for {ai_client.label_for(provider)} first.")
            return
        self.fetch_btn.configure(state="disabled", text="…")
        self.log(f"Fetching models for {ai_client.label_for(provider)}…")

        def work() -> None:
            try:
                models = ai_client.list_models(provider, api_key=key, base_url=base)
                err = None
            except Exception as exc:  # noqa: BLE001
                models, err = [], str(exc)

            def apply() -> None:
                self.fetch_btn.configure(state="normal", text="↻ Fetch")
                if err:
                    self.log(f"Could not fetch models: {err}")
                    return
                if not models:
                    self.log("No models returned by the provider.")
                    return
                current = self.model_combo.get().strip()
                if current not in models:
                    self.model_combo.delete(0, "end")
                    self.model_combo.insert(0, models[0])
                self.log(f"Fetched {len(models)} models. First: {models[0]}. Type the model name in the Model field.")
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _save_settings(self) -> None:
        self._collect_runtime_settings()
        self._collect_workflow_repeat()
        self.cfg.region.full_screen = self.full_screen_var.get()
        self.cfg.steps = [s.as_dict() for s in self._steps]
        self.cfg.restart_steps = [s.as_dict() for s in self._restart_steps]
        try:
            self.cfg.save()
            self.log("Settings saved to config.json.")
        except Exception as exc:  # noqa: BLE001
            self.log(f"Could not save settings: {exc}")

    def _on_close(self) -> None:
        self._loop_stop.set()
        self._test_stop.set()
        self._wf_stop.set()
        try:
            self._collect_runtime_settings()
            self.cfg.region.full_screen = self.full_screen_var.get()
            self.cfg.steps = [s.as_dict() for s in self._steps]
            self.cfg.restart_steps = [s.as_dict() for s in self._restart_steps]
            self._collect_workflow_repeat()
            self.cfg.save()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()


class StepEditor(ctk.CTkToplevel):
    """Modal dialog to create or edit a single workflow Step."""

    def __init__(self, app: "App", step: Step, on_save) -> None:
        super().__init__(app)
        self.app = app
        self.step = Step.from_dict(step.as_dict())  # edit a copy
        self.on_save = on_save

        self.title(f"{STEP_KINDS.get(step.kind, step.kind)} — step")
        self.geometry("480x520")
        self.resizable(True, True)
        self.minsize(440, 420)
        self.transient(app)
        try:
            self.attributes("-toolwindow", True)
        except Exception:
            pass
        self.grid_columnconfigure(0, weight=1)

        self.body = ctk.CTkScrollableFrame(self)
        self.body.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.body.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._row = 0
        self._build_fields()

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
        btns.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(btns, text="Cancel", fg_color="gray", command=self.destroy).grid(
            row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(btns, text="Save step", command=self._save).grid(
            row=0, column=1, sticky="ew", padx=(6, 0))

        self.after(120, self._grab)

    def _grab(self) -> None:
        try:
            self.grab_set()
            self.focus_force()
        except Exception:  # noqa: BLE001
            pass

    # -- field builders ------------------------------------------------
    def _label(self, text: str) -> None:
        ctk.CTkLabel(self.body, text=text).grid(
            row=self._row, column=0, sticky="w", padx=8, pady=6)

    def _next(self) -> int:
        r = self._row
        self._row += 1
        return r

    def _add_point_fields(self, label: str = "Target point") -> None:
        self.use_point_var = ctk.BooleanVar(value=self.step.use_point)
        ctk.CTkCheckBox(
            self.body, text=f"Use a fixed {label.lower()} (otherwise current cursor)",
            variable=self.use_point_var,
        ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=6)

        r = self._next()
        ctk.CTkLabel(self.body, text="X, Y").grid(row=r, column=0, sticky="w", padx=8, pady=6)
        ptfr = ctk.CTkFrame(self.body, fg_color="transparent")
        ptfr.grid(row=r, column=1, sticky="ew", padx=8, pady=6)
        self.x_entry = ctk.CTkEntry(ptfr, width=70)
        self.x_entry.insert(0, str(self.step.x))
        self.x_entry.pack(side="left")
        self.y_entry = ctk.CTkEntry(ptfr, width=70)
        self.y_entry.insert(0, str(self.step.y))
        self.y_entry.pack(side="left", padx=6)
        ctk.CTkButton(ptfr, text="🎯 Pick", width=70, command=self._pick).pack(side="left")

    def _build_fields(self) -> None:
        kind = self.step.kind

        if kind == "click":
            r = self._next()
            ctk.CTkLabel(self.body, text="Button").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.button_menu = ctk.CTkSegmentedButton(self.body, values=["left", "right", "middle"])
            self.button_menu.set(self.step.button)
            self.button_menu.grid(row=r, column=1, sticky="w", padx=8, pady=6)

            r = self._next()
            ctk.CTkLabel(self.body, text="Clicks").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.clicks_menu = ctk.CTkSegmentedButton(self.body, values=["1", "2", "3"])
            self.clicks_menu.set(str(self.step.clicks))
            self.clicks_menu.grid(row=r, column=1, sticky="w", padx=8, pady=6)
            self._add_point_fields("click point")

        elif kind == "move":
            self.step.use_point = True
            self._add_point_fields("destination")

        elif kind == "scroll":
            r = self._next()
            ctk.CTkLabel(self.body, text="Direction").grid(
                row=r, column=0, sticky="w", padx=8, pady=6)
            self.scroll_dir_menu = ctk.CTkSegmentedButton(self.body, values=["Down", "Up"])
            self.scroll_dir_menu.set("Up" if self.step.amount > 0 else "Down")
            self.scroll_dir_menu.grid(row=r, column=1, sticky="w", padx=8, pady=6)

            r = self._next()
            ctk.CTkLabel(self.body, text="Amount (notches)").grid(
                row=r, column=0, sticky="w", padx=8, pady=6)
            self.notches_entry = ctk.CTkEntry(self.body, width=90)
            self.notches_entry.insert(0, str(abs(self.step.amount) or 3))
            self.notches_entry.grid(row=r, column=1, sticky="w", padx=8, pady=6)
            ctk.CTkLabel(
                self.body, text="Tip: one notch ≈ one mouse-wheel click. Try 3–5; "
                                "increase if the page barely moves.",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))
            self._add_point_fields("scroll point")

        elif kind == "type_text":
            self._label("Text to type")
            self.text_box = ctk.CTkTextbox(self.body, height=120, wrap="word")
            self.text_box.grid(row=self._next(), column=0, columnspan=2, sticky="ew", padx=8, pady=6)
            self.text_box.insert("1.0", self.step.text)
            self.clear_var = ctk.BooleanVar(value=self.step.clear_first)
            ctk.CTkCheckBox(self.body, text="Clear field first (Ctrl+A, Delete)",
                            variable=self.clear_var).grid(
                row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=6)

        elif kind == "key":
            self._label("Key or hotkey")
            r = self._row
            self.keys_combo = ctk.CTkEntry(self.body, placeholder_text="enter, tab, ctrl+v…")
            self.keys_combo.insert(0, self.step.keys or "enter")
            self.keys_combo.grid(row=r, column=1, sticky="ew", padx=8, pady=6)
            self._next()
            ctk.CTkLabel(
                self.body,
                text="Pick a key from the list, or type your own combo "
                     "(e.g. ctrl+shift+s). Common keys: enter, tab, esc, end, "
                     "home, pageup, pagedown, arrows, f1–f12.",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

        elif kind == "scroll_capture":
            ctk.CTkLabel(
                self.body,
                text="Scrolls the target area from top to bottom, captures each "
                     "screenful, and stitches them into one tall image of the "
                     "whole page — de-duplicating the overlap automatically. "
                     "Optionally save it and/or ask the AI about the entire page.",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(2, 6))

            r = self._next()
            ctk.CTkLabel(self.body, text="Area to capture").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            ctk.CTkButton(self.body, text="Select region…", command=self._pick_region).grid(
                row=r, column=1, sticky="w", padx=8, pady=6)
            self.region_label = ctk.CTkLabel(
                self.body, text=self._region_text(), text_color="gray", anchor="w")
            self.region_label.grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

            r = self._next()
            ctk.CTkLabel(self.body, text="Scroll method").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.scroll_method_seg = ctk.CTkSegmentedButton(
                self.body, values=["Wheel", "Arrow keys", "Page Down"])
            self.scroll_method_seg.set(
                {"arrows": "Arrow keys", "pagedown": "Page Down"}.get(self.step.scroll_method, "Wheel"))
            self.scroll_method_seg.grid(row=r, column=1, sticky="w", padx=8, pady=6)
            ctk.CTkLabel(
                self.body,
                text="Over AnyDesk/RDP, Wheel (cursor over the modal) and Arrow keys "
                     "give small, reliable steps that stitch cleanly. Page Down is "
                     "fastest but jumps almost a full screen, so overlap may be tight.\n"
                     "Arrow keys / Page Down need the pane focused, so the scroll "
                     "point below is CLICKED first — set it on an EMPTY spot inside "
                     "the modal (not a link/button).",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

            r = self._next()
            ctk.CTkLabel(self.body, text="Steps per scroll").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.notches_entry = ctk.CTkEntry(self.body, width=90)
            self.notches_entry.insert(0, str(abs(self.step.amount) or 3))
            self.notches_entry.grid(row=r, column=1, sticky="w", padx=8, pady=6)
            ctk.CTkLabel(
                self.body, text="Wheel notches (or Down-arrow presses) per step. "
                                "Smaller = more overlap = safer stitching (try 2–4).",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

            r = self._next()
            ctk.CTkLabel(self.body, text="Pause between scrolls (sec)").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            pausefr = ctk.CTkFrame(self.body, fg_color="transparent")
            pausefr.grid(row=r, column=1, sticky="w", padx=8, pady=6)
            self.min_entry = ctk.CTkEntry(pausefr, width=70)
            self.min_entry.insert(0, str(self.step.min_delay if self.step.min_delay else 0.5))
            self.min_entry.pack(side="left")
            ctk.CTkLabel(pausefr, text="to").pack(side="left", padx=6)
            self.max_entry = ctk.CTkEntry(pausefr, width=70)
            self.max_entry.insert(0, str(self.step.max_delay if self.step.max_delay else 1.4))
            self.max_entry.pack(side="left")
            ctk.CTkLabel(
                self.body, text="Random wait in this range after each scroll — looks "
                                "human and lets the AnyDesk→Chrome view finish "
                                "repainting before the next capture. Raise both if "
                                "the connection is laggy.",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

            r = self._next()
            ctk.CTkLabel(self.body, text="Max scroll steps").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.maxscroll_entry = ctk.CTkEntry(self.body, width=90)
            self.maxscroll_entry.insert(0, str(self.step.max_scrolls or 15))
            self.maxscroll_entry.grid(row=r, column=1, sticky="w", padx=8, pady=6)
            ctk.CTkLabel(
                self.body, text="Safety cap. It stops earlier on its own once the "
                                "page can't scroll any further.",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

            self.start_top_var = ctk.BooleanVar(value=self.step.start_from_top)
            ctk.CTkCheckBox(self.body, text="Scroll to the top first, then capture from the beginning",
                            variable=self.start_top_var).grid(
                row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=6)

            self.return_top_var = ctk.BooleanVar(value=self.step.return_to_top)
            ctk.CTkCheckBox(self.body, text="Scroll back to the top when finished",
                            variable=self.return_top_var).grid(
                row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=6)

            self.save_disk_var = ctk.BooleanVar(value=self.step.save_to_disk)
            ctk.CTkCheckBox(self.body, text="Save the stitched image to the captures/ folder",
                            variable=self.save_disk_var).grid(
                row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=6)

            # --- Paste the stitched image into another window ---
            self.paste_win_var = ctk.BooleanVar(value=self.step.paste_to_window)
            ctk.CTkCheckBox(
                self.body, text="Paste the stitched image into another window afterwards",
                variable=self.paste_win_var,
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(10, 4))

            r = self._next()
            ctk.CTkLabel(self.body, text="Paste destination X, Y").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            destfr = ctk.CTkFrame(self.body, fg_color="transparent")
            destfr.grid(row=r, column=1, sticky="ew", padx=8, pady=6)
            self.dest_x_entry = ctk.CTkEntry(destfr, width=70)
            self.dest_x_entry.insert(0, str(self.step.dest_x))
            self.dest_x_entry.pack(side="left")
            self.dest_y_entry = ctk.CTkEntry(destfr, width=70)
            self.dest_y_entry.insert(0, str(self.step.dest_y))
            self.dest_y_entry.pack(side="left", padx=6)
            ctk.CTkButton(destfr, text="🎯 Pick", width=70,
                          command=lambda: self._pick_into(self.dest_x_entry, self.dest_y_entry)).pack(side="left")
            ctk.CTkLabel(
                self.body, text="The field/window to paste into is focused by clicking "
                                "here first, then Ctrl+V. Leave 0,0 to paste at the "
                                "current cursor. Clipboard is re-copied right before "
                                "pasting in case RDP sync wipes it.",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

            self.paste_clear_var = ctk.BooleanVar(value=self.step.clear_first)
            ctk.CTkCheckBox(self.body, text="Clear/select the field first (Ctrl+A) before pasting",
                            variable=self.paste_clear_var).grid(
                row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=6)

            r = self._next()
            ctk.CTkLabel(self.body, text="Press after paste").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.paste_keys_entry = ctk.CTkEntry(self.body, placeholder_text="optional, e.g. enter")
            self.paste_keys_entry.insert(0, self.step.keys)
            self.paste_keys_entry.grid(row=r, column=1, sticky="ew", padx=8, pady=6)

            self._label("Ask the AI about the full page (optional)")
            self.prompt_box = ctk.CTkTextbox(self.body, height=80, wrap="word")
            self.prompt_box.grid(row=self._next(), column=0, columnspan=2, sticky="ew", padx=8, pady=6)
            self.prompt_box.insert("1.0", self.step.prompt)
            ctk.CTkLabel(
                self.body, text="Leave blank to only capture/stitch. If set, the whole "
                                "stitched page is sent to the AI and the reply is stored "
                                "as the AI answer (add a “Type AI answer” step to type it).",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

            r = self._next()
            ctk.CTkLabel(self.body, text="Remember answer as (optional)").grid(
                row=r, column=0, sticky="w", padx=8, pady=6)
            self.var_entry = ctk.CTkEntry(self.body, placeholder_text="e.g. page_text")
            self.var_entry.insert(0, self.step.var if self.step.var != "value" else "")
            self.var_entry.grid(row=r, column=1, sticky="ew", padx=8, pady=6)

            self._add_point_fields("scroll point")
            ctk.CTkLabel(
                self.body, text="The scroll point is where the mouse hovers while "
                                "scrolling, so the right pane receives the wheel. "
                                "Leave it off to scroll from the centre of the area.",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

        elif kind == "capture_ai":
            self._label("Prompt override (optional)")
            self.prompt_box = ctk.CTkTextbox(self.body, height=90, wrap="word")
            self.prompt_box.grid(row=self._next(), column=0, columnspan=2, sticky="ew", padx=8, pady=6)
            self.prompt_box.insert("1.0", self.step.prompt)
            ctk.CTkLabel(self.body, text="(blank = use the global prompt from the Run tab)",
                         text_color="gray").grid(
                row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))
            self.type_answer_var = ctk.BooleanVar(value=self.step.type_answer)
            ctk.CTkCheckBox(self.body, text="Type the AI answer immediately after capturing",
                            variable=self.type_answer_var).grid(
                row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=6)
            self.clear_var = ctk.BooleanVar(value=self.step.clear_first)
            ctk.CTkCheckBox(self.body, text="Clear field first when typing",
                            variable=self.clear_var).grid(
                row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=6)
            self._add_point_fields("input point")

        elif kind == "ai_find_click":
            self._label("What to click")
            self.find_box = ctk.CTkTextbox(self.body, height=80, wrap="word")
            self.find_box.grid(row=self._next(), column=0, columnspan=2, sticky="ew", padx=8, pady=6)
            self.find_box.insert("1.0", self.step.text)
            ctk.CTkLabel(
                self.body,
                text="Describe the element for the AI to locate, e.g. "
                     "\"the blue Submit button\", \"the X close icon top-right\", "
                     "\"the Next link\". It captures the Run-tab area, asks the AI "
                     "for its position, then clicks it.",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

            r = self._next()
            ctk.CTkLabel(self.body, text="Button").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.button_menu = ctk.CTkSegmentedButton(self.body, values=["left", "right", "middle"])
            self.button_menu.set(self.step.button)
            self.button_menu.grid(row=r, column=1, sticky="w", padx=8, pady=6)

            r = self._next()
            ctk.CTkLabel(self.body, text="Clicks").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.clicks_menu = ctk.CTkSegmentedButton(self.body, values=["1", "2", "3"])
            self.clicks_menu.set(str(self.step.clicks))
            self.clicks_menu.grid(row=r, column=1, sticky="w", padx=8, pady=6)

        elif kind == "ai_assert":
            self._label("Condition to verify")
            self.prompt_box = ctk.CTkTextbox(self.body, height=90, wrap="word")
            self.prompt_box.grid(row=self._next(), column=0, columnspan=2, sticky="ew", padx=8, pady=6)
            self.prompt_box.insert("1.0", self.step.prompt)
            ctk.CTkLabel(
                self.body,
                text="The AI captures the screen and decides if this is TRUE. "
                     "If it is NOT met, the workflow STOPS. Phrase it as a clear "
                     "yes/no check, e.g. \"The submission was accepted and a green "
                     "success message is visible\" or \"The Inputs card is showing "
                     "a task ID\".",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

            r = self._next()
            ctk.CTkLabel(self.body, text="Attempts before stopping").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.attempts_entry = ctk.CTkEntry(self.body, width=90)
            self.attempts_entry.insert(0, str(self.step.attempts or 1))
            self.attempts_entry.grid(row=r, column=1, sticky="w", padx=8, pady=6)
            ctk.CTkLabel(
                self.body,
                text="Re-checks this many times (waiting ~1s between) before giving "
                     "up — useful while a page is still loading.",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

            self.run_restart_var = ctk.BooleanVar(value=self.step.run_restart)
            ctk.CTkCheckBox(
                self.body,
                text="When check fails, run restart workflow then restart main workflow from step 1",
                variable=self.run_restart_var,
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))
            ctk.CTkLabel(
                self.body,
                text="Build the recovery steps on the Restart tab. If disabled, a failed "
                     "check stops the workflow as before.",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

        elif kind == "conditional_click":
            ctk.CTkLabel(
                self.body,
                text="Reads text (clipboard or a memory slot) and clicks the point "
                     "of the matching option. With AI off it does case-insensitive "
                     "substring matching; with AI on, the model reads the text and "
                     "picks the best option (handles verbose verdicts).",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(2, 6))

            self.read_clipboard_var = ctk.BooleanVar(value=self.step.read_clipboard)
            ctk.CTkCheckBox(
                self.body, text="Read from clipboard (otherwise from a memory slot)",
                variable=self.read_clipboard_var,
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=6)

            r = self._next()
            ctk.CTkLabel(self.body, text="Memory slot (if not clipboard)").grid(
                row=r, column=0, sticky="w", padx=8, pady=6)
            self.var_entry = ctk.CTkEntry(self.body, placeholder_text="e.g. verdict")
            self.var_entry.insert(0, self.step.var)
            self.var_entry.grid(row=r, column=1, sticky="ew", padx=8, pady=6)

            self.use_ai_var = ctk.BooleanVar(value=self.step.use_ai)
            ctk.CTkCheckBox(
                self.body, text="Use AI to decide which option matches",
                variable=self.use_ai_var,
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=6)

            self._label("AI instruction (when AI is on)")
            self.prompt_box = ctk.CTkTextbox(self.body, height=60, wrap="word")
            self.prompt_box.grid(row=self._next(), column=0, columnspan=2, sticky="ew", padx=8, pady=6)
            self.prompt_box.insert("1.0", self.step.prompt or
                                   "The text is a verdict comparing two responses. "
                                   "Decide which option it favours.")
            ctk.CTkLabel(
                self.body,
                text="The option phrases below are the choices the AI picks from "
                     "(it replies with one of them). Keep them short and distinct, "
                     "e.g. \"Response A\" and \"Response B\".",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

            r = self._next()
            ctk.CTkLabel(self.body, text="Click button").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.button_menu = ctk.CTkSegmentedButton(self.body, values=["left", "right", "middle"])
            self.button_menu.set(self.step.button)
            self.button_menu.grid(row=r, column=1, sticky="w", padx=8, pady=6)

            r = self._next()
            ctk.CTkLabel(self.body, text="Clicks").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.clicks_menu = ctk.CTkSegmentedButton(self.body, values=["1", "2", "3"])
            self.clicks_menu.set(str(self.step.clicks))
            self.clicks_menu.grid(row=r, column=1, sticky="w", padx=8, pady=6)

            ctk.CTkLabel(self.body, text="Options / conditions (checked top to bottom)").grid(
                row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(8, 2))
            self.rules_frame = ctk.CTkFrame(self.body, fg_color="transparent")
            self.rules_frame.grid(row=self._next(), column=0, columnspan=2, sticky="ew", padx=4, pady=2)
            self.rules_frame.grid_columnconfigure(0, weight=1)
            self._rule_rows = []
            existing = self.step.rules or [{"contains": "Response A"}, {"contains": "Response B"}]
            for rule in existing:
                self._add_rule_row(rule)
            ctk.CTkButton(self.body, text="➕ Add condition", command=lambda: self._add_rule_row()).grid(
                row=self._next(), column=0, sticky="w", padx=8, pady=(2, 6))

        elif kind == "type_answer":
            self.clear_var = ctk.BooleanVar(value=self.step.clear_first)
            ctk.CTkCheckBox(self.body, text="Clear field first (Ctrl+A, Delete)",
                            variable=self.clear_var).grid(
                row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=6)
            self._add_point_fields("input point")

        elif kind == "save_clipboard":
            r = self._next()
            ctk.CTkLabel(self.body, text="Memory name").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.var_entry = ctk.CTkEntry(self.body, placeholder_text="e.g. task_id")
            self.var_entry.insert(0, self.step.var)
            self.var_entry.grid(row=r, column=1, sticky="ew", padx=8, pady=6)
            ctk.CTkLabel(
                self.body,
                text="Reads whatever is on the clipboard right now (e.g. after a "
                     "“copy” button) and stores it under this name. Use a “Type "
                     "remembered value” step later to paste it.",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

        elif kind == "image_paste":
            ctk.CTkLabel(
                self.body,
                text="Crops a screen rectangle locally and copies it to the "
                     "clipboard as an IMAGE, then clicks the destination field "
                     "and pastes with Ctrl+V. The picture itself is pasted — not "
                     "text. Choose the crop area manually, or let the AI find it.",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(2, 6))

            self.ai_region_var = ctk.BooleanVar(value=self.step.use_ai_region)
            ctk.CTkCheckBox(
                self.body, text="Let the AI find the area to crop (describe it below)",
                variable=self.ai_region_var,
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=6)

            self._label("Describe the area to crop (AI mode)")
            self.crop_desc_box = ctk.CTkTextbox(self.body, height=70, wrap="word")
            self.crop_desc_box.grid(row=self._next(), column=0, columnspan=2, sticky="ew", padx=8, pady=6)
            self.crop_desc_box.insert("1.0", self.step.prompt)
            ctk.CTkLabel(
                self.body,
                text="e.g. \"the Input card, from the 'Inputs' header down to the "
                     "end of its text\". The AI returns the rectangle; the app "
                     "crops it from the capture area. (Blank = global prompt.)",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

            r = self._next()
            ctk.CTkLabel(self.body, text="AI attempts (if not found)").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.attempts_entry = ctk.CTkEntry(self.body, width=90)
            self.attempts_entry.insert(0, str(self.step.attempts or 3))
            self.attempts_entry.grid(row=r, column=1, sticky="w", padx=8, pady=6)

            r = self._next()
            ctk.CTkLabel(self.body, text="Manual crop rectangle").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            ctk.CTkButton(self.body, text="Select region…", command=self._pick_region).grid(
                row=r, column=1, sticky="w", padx=8, pady=6)
            self.region_label = ctk.CTkLabel(
                self.body, text=self._region_text(), text_color="gray", anchor="w")
            self.region_label.grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

            r = self._next()
            ctk.CTkLabel(self.body, text="Destination click button").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.button_menu = ctk.CTkSegmentedButton(self.body, values=["left", "right", "middle"])
            self.button_menu.set(self.step.button)
            self.button_menu.grid(row=r, column=1, sticky="w", padx=8, pady=6)

            r = self._next()
            ctk.CTkLabel(self.body, text="Destination clicks").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.clicks_menu = ctk.CTkSegmentedButton(self.body, values=["1", "2", "3"])
            self.clicks_menu.set(str(self.step.clicks))
            self.clicks_menu.grid(row=r, column=1, sticky="w", padx=8, pady=6)

            r = self._next()
            ctk.CTkLabel(self.body, text="Press after paste").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.paste_keys_entry = ctk.CTkEntry(self.body, placeholder_text="optional, e.g. enter")
            self.paste_keys_entry.insert(0, self.step.keys)
            self.paste_keys_entry.grid(row=r, column=1, sticky="ew", padx=8, pady=6)

            self._add_point_fields("destination field point")

        elif kind == "ai_paste_macro":
            ctk.CTkLabel(
                self.body,
                text="One step that does: capture → AI reads (your prompt) → copy to "
                     "clipboard → click the destination field (this focuses the "
                     "correct window) → optional clear → Ctrl+V → optional key. "
                     "No blind Alt+Tab — you click exactly where it should paste.",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(2, 6))
            self._label("What to read from the screen")
            self.prompt_box = ctk.CTkTextbox(self.body, height=80, wrap="word")
            self.prompt_box.grid(row=self._next(), column=0, columnspan=2, sticky="ew", padx=8, pady=6)
            self.prompt_box.insert("1.0", self.step.prompt)

            r = self._next()
            ctk.CTkLabel(self.body, text="Memory name").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.var_entry = ctk.CTkEntry(self.body, placeholder_text="e.g. answer")
            self.var_entry.insert(0, self.step.var)
            self.var_entry.grid(row=r, column=1, sticky="ew", padx=8, pady=6)

            r = self._next()
            ctk.CTkLabel(self.body, text="Destination click button").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.button_menu = ctk.CTkSegmentedButton(self.body, values=["left", "right", "middle"])
            self.button_menu.set(self.step.button)
            self.button_menu.grid(row=r, column=1, sticky="w", padx=8, pady=6)

            r = self._next()
            ctk.CTkLabel(self.body, text="Destination clicks").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.clicks_menu = ctk.CTkSegmentedButton(self.body, values=["1", "2", "3"])
            self.clicks_menu.set(str(self.step.clicks))
            self.clicks_menu.grid(row=r, column=1, sticky="w", padx=8, pady=6)

            self.clear_var = ctk.BooleanVar(value=self.step.clear_first)
            ctk.CTkCheckBox(
                self.body, text="Clear field first (Ctrl+A before paste)",
                variable=self.clear_var,
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=6)

            r = self._next()
            ctk.CTkLabel(self.body, text="Press after paste").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.paste_keys_entry = ctk.CTkEntry(self.body, placeholder_text="optional, e.g. enter")
            self.paste_keys_entry.insert(0, self.step.keys)
            self.paste_keys_entry.grid(row=r, column=1, sticky="ew", padx=8, pady=6)

            self._add_point_fields("destination field point")

        elif kind == "remember_screen":
            r = self._next()
            ctk.CTkLabel(self.body, text="Memory name").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.var_entry = ctk.CTkEntry(self.body, placeholder_text="e.g. task_id")
            self.var_entry.insert(0, self.step.var)
            self.var_entry.grid(row=r, column=1, sticky="ew", padx=8, pady=6)
            self._label("What to read from the screen")
            self.prompt_box = ctk.CTkTextbox(self.body, height=90, wrap="word")
            self.prompt_box.grid(row=self._next(), column=0, columnspan=2, sticky="ew", padx=8, pady=6)
            self.prompt_box.insert("1.0", self.step.prompt)
            ctk.CTkLabel(
                self.body,
                text="Describe the value to extract and ask for ONLY that value, "
                     "e.g. \"Read the task ID shown and reply with only the number.\" "
                     "It captures the Run-tab area. (Blank = use the global prompt.)",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

        elif kind == "type_memory":
            r = self._next()
            ctk.CTkLabel(self.body, text="Memory name").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.var_entry = ctk.CTkEntry(self.body, placeholder_text="e.g. task_id")
            self.var_entry.insert(0, self.step.var)
            self.var_entry.grid(row=r, column=1, sticky="ew", padx=8, pady=6)

            self.instant_var = ctk.BooleanVar(value=self.step.paste_instant)
            ctk.CTkCheckBox(
                self.body,
                text="Paste instantly (clipboard + Ctrl+V) instead of typing",
                variable=self.instant_var,
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=6)
            ctk.CTkLabel(
                self.body,
                text="Instant paste is immediate and avoids typos — good for IDs, "
                     "URLs, and code. Leave off to type it out character by "
                     "character (human-like).",
                text_color="gray", wraplength=380, justify="left",
            ).grid(row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

            self.clear_var = ctk.BooleanVar(value=self.step.clear_first)
            ctk.CTkCheckBox(self.body, text="Clear field first (Ctrl+A, Delete)",
                            variable=self.clear_var).grid(
                row=self._next(), column=0, columnspan=2, sticky="w", padx=8, pady=6)
            self._add_point_fields("input point")

        elif kind == "wait":
            r = self._next()
            ctk.CTkLabel(self.body, text="Min seconds").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.min_entry = ctk.CTkEntry(self.body, width=90)
            self.min_entry.insert(0, str(self.step.min_delay))
            self.min_entry.grid(row=r, column=1, sticky="w", padx=8, pady=6)
            r = self._next()
            ctk.CTkLabel(self.body, text="Max seconds").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.max_entry = ctk.CTkEntry(self.body, width=90)
            self.max_entry.insert(0, str(self.step.max_delay))
            self.max_entry.grid(row=r, column=1, sticky="w", padx=8, pady=6)

    # -- region picking ------------------------------------------------
    def _region_text(self) -> str:
        s = self.step
        if s.use_region and s.region_width and s.region_height:
            return f"{s.region_width}×{s.region_height} at ({s.region_left}, {s.region_top})"
        return "Not set — will use the Test-tab capture area."

    def _pick_region(self) -> None:
        self.withdraw()
        self.app.withdraw()
        self.after(250, self._do_pick_region)

    def _do_pick_region(self) -> None:
        try:
            region = screen.select_region(self.app)
        finally:
            self.app.deiconify()
            self.deiconify()
            self._grab()
        if region is not None:
            self.step.use_region = True
            self.step.region_left = region.left
            self.step.region_top = region.top
            self.step.region_width = region.width
            self.step.region_height = region.height
            self.region_label.configure(text=self._region_text())
            self.app.log(f"Crop region set to {region.width}×{region.height} at ({region.left}, {region.top}).")
        else:
            self.app.log("Crop region selection cancelled.")

    # -- conditional-click rule rows ----------------------------------
    def _add_rule_row(self, rule: Optional[dict] = None) -> None:
        rule = rule or {}
        row = ctk.CTkFrame(self.rules_frame)
        row.pack(fill="x", pady=3)
        row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(row, text="If contains").grid(row=0, column=0, padx=(8, 4), pady=6)
        contains = ctk.CTkEntry(row, placeholder_text="e.g. Response A")
        contains.insert(0, str(rule.get("contains", "")))
        contains.grid(row=0, column=1, sticky="ew", padx=4, pady=6)

        ctk.CTkLabel(row, text="→").grid(row=0, column=2, padx=2)
        x_entry = ctk.CTkEntry(row, width=56, placeholder_text="x")
        x_entry.insert(0, str(rule.get("x", "")))
        x_entry.grid(row=0, column=3, padx=2, pady=6)
        y_entry = ctk.CTkEntry(row, width=56, placeholder_text="y")
        y_entry.insert(0, str(rule.get("y", "")))
        y_entry.grid(row=0, column=4, padx=2, pady=6)

        entry = {"frame": row, "contains": contains, "x": x_entry, "y": y_entry}
        ctk.CTkButton(row, text="🎯", width=34,
                      command=lambda: self._pick_into(x_entry, y_entry)).grid(
            row=0, column=5, padx=2, pady=6)
        ctk.CTkButton(row, text="✕", width=34, fg_color="gray",
                      command=lambda: self._remove_rule_row(entry)).grid(
            row=0, column=6, padx=(2, 8), pady=6)
        self._rule_rows.append(entry)

    def _remove_rule_row(self, entry: dict) -> None:
        try:
            self._rule_rows.remove(entry)
        except ValueError:
            pass
        entry["frame"].destroy()

    # -- coordinate picking -------------------------------------------
    def _pick(self) -> None:
        self._pick_into(self.x_entry, self.y_entry, on_set=lambda: self.use_point_var.set(True))

    def _pick_into(self, x_entry, y_entry, on_set=None) -> None:
        self.app.log("Left-click the target on screen (right-click to cancel)…")
        self.withdraw()
        self.app.iconify()

        def on_done(pt) -> None:
            def _apply() -> None:
                self.app.deiconify()
                self.deiconify()
                self._grab()
                if pt is not None:
                    x_entry.delete(0, "end")
                    x_entry.insert(0, str(pt[0]))
                    y_entry.delete(0, "end")
                    y_entry.insert(0, str(pt[1]))
                    if on_set is not None:
                        on_set()
                    self.app.log(f"Picked ({pt[0]}, {pt[1]}).")
                else:
                    self.app.log("Pick cancelled.")
            self.app.after(0, _apply)

        automation.pick_point_async(on_done)

    # -- save ----------------------------------------------------------
    @staticmethod
    def _to_int(value: str, default: int = 0) -> int:
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _to_float(value: str, default: float = 0.0) -> float:
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    def _save(self) -> None:
        s = self.step
        kind = s.kind

        if hasattr(self, "use_point_var"):
            s.use_point = self.use_point_var.get()
            s.x = self._to_int(self.x_entry.get())
            s.y = self._to_int(self.y_entry.get())

        if kind == "click":
            s.button = self.button_menu.get()
            s.clicks = self._to_int(self.clicks_menu.get(), 1)
        elif kind == "scroll":
            notches = abs(self._to_int(self.notches_entry.get(), 3)) or 3
            s.amount = notches if self.scroll_dir_menu.get() == "Up" else -notches
        elif kind == "type_text":
            s.text = self.text_box.get("1.0", "end").rstrip("\n")
            s.clear_first = self.clear_var.get()
        elif kind == "scroll_capture":
            notches = abs(self._to_int(self.notches_entry.get(), 3)) or 3
            s.amount = -notches
            s.scroll_method = {"Wheel": "wheel", "Arrow keys": "arrows",
                               "Page Down": "pagedown"}.get(self.scroll_method_seg.get(), "wheel")
            s.max_scrolls = max(1, self._to_int(self.maxscroll_entry.get(), 15))
            s.min_delay = self._to_float(self.min_entry.get(), 0.5)
            s.max_delay = self._to_float(self.max_entry.get(), 1.4)
            if s.max_delay < s.min_delay:
                s.max_delay = s.min_delay
            s.start_from_top = self.start_top_var.get()
            s.return_to_top = self.return_top_var.get()
            s.save_to_disk = self.save_disk_var.get()
            s.paste_to_window = self.paste_win_var.get()
            s.dest_x = self._to_int(self.dest_x_entry.get())
            s.dest_y = self._to_int(self.dest_y_entry.get())
            s.clear_first = self.paste_clear_var.get()
            s.keys = self.paste_keys_entry.get().strip().lower()
            s.prompt = self.prompt_box.get("1.0", "end").strip()
            s.var = self.var_entry.get().strip()
        elif kind == "key":
            s.keys = self.keys_combo.get().strip().lower()
        elif kind == "capture_ai":
            s.prompt = self.prompt_box.get("1.0", "end").strip()
            s.type_answer = self.type_answer_var.get()
            s.clear_first = self.clear_var.get()
        elif kind == "ai_find_click":
            s.text = self.find_box.get("1.0", "end").strip()
            s.button = self.button_menu.get()
            s.clicks = self._to_int(self.clicks_menu.get(), 1)
        elif kind == "ai_assert":
            s.prompt = self.prompt_box.get("1.0", "end").strip()
            s.attempts = max(1, self._to_int(self.attempts_entry.get(), 1))
            s.run_restart = self.run_restart_var.get()
        elif kind == "conditional_click":
            s.read_clipboard = self.read_clipboard_var.get()
            s.var = self.var_entry.get().strip() or "value"
            s.use_ai = self.use_ai_var.get()
            s.prompt = self.prompt_box.get("1.0", "end").strip()
            s.button = self.button_menu.get()
            s.clicks = self._to_int(self.clicks_menu.get(), 1)
            rules = []
            for row in self._rule_rows:
                contains = row["contains"].get().strip()
                if not contains:
                    continue
                rules.append({
                    "contains": contains,
                    "x": self._to_int(row["x"].get()),
                    "y": self._to_int(row["y"].get()),
                })
            s.rules = rules
        elif kind == "image_paste":
            s.use_ai_region = self.ai_region_var.get()
            s.attempts = max(1, self._to_int(self.attempts_entry.get(), 3))
            s.prompt = self.crop_desc_box.get("1.0", "end").strip()
            s.button = self.button_menu.get()
            s.clicks = self._to_int(self.clicks_menu.get(), 1)
            s.keys = self.paste_keys_entry.get().strip().lower()
        elif kind == "ai_paste_macro":
            s.prompt = self.prompt_box.get("1.0", "end").strip()
            s.var = self.var_entry.get().strip() or "value"
            s.button = self.button_menu.get()
            s.clicks = self._to_int(self.clicks_menu.get(), 1)
            s.clear_first = self.clear_var.get()
            s.keys = self.paste_keys_entry.get().strip().lower()
        elif kind == "type_answer":
            s.clear_first = self.clear_var.get()
        elif kind == "save_clipboard":
            s.var = self.var_entry.get().strip() or "value"
        elif kind == "remember_screen":
            s.var = self.var_entry.get().strip() or "value"
            s.prompt = self.prompt_box.get("1.0", "end").strip()
        elif kind == "type_memory":
            s.var = self.var_entry.get().strip() or "value"
            s.clear_first = self.clear_var.get()
            s.paste_instant = self.instant_var.get()
        elif kind == "wait":
            s.min_delay = self._to_float(self.min_entry.get(), 0.5)
            s.max_delay = self._to_float(self.max_entry.get(), s.min_delay)
            if s.max_delay < s.min_delay:
                s.max_delay = s.min_delay

        self.on_save(s)
        self.destroy()


def main() -> None:
    # Tk sometimes fails to create its internal menu window during startup over
    # RDP / AnyDesk. On most Windows builds this is a fatal native abort (only
    # the run.ps1 relaunch loop can recover it), but some builds instead raise a
    # catchable tkinter.TclError. Handle that case here with a few quick
    # in-process retries so we don't need a full process restart, and print the
    # sentinel string run.ps1 watches for if we still give up.
    import tkinter as _tk

    app = None
    for attempt in range(1, 6):
        try:
            app = App()
            break
        except _tk.TclError as exc:
            msg = str(exc)
            print("Failed to create the menu window", flush=True)
            print(f"Tk startup failed (attempt {attempt}/5): {msg}", flush=True)
            time.sleep(min(0.5 * attempt, 2.0))
    if app is None:
        raise SystemExit("Tk could not initialise its window after several attempts.")
    app.mainloop()


if __name__ == "__main__":
    main()
