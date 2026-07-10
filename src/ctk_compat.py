"""A drop-in CustomTkinter replacement backed by native tkinter/ttk widgets.

CustomTkinter draws every widget on a tkinter.Canvas using a bundled shapes
font.  Over remote-desktop sessions (RDP / AnyDesk) that canvas content is
frequently never painted, leaving the whole UI invisible while native widgets
(Text/Entry) still render.  This module re-implements the subset of the
CustomTkinter API that the app uses with classic tk widgets, which paint
reliably everywhere.  The public names match CustomTkinter so the rest of the
code can simply do ``from . import ctk_compat as ctk``.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Optional

# ── Dark palette ────────────────────────────────────────────────────────────
WIN_BG = "#242424"
FRAME_BG = "#2b2b2b"
ROW_BG = "#333333"
BTN = "#1f6aa5"
BTN_HOVER = "#144870"
BTN_TEXT = "#dce4ee"
ENTRY_BG = "#343638"
ENTRY_FG = "#dce4ee"
TEXT_BG = "#1d1e1e"
TEXT_FG = "#dce4ee"
LABEL_FG = "#dce4ee"
SEG_ON = "#1f6aa5"
SEG_OFF = "#3a3a3a"
SEG_HOVER = "#4a4a4a"
DISABLED_BG = "#2a2a2a"
DISABLED_FG = "#777777"
SCROLLBAR = "#4a4a4a"
DEFAULT_FONT = ("Segoe UI", 13)

_appearance = "Dark"

# Re-export tkinter variable classes (the app uses ctk.StringVar etc.)
StringVar = tk.StringVar
BooleanVar = tk.BooleanVar
IntVar = tk.IntVar
DoubleVar = tk.DoubleVar


def set_appearance_mode(mode: str) -> None:
    global _appearance
    _appearance = mode


def set_default_color_theme(_name: str) -> None:
    pass


def get_appearance_mode() -> str:
    return "Dark"


class _Theme:
    """Minimal stand-in for customtkinter.ThemeManager."""

    theme = {
        "CTkLabel": {"text_color": LABEL_FG},
        "CTkButton": {"fg_color": BTN, "text_color": BTN_TEXT},
    }


ThemeManager = _Theme()


# ── helpers ─────────────────────────────────────────────────────────────────
def _bg_of(widget) -> str:
    try:
        return widget.cget("bg")
    except Exception:
        return WIN_BG


def _resolve_color(value, parent=None, default=None):
    """Map a CustomTkinter color spec to a plain tk color string."""
    if value is None:
        return default
    if value == "transparent":
        return _bg_of(parent) if parent is not None else (default or WIN_BG)
    if isinstance(value, (list, tuple)):
        # CTk uses [light, dark]; we always render dark.
        return value[-1]
    return value


def _extract_image(image):
    if image is None:
        return None
    if isinstance(image, CTkImage):
        return image.photo
    return image


# Font metrics are cached per font spec so px→char/line conversion is both
# accurate (matches the actual rendered font) and cheap.
_FONT_METRICS: dict = {}


def _font_metrics(font) -> tuple[float, int]:
    """Return (average char pixel width, line height in px) for a font spec."""
    key = tuple(font) if isinstance(font, (list, tuple)) else font
    cached = _FONT_METRICS.get(key)
    if cached is not None:
        return cached
    char_w, line_h = 8.0, 18
    try:
        f = tkfont.Font(font=font or DEFAULT_FONT)
        sample = "0123456789abcdefghijklmnopqrstuvwxyz"
        char_w = max(5.0, f.measure(sample) / len(sample))
        line_h = max(12, f.metrics("linespace"))
    except Exception:
        pass
    _FONT_METRICS[key] = (char_w, line_h)
    return char_w, line_h


def _px_to_chars(px: int, font=None) -> int:
    """Convert a pixel width to the character count tk Entry/Text expect."""
    char_w, _ = _font_metrics(font or DEFAULT_FONT)
    return max(1, round(px / char_w))


class _GridMixin:
    """Adds CustomTkinter's tuple support to grid row/column configure."""

    def grid_columnconfigure(self, index, **kw):  # type: ignore[override]
        if isinstance(index, (list, tuple)):
            for i in index:
                super().grid_columnconfigure(i, **kw)
        else:
            super().grid_columnconfigure(index, **kw)

    def grid_rowconfigure(self, index, **kw):  # type: ignore[override]
        if isinstance(index, (list, tuple)):
            for i in index:
                super().grid_rowconfigure(i, **kw)
        else:
            super().grid_rowconfigure(index, **kw)


