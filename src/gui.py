"""CustomTkinter GUI for the screen-automation AI assistant."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Optional

import customtkinter as ctk
from PIL import Image

from . import ai_client, automation, screen, workflow
from .config import AppConfig, Point, Region
from .workflow import STEP_KINDS, RunContext, Step, WorkflowRunner

PREVIEW_MAX = (460, 300)

COMMON_KEYS = [
    "enter", "tab", "esc", "space", "backspace", "delete",
    "up", "down", "left", "right",
    "home", "end", "pageup", "pagedown",
    "ctrl+a", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+s", "ctrl+f",
    "alt+tab", "f5",
]


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = AppConfig.load()

        ctk.set_appearance_mode(self.cfg.appearance)
        ctk.set_default_color_theme("blue")

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
        self._wf_stop = threading.Event()
        self._wf_thread: Optional[threading.Thread] = None
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
        self.tab_settings = self.tabs.add("Settings")

        self._build_run_tab()
        self._build_workflow_tab()
        self._build_settings_tab()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
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
        self.test_kind_menu = ctk.CTkOptionMenu(
            ts, values=list(STEP_KINDS.values()), command=self._on_pick_test_kind, width=200
        )
        self.test_kind_menu.set("Pick a step type…")
        self.test_kind_menu.grid(row=1, column=0, padx=(10, 6), pady=(0, 10))
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
        tab.grid_rowconfigure(1, weight=1)

        # --- Toolbar ---
        bar = ctk.CTkFrame(tab)
        bar.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 6))
        bar.grid_columnconfigure(6, weight=1)

        self.add_step_menu = ctk.CTkOptionMenu(
            bar, values=list(STEP_KINDS.values()), command=self._on_add_step,
            width=170,
        )
        self.add_step_menu.set("➕  Add step…")
        self.add_step_menu.grid(row=0, column=0, padx=(8, 6), pady=8)

        self.wf_run_btn = ctk.CTkButton(bar, text="▶  Run workflow", command=self._run_workflow, width=130)
        self.wf_run_btn.grid(row=0, column=1, padx=4, pady=8)

        ctk.CTkLabel(bar, text="repeat").grid(row=0, column=3, padx=(12, 2))
        self.repeat_var = ctk.StringVar(value=str(self.cfg.workflow_repeat))
        ctk.CTkEntry(bar, textvariable=self.repeat_var, width=48).grid(row=0, column=4, padx=2)
        ctk.CTkLabel(bar, text="×").grid(row=0, column=5, padx=(0, 8))

        ctk.CTkButton(bar, text="💾 Save", command=self._save_workflow, width=70).grid(
            row=0, column=7, padx=(4, 4), pady=8
        )
        self.wf_stop_btn = ctk.CTkButton(
            bar, text="⏹  Stop", command=self._stop_workflow, width=80,
            fg_color="#a13c3c", hover_color="#7d2e2e",
        )
        self.wf_stop_btn.grid(row=0, column=8, padx=(4, 8), pady=8)

        # --- Step list ---
        self.steps_frame = ctk.CTkScrollableFrame(
            tab, label_text="Steps  (tip: type a new number in a step's box + Enter to move it)")
        self.steps_frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self.steps_frame.grid_columnconfigure(0, weight=1)

        # --- Workflow log ---
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

        self._refresh_step_list()

    def _refresh_step_list(self) -> None:
        for child in self.steps_frame.winfo_children():
            child.destroy()

        if not self._steps:
            ctk.CTkLabel(
                self.steps_frame,
                text="No steps yet. Use “Add step…” to build your sequence:\n"
                     "e.g. Click → Wait → Capture + ask AI → Type AI answer → Press Enter.",
                text_color="gray", justify="left",
            ).grid(row=0, column=0, sticky="w", padx=12, pady=20)
            return

        for i, step in enumerate(self._steps):
            row = ctk.CTkFrame(self.steps_frame)
            row.grid(row=i, column=0, sticky="ew", padx=4, pady=3)
            row.grid_columnconfigure(2, weight=1)

            pos_var = ctk.StringVar(value=str(i + 1))
            pos_entry = ctk.CTkEntry(row, width=42, textvariable=pos_var, justify="center")
            pos_entry.grid(row=0, column=0, padx=(8, 2), pady=6)
            pos_entry.bind(
                "<Return>",
                lambda _e, idx=i, v=pos_var: self._move_step_to_position(idx, v.get()),
            )
            pos_entry.bind("<FocusIn>", lambda _e, w=pos_entry: w.select_range(0, "end"))

            ctk.CTkButton(
                row,
                text="On" if step.enabled else "Off",
                width=44,
                fg_color="#2f6f43" if step.enabled else "#5a5a5a",
                hover_color="#27583a" if step.enabled else "#4a4a4a",
                command=lambda idx=i: self._toggle_step(idx),
            ).grid(row=0, column=1, padx=2)

            color = None if step.enabled else "gray"
            text = step.summary() if step.enabled else f"⊘ {step.summary()}  (disabled)"
            ctk.CTkLabel(
                row, text=text, anchor="w", text_color=color,
                font=("Segoe UI", 12),
            ).grid(row=0, column=2, sticky="ew", padx=6)

            ctk.CTkButton(
                row, text="▶ here", width=56, fg_color="#2f6f43", hover_color="#27583a",
                command=lambda idx=i: self._run_workflow_from(idx),
            ).grid(row=0, column=3, padx=(2, 1), pady=4)
            ctk.CTkButton(
                row, text="▶ one", width=52, fg_color="#2f6f43", hover_color="#27583a",
                command=lambda idx=i: self._run_only_step(idx),
            ).grid(row=0, column=4, padx=(1, 6))

            ctk.CTkButton(row, text="✎", width=30, command=lambda idx=i: self._edit_step(idx)).grid(
                row=0, column=5, padx=2, pady=4)
            ctk.CTkButton(row, text="⤒", width=30, command=lambda idx=i: self._move_step_to(idx, 0)).grid(
                row=0, column=6, padx=2)
            ctk.CTkButton(row, text="▲", width=30, command=lambda idx=i: self._move_step(idx, -1)).grid(
                row=0, column=7, padx=2)
            ctk.CTkButton(row, text="▼", width=30, command=lambda idx=i: self._move_step(idx, 1)).grid(
                row=0, column=8, padx=2)
            ctk.CTkButton(row, text="⤓", width=30, command=lambda idx=i: self._move_step_to(idx, len(self._steps) - 1)).grid(
                row=0, column=9, padx=2)
            ctk.CTkButton(
                row, text="✕", width=30, fg_color="#a13c3c", hover_color="#7d2e2e",
                command=lambda idx=i: self._delete_step(idx),
            ).grid(row=0, column=10, padx=(2, 8))

    # -- step list mutations ------------------------------------------
    def _kind_from_label(self, label: str) -> Optional[str]:
        for kind, name in STEP_KINDS.items():
            if name == label:
                return kind
        return None

    def _on_add_step(self, label: str) -> None:
        kind = self._kind_from_label(label)
        self.add_step_menu.set("➕  Add step…")
        if kind is None:
            return
        step = Step(kind=kind)
        if kind in ("move", "ai_paste_macro", "image_paste"):
            step.use_point = True
        StepEditor(self, step, on_save=lambda s: self._append_step(s))

    def _append_step(self, step: Step) -> None:
        self._steps.append(step)
        self._refresh_step_list()

    def _edit_step(self, index: int) -> None:
        step = self._steps[index]
        StepEditor(self, step, on_save=lambda s, idx=index: self._replace_step(idx, s))

    def _replace_step(self, index: int, step: Step) -> None:
        self._steps[index] = step
        self._refresh_step_list()

    def _delete_step(self, index: int) -> None:
        del self._steps[index]
        self._refresh_step_list()

    def _move_step(self, index: int, delta: int) -> None:
        new = index + delta
        if 0 <= new < len(self._steps):
            self._steps[index], self._steps[new] = self._steps[new], self._steps[index]
            self._refresh_step_list()

    def _move_step_to(self, index: int, target: int) -> None:
        if index == target or not (0 <= target < len(self._steps)):
            return
        step = self._steps.pop(index)
        self._steps.insert(target, step)
        self._refresh_step_list()

    def _move_step_to_position(self, index: int, value: str) -> None:
        """Move the step at ``index`` to the 1-based position typed by the user."""
        try:
            target = int(float(value)) - 1
        except (ValueError, TypeError):
            self._refresh_step_list()  # reset the box to its real value
            return
        target = max(0, min(len(self._steps) - 1, target))
        if target == index:
            self._refresh_step_list()
        else:
            self._move_step_to(index, target)

    def _toggle_step(self, index: int) -> None:
        self._steps[index].enabled = not self._steps[index].enabled
        self._refresh_step_list()

    # -- run / stop ----------------------------------------------------
    def _save_workflow(self) -> None:
        self.cfg.steps = [s.as_dict() for s in self._steps]
        try:
            self.cfg.workflow_repeat = max(1, int(self.repeat_var.get()))
        except ValueError:
            self.cfg.workflow_repeat = 1
        self._save_settings()
        self.log(f"Workflow saved ({len(self._steps)} steps).")

    def _start_run(self, steps: list, repeat: int, header: str,
                   start_number: int = 1) -> None:
        """Shared launcher for full / from-here / single-step runs."""
        if self._wf_thread and self._wf_thread.is_alive():
            self.log("Workflow already running.")
            return
        if not any(s.enabled for s in steps):
            self.log("No enabled steps to run.")
            return
        self._collect_runtime_settings()
        self._wf_stop.clear()
        self.wf_run_btn.configure(state="disabled")
        self.log(header)
        if any(s.enabled and s.kind in ("capture_ai", "ai_find_click",
                                        "remember_screen", "image_paste")
               for s in steps):
            self.log("Screen captures are local to this PC (invisible to an AnyDesk "
                     "remote); only clicks/keys are sent.")

        ctx = RunContext(
            cfg=self.cfg, log=self.log,
            on_image=self._show_preview, on_answer=self._set_answer,
        )
        runner = WorkflowRunner(steps, ctx, self._wf_stop)

        def _run() -> None:
            try:
                runner.run(repeat=repeat, start_number=start_number)
            finally:
                self.after(0, lambda: self.wf_run_btn.configure(state="normal"))

        self._wf_thread = threading.Thread(target=_run, daemon=True)
        self._wf_thread.start()

    def _run_workflow(self) -> None:
        try:
            repeat = max(1, int(self.repeat_var.get()))
        except ValueError:
            repeat = 1
        self.cfg.workflow_repeat = repeat
        n = sum(s.enabled for s in self._steps)
        self._start_run(self._steps, repeat,
                        f"Starting workflow ({n} steps × {repeat})…", start_number=1)

    def _run_workflow_from(self, index: int) -> None:
        if not (0 <= index < len(self._steps)):
            return
        steps = self._steps[index:]
        n = sum(s.enabled for s in steps)
        self._start_run(steps, 1,
                        f"Starting from step {index + 1} ({n} steps)…",
                        start_number=index + 1)

    def _run_only_step(self, index: int) -> None:
        if not (0 <= index < len(self._steps)):
            return
        step = self._steps[index]
        # Force-run this one step even if its checkbox is unticked.
        run_step = Step.from_dict({**step.as_dict(), "enabled": True})
        self._start_run([run_step], 1,
                        f"Running only step {index + 1}: {step.summary()}",
                        start_number=index + 1)

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
        self.provider_menu = ctk.CTkOptionMenu(
            frame, values=ai_client.provider_labels(), command=self._on_provider_change
        )
        self.provider_menu.set(ai_client.label_for(self.cfg.provider))
        self.provider_menu.grid(row=r, column=1, columnspan=2, sticky="ew", padx=10, pady=8)

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
        self.model_combo = ctk.CTkComboBox(frame, values=[ai_client.default_model(self.cfg.provider)])
        self.model_combo.grid(row=r, column=1, sticky="ew", padx=10, pady=8)
        self.model_combo.set(
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

        r = row_label("Appearance")
        self.appearance_menu = ctk.CTkOptionMenu(
            frame, values=["System", "Dark", "Light"], command=self._on_appearance
        )
        self.appearance_menu.grid(row=r, column=1, sticky="w", padx=10, pady=8)
        self.appearance_menu.set(self.cfg.appearance)

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
        self.test_kind_menu.set("Pick a step type…")
        kind = None
        for k, name in STEP_KINDS.items():
            if name == label:
                kind = k
                break
        if kind is None:
            return
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
            try:
                runner.run(repeat=1)
            finally:
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
        self.cfg.use_directinput = self.directinput_var.get()
        automation.set_directinput(self.cfg.use_directinput)
        self.cfg.disable_failsafe = self.failsafe_var.get()
        automation.set_failsafe(not self.cfg.disable_failsafe)
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
        ctk.set_appearance_mode(value)
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
        self.model_combo.configure(values=[ai_client.default_model(new_provider)])
        self.model_combo.set(model)
        self._update_provider_hints(new_provider)
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
                current = self.model_combo.get()
                self.model_combo.configure(values=models)
                if current not in models:
                    self.model_combo.set(models[0])
                self.log(f"Fetched {len(models)} models — choose one from the Model dropdown.")
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _save_settings(self) -> None:
        self._collect_runtime_settings()
        self.cfg.region.full_screen = self.full_screen_var.get()
        self.cfg.steps = [s.as_dict() for s in self._steps]
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
            try:
                self.cfg.workflow_repeat = max(1, int(self.repeat_var.get()))
            except ValueError:
                self.cfg.workflow_repeat = 1
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
            self.button_menu = ctk.CTkOptionMenu(self.body, values=["left", "right", "middle"])
            self.button_menu.set(self.step.button)
            self.button_menu.grid(row=r, column=1, sticky="w", padx=8, pady=6)

            r = self._next()
            ctk.CTkLabel(self.body, text="Clicks").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.clicks_menu = ctk.CTkOptionMenu(self.body, values=["1", "2", "3"])
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
            self.scroll_dir_menu = ctk.CTkOptionMenu(self.body, values=["Down", "Up"])
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
            self.keys_combo = ctk.CTkComboBox(self.body, values=COMMON_KEYS)
            self.keys_combo.set(self.step.keys or "enter")
            self.keys_combo.grid(row=r, column=1, sticky="ew", padx=8, pady=6)
            self._next()
            ctk.CTkLabel(
                self.body,
                text="Pick a key from the list, or type your own combo "
                     "(e.g. ctrl+shift+s). Common keys: enter, tab, esc, end, "
                     "home, pageup, pagedown, arrows, f1–f12.",
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
            self.button_menu = ctk.CTkOptionMenu(self.body, values=["left", "right", "middle"])
            self.button_menu.set(self.step.button)
            self.button_menu.grid(row=r, column=1, sticky="w", padx=8, pady=6)

            r = self._next()
            ctk.CTkLabel(self.body, text="Clicks").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.clicks_menu = ctk.CTkOptionMenu(self.body, values=["1", "2", "3"])
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
            self.button_menu = ctk.CTkOptionMenu(self.body, values=["left", "right", "middle"])
            self.button_menu.set(self.step.button)
            self.button_menu.grid(row=r, column=1, sticky="w", padx=8, pady=6)

            r = self._next()
            ctk.CTkLabel(self.body, text="Clicks").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.clicks_menu = ctk.CTkOptionMenu(self.body, values=["1", "2", "3"])
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
            self.button_menu = ctk.CTkOptionMenu(self.body, values=["left", "right", "middle"])
            self.button_menu.set(self.step.button)
            self.button_menu.grid(row=r, column=1, sticky="w", padx=8, pady=6)

            r = self._next()
            ctk.CTkLabel(self.body, text="Destination clicks").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.clicks_menu = ctk.CTkOptionMenu(self.body, values=["1", "2", "3"])
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
            self.button_menu = ctk.CTkOptionMenu(self.body, values=["left", "right", "middle"])
            self.button_menu.set(self.step.button)
            self.button_menu.grid(row=r, column=1, sticky="w", padx=8, pady=6)

            r = self._next()
            ctk.CTkLabel(self.body, text="Destination clicks").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            self.clicks_menu = ctk.CTkOptionMenu(self.body, values=["1", "2", "3"])
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
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
