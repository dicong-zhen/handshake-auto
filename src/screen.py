"""Screen capture and interactive region selection."""

from __future__ import annotations

import tkinter as tk
from datetime import datetime
from pathlib import Path
from typing import Optional

import mss
from PIL import Image, ImageDraw

from .config import Region

CAPTURES_DIR = Path(__file__).resolve().parent.parent / "captures"


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


def save_capture(image: Image.Image) -> Path:
    """Save a capture to the captures/ folder and return its path."""
    CAPTURES_DIR.mkdir(exist_ok=True)
    name = datetime.now().strftime("capture_%Y%m%d_%H%M%S_%f.png")
    path = CAPTURES_DIR / name
    image.save(path)
    return path


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

    state = {"start_x": 0, "start_y": 0, "rect": None}

    def on_press(event: "tk.Event") -> None:
        state["start_x"] = event.x_root
        state["start_y"] = event.y_root
        if state["rect"] is not None:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="#00d1ff", width=2,
        )

    def on_drag(event: "tk.Event") -> None:
        if state["rect"] is not None:
            sx = state["start_x"] - overlay.winfo_rootx()
            sy = state["start_y"] - overlay.winfo_rooty()
            canvas.coords(state["rect"], sx, sy, event.x, event.y)

    def on_release(event: "tk.Event") -> None:
        left = min(state["start_x"], event.x_root)
        top = min(state["start_y"], event.y_root)
        width = abs(event.x_root - state["start_x"])
        height = abs(event.y_root - state["start_y"])
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

    overlay.focus_force()
    overlay.grab_set()
    overlay.wait_window()
    return result["region"]