def _apply_dark_titlebar(win) -> None:
    """Best-effort dark title bar on Windows 10/11."""
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        value = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (20 new, 19 old)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)
            )
    except Exception:
        pass


# ── top-level windows ───────────────────────────────────────────────────────
class CTk(_GridMixin, tk.Tk):
    def __init__(self, *args, fg_color=None, **kw):
        super().__init__(*args, **kw)
        self.configure(bg=_resolve_color(fg_color, default=WIN_BG))
        self.after(60, lambda: _apply_dark_titlebar(self))


class CTkToplevel(_GridMixin, tk.Toplevel):
    def __init__(self, *args, fg_color=None, **kw):
        super().__init__(*args, **kw)
        self.configure(bg=_resolve_color(fg_color, default=WIN_BG))
        self.after(60, lambda: _apply_dark_titlebar(self))


# ── frame ───────────────────────────────────────────────────────────────────
class CTkFrame(_GridMixin, tk.Frame):
    def __init__(self, master, fg_color=None, corner_radius=None, border_width=0,
                 border_color=None, width=0, height=0, bg_color=None, **kw):
        bg = _resolve_color(fg_color, master, FRAME_BG)
        opts = dict(bg=bg, highlightthickness=int(border_width or 0))
        if border_width:
            opts["highlightbackground"] = _resolve_color(border_color, master, SEG_OFF)
            opts["highlightcolor"] = opts["highlightbackground"]
        if width:
            opts["width"] = width
        if height:
            opts["height"] = height
        super().__init__(master, **opts, **kw)

    def configure(self, **kw):  # type: ignore[override]
        if "fg_color" in kw:
            kw["bg"] = _resolve_color(kw.pop("fg_color"), self.master, FRAME_BG)
        kw.pop("corner_radius", None)
        kw.pop("border_color", None)
        return super().configure(**kw)


# ── label ───────────────────────────────────────────────────────────────────
class CTkLabel(tk.Label):
    def __init__(self, master, text="", text_color=None, fg_color=None, font=None,
                 anchor="w", justify=None, wraplength=0, width=0, height=0,
                 image=None, corner_radius=None, **kw):
        self._imgref = _extract_image(image)
        bg = _resolve_color(fg_color, master, _bg_of(master))
        fg = _resolve_color(text_color, master, LABEL_FG)
        opts = dict(text=text, bg=bg, fg=fg, font=font or DEFAULT_FONT, anchor=anchor)
        if justify:
            opts["justify"] = justify
        if wraplength:
            opts["wraplength"] = wraplength
        if self._imgref is not None:
            opts["image"] = self._imgref
            # tk's "compound" accepts bottom/center/left/none/right/top only;
            # "none" shows the image alone when there is no text.
            opts["compound"] = "left" if text else "none"
        super().__init__(master, **opts, **kw)

    def configure(self, **kw):  # type: ignore[override]
        if "text_color" in kw:
            kw["fg"] = _resolve_color(kw.pop("text_color"), self.master, LABEL_FG)
        if "fg_color" in kw:
            kw["bg"] = _resolve_color(kw.pop("fg_color"), self.master, _bg_of(self.master))
        if "image" in kw:
            self._imgref = _extract_image(kw.pop("image"))
            kw["image"] = self._imgref
            if self._imgref is not None and not kw.get("text"):
                kw.setdefault("compound", "none")
        kw.pop("corner_radius", None)
        return super().configure(**kw)


