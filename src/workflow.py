"""Workflow model and execution engine.

A *workflow* is an ordered list of :class:`Step` objects. Each step has a
``kind`` and a set of parameters. The :class:`WorkflowRunner` executes the
steps in order on a background thread, inserting human-like delays between
steps and supporting cooperative cancellation via a ``threading.Event``.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Callable, Optional

from PIL import Image

from . import ai_client, automation, screen
from .config import AppConfig, Region

# Available step kinds and their human-readable names (used by the GUI).
STEP_KINDS: dict[str, str] = {
    "click": "Click",
    "move": "Move mouse",
    "scroll": "Scroll",
    "type_text": "Type text",
    "key": "Press key / hotkey",
    "capture_ai": "Capture + ask AI",
    "type_answer": "Type AI answer",
    "ai_find_click": "AI: find & click",
    "ai_assert": "AI check — stop if condition fails",
    "conditional_click": "If text → click",
    "image_paste": "Crop image → paste to window",
    "ai_paste_macro": "AI read → paste to other window",
    "save_clipboard": "Remember clipboard",
    "remember_screen": "Remember screen value (AI)",
    "type_memory": "Type remembered value",
    "wait": "Wait",
}


@dataclass
class Step:
    kind: str = "click"
    enabled: bool = True

    # coordinates (used by click / move / scroll / type targets)
    x: int = 0
    y: int = 0
    use_point: bool = False

    # click
    button: str = "left"   # left | right | middle
    clicks: int = 1

    # type_text / type_answer
    text: str = ""
    clear_first: bool = True

    # key
    keys: str = ""         # e.g. "enter", "ctrl+a"

    # scroll
    amount: int = -3       # negative = down, positive = up

    # capture_ai
    prompt: str = ""       # optional override of the global prompt
    type_answer: bool = False

    # image_paste crop rectangle (falls back to the global region if unset)
    use_region: bool = False
    region_left: int = 0
    region_top: int = 0
    region_width: int = 0
    region_height: int = 0
    # image_paste: let the AI find the crop rectangle (uses `prompt` to describe it)
    use_ai_region: bool = False
    # how many times to retry an AI lookup that returns nothing (>=1)
    attempts: int = 3

    # memory (save_clipboard / type_memory)
    var: str = "value"
    # type_memory: paste instantly via clipboard + Ctrl+V instead of typing
    paste_instant: bool = False

    # conditional_click: read text from clipboard (else memory `var`) and click
    # the point of the first matching rule. Each rule: {"contains", "x", "y"}.
    # When `use_ai` is set, the AI decides which rule applies (using `prompt` as
    # the instruction) instead of plain substring matching.
    read_clipboard: bool = True
    rules: list = field(default_factory=list)
    use_ai: bool = False

    # wait
    min_delay: float = 0.5
    max_delay: float = 1.5

    # ai_assert: when the check fails, run the restart workflow then loop the main workflow
    run_restart: bool = False

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Step":
        valid = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in valid})

    # -- display -------------------------------------------------------
    def summary(self) -> str:
        at = f" at ({self.x}, {self.y})" if self.use_point else " at cursor"
        if self.kind == "click":
            kind = {1: "Click", 2: "Double-click", 3: "Triple-click"}.get(self.clicks, f"{self.clicks}× click")
            btn = "" if self.button == "left" else f" ({self.button})"
            return f"{kind}{btn}{at}"
        if self.kind == "move":
            return f"Move mouse to ({self.x}, {self.y})"
        if self.kind == "scroll":
            direction = "up" if self.amount > 0 else "down"
            return f"Scroll {direction} {abs(self.amount)}{at if self.use_point else ''}"
        if self.kind == "type_text":
            preview = (self.text[:32] + "…") if len(self.text) > 32 else self.text
            return f"Type text: \"{preview}\""
        if self.kind == "key":
            return f"Press: {self.keys or '(unset)'}"
        if self.kind == "capture_ai":
            extra = " → type answer" if self.type_answer else ""
            return f"Capture + ask AI{extra}"
        if self.kind == "ai_find_click":
            desc = (self.text[:30] + "…") if len(self.text) > 30 else (self.text or "(describe target)")
            kind = {1: "click", 2: "double-click", 3: "triple-click"}.get(self.clicks, f"{self.clicks}× click")
            btn = "" if self.button == "left" else f" {self.button}"
            return f"AI find &{btn} {kind}: \"{desc}\""
        if self.kind == "ai_assert":
            desc = (self.prompt[:34] + "…") if len(self.prompt) > 34 else (self.prompt or "(condition)")
            extra = " → restart main" if self.run_restart else ""
            return f"AI check (stop if fails): \"{desc}\"{extra}"
        if self.kind == "conditional_click":
            src = "clipboard" if self.read_clipboard else f"[{self.var}]"
            n = len(self.rules)
            how = "AI decides" if self.use_ai else "matches"
            return f"If {src} {how} → click ({n} option{'s' if n != 1 else ''})"
        if self.kind == "image_paste":
            if self.use_ai_region:
                desc = (self.prompt[:24] + "…") if len(self.prompt) > 24 else (self.prompt or "AI-detected area")
                area = f"AI crop: \"{desc}\""
            elif self.use_region and self.region_width and self.region_height:
                area = f"{self.region_width}×{self.region_height} crop"
            else:
                area = "capture area"
            where = f"({self.x}, {self.y})" if self.use_point else "cursor"
            after = f" → {self.keys}" if self.keys.strip() else ""
            return f"Crop image ({area}) → click {where} → paste image{after}"
        if self.kind == "ai_paste_macro":
            where = f"({self.x}, {self.y})" if self.use_point else "cursor"
            after = f" → {self.keys}" if self.keys.strip() else ""
            return f"AI read → clipboard → click {where} → paste{after} → [{self.var}]"
        if self.kind == "type_answer":
            return f"Type AI answer{at if self.use_point else ''}"
        if self.kind == "save_clipboard":
            return f"Remember clipboard → [{self.var}]"
        if self.kind == "remember_screen":
            return f"Remember screen (AI) → [{self.var}]"
        if self.kind == "type_memory":
            verb = "Paste remembered" if self.paste_instant else "Type remembered"
            return f"{verb} [{self.var}]{at if self.use_point else ''}"
        if self.kind == "wait":
            if self.min_delay == self.max_delay:
                return f"Wait {self.min_delay:g}s"
            return f"Wait {self.min_delay:g}–{self.max_delay:g}s"
        return self.kind


@dataclass
class RunContext:
    cfg: AppConfig
    log: Callable[[str], None]
    on_image: Callable[[Image.Image], None] = lambda img: None
    on_answer: Callable[[str], None] = lambda text: None
    last_answer: str = ""
    memory: dict = field(default_factory=dict)
    restart_steps: list = field(default_factory=list)
    main_steps: list = field(default_factory=list)
    on_pass_complete: Callable[["RunRecord"], None] = lambda r: None


@dataclass
class RunRecord:
    """One completed workflow pass (single iteration), stored in history."""
    started_at: str       # local timestamp when this pass started
    run_type: str         # "Full workflow", "From step N", "Single step N"
    total_steps: int      # number of enabled steps in this pass
    pass_number: int      # 1-based index within the repeat run
    total_passes: int     # total requested repeats for the run
    restarts: int         # restart-workflow cycles triggered in this pass
    duration_s: float     # wall-clock seconds for this pass
    status: str           # "Finished" | "Stopped" | "Failed"
    fail_reason: str = "" # brief reason when status == "Failed"

    def as_dict(self) -> dict:
        return asdict(self)


class WorkflowRunner:
    """Executes a list of steps with optional human-like pacing."""

    def __init__(
        self,
        steps: list[Step],
        ctx: RunContext,
        stop_event: threading.Event,
        *,
        sub_workflow: bool = False,
    ) -> None:
        self.steps = steps
        self.ctx = ctx
        self.stop = stop_event
        self._in_restart = False
        self._sub_workflow = sub_workflow
        # populated after run() returns
        self.passes_completed: int = 0
        self.restarts: int = 0
        self.final_status: str = "Finished"   # Finished | Stopped | Failed
        self.fail_reason: str = ""

    def _steps_to_run(self) -> list[Step]:
        if self._sub_workflow:
            return self.steps
        return self.ctx.main_steps or self.steps

    def run(
        self,
        repeat: int = 1,
        start_number: int = 1,
        start_at: int = 0,
        end_at: Optional[int] = None,
        run_type: str = "Workflow run",
        total_steps: int = 0,
    ) -> None:
        all_steps = self._steps_to_run()
        if end_at is None:
            end_at = len(all_steps)
        run_start_at = max(0, min(start_at, len(all_steps)))
        run_end_at = max(run_start_at, min(end_at, len(all_steps)))
        loops = repeat if repeat > 0 else 1
        for i in range(loops):
            if self.stop.is_set():
                self.final_status = "Stopped"
                break
            if loops > 1:
                self.ctx.log(f"--- Pass {i + 1} of {loops} ---")
            log_start = start_number
            pass_restarts = 0
            pass_start = time.monotonic()
            pass_started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            pass_status = "Finished"
            pass_fail_reason = ""
            while True:
                if self.stop.is_set():
                    self.ctx.log("Stopped.")
                    self.final_status = "Stopped"
                    pass_status = "Stopped"
                    self._emit_pass(run_type, total_steps, i + 1, loops,
                                    pass_restarts, pass_start, pass_started_at,
                                    pass_status, pass_fail_reason)
                    return
                restart_main = False
                for offset, step in enumerate(all_steps[run_start_at:run_end_at]):
                    index = log_start + offset
                    if self.stop.is_set():
                        self.ctx.log("Stopped.")
                        self.final_status = "Stopped"
                        pass_status = "Stopped"
                        self._emit_pass(run_type, total_steps, i + 1, loops,
                                        pass_restarts, pass_start, pass_started_at,
                                        pass_status, pass_fail_reason)
                        return
                    if not step.enabled:
                        continue
                    self.ctx.log(f"Step {index}: {step.summary()}")
                    try:
                        outcome = self._exec(step)
                    except automation.FailSafeException:
                        self.ctx.log("Fail-safe triggered (mouse in corner). Stopping.")
                        self.stop.set()
                        self.final_status = "Stopped"
                        self.fail_reason = "Fail-safe triggered"
                        pass_status = "Stopped"
                        pass_fail_reason = "Fail-safe triggered"
                        self._emit_pass(run_type, total_steps, i + 1, loops,
                                        pass_restarts, pass_start, pass_started_at,
                                        pass_status, pass_fail_reason)
                        return
                    except ai_client.AIError as exc:
                        self.ctx.log(f"  AI error: {exc}")
                        outcome = None
                    except Exception as exc:  # noqa: BLE001
                        self.ctx.log(f"  Error: {exc}")
                        outcome = None
                    if outcome == "stop":
                        self.final_status = "Failed"
                        pass_status = "Failed"
                        pass_fail_reason = self.fail_reason
                        self._emit_pass(run_type, total_steps, i + 1, loops,
                                        pass_restarts, pass_start, pass_started_at,
                                        pass_status, pass_fail_reason)
                        return
                    if outcome == "restart_main":
                        restart_main = True
                        break
                    self._human_pause()
                if restart_main and not self._sub_workflow:
                    self.restarts += 1
                    pass_restarts += 1
                    run_start_at = 0
                    run_end_at = len(all_steps)
                    log_start = 1
                    self.ctx.log("--- Restarting main workflow from step 1 ---")
                    continue
                break
            self.passes_completed += 1
            self._emit_pass(run_type, total_steps, i + 1, loops,
                            pass_restarts, pass_start, pass_started_at,
                            pass_status, pass_fail_reason)
        if not self.stop.is_set():
            self.ctx.log("Workflow finished.")

    def _emit_pass(
        self,
        run_type: str,
        total_steps: int,
        pass_number: int,
        total_passes: int,
        restarts: int,
        pass_start: float,
        started_at: str,
        status: str,
        fail_reason: str,
    ) -> None:
        if self._sub_workflow:
            return
        record = RunRecord(
            started_at=started_at,
            run_type=run_type,
            total_steps=total_steps,
            pass_number=pass_number,
            total_passes=total_passes,
            restarts=restarts,
            duration_s=round(time.monotonic() - pass_start, 1),
            status=status,
            fail_reason=fail_reason,
        )
        self.ctx.on_pass_complete(record)

    # -- per-step execution -------------------------------------------
    def _exec(self, step: Step) -> Optional[str]:
        cfg = self.ctx.cfg
        human = cfg.humanize
        point = (step.x, step.y)

        if step.kind == "click":
            if step.use_point:
                automation.click(*point, button=step.button, clicks=step.clicks, human=human)
            else:
                automation.click(button=step.button, clicks=step.clicks, human=human)

        elif step.kind == "move":
            automation.move(*point, human=human)

        elif step.kind == "scroll":
            if step.use_point:
                automation.scroll(step.amount, step.x, step.y, human=human)
            else:
                automation.scroll(step.amount, human=human)

        elif step.kind == "type_text":
            automation.type_text(step.text, clear_first=step.clear_first,
                                 interval=cfg.type_interval, human=human)

        elif step.kind == "key":
            automation.press_keys(step.keys, human=human)

        elif step.kind == "capture_ai":
            image = screen.capture(cfg.region)
            self.ctx.on_image(image)
            prompt = step.prompt.strip() or cfg.prompt
            self.ctx.log(f"  Asking {cfg.model}…")
            answer = ai_client.ask(
                image, prompt,
                api_key=cfg.api_key, model=cfg.model,
                base_url=cfg.base_url or None, provider=cfg.provider,
            )
            self.ctx.last_answer = answer
            self.ctx.on_answer(answer)
            self.ctx.log(f"  AI answered ({len(answer)} chars).")
            if step.type_answer and answer:
                if step.use_point:
                    automation.click_and_type(point, answer, clear_first=step.clear_first,
                                              interval=cfg.type_interval, human=human)
                else:
                    automation.type_text(answer, clear_first=step.clear_first,
                                         interval=cfg.type_interval, human=human)

        elif step.kind == "ai_find_click":
            desc = step.text.strip()
            if not desc:
                self.ctx.log("  No target description set; skipping.")
                return
            image = screen.capture(cfg.region)
            self.ctx.on_image(image)
            self.ctx.log(f"  Captured {image.width}×{image.height}px; asking {cfg.model} to locate: \"{desc}\"…")
            loc = ai_client.locate(
                image, desc,
                api_key=cfg.api_key, model=cfg.model,
                base_url=cfg.base_url or None, provider=cfg.provider,
                refine=cfg.ai_locate_refine, log=self.ctx.log,
            )
            if loc is None:
                self.ctx.log("  AI could not find that element on screen.")
                return
            img_x = int(loc[0] * image.width)
            img_y = int(loc[1] * image.height)
            self.ctx.on_image(screen.mark(image, img_x, img_y))
            origin_x, origin_y = screen.region_origin(cfg.region)
            sx = origin_x + img_x
            sy = origin_y + img_y
            self.ctx.log(f"  AI located it at ({sx}, {sy}); clicking.")
            automation.click(sx, sy, button=step.button, clicks=step.clicks, human=human)

        elif step.kind == "ai_assert":
            cond = step.prompt.strip() or "the screen is in the expected state"
            attempts = max(1, int(step.attempts or 1))
            met, reason = None, ""
            for attempt in range(1, attempts + 1):
                if self.stop.is_set():
                    return None
                image = screen.capture(cfg.region)
                self.ctx.on_image(image)
                self.ctx.log(f"  Checking (attempt {attempt}/{attempts}): \"{cond}\"…")
                met, reason = ai_client.check_condition(
                    image, cond,
                    api_key=cfg.api_key, model=cfg.model,
                    base_url=cfg.base_url or None, provider=cfg.provider,
                    log=self.ctx.log,
                )
                if met:
                    break
                if attempt < attempts:
                    self.ctx.log("  Condition not met yet; waiting and re-checking…")
                    self._sleep(1.0)
            if met:
                self.ctx.log(f"  ✓ Condition met. {reason}".rstrip())
                return None
            why = reason or ("could not parse AI reply" if met is None else "")
            self.ctx.log(f"  ✗ Condition NOT met. {why}".rstrip())
            restart_steps = self.ctx.restart_steps
            if (step.run_restart and not self._sub_workflow and not self._in_restart
                    and restart_steps and any(s.enabled for s in restart_steps)):
                self.ctx.log("  Running restart workflow, then restarting main workflow…")
                self._run_restart_workflow(restart_steps)
                if self.stop.is_set():
                    return "stop"
                return "restart_main"
            self.ctx.log("  Stopping workflow.")
            self.stop.set()
            self.fail_reason = f"AI check failed: {step.prompt[:60]}"
            return "stop"

        elif step.kind == "conditional_click":
            if step.read_clipboard:
                value = automation.get_clipboard()
                source = "clipboard"
            else:
                value = self.ctx.memory.get(step.var, "")
                source = f"[{step.var}]"
            preview = value.replace("\n", " ")
            preview = (preview[:60] + "…") if len(preview) > 60 else preview
            self.ctx.log(f"  Condition text from {source}: \"{preview}\"")
            if not value:
                self.ctx.log("  Condition text is empty; skipping.")
                return
            matched = None
            if step.use_ai:
                options = [str(r.get("contains", "")).strip() for r in step.rules
                           if str(r.get("contains", "")).strip()]
                self.ctx.log(f"  Asking {cfg.model} to choose among: {options}…")
                choice = ai_client.classify(
                    value, options, instruction=step.prompt,
                    api_key=cfg.api_key, model=cfg.model,
                    base_url=cfg.base_url or None, provider=cfg.provider,
                    log=self.ctx.log,
                )
                if choice is not None:
                    self.ctx.log(f"  AI chose: \"{choice}\"")
                    matched = next((r for r in step.rules
                                    if str(r.get("contains", "")).strip() == choice), None)
            else:
                haystack = value.lower()
                for rule in step.rules:
                    needle = str(rule.get("contains", "")).strip()
                    if needle and needle.lower() in haystack:
                        matched = rule
                        break
            if matched is None:
                self.ctx.log("  No condition matched; skipping click.")
                return
            mx, my = int(matched.get("x", 0)), int(matched.get("y", 0))
            self.ctx.log(f"  Matched \"{matched.get('contains')}\" → click ({mx}, {my}).")
            automation.click(mx, my, button=step.button, clicks=step.clicks, human=human)

        elif step.kind == "image_paste":
            if step.use_region and step.region_width and step.region_height:
                region = Region(
                    full_screen=False,
                    left=step.region_left, top=step.region_top,
                    width=step.region_width, height=step.region_height,
                )
            else:
                region = cfg.region
            image = screen.capture(region)

            if step.use_ai_region:
                desc = step.prompt.strip() or cfg.prompt
                attempts = max(1, int(step.attempts or 1))
                box = None
                for attempt in range(1, attempts + 1):
                    if self.stop.is_set():
                        return
                    self.ctx.log(f"  Asking {cfg.model} to find the area to crop "
                                 f"(attempt {attempt}/{attempts})…")
                    box = ai_client.locate_region(
                        image, desc,
                        api_key=cfg.api_key, model=cfg.model,
                        base_url=cfg.base_url or None, provider=cfg.provider,
                        pad=6, refine=cfg.ai_locate_refine, log=self.ctx.log,
                    )
                    if box is not None:
                        break
                    if attempt < attempts:
                        self.ctx.log("  Not found; re-capturing and retrying…")
                        self._sleep(0.6)
                        image = screen.capture(region)
                if box is None:
                    self.ctx.log(f"  AI could not find that area after {attempts} attempts; skipping.")
                    return
                self.ctx.on_image(screen.mark_box(image, box))
                self.ctx.log(f"  AI crop box: {box}.")
                crop = image.crop(box)
            else:
                crop = image
                self.ctx.on_image(crop)

            if not automation.set_clipboard_image(crop):
                self.ctx.log("  Could not copy the image to the clipboard.")
                return
            self.ctx.log(f"  Cropped {crop.width}×{crop.height} image → clipboard.")
            if step.use_point:
                self.ctx.log(f"  Clicking destination ({step.x}, {step.y}) to focus it…")
                automation.click(step.x, step.y, button=step.button, clicks=step.clicks, human=human)
            else:
                self.ctx.log("  Clicking at the cursor to focus the destination…")
                automation.click(button=step.button, clicks=step.clicks, human=human)
            self._sleep(0.3)
            self.ctx.log("  Pasting image (Ctrl+V)…")
            automation.press_keys("ctrl+v", human=human)
            if step.keys.strip():
                self._sleep(0.2)
                self.ctx.log(f"  Pressing {step.keys}…")
                automation.press_keys(step.keys.strip(), human=human)
            self.ctx.log("  Done (image pasted into the target window).")

        elif step.kind == "ai_paste_macro":
            image = screen.capture(cfg.region)
            self.ctx.on_image(image)
            prompt = step.prompt.strip() or cfg.prompt
            self.ctx.log(f"  Asking {cfg.model} to read the screen…")
            value = ai_client.ask(
                image, prompt,
                api_key=cfg.api_key, model=cfg.model,
                base_url=cfg.base_url or None, provider=cfg.provider,
            )
            self.ctx.last_answer = value
            if step.var:
                self.ctx.memory[step.var] = value
            self.ctx.on_answer(value)
            if not value:
                self.ctx.log("  Empty AI result; skipping paste.")
                return
            preview = value.replace("\n", " ")
            preview = (preview[:40] + "…") if len(preview) > 40 else preview
            self.ctx.log(f"  AI value: \"{preview}\" → clipboard.")
            automation.set_clipboard(value)
            # Focus the destination by clicking it directly — far more reliable
            # than a blind Alt+Tab, which lands on an unpredictable window.
            if step.use_point:
                self.ctx.log(f"  Clicking destination field ({step.x}, {step.y}) to focus it…")
                automation.click(step.x, step.y, button=step.button, clicks=step.clicks, human=human)
            else:
                self.ctx.log("  Clicking at the cursor to focus the destination field…")
                automation.click(button=step.button, clicks=step.clicks, human=human)
            self._sleep(0.3)
            if step.clear_first:
                automation.press_keys("ctrl+a", human=human)
                self._sleep(0.1)
            self.ctx.log("  Pasting (Ctrl+V)…")
            automation.press_keys("ctrl+v", human=human)
            if step.keys.strip():
                self._sleep(0.2)
                self.ctx.log(f"  Pressing {step.keys}…")
                automation.press_keys(step.keys.strip(), human=human)
            self.ctx.log("  Done (focused target field and pasted).")

        elif step.kind == "type_answer":
            answer = self.ctx.last_answer
            if not answer:
                self.ctx.log("  No AI answer available yet; skipping.")
                return
            if step.use_point:
                automation.click_and_type(point, answer, clear_first=step.clear_first,
                                          interval=cfg.type_interval, human=human)
            else:
                automation.type_text(answer, clear_first=step.clear_first,
                                     interval=cfg.type_interval, human=human)

        elif step.kind == "save_clipboard":
            value = automation.get_clipboard()
            self.ctx.memory[step.var] = value
            preview = value.replace("\n", " ")
            preview = (preview[:40] + "…") if len(preview) > 40 else preview
            self.ctx.log(f"  Saved clipboard to [{step.var}]: \"{preview}\"")

        elif step.kind == "remember_screen":
            image = screen.capture(cfg.region)
            self.ctx.on_image(image)
            prompt = step.prompt.strip() or cfg.prompt
            self.ctx.log(f"  Asking {cfg.model} to read the screen…")
            value = ai_client.ask(
                image, prompt,
                api_key=cfg.api_key, model=cfg.model,
                base_url=cfg.base_url or None, provider=cfg.provider,
            )
            self.ctx.memory[step.var] = value
            self.ctx.last_answer = value
            self.ctx.on_answer(value)
            preview = value.replace("\n", " ")
            preview = (preview[:40] + "…") if len(preview) > 40 else preview
            self.ctx.log(f"  Saved screen value to [{step.var}]: \"{preview}\"")

        elif step.kind == "type_memory":
            value = self.ctx.memory.get(step.var, "")
            if not value:
                self.ctx.log(f"  Memory [{step.var}] is empty; skipping.")
                return
            if step.paste_instant:
                automation.set_clipboard(value)
                if step.use_point:
                    automation.click(*point, human=human)
                    self._sleep(0.2)
                if step.clear_first:
                    automation.press_keys("ctrl+a", human=human)
                    self._sleep(0.05)
                self.ctx.log(f"  Pasting [{step.var}] instantly (Ctrl+V).")
                automation.press_keys("ctrl+v", human=human)
            elif step.use_point:
                automation.click_and_type(point, value, clear_first=step.clear_first,
                                          interval=cfg.type_interval, human=human)
            else:
                automation.type_text(value, clear_first=step.clear_first,
                                     interval=cfg.type_interval, human=human)

        elif step.kind == "wait":
            self._sleep(random.uniform(step.min_delay, step.max_delay))

        return None

    def _run_restart_workflow(self, steps: list[Step]) -> None:
        """Run the recovery workflow once (no nested restart-on-fail)."""
        self._in_restart = True
        try:
            for index, step in enumerate(steps, start=1):
                if self.stop.is_set():
                    return
                if not step.enabled:
                    continue
                self.ctx.log(f"  Restart step {index}: {step.summary()}")
                try:
                    self._exec(step)
                except automation.FailSafeException:
                    self.ctx.log("Fail-safe triggered (mouse in corner). Stopping.")
                    self.stop.set()
                    return
                except ai_client.AIError as exc:
                    self.ctx.log(f"  AI error: {exc}")
                except Exception as exc:  # noqa: BLE001
                    self.ctx.log(f"  Error: {exc}")
                self._human_pause()
        finally:
            self._in_restart = False

    # -- pacing --------------------------------------------------------
    def _human_pause(self) -> None:
        cfg = self.ctx.cfg
        if not cfg.humanize:
            return
        self._sleep(random.uniform(cfg.humanize_min, cfg.humanize_max))

    def _sleep(self, seconds: float) -> None:
        """Sleep in small slices so a Stop request is honoured promptly."""
        end = time.time() + max(0.0, seconds)
        while time.time() < end and not self.stop.is_set():
            time.sleep(min(0.1, end - time.time()))
