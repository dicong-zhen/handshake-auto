"""Screen capture and interactive region selection."""

from __future__ import annotations

import random
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from typing import Optional

import mss
from PIL import Image, ImageDraw

from .config import Region

CAPTURES_DIR = Path(__file__).resolve().parent.parent / "captures"


def is_screen_available(min_width: int = 200, min_height: int = 200) -> bool:
    """Return True if the primary monitor reports a usable resolution.

    Returns False when an RDP session is disconnected / minimised and the
    virtual display collapses to a very small or zero size.
    """
    try:
        with mss.mss() as sct:
            if len(sct.monitors) < 2:
                return False
            mon = sct.monitors[1]
            return mon["width"] >= min_width and mon["height"] >= min_height
    except Exception:
        return False


def capture(region: Region) -> Image.Image:
    """Capture the screen (full primary monitor or a region) as a PIL Image."""
    with mss.mss() as sct:
        if region.full_screen or region.width <= 0 or region.height <= 0:
            monitor = sct.monitors[1]  # primary monitor
        else:
            monitor = {
                "left": region.left,
                "top": region.top,
                "width": region.width,
                "height": region.height,
            }
        shot = sct.grab(monitor)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def mark(image: Image.Image, x: int, y: int) -> Image.Image:
    """Return a copy of the image with a crosshair marker drawn at (x, y)."""
    out = image.copy().convert("RGB")
    draw = ImageDraw.Draw(out)
    r = max(10, min(out.width, out.height) // 30)
    red = (255, 40, 40)
    draw.ellipse([x - r, y - r, x + r, y + r], outline=red, width=3)
    draw.line([x - r - 6, y, x + r + 6, y], fill=red, width=2)
    draw.line([x, y - r - 6, x, y + r + 6], fill=red, width=2)
    return out


def mark_box(image: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    """Return a copy of the image with a rectangle drawn around ``box``
    (left, top, right, bottom). Used to preview an AI-detected crop area."""
    out = image.copy().convert("RGB")
    draw = ImageDraw.Draw(out)
    draw.rectangle(list(box), outline=(255, 40, 40), width=3)
    return out


def region_origin(region: Region) -> tuple[int, int]:
    """Return the absolute (left, top) screen coordinate of a region's origin.

    For a full-screen region this is the primary monitor's top-left corner.
    Used to map image-relative coordinates back to absolute screen positions.
    """
    if region.full_screen or region.width <= 0 or region.height <= 0:
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            return (monitor["left"], monitor["top"])
    return (region.left, region.top)


def save_capture(image: Image.Image, prefix: str = "capture") -> Path:
    """Save a capture to the captures/ folder and return its path."""
    CAPTURES_DIR.mkdir(exist_ok=True)
    name = datetime.now().strftime(f"{prefix}_%Y%m%d_%H%M%S_%f.png")
    path = CAPTURES_DIR / name
    image.save(path)
    return path


def region_center(region: Region) -> tuple[int, int]:
    """Absolute screen coordinate of a region's centre (used as a scroll anchor)."""
    if region.full_screen or region.width <= 0 or region.height <= 0:
        with mss.mss() as sct:
            mon = sct.monitors[1]
            return (mon["left"] + mon["width"] // 2, mon["top"] + mon["height"] // 2)
    return (region.left + region.width // 2, region.top + region.height // 2)


# ---------------------------------------------------------------------------
# Scroll-and-stitch full-page capture
# ---------------------------------------------------------------------------
def _row_signature(img: Image.Image, bands: int, target_h: int) -> tuple[list, int]:
    """Reduce an image to a compact per-row brightness signature.

    The image is converted to greyscale and box-averaged down to ``bands``
    columns by ``target_h`` rows, so each row becomes a short tuple of average
    brightness values.  Comparing these row signatures lets us find how far the
    page scrolled between two captures cheaply (no numpy needed).
    """
    grey = img.convert("L")
    h = max(1, min(img.height, target_h))
    small = grey.resize((bands, h), Image.BOX)
    flat = list(small.getdata())
    rows = [flat[i * bands:(i + 1) * bands] for i in range(h)]
    return rows, h


def _vertical_advance(prev: Image.Image, curr: Image.Image) -> tuple[int, float]:
    """Estimate how many pixels ``curr`` scrolled DOWN relative to ``prev``.

    Returns ``(advance_px, match_score)`` where ``advance_px`` is in the full
    resolution of the captures and ``match_score`` is the mean per-band
    difference at the best alignment (lower = better).

    After scrolling down, the top of ``curr`` shows content that sits ``s``
    pixels below the top of ``prev``.  We take a fixed-height template from the
    top of ``curr`` and slide it down ``prev`` to find the offset that matches
    best; that offset is the scroll distance.  Using a *constant* template
    height for every candidate offset avoids the bias that a variable-length
    comparison has toward tiny overlaps.
    """
    bands, target_h = 6, 260
    prev_rows, hp = _row_signature(prev, bands, target_h)
    curr_rows, hc = _row_signature(curr, bands, target_h)
    h = min(hp, hc)
    template_h = max(6, int(h * 0.35))
    if template_h >= h:
        template_h = max(1, h - 1)
    best_s, best_diff = 0, None
    for s in range(0, h - template_h + 1):
        total = 0
        for i in range(template_h):
            pr = prev_rows[s + i]
            cr = curr_rows[i]
            total += abs(pr[0] - cr[0]) + abs(pr[1] - cr[1]) + abs(pr[2] - cr[2]) \
                + abs(pr[3] - cr[3]) + abs(pr[4] - cr[4]) + abs(pr[5] - cr[5])
        diff = total / (template_h * bands)
        if best_diff is None or diff < best_diff:
            best_diff, best_s = diff, s
    scale = prev.height / h
    return int(round(best_s * scale)), (best_diff if best_diff is not None else 0.0)


def _append_below(canvas: Image.Image, curr: Image.Image, advance: int) -> Image.Image:
    """Append the bottom ``advance`` pixels of ``curr`` beneath ``canvas``."""
    advance = max(0, min(advance, curr.height))
    if advance <= 0:
        return canvas
    strip = curr.crop((0, curr.height - advance, curr.width, curr.height))
    out = Image.new("RGB", (canvas.width, canvas.height + advance))
    out.paste(canvas, (0, 0))
    out.paste(strip, (0, canvas.height))
    return out


SCROLL_METHODS = ("wheel", "arrows", "pagedown")


def _scroll_down(method: str, hover, notches: int, human: bool) -> None:
    """Advance the page DOWN one step using the chosen input method.

    All methods are sent through the shared automation backend (DirectInput
    scan-codes when enabled), so over AnyDesk/RDP they look like ordinary
    hardware input rather than synthetic messages.
    """
    from . import automation

    if method == "arrows":
        # A short burst of Down-arrow presses = small, reliable increments with
        # lots of overlap. Cadence is jittered so it isn't machine-regular.
        for _ in range(max(1, notches)):
            automation.press_keys("down", human=False)
            time.sleep(random.uniform(0.03, 0.11) if human else 0.02)
    elif method == "pagedown":
        automation.press_keys("pagedown", human=False)
    else:  # wheel
        n = notches
        if human:
            n = max(1, notches + random.choice([-1, 0, 0, 1]))
        automation.scroll(-n, hover[0], hover[1], human=human)


def _scroll_up(method: str, hover, notches: int) -> None:
    """Move the page UP one step (used to reach / restore the top)."""
    from . import automation

    if method in ("arrows", "pagedown"):
        automation.press_keys("pageup", human=False)
    else:
        automation.scroll(max(6, abs(notches) * 4), hover[0], hover[1], human=False)


def _settle(settle_min: float, settle_max: float, human: bool) -> None:
    """Human-like pause that also absorbs AnyDesk/RDP round-trip + render lag."""
    lo, hi = max(0.0, settle_min), max(settle_min, settle_max)
    delay = random.uniform(lo, hi) if human else hi
    # Occasional longer "reading" pause so the cadence isn't uniform.
    if human and random.random() < 0.15:
        delay += random.uniform(0.3, 0.9)
    time.sleep(delay)


def _focus_click(hover, log) -> None:
    """Click the scroll anchor so a keyboard-scrolled pane (Chrome modal / div)
    actually receives Arrow / Page Down / Ctrl+Home keystrokes.

    Wheel scrolling targets whatever is under the cursor and needs no focus,
    but key events go to the focused element — without this click, Arrow /
    Page Down do nothing and the capture never advances.
    """
    from . import automation

    try:
        automation.click(hover[0], hover[1], human=False)
        time.sleep(0.15)
        if log is not None:
            log("  Clicked the scroll point to focus the pane for keyboard scrolling.")
    except Exception:  # noqa: BLE001
        pass


def _recapture_after_lag(region, settle_max) -> "Image.Image":
    """Wait longer and re-capture WITHOUT scrolling again.

    Over AnyDesk/RDP a capture can land before the remote view has repainted,
    which looks like "no movement".  Re-capturing after an extra pause reveals
    the real frame — and because we don't scroll again, no unnecessary scroll
    action is issued when we're genuinely at the top/bottom.
    """
    time.sleep(max(settle_max, 0.6) + 0.35)
    return capture(region)


def _scroll_to_top(region, hover, method, notches, settle_min, settle_max,
                   human, stop, log) -> int:
    """Move up until the view stops changing (the top of the page).

    For key-based methods a Ctrl+Home is tried first (fast, if the pane is
    focused), then it confirms by moving up until nothing changes — with a
    lag-tolerant recheck so a slow AnyDesk repaint isn't mistaken for the top.
    """
    from . import automation

    if method in ("arrows", "pagedown"):
        try:
            automation.press_keys("ctrl+home", human=False)
        except Exception:  # noqa: BLE001
            pass
        _settle(settle_min, settle_max, human)

    prev = capture(region)
    steps = 0
    for _ in range(80):  # generous safety cap
        if stop is not None and stop.is_set():
            break
        _scroll_up(method, hover, notches)
        _settle(settle_min, settle_max, human)
        curr = capture(region)
        # advance of `prev` relative to `curr` = how far we just moved up.
        advance, _ = _vertical_advance(curr, prev)
        if advance <= 2:
            curr = _recapture_after_lag(region, settle_max)
            advance, _ = _vertical_advance(curr, prev)
            if advance <= 2:
                break
        steps += 1
        prev = curr
    if log is not None:
        log(f"  Reached the top ({steps} step(s)).")
    return steps


def scroll_capture(
    region: Region,
    *,
    hover: Optional[tuple[int, int]] = None,
    method: str = "wheel",
    notches: int = 3,
    max_scrolls: int = 15,
    settle_min: float = 0.5,
    settle_max: float = 1.4,
    human: bool = True,
    start_from_top: bool = True,
    return_to_top: bool = True,
    max_height: int = 20000,
    stop=None,
    log=None,
    on_progress=None,
) -> Image.Image:
    """Scroll the ``region`` top-to-bottom, stitching every viewport into one
    tall image of the whole (sub)page.

    ``method`` is one of ``wheel`` (mouse wheel over ``hover``), ``arrows``
    (a burst of Down-arrow presses), or ``pagedown``.  Between steps it waits a
    randomised ``settle_min..settle_max`` seconds when ``human`` is set — this
    both looks human and gives the AnyDesk → remote-Chrome round trip time to
    repaint before the next capture.  Consecutive captures are aligned by
    overlap detection, so wheel "notches" needn't map to a fixed pixel
    distance.  Stops when the page no longer moves (bottom), ``max_scrolls`` is
    hit, ``max_height`` is exceeded, or ``stop`` is set.
    """
    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    if method not in SCROLL_METHODS:
        method = "wheel"
    if hover is None:
        hover = region_center(region)
    notches = abs(notches) or 3

    # Keyboard scrolling needs the pane focused; a wheel needs nothing.
    if method in ("arrows", "pagedown"):
        _focus_click(hover, log)

    if start_from_top:
        _scroll_to_top(region, hover, method, notches, settle_min, settle_max,
                       human, stop, log)

    prev_view = capture(region)
    stitched = prev_view.copy()
    if on_progress is not None:
        on_progress(prev_view)

    steps_done = 0
    for _ in range(max(1, max_scrolls)):
        if stop is not None and stop.is_set():
            _log("  Stopped during scroll-capture.")
            break
        _scroll_down(method, hover, notches, human)
        _settle(settle_min, settle_max, human)
        curr = capture(region)
        advance, score = _vertical_advance(prev_view, curr)
        if advance <= 2:
            # Might just be AnyDesk repaint lag — wait and re-check (no re-scroll)
            # before concluding we've hit the bottom.
            curr = _recapture_after_lag(region, settle_max)
            advance, score = _vertical_advance(prev_view, curr)
            if advance <= 2:
                _log(f"  Reached the bottom after {steps_done} scroll(s).")
                break
        stitched = _append_below(stitched, curr, advance)
        steps_done += 1
        _log(f"  Scroll {steps_done}: +{advance}px (total {stitched.height}px).")
        if on_progress is not None:
            on_progress(curr)
        prev_view = curr
        if stitched.height >= max_height:
            _log(f"  Reached the {max_height}px height cap; stopping.")
            break

    if return_to_top and steps_done:
        for _ in range(steps_done + 2):
            if stop is not None and stop.is_set():
                break
            _scroll_up(method, hover, notches)
            time.sleep(random.uniform(0.05, 0.15) if human else 0.05)

    if on_progress is not None:
        on_progress(stitched)
    return stitched


def select_region(parent: Optional[tk.Misc] = None) -> Optional[Region]:
    """Show a fullscreen translucent overlay and let the user drag a rectangle.

    Returns a Region for the selected area, or None if cancelled (Esc / empty).
    """
    result: dict[str, Optional[Region]] = {"region": None}

    overlay = tk.Toplevel(parent) if parent is not None else tk.Tk()
    overlay.attributes("-fullscreen", True)
    overlay.attributes("-alpha", 0.25)
    overlay.attributes("-topmost", True)
    overlay.configure(bg="black", cursor="cross")

    canvas = tk.Canvas(overlay, bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    hint = canvas.create_text(
        overlay.winfo_screenwidth() // 2,
        40,
        text="Drag to select a region   |   Esc to cancel",
        fill="white",
        font=("Segoe UI", 18, "bold"),
    )

    # Drawing uses canvas-local (widget) coordinates throughout so the drawn
    # rectangle always tracks the pointer; the absolute screen region is
    # derived at release by adding the overlay's on-screen origin. Mixing
    # widget and root coordinates (the previous behaviour) drew an offset /
    # wrong rectangle whenever the overlay origin was not (0, 0), e.g. on
    # multi-monitor or RDP virtual desktops.
    state = {"start_x": 0, "start_y": 0, "rect": None}

    def on_press(event: "tk.Event") -> None:
        state["start_x"] = event.x
        state["start_y"] = event.y
        if state["rect"] is not None:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="#00d1ff", width=2,
        )

    def on_drag(event: "tk.Event") -> None:
        if state["rect"] is not None:
            canvas.coords(
                state["rect"], state["start_x"], state["start_y"],
                event.x, event.y,
            )

    def on_release(event: "tk.Event") -> None:
        origin_x = overlay.winfo_rootx()
        origin_y = overlay.winfo_rooty()
        left = origin_x + min(state["start_x"], event.x)
        top = origin_y + min(state["start_y"], event.y)
        width = abs(event.x - state["start_x"])
        height = abs(event.y - state["start_y"])
        if width >= 5 and height >= 5:
            result["region"] = Region(
                full_screen=False, left=left, top=top, width=width, height=height
            )
        overlay.destroy()

    def on_cancel(_event: "tk.Event") -> None:
        overlay.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    overlay.bind("<Escape>", on_cancel)

    # Make sure the overlay is actually mapped before grabbing input, otherwise
    # the grab can fail (window not viewable) and leave the UI unresponsive.
    overlay.update_idletasks()
    overlay.focus_force()
    try:
        overlay.grab_set()
    except Exception:
        overlay.after(80, lambda: _safe_grab(overlay))
    overlay.wait_window()
    return result["region"]


def _safe_grab(window: "tk.Misc") -> None:
    try:
        window.grab_set()
    except Exception:
        pass