# ── button (Frame + Label so we get pixel sizing + any color over RDP) ───────
class CTkButton(_GridMixin, tk.Frame):
    def __init__(self, master, text="", command=None, width=140, height=28,
                 fg_color=None, hover_color=None, text_color=None, image=None,
                 state="normal", font=None, anchor="center", corner_radius=None,
                 border_width=0, border_color=None, **kw):
        self._base = _resolve_color(fg_color, master, BTN)
        if self._base == "transparent":
            self._base = _bg_of(master)
        self._hover = _resolve_color(hover_color, master, BTN_HOVER)
        self._txt_color = _resolve_color(text_color, master, BTN_TEXT)
        self._command = command
        self._state = state
        self._imgref = _extract_image(image)
        self._font = font or DEFAULT_FONT
        self._anchor = anchor

        # Grow the fixed pixel box to fit the label so text is never clipped,
        # and give every button a consistent minimum height that lines up with
        # entries/checkboxes in the same row.
        width = self._fit_width(text, width)
        height = max(height, self._fit_height())

        super().__init__(master, bg=self._base, width=width, height=height,
                         highlightthickness=int(border_width or 0), bd=0)
        if border_width:
            tk.Frame.configure(
                self, highlightbackground=_resolve_color(border_color, master, SEG_OFF))
        self.grid_propagate(False)
        self.pack_propagate(False)

        compound = "left" if (text and self._imgref is not None) else "none"
        self._label = tk.Label(
            self, text=text, bg=self._base, fg=self._txt_color,
            font=font or DEFAULT_FONT, image=self._imgref, compound=compound,
        )
        if anchor in ("w", "nw", "sw"):
            self._label.place(relx=0.0, x=8, rely=0.5, anchor="w")
        else:
            self._label.place(relx=0.5, rely=0.5, anchor="center")

        for w in (self, self._label):
            w.bind("<Button-1>", self._on_click)
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
        self._apply_state()

    def _fit_width(self, text: str, requested: int) -> int:
        """Requested width, expanded if needed so the label fits without clipping."""
        char_w, _ = _font_metrics(self._font)
        text_px = round(len(text or "") * char_w)
        img_px = 24 if self._imgref is not None else 0
        pad = 20 if self._anchor in ("center",) else 16
        return max(requested, text_px + img_px + pad)

    def _fit_height(self) -> int:
        _, line_h = _font_metrics(self._font)
        return line_h + 10

    def _paint(self, color) -> None:
        # Bypass the overridden configure() to avoid recursing into _apply_state.
        try:
            tk.Frame.configure(self, bg=color)
            self._label.configure(bg=color)
        except Exception:
            pass

    def _apply_state(self) -> None:
        if self._state == "disabled":
            self._paint(DISABLED_BG)
            self._label.configure(fg=DISABLED_FG)
        else:
            self._paint(self._base)
            self._label.configure(fg=self._txt_color)

    def _on_click(self, _e=None):
        if self._state != "disabled" and self._command:
            self._command()
        return "break"

    def _on_enter(self, _e=None):
        if self._state != "disabled":
            self._paint(self._hover)

    def _on_leave(self, _e=None):
        if self._state != "disabled":
            self._paint(self._base)

    def configure(self, **kw):  # type: ignore[override]
        if "text" in kw:
            new_text = kw.pop("text")
            self._label.configure(text=new_text)
            # Keep the box wide enough for the new label (never shrink below the
            # current width so expanded/`sticky` buttons don't jump around).
            try:
                fitted = self._fit_width(new_text, self.winfo_reqwidth())
                tk.Frame.configure(self, width=fitted)
            except Exception:
                pass
        if "fg_color" in kw:
            self._base = _resolve_color(kw.pop("fg_color"), self.master, BTN)
            if self._base == "transparent":
                self._base = _bg_of(self.master)
        if "hover_color" in kw:
            self._hover = _resolve_color(kw.pop("hover_color"), self.master, BTN_HOVER)
        if "text_color" in kw:
            self._txt_color = _resolve_color(kw.pop("text_color"), self.master, BTN_TEXT)
            self._label.configure(fg=self._txt_color)
        if "command" in kw:
            self._command = kw.pop("command")
        if "image" in kw:
            self._imgref = _extract_image(kw.pop("image"))
            self._label.configure(image=self._imgref)
        if "state" in kw:
            self._state = kw.pop("state")
        kw.pop("corner_radius", None)
        kw.pop("border_color", None)
        if kw:
            super().configure(**kw)
        self._apply_state()

    # allow ctk-style .cget passthrough for a couple keys used internally
    def cget(self, key):  # type: ignore[override]
        if key == "state":
            return self._state
        return super().cget(key)


