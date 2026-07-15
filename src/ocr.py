"""Classic (non-AI) OCR fallback for reading text off the screen.

This is used when the configured AI provider can't be reached — most commonly
when the account has **run out of usage / credits**, is being **rate-limited**,
or no API key is set.  Instead of failing the step, the workflow can fall back
to reading the text with an on-device OCR engine.

Two backends are supported and tried in order:

1. **Windows OCR** (``winocr`` → ``Windows.Media.Ocr``).  Ships with Windows
   10/11, so it needs no external binary — just ``pip install winocr`` plus the
   English OCR language pack (usually preinstalled).  This is the preferred
   backend for the RDP/AnyDesk Windows machines this app targets.
2. **Tesseract** (``pytesseract``).  Requires the separate Tesseract-OCR
   binary; we auto-detect it in the usual install locations / on ``PATH``.

Both are optional imports, so the app keeps working if neither is installed —
:func:`available` reports whether a usable backend exists.
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

from PIL import Image


class OCRUnavailable(RuntimeError):
    """Raised by :func:`read_text` when no OCR backend is installed/usable."""


# Common Tesseract install locations on Windows (checked when it isn't on PATH).
_TESSERACT_HINTS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


def _have_winocr() -> bool:
    try:
        import winocr  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _find_tesseract() -> Optional[str]:
    exe = shutil.which("tesseract")
    if exe:
        return exe
    for path in _TESSERACT_HINTS:
        if os.path.exists(path):
            return path
    return None


def _have_tesseract() -> bool:
    try:
        import pytesseract  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return _find_tesseract() is not None


def _winocr_read(image: Image.Image, lang: str) -> Optional[str]:
    """Read text via the Windows OCR engine, or ``None`` if it can't be used.

    ``recognize_pil_sync`` wraps the async WinRT call in ``asyncio.run`` — that
    only works on a thread with no running event loop, which is the case for the
    workflow's background worker thread.
    """
    try:
        import winocr
    except Exception:  # noqa: BLE001
        return None
    try:
        result = winocr.recognize_pil_sync(image.convert("RGB"), lang)
    except Exception:  # noqa: BLE001 - missing language pack, WinRT errors, …
        return None
    if isinstance(result, dict):
        text = result.get("text", "")
    else:  # some versions return an object with a .text attribute
        text = getattr(result, "text", "")
    return (text or "").strip()


def _tesseract_read(image: Image.Image) -> Optional[str]:
    """Read text via Tesseract, or ``None`` if it isn't installed/usable."""
    try:
        import pytesseract
    except Exception:  # noqa: BLE001
        return None
    exe = _find_tesseract()
    if not exe:
        return None
    try:
        pytesseract.pytesseract.tesseract_cmd = exe
        return pytesseract.image_to_string(image.convert("RGB")).strip()
    except Exception:  # noqa: BLE001
        return None


def available() -> bool:
    """True if at least one OCR backend can be used right now."""
    return _have_winocr() or _have_tesseract()


def backend_name() -> Optional[str]:
    """Human-readable name of the backend that would be used, or ``None``."""
    if _have_winocr():
        return "Windows OCR"
    if _have_tesseract():
        return "Tesseract"
    return None


def install_hint() -> str:
    """Guidance shown when no OCR backend is available."""
    return (
        "No OCR backend found. On Windows 10/11 run 'pip install winocr' "
        "(uses the built-in Windows OCR — no extra download). Alternatively "
        "install Tesseract-OCR and 'pip install pytesseract'."
    )


def read_text(image: Image.Image, *, lang: str = "en", log=None) -> str:
    """Extract text from ``image`` using the first available OCR backend.

    Returns the recognised text (which may be an empty string if the region has
    no readable text).  Raises :class:`OCRUnavailable` when no backend works.
    """
    if _have_winocr():
        text = _winocr_read(image, lang)
        if text is not None:
            if log is not None:
                log("  Read the region with Windows OCR.")
            return text

    if _have_tesseract():
        text = _tesseract_read(image)
        if text is not None:
            if log is not None:
                log("  Read the region with Tesseract OCR.")
            return text

    raise OCRUnavailable(install_hint())
