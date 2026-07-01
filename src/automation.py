"""Mouse and keyboard automation, plus interactive coordinate picking.

Two input backends are supported:

* **DirectInput** (``pydirectinput``) sends hardware *scan-code* input. This is
  required for remote-desktop tools (AnyDesk, RDP, Parsec, …) and many games,
  which ignore the virtual-key events that PyAutoGUI sends.
* **Standard** (``pyautogui``) is the fallback.

DirectInput is used by default when available. Call :func:`set_directinput`
to switch at runtime.

All actions support an optional "human-like" mode that adds small random
offsets, variable movement durations, and per-character typing jitter so the
automation looks less robotic.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Callable, Optional

import pyautogui
from pynput import mouse

try:
    import pyperclip
except Exception:  # pragma: no cover - optional
    pyperclip = None

# Safety: slamming the mouse into a screen corner aborts any pyautogui action.
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05

try:
    import pydirectinput as _pdi

    _pdi.FAILSAFE = True
    _pdi.PAUSE = 0.02
    HAVE_DIRECTINPUT = True
except Exception:  # pragma: no cover - optional dependency
    _pdi = None
    HAVE_DIRECTINPUT = False

# Use DirectInput by default whenever it is available.
_USE_DIRECTINPUT = HAVE_DIRECTINPUT

# Whether human-mode typing makes the occasional typo and corrects it.
_TYPOS_ENABLED = True

FailSafeException = pyautogui.FailSafeException

# Rough QWERTY neighbours, used to pick a believable wrong key for a typo.
_QWERTY_NEIGHBOURS = {
    "q": "wa", "w": "qase", "e": "wsdr", "r": "edft", "t": "rfgy", "y": "tghu",
    "u": "yhji", "i": "ujko", "o": "iklp", "p": "ol",
    "a": "qwsz", "s": "awedxz", "d": "serfcx", "f": "drtgvc", "g": "ftyhbv",
    "h": "gyujnb", "j": "huikmn", "k": "jiolm", "l": "kop",
    "z": "asx", "x": "zsdc", "c": "xdfv", "v": "cfgb", "b": "vghn",
    "n": "bhjm", "m": "njk",
}


def set_directinput(enabled: bool) -> None:
    """Enable/disable the hardware scan-code backend (for AnyDesk/RDP/games)."""
    global _USE_DIRECTINPUT
    _USE_DIRECTINPUT = bool(enabled) and HAVE_DIRECTINPUT


def set_failsafe(enabled: bool) -> None:
    """Enable/disable the corner-of-screen fail-safe on both backends.

    The fail-safe aborts any action when the cursor sits in a screen corner.
    On a minimized/disconnected RDP session the cursor reads as (0, 0), which
    falsely trips it — disable it for unattended VPS automation.
    """
    pyautogui.FAILSAFE = bool(enabled)
    if _pdi is not None:
        _pdi.FAILSAFE = bool(enabled)


def set_typos(enabled: bool) -> None:
    """Enable/disable occasional human-like typos (typed then corrected)."""
    global _TYPOS_ENABLED
    _TYPOS_ENABLED = bool(enabled)


def _typo_char(char: str) -> Optional[str]:
    """Return a believable wrong character for ``char`` (an adjacent key),
    preserving case, or ``None`` if no sensible substitute exists."""
    low = char.lower()
    neighbours = _QWERTY_NEIGHBOURS.get(low)
    if not neighbours:
        return None
    wrong = random.choice(neighbours)
    return wrong.upper() if char.isupper() else wrong


def using_directinput() -> bool:
    return _USE_DIRECTINPUT


def backend_name() -> str:
    return "DirectInput (scan-code)" if _USE_DIRECTINPUT else "Standard (pyautogui)"


def _be():
    return _pdi if _USE_DIRECTINPUT else pyautogui


# ----------------------------------------------------------------------
# Low-level helpers
# ----------------------------------------------------------------------
def _move_duration(human: bool) -> float:
    return random.uniform(0.15, 0.5) if human else 0.0


def _jitter(value: int, human: bool, spread: int = 2) -> int:
    return value + random.randint(-spread, spread) if human else value


def move(x: int, y: int, *, human: bool = True) -> None:
    _be().moveTo(_jitter(x, human), _jitter(y, human), duration=_move_duration(human))


def click(
    x: Optional[int] = None,
    y: Optional[int] = None,
    *,
    button: str = "left",
    clicks: int = 1,
    human: bool = True,
) -> None:
    """Click at a point (or the current position if x/y are None)."""
    be = _be()
    interval = random.uniform(0.05, 0.15) if human else 0.0
    if x is None or y is None:
        be.click(button=button, clicks=clicks, interval=interval)
        return
    be.click(
        x=_jitter(x, human), y=_jitter(y, human),
        button=button, clicks=clicks,
        duration=_move_duration(human), interval=interval,
    )


def scroll(amount: int, x: Optional[int] = None, y: Optional[int] = None,
           *, human: bool = True) -> None:
    """Scroll vertically. Positive = up, negative = down.

    To look human, a large scroll is broken into a few smaller steps.
    """
    be = _be()
    if x is not None and y is not None:
        move(x, y, human=human)
    if human and abs(amount) > 3:
        remaining = amount
        step = 3 if amount > 0 else -3
        while abs(remaining) > 0:
            chunk = step if abs(remaining) >= abs(step) else remaining
            be.scroll(chunk)
            remaining -= chunk
            time.sleep(random.uniform(0.05, 0.2))
    else:
        be.scroll(amount)


def press_keys(spec: str, *, human: bool = True) -> None:
    """Press a key or a hotkey combo.

    Examples: "enter", "tab", "esc", "end", "ctrl+a", "ctrl+shift+s".
    """
    spec = spec.strip().lower()
    if not spec:
        return
    be = _be()
    parts = [p.strip() for p in spec.split("+") if p.strip()]
    if len(parts) == 1:
        be.press(parts[0])
    else:
        be.hotkey(*parts)
    if human:
        time.sleep(random.uniform(0.05, 0.15))


def _write(be, text: str, interval: float) -> None:
    """Backend-aware text writer (DirectInput needs auto_shift for uppercase)."""
    if be is _pdi:
        be.write(text, interval=interval, auto_shift=True)
    else:
        be.write(text, interval=interval)


def type_text(
    text: str,
    *,
    clear_first: bool = False,
    interval: float = 0.06,
    human: bool = True,
) -> None:
    """Type text at the current focus. Optionally clears the field first
    (Ctrl+A, Delete).

    When ``human`` is set, ``interval`` is treated as the *average* per-character
    delay, and the cadence is varied to look like real typing: each keystroke is
    jittered around that mean, with extra pauses after spaces (between words) and
    after sentence/clause punctuation, plus the occasional brief hesitation.
    Increase ``interval`` (the "Typing speed" setting) to type more slowly.
    """
    be = _be()
    if clear_first:
        be.hotkey("ctrl", "a")
        be.press("delete")
        if human:
            time.sleep(random.uniform(0.08, 0.22))
    if not human:
        _write(be, text, interval)
        return

    base = max(0.0, interval)
    for char in text:
        # Occasionally fumble a letter: type a neighbouring key, notice, then
        # backspace and continue (the correct key is typed just below).
        if _TYPOS_ENABLED and char.isalpha() and random.random() < 0.018:
            wrong = _typo_char(char)
            if wrong is not None:
                _write(be, wrong, 0.0)
                time.sleep(random.uniform(base * 0.6, base * 1.6))
                time.sleep(random.uniform(0.12, 0.35))  # "notice" the mistake
                be.press("backspace")
                time.sleep(random.uniform(0.06, 0.18))

        _write(be, char, 0.0)
        delay = random.uniform(base * 0.4, base * 1.9)  # baseline keystroke jitter
        if char == " ":
            delay += random.uniform(base * 1.5, base * 4.0) + random.uniform(0.0, 0.04)
        elif char in ",;:":
            delay += random.uniform(0.05, 0.18)
        elif char in ".!?":
            delay += random.uniform(0.15, 0.40)
        if random.random() < 0.02:  # occasional human hesitation
            delay += random.uniform(0.25, 0.6)
        time.sleep(delay)


def click_and_type(
    point: tuple[int, int],
    text: str,
    *,
    clear_first: bool = True,
    interval: float = 0.06,
    human: bool = True,
) -> None:
    """Click an input location, then type the given text into it."""
    click(*point, human=human)
    time.sleep(random.uniform(0.1, 0.25) if human else 0.15)
    type_text(text, clear_first=clear_first, interval=interval, human=human)


def get_clipboard() -> str:
    """Return the current clipboard text (empty string if unavailable)."""
    if pyperclip is None:
        return ""
    try:
        return pyperclip.paste() or ""
    except Exception:  # noqa: BLE001
        return ""


def set_clipboard(text: str) -> None:
    if pyperclip is None:
        return
    try:
        pyperclip.copy(text)
    except Exception:  # noqa: BLE001
        pass


def set_clipboard_image(image) -> bool:
    """Put a PIL image on the Windows clipboard as a device-independent bitmap
    (``CF_DIB``) so it can be pasted as a real image with Ctrl+V.

    Implemented with ``ctypes`` (no extra dependency). Returns True on success.
    """
    import ctypes
    import io
    from ctypes import wintypes

    try:
        buf = io.BytesIO()
        image.convert("RGB").save(buf, "BMP")
        bmp = buf.getvalue()
        buf.close()
        dib = bmp[14:]  # strip the 14-byte BITMAPFILEHEADER -> raw DIB
    except Exception:  # noqa: BLE001
        return False

    CF_DIB = 8
    GMEM_MOVABLE = 0x0002

    try:
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
    except AttributeError:
        return False  # not Windows

    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.SetClipboardData.restype = wintypes.HANDLE
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

    if not u32.OpenClipboard(None):
        return False
    try:
        u32.EmptyClipboard()
        handle = k32.GlobalAlloc(GMEM_MOVABLE, len(dib))
        if not handle:
            return False
        ptr = k32.GlobalLock(handle)
        if not ptr:
            return False
        ctypes.memmove(ptr, dib, len(dib))
        k32.GlobalUnlock(handle)
        # On success the system owns the memory; do not free it.
        return bool(u32.SetClipboardData(CF_DIB, handle))
    except Exception:  # noqa: BLE001
        return False
    finally:
        u32.CloseClipboard()


def pick_point_async(callback: Callable[[Optional[tuple[int, int]]], None]) -> None:
    """Listen (globally) for the user's next left-click and report its
    coordinates via ``callback``. Right-click cancels (callback receives None).

    Runs the listener on a background thread so the GUI stays responsive.
    """

    def _run() -> None:
        captured: dict[str, Optional[tuple[int, int]]] = {"pt": None}

        def on_click(x, y, button, pressed):
            if not pressed:
                return None
            if button == mouse.Button.left:
                captured["pt"] = (int(x), int(y))
                return False  # stop listener
            if button == mouse.Button.right:
                captured["pt"] = None
                return False
            return None

        with mouse.Listener(on_click=on_click) as listener:
            listener.join()

        callback(captured["pt"])

    threading.Thread(target=_run, daemon=True).start()