# ── entry ───────────────────────────────────────────────────────────────────
class CTkEntry(tk.Entry):
    def __init__(self, master, textvariable=None, width=140, placeholder_text="",
                 show=None, fg_color=None, text_color=None, font=None, justify="left",
                 border_width=None, corner_radius=None, **kw):
        self._placeholder = placeholder_text or ""
        self._has_placeholder = False
        _font = font or DEFAULT_FONT
        opts = dict(
            bg=_resolve_color(fg_color, master, ENTRY_BG),
            fg=_resolve_color(text_color, master, ENTRY_FG),
            insertbackground=ENTRY_FG,
            disabledbackground=DISABLED_BG,
            relief="flat",
            font=_font,
            justify=justify,
            width=_px_to_chars(width, _font),
        )
        if textvariable is not None:
            opts["textvariable"] = textvariable
        if show is not None:
            opts["show"] = show
        super().__init__(master, **opts, **kw)
        self._show = show or ""
        if self._placeholder:
            self._show_placeholder()
            self.bind("<FocusIn>", self._clear_placeholder, add="+")
            self.bind("<FocusOut>", self._restore_placeholder, add="+")

    def _show_placeholder(self):
        if not super().get():
            self._has_placeholder = True
            super().configure(show="", fg=DISABLED_FG)
            super().insert(0, self._placeholder)

    def _clear_placeholder(self, _e=None):
        if self._has_placeholder:
            self._has_placeholder = False
            super().delete(0, "end")
            super().configure(show=self._show, fg=ENTRY_FG)

    def _restore_placeholder(self, _e=None):
        if not super().get():
            self._show_placeholder()

    def get(self):  # type: ignore[override]
        if self._has_placeholder:
            return ""
        return super().get()

    def insert(self, index, string):  # type: ignore[override]
        if self._has_placeholder:
            self._has_placeholder = False
            super().delete(0, "end")
            super().configure(show=self._show, fg=ENTRY_FG)
        return super().insert(index, string)

    def configure(self, **kw):  # type: ignore[override]
        if "placeholder_text" in kw:
            self._placeholder = kw.pop("placeholder_text") or ""
        if "fg_color" in kw:
            kw["bg"] = _resolve_color(kw.pop("fg_color"), self.master, ENTRY_BG)
        if "text_color" in kw:
            kw["fg"] = _resolve_color(kw.pop("text_color"), self.master, ENTRY_FG)
        if "show" in kw:
            self._show = kw["show"] or ""
        kw.pop("corner_radius", None)
        kw.pop("border_color", None)
        return super().configure(**kw)


# ── checkbox ────────────────────────────────────────────────────────────────
class CTkCheckBox(tk.Checkbutton):
    def __init__(self, master, text="", variable=None, command=None, fg_color=None,
                 text_color=None, font=None, **kw):
        bg = _bg_of(master)
        opts = dict(
            text=text, bg=bg, fg=_resolve_color(text_color, master, LABEL_FG),
            activebackground=bg, activeforeground=LABEL_FG,
            selectcolor=_resolve_color(fg_color, master, ENTRY_BG),
            font=font or DEFAULT_FONT, anchor="w", takefocus=0,
        )
        if variable is not None:
            opts["variable"] = variable
        if command is not None:
            opts["command"] = command
        super().__init__(master, **opts, **kw)

    def configure(self, **kw):  # type: ignore[override]
        if "text_color" in kw:
            kw["fg"] = _resolve_color(kw.pop("text_color"), self.master, LABEL_FG)
        if "fg_color" in kw:
            kw["selectcolor"] = _resolve_color(kw.pop("fg_color"), self.master, ENTRY_BG)
        return super().configure(**kw)


