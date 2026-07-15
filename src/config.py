"""Persistent configuration handling.

Settings are loaded from (in order of precedence):
1. ``config.json`` in the project root (written by the GUI).
2. Environment variables / a ``.env`` file.
3. Built-in defaults.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
HISTORY_PATH = PROJECT_ROOT / "history.json"

DEFAULT_PROMPT = (
    "You are looking at a screenshot of a Windows screen. "
    "Read any question, problem, or task shown in the image and provide the "
    "best answer. Reply with ONLY the answer text that should be typed into "
    "the input field - no explanations, no labels, no quotes."
)


@dataclass
class Region:
    """A screen capture region. When ``full_screen`` is True, the other
    values are ignored and the primary monitor is captured."""

    full_screen: bool = True
    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Point:
    """A screen coordinate. ``set`` indicates the user has chosen it."""

    x: int = 0
    y: int = 0
    set: bool = False

    def as_tuple(self) -> tuple[int, int]:
        return (self.x, self.y)


@dataclass
class AppConfig:
    api_key: str = ""
    model: str = "gpt-4o-mini"
    base_url: str = ""
    prompt: str = DEFAULT_PROMPT

    # AI provider selection. ``api_keys`` / ``models`` remember a value per
    # provider so switching providers restores the right key/model.
    provider: str = "openai"
    api_keys: dict = field(default_factory=dict)
    models: dict = field(default_factory=dict)
    # Two-pass "zoom in" refinement for AI: find & click (more accurate, slower).
    ai_locate_refine: bool = True
    # Fall back to on-device OCR to read screen text when the AI provider is
    # unavailable (out of usage/credits, rate-limited, or no key set).
    ocr_fallback: bool = True

    region: Region = field(default_factory=Region)
    input_point: Point = field(default_factory=Point)
    submit_point: Point = field(default_factory=Point)

    click_submit: bool = False
    clear_before_type: bool = True
    type_interval: float = 0.06
    loop_enabled: bool = False
    loop_seconds: float = 10.0
    appearance: str = "System"  # System | Dark | Light

    # Human-like pacing applied across automation actions.
    humanize: bool = True
    humanize_min: float = 0.4
    humanize_max: float = 1.2
    # Occasionally mistype a key and correct it (only when humanize is on).
    humanize_typos: bool = True

    # Send hardware scan-code input (needed for AnyDesk / RDP / games).
    use_directinput: bool = True
    # Disable the corner-of-screen fail-safe (needed for minimized RDP / VPS).
    disable_failsafe: bool = False
    # Pause RDP clipboard sync (kill rdpclip.exe) while a workflow runs so the
    # client machine can't overwrite the clipboard mid-paste.  Restored after.
    manage_rdp_clipboard: bool = True

    # Workflow: ordered list of step dicts (see workflow.Step) + repeat count.
    steps: list = field(default_factory=list)
    workflow_repeat: int = 1
    # Steps run when an AI check fails with "run restart workflow" enabled.
    restart_steps: list = field(default_factory=list)

    # Multiple named workflows.  Each entry:
    #   { "steps": [...], "restart_steps": [...], "repeat": N }
    named_workflows: dict = field(default_factory=dict)
    active_workflow: str = "Default"

    # ---- persistence -------------------------------------------------
    @classmethod
    def load(cls) -> "AppConfig":
        cfg = cls()
        # Layer 1: environment / .env
        cfg.api_key = os.getenv("OPENAI_API_KEY", cfg.api_key)
        cfg.model = os.getenv("OPENAI_MODEL", cfg.model)
        cfg.base_url = os.getenv("OPENAI_BASE_URL", cfg.base_url)
        cfg.provider = os.getenv("AI_PROVIDER", cfg.provider)

        # Layer 2: config.json (overrides env if present)
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                cfg._apply(data)
            except (json.JSONDecodeError, OSError):
                pass
        return cfg

    def _apply(self, data: dict) -> None:
        for key in ("api_key", "model", "base_url", "prompt", "click_submit",
                    "clear_before_type", "type_interval", "loop_enabled",
                    "loop_seconds", "appearance", "humanize", "humanize_min",
                    "humanize_max", "humanize_typos", "workflow_repeat",
                    "use_directinput", "disable_failsafe", "provider",
                    "ai_locate_refine", "manage_rdp_clipboard", "ocr_fallback"):
            if key in data and data[key] not in (None, ""):
                setattr(self, key, data[key])
        if "steps" in data and isinstance(data["steps"], list):
            self.steps = data["steps"]
        if "workflow_repeat" in data and isinstance(data["workflow_repeat"], int):
            self.workflow_repeat = max(1, data["workflow_repeat"])
        if "restart_steps" in data and isinstance(data["restart_steps"], list):
            self.restart_steps = data["restart_steps"]

        # Named workflows ------------------------------------------------
        if "named_workflows" in data and isinstance(data["named_workflows"], dict):
            self.named_workflows = data["named_workflows"]
        if "active_workflow" in data and isinstance(data["active_workflow"], str):
            self.active_workflow = data["active_workflow"]

        # Migrate old single-workflow config into named_workflows
        if not self.named_workflows:
            self.named_workflows["Default"] = {
                "steps": self.steps,
                "restart_steps": self.restart_steps,
                "repeat": self.workflow_repeat,
            }
            self.active_workflow = "Default"

        # Load the active workflow's data into the flat fields used by the rest
        # of the app.  This lets existing code keep reading cfg.steps etc.
        if self.active_workflow in self.named_workflows:
            wf = self.named_workflows[self.active_workflow]
            self.steps = wf.get("steps", self.steps)
            self.restart_steps = wf.get("restart_steps", self.restart_steps)
            self.workflow_repeat = max(1, wf.get("repeat", self.workflow_repeat))

        if "api_keys" in data and isinstance(data["api_keys"], dict):
            self.api_keys = data["api_keys"]
        if "models" in data and isinstance(data["models"], dict):
            self.models = data["models"]
        # api_key/base_url may legitimately be empty strings in config.json
        if "api_key" in data and data["api_key"] is not None:
            self.api_key = data["api_key"]
        if "base_url" in data and data["base_url"] is not None:
            self.base_url = data["base_url"]
        if "region" in data and isinstance(data["region"], dict):
            self.region = Region(**{**self.region.as_dict(), **data["region"]})
        if "input_point" in data and isinstance(data["input_point"], dict):
            self.input_point = Point(**{**asdict(self.input_point), **data["input_point"]})
        if "submit_point" in data and isinstance(data["submit_point"], dict):
            self.submit_point = Point(**{**asdict(self.submit_point), **data["submit_point"]})

    def save(self) -> None:
        data = {
            "api_key": self.api_key,
            "model": self.model,
            "base_url": self.base_url,
            "prompt": self.prompt,
            "provider": self.provider,
            "api_keys": self.api_keys,
            "models": self.models,
            "ai_locate_refine": self.ai_locate_refine,
            "ocr_fallback": self.ocr_fallback,
            "region": self.region.as_dict(),
            "input_point": asdict(self.input_point),
            "submit_point": asdict(self.submit_point),
            "click_submit": self.click_submit,
            "clear_before_type": self.clear_before_type,
            "type_interval": self.type_interval,
            "loop_enabled": self.loop_enabled,
            "loop_seconds": self.loop_seconds,
            "appearance": self.appearance,
            "humanize": self.humanize,
            "humanize_min": self.humanize_min,
            "humanize_max": self.humanize_max,
            "humanize_typos": self.humanize_typos,
            "use_directinput": self.use_directinput,
            "disable_failsafe": self.disable_failsafe,
            "manage_rdp_clipboard": self.manage_rdp_clipboard,
            # Keep flat steps/restart_steps in sync with the active named workflow
            # so that old config readers can still fall back to them.
            "steps": self.steps,
            "workflow_repeat": self.workflow_repeat,
            "restart_steps": self.restart_steps,
            "named_workflows": self.named_workflows,
            "active_workflow": self.active_workflow,
        }
        # Avoid duplicating large step lists: if named_workflows already stores
        # the active workflow's steps we can keep the top-level list empty to
        # prevent the config file from tripling in size.
        if self.named_workflows:
            data["steps"] = []
            data["restart_steps"] = []
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