# ── textbox ─────────────────────────────────────────────────────────────────
class CTkTextbox(tk.Text):
    def __init__(self, master, height=120, width=0, wrap="word", font=None,
                 fg_color=None, text_color=None, corner_radius=None, **kw):
        _font = font or DEFAULT_FONT
        _, line_h = _font_metrics(_font)
        opts = dict(
            bg=_resolve_color(fg_color, master, TEXT_BG),
            fg=_resolve_color(text_color, master, TEXT_FG),
            insertbackground=TEXT_FG,
            relief="flat",
            wrap=wrap,
            height=max(2, round(height / line_h)),
            font=_font,
            padx=6, pady=4,
        )
        if width:
            opts["width"] = _px_to_chars(width, _font)
        super().__init__(master, **opts, **kw)

    def configure(self, **kw):  # type: ignore[override]
        if "fg_color" in kw:
            kw["bg"] = _resolve_color(kw.pop("fg_color"), self.master, TEXT_BG)
        if "text_color" in kw:
            kw["fg"] = _resolve_color(kw.pop("text_color"), self.master, TEXT_FG)
        kw.pop("corner_radius", None)
        return super().configure(**kw)


# ── scrollable frame ────────────────────────────────────────────────────────
class CTkScrollableFrame(_GridMixin, tk.Frame):
    """A frame that scrolls vertically.

    Like CustomTkinter's version, geometry-manager calls (pack/grid/place)
    operate on the OUTER container, while children added with this widget as
    their master land in the inner scrollable area.
    """

    def __init__(self, master, label_text=None, fg_color=None, width=0, height=0, **kw):
        bg = _resolve_color(fg_color, master, FRAME_BG)
        self._outer = tk.Frame(master, bg=bg, highlightthickness=0)

        if label_text:
            self._header = tk.Label(
                self._outer, text=label_text, bg=ROW_BG, fg=LABEL_FG,
                font=DEFAULT_FONT, anchor="w", padx=8, pady=4,
            )
            self._header.pack(side="top", fill="x")

        self._canvas = tk.Canvas(self._outer, bg=bg, highlightthickness=0, bd=0)
        self._vsb = ttk.Scrollbar(self._outer, orient="vertical",
                                  command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._vsb.set)
        self._vsb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        super().__init__(self._canvas, bg=bg, highlightthickness=0)
        self._win = self._canvas.create_window((0, 0), window=self, anchor="nw")

        self.bind("<Configure>", self._on_inner_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        # Re-apply width/scrollregion when the frame first becomes visible.
        # Tabs other than the first are built while hidden, so the canvas has
        # no real width yet and their content would otherwise appear blank
        # until the user interacts with it.
        self._canvas.bind("<Map>", self._on_map)
        self._canvas.bind("<Enter>", lambda _e: self._bind_wheel())
        self._canvas.bind("<Leave>", lambda _e: self._unbind_wheel())

    def _on_inner_configure(self, _e=None):
        try:
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        except Exception:
            pass

    def _on_canvas_configure(self, event):
        self._canvas.itemconfigure(self._win, width=event.width)

    def _on_map(self, _e=None):
        try:
            self._canvas.update_idletasks()
            width = self._canvas.winfo_width()
            if width > 1:
                self._canvas.itemconfigure(self._win, width=width)
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        except Exception:
            pass

    def _bind_wheel(self):
        try:
            self._canvas.bind_all("<MouseWheel>", self._on_wheel)
        except Exception:
            pass

    def _unbind_wheel(self):
        try:
            self._canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass

    def _on_wheel(self, event):
        # The binding is global (bind_all) while the pointer is over this
        # frame; guard against a canvas that has since been destroyed (e.g. a
        # popup closed while the pointer was still inside it) to avoid Tcl
        # errors on the next scroll.
        try:
            if not self._canvas.winfo_exists():
                self._unbind_wheel()
                return
            self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            self._unbind_wheel()

    # proxy geometry managers to the outer container
    def pack(self, **kw):
        self._outer.pack(**kw)

    def grid(self, **kw):
        self._outer.grid(**kw)

    def place(self, **kw):
        self._outer.place(**kw)

    def pack_forget(self):
        self._outer.pack_forget()

    def grid_forget(self):
        self._outer.grid_forget()

    def destroy(self):
        # self (the inner frame) lives inside self._outer, so destroying _outer
        # will recursively call this destroy() again — guard against that.
        if getattr(self, "_destroying", False):
            return super().destroy()
        self._destroying = True
        try:
            self._unbind_wheel()
        except Exception:
            pass
        self._outer.destroy()

    def configure(self, **kw):  # type: ignore[override]
        if "fg_color" in kw:
            bg = _resolve_color(kw.pop("fg_color"), self.master, FRAME_BG)
            self._canvas.configure(bg=bg)
            self._outer.configure(bg=bg)
            kw["bg"] = bg
        return super().configure(**kw)


# ── segmented button ────────────────────────────────────────────────────────
class CTkSegmentedButton(_GridMixin, tk.Frame):
    def __init__(self, master, values=None, command=None, font=None, **kw):
        super().__init__(master, bg=_bg_of(master))
        self._command = command
        self._values = list(values or [])
        self._value = None
        self._btns = {}
        for v in self._values:
            b = tk.Label(self, text=v, bg=SEG_OFF, fg=BTN_TEXT, font=font or DEFAULT_FONT,
                         padx=12, pady=4, cursor="hand2")
            b.pack(side="left", padx=1)
            b.bind("<Button-1>", lambda _e, val=v: self._pick(val))
            self._btns[v] = b

    def _pick(self, value):
        self.set(value)
        if self._command:
            try:
                self._command(value)
            except Exception:
                pass

    def set(self, value):
        self._value = value
        for v, b in self._btns.items():
            b.configure(bg=SEG_ON if v == value else SEG_OFF)

    def get(self):
        return self._value or ""

    def configure(self, **kw):  # type: ignore[override]
        if "values" in kw:
            kw.pop("values")
        return super().configure(**kw)


# ── tab view ────────────────────────────────────────────────────────────────
class CTkTabview(_GridMixin, tk.Frame):
    def __init__(self, master, command=None, fg_color=None, **kw):
        super().__init__(master, bg=_resolve_color(fg_color, master, WIN_BG))
        self._command = command
        self._tabs = {}
        self._btns = {}
        self._current = None

        self._bar = tk.Frame(self, bg=WIN_BG)
        self._bar.pack(side="top", fill="x", padx=6, pady=(6, 0))
        self._content = tk.Frame(self, bg=FRAME_BG)
        self._content.pack(side="top", fill="both", expand=True, padx=6, pady=6)
        self._content.grid_rowconfigure(0, weight=1)
        self._content.grid_columnconfigure(0, weight=1)

    def add(self, name):
        frame = CTkFrame(self._content, fg_color=FRAME_BG)
        self._tabs[name] = frame
        btn = tk.Label(self._bar, text=name, bg=SEG_OFF, fg=BTN_TEXT,
                       font=("Segoe UI", 13), padx=16, pady=6, cursor="hand2")
        btn.pack(side="left", padx=(0, 2))
        btn.bind("<Button-1>", lambda _e, n=name: self.set(n))
        btn.bind("<Enter>", lambda _e, n=name: self._hover(n, True))
        btn.bind("<Leave>", lambda _e, n=name: self._hover(n, False))
        self._btns[name] = btn
        if self._current is None:
            self.set(name)
        return frame

    def _hover(self, name, entering):
        if name == self._current:
            return
        self._btns[name].configure(bg=SEG_HOVER if entering else SEG_OFF)

    def set(self, name):
        if name not in self._tabs:
            return
        if self._current is not None and self._current in self._tabs:
            self._tabs[self._current].grid_forget()
            self._btns[self._current].configure(bg=SEG_OFF)
        self._current = name
        self._tabs[name].grid(row=0, column=0, sticky="nsew")
        self._btns[name].configure(bg=SEG_ON)
        # Force a layout pass so content built while this tab was hidden gets
        # its real geometry immediately instead of appearing blank.
        try:
            self._tabs[name].update_idletasks()
        except Exception:
            pass
        if self._command:
            try:
                self._command()
            except Exception:
                pass

    def get(self):
        return self._current


# ── image ───────────────────────────────────────────────────────────────────
class CTkImage:
    def __init__(self, light_image=None, dark_image=None, size=None):
        from PIL import ImageTk
        img = dark_image or light_image
        if size is not None:
            try:
                img = img.resize(size)
            except Exception:
                pass
        self.photo = ImageTk.PhotoImage(img)
