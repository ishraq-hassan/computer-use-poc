"""
OpenAI Computer Use (CUA) loop targeting the native Windows Desktop.

Implements Option 1 from the docs: screenshot → model → execute actions → screenshot → repeat.
Uses pyautogui for keyboard, raw Win32 (ctypes) for mouse, and PIL.ImageGrab for visuals.

We use pyautogui (not pynput) for keyboard on Windows because pynput's
SendInput-based keystrokes silently fail to reach the foreground window on
Windows 11 in practice — actions execute without errors but produce no
visible effect. pyautogui's variant lands reliably.

We do NOT use pyautogui for mouse clicks because pyautogui.click() on Windows
calls mouse_event with MOUSEEVENTF_ABSOLUTE and normalizes coords by
pyautogui.size() — which returns the PRIMARY monitor's dimensions, not the
virtual desktop's. On a multi-monitor setup, a click at (x, y) on the
secondary monitor gets remapped onto the primary, landing in the wrong place.
Instead we call Win32 SetCursorPos + mouse_event (without MOUSEEVENTF_ABSOLUTE)
directly: SetCursorPos accepts physical pixels across the whole virtual
desktop, and mouse_event without the ABSOLUTE flag clicks at the current
cursor position, so multi-monitor clicks land where the model intended.

Multi-monitor: captures the entire virtual desktop (all monitors) so the model
can see every display. Model coords are relative to the screenshot's top-left;
pyautogui's SetCursorPos uses primary-monitor-origin coords (which extend to
negative x/y for monitors left of / above the primary). We translate every
emitted coord by the virtual-screen offset before clicking.

DPI: the process is marked per-monitor DPI-aware at module load. This forces
GetSystemMetrics, PIL.ImageGrab.grab, and pyautogui's mouse calls to all
operate in physical pixels, so the model's screenshot dimensions, the model's
click coordinates, and the actual cursor position are in a single consistent
coordinate space. Without this, ImageGrab silently flips the process to
DPI-aware on its first grab, causing GetSystemMetrics (queried earlier) to be
in logical pixels while the screenshot is in physical pixels — clicks on a
HiDPI secondary monitor would then land on the wrong position.
"""

from __future__ import annotations

import base64
import ctypes
import io
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any


def _set_dpi_aware() -> None:
    """Mark this process as per-monitor DPI-aware. Must run before geometry queries."""
    try:
        # Windows 8.1+: PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            # Fallback for Windows 7/Vista: system-wide DPI awareness
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


_set_dpi_aware()

import pyautogui  # noqa: E402
from openai import OpenAI  # noqa: E402
from PIL import ImageDraw, ImageGrab, Image  # noqa: E402
from rich.console import Console  # noqa: E402

from .cost_tracker import CostTracker  # noqa: E402

console = Console()

# Move cursor to a screen corner to abort the agent. Default True; keep on as a kill switch.
pyautogui.FAILSAFE = True


# ── Raw Win32 mouse helpers (multi-monitor correct) ─────────────────────────

# mouse_event flags
_MEF_LEFTDOWN = 0x0002
_MEF_LEFTUP = 0x0004
_MEF_RIGHTDOWN = 0x0008
_MEF_RIGHTUP = 0x0010
_MEF_MIDDLEDOWN = 0x0020
_MEF_MIDDLEUP = 0x0040
_MEF_WHEEL = 0x0800
_MEF_HWHEEL = 0x1000

_BUTTON_DOWN_UP = {
    "left": (_MEF_LEFTDOWN, _MEF_LEFTUP),
    "right": (_MEF_RIGHTDOWN, _MEF_RIGHTUP),
    "middle": (_MEF_MIDDLEDOWN, _MEF_MIDDLEUP),
}


def _move_cursor(x: int, y: int) -> None:
    """Move the cursor to physical-pixel (x, y) on the virtual desktop."""
    ctypes.windll.user32.SetCursorPos(int(x), int(y))


def _get_cursor_pos() -> tuple[int, int]:
    """Read the cursor's current position in physical pixels."""
    point = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def _click_at(x: int, y: int, button: str = "left", clicks: int = 1) -> tuple[int, int]:
    """Move to (x, y) and click via Win32. Returns the cursor position actually
    reached after SetCursorPos — if it differs from (x, y) the caller should
    treat that as a coordinate-mapping bug and surface it in the log."""
    _move_cursor(x, y)
    time.sleep(0.05)
    actual = _get_cursor_pos()
    down, up = _BUTTON_DOWN_UP.get(button, _BUTTON_DOWN_UP["left"])
    for i in range(clicks):
        if i > 0:
            time.sleep(0.08)  # gap so consecutive clicks register as separate
        ctypes.windll.user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.02)
        ctypes.windll.user32.mouse_event(up, 0, 0, 0, 0)
    return actual


def _mouse_down(button: str = "left") -> None:
    down, _ = _BUTTON_DOWN_UP.get(button, _BUTTON_DOWN_UP["left"])
    ctypes.windll.user32.mouse_event(down, 0, 0, 0, 0)


def _mouse_up(button: str = "left") -> None:
    _, up = _BUTTON_DOWN_UP.get(button, _BUTTON_DOWN_UP["left"])
    ctypes.windll.user32.mouse_event(up, 0, 0, 0, 0)


def _scroll(dy: int, dx: int = 0) -> None:
    """Wheel scroll at current cursor position. dy>0 scrolls up; dx>0 scrolls right."""
    if dy:
        ctypes.windll.user32.mouse_event(_MEF_WHEEL, 0, 0, int(dy * 120), 0)
    if dx:
        ctypes.windll.user32.mouse_event(_MEF_HWHEEL, 0, 0, int(dx * 120), 0)


# ── Window inspection (descriptive logging + model context) ─────────────────

_GA_ROOT = 2  # for GetAncestor: top-level owner


def _get_window_title(hwnd: int) -> str:
    """Return the title of an HWND, or '' if untitled / inaccessible."""
    if not hwnd:
        return ""
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    if length == 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _ascii_safe(s: str) -> str:
    """Encode a string with non-ASCII chars escaped — safe for legacy Windows consoles.

    Window titles can contain emoji or media-control glyphs (e.g. ● U+25CF in
    YouTube tab titles) that crash plain print() under cp1252. Use this for
    anything we print to the terminal; the API input still gets full Unicode.
    """
    return s.encode("ascii", "backslashreplace").decode("ascii")


def window_at(x: int, y: int) -> str:
    """Return the title of the top-level window at physical screen point (x, y)."""
    point = wintypes.POINT(int(x), int(y))
    # WindowFromPoint takes POINT by value
    ctypes.windll.user32.WindowFromPoint.argtypes = [wintypes.POINT]
    ctypes.windll.user32.WindowFromPoint.restype = ctypes.c_void_p
    hwnd = ctypes.windll.user32.WindowFromPoint(point)
    if not hwnd:
        return "<no window>"
    root = ctypes.windll.user32.GetAncestor(hwnd, _GA_ROOT)
    title = _get_window_title(root or hwnd)
    return title or "<untitled>"


_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


_DWMWA_CLOAKED = 14
# Class names of system-overlay windows that report as visible but should not
# appear in the user-facing window list (desktop, search popup, etc.).
_GHOST_CLASSES = {"Progman", "WorkerW", "Windows.UI.Core.CoreWindow"}


def _is_cloaked(hwnd: int) -> bool:
    """True if DWM has cloaked this window (e.g. on another virtual desktop)."""
    cloaked = ctypes.c_int(0)
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd,
            _DWMWA_CLOAKED,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
    except OSError:
        return False
    return cloaked.value != 0


def _get_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def list_visible_windows(max_count: int = 25) -> list[dict]:
    """Enumerate visible top-level windows with non-empty titles and bounding
    rectangles, filtering out ghost / system-overlay windows. Foreground first.
    """
    fg_hwnd = ctypes.windll.user32.GetForegroundWindow()
    vleft, vtop, vwidth, vheight = get_virtual_screen_rect()
    items: list[dict] = []

    def callback(hwnd, _lparam):
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        if _is_cloaked(hwnd):
            return True
        if _get_class_name(hwnd) in _GHOST_CLASSES:
            return True
        title = _get_window_title(hwnd)
        if not title:
            return True
        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return True
        # Drop windows that span the whole virtual desktop (overlays like
        # NVIDIA's, screen recorders, etc.).
        if (
            rect.left <= vleft
            and rect.top <= vtop
            and rect.right >= vleft + vwidth
            and rect.bottom >= vtop + vheight
        ):
            return True
        items.append(
            {
                "hwnd": hwnd,
                "title": title,
                "is_foreground": hwnd == fg_hwnd,
                "left": rect.left,
                "top": rect.top,
                "right": rect.right,
                "bottom": rect.bottom,
                "width": w,
                "height": h,
            }
        )
        return True

    ctypes.windll.user32.EnumWindows(_EnumWindowsProc(callback), 0)
    # Foreground first, then alphabetical by title
    items.sort(key=lambda w: (not w["is_foreground"], w["title"].lower()))
    return items[:max_count]


def format_window_context(
    capture_bbox: tuple[int, int, int, int] | None = None,
) -> str:
    """Plain-text summary of the OS window state with bounding boxes.

    Only windows that overlap the captured region are listed — if a window
    isn't in the screenshot, the model can't click it, so listing it just
    invites bad coords. The screenshot's dimensions are stated explicitly so
    the model knows the valid click range.
    """
    windows = list_visible_windows()

    if capture_bbox is None:
        cap_left, cap_top = 0, 0
        cap_right = cap_bottom = None
        size_line = ""
    else:
        cap_left, cap_top, cap_right, cap_bottom = capture_bbox
        cap_w = cap_right - cap_left
        cap_h = cap_bottom - cap_top
        size_line = (
            f"Screenshot is {cap_w}x{cap_h}. "
            f"Valid click range: (0, 0) to ({cap_w - 1}, {cap_h - 1})."
        )

    def overlaps(w: dict) -> bool:
        if cap_right is None or cap_bottom is None:
            return True
        return not (
            w["right"] <= cap_left
            or w["left"] >= cap_right
            or w["bottom"] <= cap_top
            or w["top"] >= cap_bottom
        )

    visible = [w for w in windows if overlaps(w)]

    if not visible:
        body = "Window context: (no visible windows in the captured region)"
        return f"{size_line}\n{body}" if size_line else body

    def fmt(w: dict) -> str:
        rl = w["left"] - cap_left
        rt = w["top"] - cap_top
        return f"{w['title']!r} at ({rl}, {rt}) size {w['width']}x{w['height']}"

    lines = []
    if size_line:
        lines.append(size_line)
    lines.append(
        "Window context (positions match the screenshot coords; only windows"
        " visible in this view are listed):"
    )
    fg = next((w for w in visible if w["is_foreground"]), None)
    if fg:
        lines.append(f"  Foreground (receives keyboard input): {fmt(fg)}")
    else:
        # Foreground exists but is off-screen of the captured region
        any_fg = next((w for w in windows if w["is_foreground"]), None)
        if any_fg:
            lines.append(
                "  Foreground window is outside this view — click any visible"
                " window first to redirect keyboard focus."
            )
    others = [w for w in visible if not w["is_foreground"]]
    if others:
        lines.append("  Other visible windows:")
        for w in others:
            lines.append(f"    - {fmt(w)}")
    return "\n".join(lines)


# ── Screenshot annotation (visual click-intent log) ─────────────────────────


def save_intent_overlay(
    src_path: Path, dst_path: Path, actions: list[Any]
) -> None:
    """Copy `src_path` and draw markers at every click/move/scroll/drag target
    from `actions` (using the screenshot-relative coords the model emitted)."""
    img = Image.open(src_path).convert("RGBA")
    draw = ImageDraw.Draw(img, "RGBA")
    for i, action in enumerate(actions, start=1):
        atype = getattr(action, "type", None) or (
            action.get("type") if isinstance(action, dict) else None
        )
        if atype in ("click", "double_click", "move", "scroll"):
            ax = getattr(action, "x", None)
            ay = getattr(action, "y", None)
            if ax is None or ay is None:
                continue
            color = "red" if atype.endswith("click") else "yellow"
            r = 28
            draw.ellipse([ax - r, ay - r, ax + r, ay + r], outline=color, width=6)
            draw.ellipse([ax - 3, ay - 3, ax + 3, ay + 3], fill=color)
            draw.text((ax + r + 6, ay - 14), f"{i}: {atype}", fill=color)
        elif atype == "drag":
            path = getattr(action, "path", None)
            if not path:
                continue
            pts = []
            for p in path:
                if isinstance(p, dict):
                    pts.append((p.get("x"), p.get("y")))
                elif isinstance(p, (list, tuple)) and len(p) >= 2:
                    pts.append((p[0], p[1]))
            color = "blue"
            for j in range(1, len(pts)):
                draw.line([pts[j - 1], pts[j]], fill=color, width=4)
            for p in pts:
                draw.ellipse([p[0] - 6, p[1] - 6, p[0] + 6, p[1] + 6], fill=color)
    img.save(dst_path)


# ── Virtual-screen geometry (all monitors) ──────────────────────────────────

# GetSystemMetrics indices for the bounding rect of the virtual desktop.
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79


def get_virtual_screen_rect() -> tuple[int, int, int, int]:
    """Return (left, top, width, height) of the bounding rect of all monitors."""
    user32 = ctypes.windll.user32
    left = user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
    return left, top, width, height


# ── Per-monitor enumeration ─────────────────────────────────────────────────

_MONITORINFOF_PRIMARY = 1


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", ctypes.c_ulong),
    ]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    ctypes.c_void_p,  # HMONITOR
    ctypes.c_void_p,  # HDC
    ctypes.POINTER(wintypes.RECT),
    wintypes.LPARAM,
)


def list_monitors() -> list[dict]:
    """Enumerate every connected monitor.

    Returns a list of dicts with `index` (1 = primary), `left/top/right/bottom`
    in physical pixels on the virtual desktop, `width/height`, and `is_primary`.
    Primary monitor is always index 1; remaining monitors are sorted by
    (top, left) for predictable numbering across runs.
    """
    raw: list[dict] = []

    def callback(hmon, hdc, lprect, lparam):
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        r = info.rcMonitor
        raw.append(
            {
                "left": r.left,
                "top": r.top,
                "right": r.right,
                "bottom": r.bottom,
                "width": r.right - r.left,
                "height": r.bottom - r.top,
                "is_primary": bool(info.dwFlags & _MONITORINFOF_PRIMARY),
            }
        )
        return True

    ctypes.windll.user32.EnumDisplayMonitors(
        None, None, _MONITORENUMPROC(callback), 0
    )
    # Primary first, then others sorted by (top, left)
    raw.sort(key=lambda m: (not m["is_primary"], m["top"], m["left"]))
    for i, m in enumerate(raw, start=1):
        m["index"] = i
    return raw


def monitor_for_foreground(monitors: list[dict]) -> dict:
    """Return the monitor dict that contains the current foreground window.

    Uses the window's center point so a window straddling two monitors picks
    the one it occupies more of. Falls back to the primary monitor if no
    foreground window can be resolved.
    """
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    primary = next((m for m in monitors if m["is_primary"]), monitors[0])
    if not hwnd:
        return primary
    rect = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2
    for m in monitors:
        if m["left"] <= cx < m["right"] and m["top"] <= cy < m["bottom"]:
            return m
    return primary


def select_capture_region(
    arg: str | int, monitors: list[dict]
) -> tuple[str, tuple[int, int, int, int]]:
    """Resolve a --monitor argument to (label, (left, top, right, bottom)).

    `arg` may be:
      - "all" — entire virtual desktop spanning every monitor
      - "primary" or 1 — the primary monitor
      - 2, 3, ... — secondary monitors in the order list_monitors() returns
    """
    if isinstance(arg, str) and arg.lower() == "all":
        vleft, vtop, vwidth, vheight = get_virtual_screen_rect()
        return (
            "all monitors (virtual desktop)",
            (vleft, vtop, vleft + vwidth, vtop + vheight),
        )

    if isinstance(arg, str) and arg.lower() == "primary":
        idx = 1
    else:
        try:
            idx = int(arg)
        except (TypeError, ValueError):
            raise ValueError(
                f"--monitor must be 'all', 'primary', or a 1-based index; got {arg!r}"
            )

    if idx < 1 or idx > len(monitors):
        raise ValueError(
            f"--monitor {idx} is out of range; only {len(monitors)} monitor(s) detected"
        )

    m = monitors[idx - 1]
    label = (
        f"monitor {idx} (primary)"
        if m["is_primary"]
        else f"monitor {idx} ({m['width']}x{m['height']} @ ({m['left']}, {m['top']}))"
    )
    return label, (m["left"], m["top"], m["right"], m["bottom"])


# ── Key normalization (mapping standardized names to pyautogui key strings) ─

_KEY_MAP = {
    "BACKSPACE": "backspace",
    "DELETE": "delete",
    "ENTER": "enter",
    "RETURN": "enter",
    "TAB": "tab",
    "ESC": "esc",
    "ESCAPE": "esc",
    "UP": "up",
    "DOWN": "down",
    "LEFT": "left",
    "RIGHT": "right",
    "PAGEUP": "pageup",
    "PAGEDOWN": "pagedown",
    "HOME": "home",
    "END": "end",
    "CTRL": "ctrl",
    "CONTROL": "ctrl",
    "SHIFT": "shift",
    "ALT": "alt",
    "SPACE": "space",
    # "winleft" is more reliable than "win" for opening the Start menu via
    # pyautogui.press on Windows 10/11.
    "WIN": "winleft",
    "WINDOWS": "winleft",
    "META": "winleft",
    "SUPER": "winleft",
    "CMD": "winleft",
}

_MODIFIER_KEYS = {"ctrl", "shift", "alt", "winleft", "winright", "win"}


def map_key(key_name: str) -> str:
    """Map a model-emitted key name to pyautogui's lowercase key string."""
    return _KEY_MAP.get(key_name.upper(), key_name.lower())


# ── System instructions for the model ───────────────────────────────────────

SYSTEM_INSTRUCTIONS = """\
HARD RULES — follow exactly, no exceptions:

1. IF THE TARGET APP ISN'T VISIBLE, LAUNCH IT — don't improvise on whatever
   is visible. Before clicking on anything, first verify the screenshot
   actually contains the app the task is about. If it doesn't, your first
   action is: keypress `["WIN"]` to open Start, then `type` the app name
   ("Chrome", "Notepad", "Calculator"), then keypress `["ENTER"]`. Do NOT
   click on a generic text editor and assume it is Notepad. Do NOT use
   Ctrl+L to "open" an app — Ctrl+L only focuses a browser's address bar.

2. AT MOST 3 ACTIONS PER TURN, FEWER IS BETTER. Default to 1 action and let
   the next screenshot guide you. You may emit up to 3 actions in a turn
   ONLY when they are tightly coupled with no plausible failure point
   between them (e.g. type-then-Enter in a focused text field). If any
   action could fail or change the screen unexpectedly, stop after it and
   wait for the screenshot.

3. PREFER THE MOUSE for visible targets. If a button, link, menu item, tab,
   or text field is visible, click it with mouse coordinates. Use keyboard
   shortcuts only when there is no visible target — including the case in
   Rule 1 (launching a missing app).

4. VERIFY EVERY ACTION VISUALLY. After each action, examine the new
   screenshot. Confirm the click landed on the intended element, the typed
   text appeared in the intended field, the right window came forward. If
   anything looks wrong, recover (press Esc, click Close, click the right
   window) before continuing.

5. NO SHELL EXPANSION. Strings like %USERPROFILE%, $HOME, ~ are NOT expanded
   inside File Explorer, Save As dialogs, or Notepad. They appear as literal
   characters. Always type absolute paths, e.g. C:\\\\Users\\\\<name>\\\\Desktop.

6. APPS MAY ALREADY BE RUNNING. Notepad, Chrome, etc. may open with a stale
   document or tab. After launching an app, look at the screenshot and use
   File > New (or Ctrl+N) for a clean document if required by the task.

7. ASCII TEXT ONLY when typing. Do not emit non-ASCII characters (e.g. °, é,
   smart quotes) — they are typed as escape sequences and corrupt the input.
   Use plain ASCII substitutions ("28C" instead of "28°C").
"""


def normalize_drag_path(path: Any) -> list[tuple[int, int]]:
    """Accept drag paths as [x, y] pairs or {x, y} objects."""
    if not isinstance(path, list):
        raise ValueError("drag action requires a path array")
    normalized = []
    for point in path:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            normalized.append((point[0], point[1]))
        elif isinstance(point, dict) and "x" in point and "y" in point:
            normalized.append((point["x"], point["y"]))
        else:
            raise ValueError(
                "drag path entries must be coordinate pairs or {x, y} objects"
            )
    return normalized


# ── Modifier helper ──────────────────────────────────────────────────────────


def with_modifiers(keys: list[str] | None, action_fn):
    """Hold modifier keys via pyautogui for the duration of an action."""
    mods = [map_key(k) for k in (keys or [])]
    actual_mods = [m for m in mods if m in _MODIFIER_KEYS]

    try:
        for m in actual_mods:
            pyautogui.keyDown(m)
        action_fn()
    finally:
        for m in reversed(actual_mods):
            pyautogui.keyUp(m)


# ── Action handler ───────────────────────────────────────────────────────────


def handle_computer_actions(
    actions: list[Any],
    virtual_origin: tuple[int, int],
    capture_size: tuple[int, int] | None = None,
) -> None:
    """
    Execute a batch of model-returned actions on Windows via pyautogui.

    `virtual_origin` is the (left, top) of the captured region in physical
    screen coords. Model coords are screenshot-relative; we add `virtual_origin`
    before every mouse action so clicks land at the right physical pixel.

    `capture_size` is `(width, height)` of the captured region. If given, mouse
    actions whose coords fall outside the screenshot bounds are skipped with a
    warning — this prevents the model from accidentally clicking on a different
    monitor by emitting negative or oversized coords.
    """
    vx, vy = virtual_origin
    cap_w, cap_h = capture_size if capture_size is not None else (None, None)

    def to_screen(x: int, y: int) -> tuple[int, int]:
        return x + vx, y + vy

    def out_of_bounds(x: int, y: int) -> bool:
        if cap_w is None or cap_h is None:
            return False
        return x < 0 or y < 0 or x >= cap_w or y >= cap_h

    for action in actions:
        action_type = getattr(action, "type", None) or action.get("type")
        action_keys = getattr(action, "keys", None)

        # Debug: dump the action so we can see exactly what the model emitted
        # and confirm pyautogui receives the right inputs.
        try:
            action_repr = action.model_dump()
        except AttributeError:
            action_repr = {
                k: getattr(action, k, None)
                for k in (
                    "type",
                    "keys",
                    "text",
                    "x",
                    "y",
                    "button",
                    "scrollX",
                    "scrollY",
                    "path",
                )
                if getattr(action, k, None) is not None
            }
        console.print(f"    [dim]-> {action_repr}[/dim]")

        if action_type == "click":
            if out_of_bounds(action.x, action.y):
                console.print(
                    f"    [yellow]⚠ rejecting out-of-bounds click "
                    f"({action.x}, {action.y}); valid range is "
                    f"(0, 0) to ({cap_w - 1}, {cap_h - 1})[/yellow]"
                )
                continue
            sx, sy = to_screen(action.x, action.y)
            btn = getattr(action, "button", "left") or "left"
            target_window = window_at(sx, sy)
            console.print(
                f"    [cyan]click[/cyan] at screen ({sx}, {sy}) "
                f"[dim]→ window:[/dim] [bold]{_ascii_safe(target_window)!r}[/bold]"
            )

            def do_click():
                actual = _click_at(sx, sy, button=btn)
                if actual != (sx, sy):
                    console.print(
                        f"    [yellow]⚠ cursor at {actual} after SetCursorPos"
                        f"({sx}, {sy}) — coord mapping may be off[/yellow]"
                    )

            with_modifiers(action_keys, do_click)

        elif action_type == "double_click":
            if out_of_bounds(action.x, action.y):
                console.print(
                    f"    [yellow]⚠ rejecting out-of-bounds double_click "
                    f"({action.x}, {action.y}); valid range is "
                    f"(0, 0) to ({cap_w - 1}, {cap_h - 1})[/yellow]"
                )
                continue
            sx, sy = to_screen(action.x, action.y)
            btn = getattr(action, "button", "left") or "left"
            target_window = window_at(sx, sy)
            console.print(
                f"    [cyan]double_click[/cyan] at screen ({sx}, {sy}) "
                f"[dim]→ window:[/dim] [bold]{_ascii_safe(target_window)!r}[/bold]"
            )

            def do_double_click():
                actual = _click_at(sx, sy, button=btn, clicks=2)
                if actual != (sx, sy):
                    console.print(
                        f"    [yellow]⚠ cursor at {actual} after SetCursorPos"
                        f"({sx}, {sy}) — coord mapping may be off[/yellow]"
                    )

            with_modifiers(action_keys, do_double_click)

        elif action_type == "drag":
            path = normalize_drag_path(action.path)
            if len(path) < 2:
                raise ValueError("drag action requires at least two path points")

            screen_path = [to_screen(px, py) for px, py in path]

            def do_drag():
                start_x, start_y = screen_path[0]
                _move_cursor(start_x, start_y)
                time.sleep(0.05)
                _mouse_down("left")
                for dx, dy in screen_path[1:]:
                    _move_cursor(dx, dy)
                    time.sleep(0.02)
                _mouse_up("left")

            with_modifiers(action_keys, do_drag)

        elif action_type == "move":
            if out_of_bounds(action.x, action.y):
                console.print(
                    f"    [yellow]⚠ rejecting out-of-bounds move "
                    f"({action.x}, {action.y})[/yellow]"
                )
                continue
            sx, sy = to_screen(action.x, action.y)

            def do_move():
                _move_cursor(sx, sy)

            with_modifiers(action_keys, do_move)

        elif action_type == "scroll":
            if out_of_bounds(action.x, action.y):
                console.print(
                    f"    [yellow]⚠ rejecting out-of-bounds scroll "
                    f"({action.x}, {action.y})[/yellow]"
                )
                continue
            sx, sy = to_screen(action.x, action.y)
            dx = getattr(action, "scrollX", 0) or 0
            dy = getattr(action, "scrollY", 0) or 0

            def do_scroll():
                _move_cursor(sx, sy)
                _scroll(dy, dx)

            with_modifiers(action_keys, do_scroll)

        elif action_type == "keypress":
            keys = [map_key(k) for k in action.keys]
            if len(keys) == 1:
                pyautogui.press(keys[0])
            else:
                pyautogui.hotkey(*keys)

        elif action_type == "type":
            pyautogui.write(action.text, interval=0.01)

        elif action_type == "wait":
            time.sleep(2)

        elif action_type == "screenshot":
            pass

        else:
            console.print(f"  [yellow]⚠ Unsupported action: {action_type}[/yellow]")

        # Add a small buffer delay after any UI action (except wait/screenshot)
        if action_type not in ("wait", "screenshot"):
            time.sleep(0.5)


# ── Screenshot helper ────────────────────────────────────────────────────────


def capture_screenshot(
    bbox: tuple[int, int, int, int] | None = None,
    save_path: Path | None = None,
) -> str:
    """Capture the screen as a base64 PNG.

    `bbox` is `(left, top, right, bottom)` in virtual-desktop coords. If None,
    captures the entire virtual desktop. If `save_path` is given, also writes
    the PNG to disk for post-run inspection.
    """
    if bbox is not None:
        img = ImageGrab.grab(bbox=bbox, all_screens=True)
    else:
        img = ImageGrab.grab(all_screens=True)
    if save_path is not None:
        img.save(save_path, format="PNG")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ── Main CUA loop ───────────────────────────────────────────────────────────


def run_computer_use(
    task: str,
    model: str = "gpt-5.4-mini",
    tracker: CostTracker | None = None,
    max_steps: int = 30,
    monitor: str | int = "primary",
) -> dict[str, Any]:
    """
    Run the full computer-use loop natively on Windows.

    `monitor` selects which screen the agent operates on. "primary" (default)
    or 1 picks the primary monitor; 2, 3, ... pick others (see list_monitors);
    "all" uses the entire virtual desktop. Single-monitor mode is much more
    reliable than "all" because computer-use models struggle with stitched
    multi-monitor screenshots.
    """
    if tracker is None:
        tracker = CostTracker()

    client = OpenAI()
    monitors = list_monitors()

    follow_mode = isinstance(monitor, str) and monitor.lower() == "follow"
    if follow_mode:
        current_mon = monitor_for_foreground(monitors)
        region_label = (
            f"follow mode (initial: monitor {current_mon['index']}; "
            "agent's screenshots will track the foreground window's monitor)"
        )
        rleft, rtop, rright, rbottom = (
            current_mon["left"],
            current_mon["top"],
            current_mon["right"],
            current_mon["bottom"],
        )
    else:
        current_mon = None
        region_label, (rleft, rtop, rright, rbottom) = select_capture_region(
            monitor, monitors
        )
    rwidth, rheight = rright - rleft, rbottom - rtop
    capture_bbox = (rleft, rtop, rright, rbottom)
    virtual_origin = (rleft, rtop)

    computer_tool = {"type": "computer"}

    # Per-run screenshot directory, e.g. screenshots/run_2026-04-30_14-23-45/
    run_dir = Path("screenshots") / f"run_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)

    console.print("\n[bold cyan]🖥  Computer Use Agent (Native Windows)[/bold cyan]")
    console.print(f"  Task: [italic]{task}[/italic]")
    console.print(f"  Detected {len(monitors)} monitor(s):")
    for m in monitors:
        marker = "[bold green]*[/bold green]" if m["is_primary"] else " "
        console.print(
            f"    {marker} [cyan]monitor {m['index']}[/cyan] "
            f"{m['width']}x{m['height']} @ ({m['left']}, {m['top']})"
            + ("  [dim](primary)[/dim]" if m["is_primary"] else "")
        )
    console.print(
        f"  Capturing: [bold]{region_label}[/bold] — "
        f"{rwidth}x{rheight} @ ({rleft}, {rtop})  |  "
        f"Model: {model}  |  Max steps: {max_steps}"
    )
    console.print(f"  Screenshots: [cyan]{run_dir}[/cyan]")
    console.print(
        "  [yellow]Switch focus off this terminal in the next 3 seconds "
        "(Alt+Tab to a target window, or click on the desktop)...[/yellow]"
    )
    time.sleep(3)
    console.print()

    # ── Step 1: Initial request ──────────────────────────────────────
    t0 = time.time()
    response = client.responses.create(
        model=model,
        tools=[computer_tool],
        input=task,
        instructions=SYSTEM_INSTRUCTIONS,
        truncation="auto",
    )
    duration = time.time() - t0
    rec = tracker.record(response, "computer_use", model, duration, "initial_request")
    tracker.print_live_step(rec)

    # ── Step 2+: Action loop ─────────────────────────────────────────
    step = 0
    prev_screenshot_path: Path | None = None
    while step < max_steps:
        step += 1

        # Find computer_call in output
        computer_call = None
        for item in response.output:
            if getattr(item, "type", None) == "computer_call":
                computer_call = item
                break

        if computer_call is None:
            break

        # Execute actions
        actions = computer_call.actions
        action_types = [getattr(a, "type", "?") for a in actions]
        console.print(f"  [dim]Step {step}:[/dim] actions={action_types}")

        # Save an annotated copy of the screenshot the model based this step
        # on, with red circles at every click target. Skipped for step 1 since
        # there is no prior screenshot.
        if prev_screenshot_path is not None:
            try:
                save_intent_overlay(
                    prev_screenshot_path,
                    run_dir / f"step_{step:02d}_intent.png",
                    list(actions),
                )
            except Exception as e:
                console.print(f"  [yellow]⚠ Could not save intent overlay: {e}[/yellow]")

        try:
            handle_computer_actions(
                actions, virtual_origin, capture_size=(rwidth, rheight)
            )
        except Exception as e:
            console.print(f"  [red]⚠ Action execution failed: {e}[/red]")
            break

        # In follow mode, re-check which monitor holds the foreground window.
        # If it moved (e.g. the agent just opened Notepad on a different monitor),
        # update the capture region so the next screenshot shows the action.
        if follow_mode:
            new_mon = monitor_for_foreground(monitors)
            if new_mon["index"] != current_mon["index"]:
                console.print(
                    f"  [magenta]↪ Foreground moved to monitor {new_mon['index']} "
                    f"({new_mon['width']}x{new_mon['height']} @ "
                    f"({new_mon['left']}, {new_mon['top']})); switching capture[/magenta]"
                )
                current_mon = new_mon
                rleft, rtop = new_mon["left"], new_mon["top"]
                rright, rbottom = new_mon["right"], new_mon["bottom"]
                rwidth, rheight = new_mon["width"], new_mon["height"]
                capture_bbox = (rleft, rtop, rright, rbottom)
                virtual_origin = (rleft, rtop)

        # Capture screenshot of the selected region and save it for inspection
        screenshot_path = run_dir / f"step_{step:02d}.png"
        screenshot_b64 = capture_screenshot(
            bbox=capture_bbox, save_path=screenshot_path
        )
        prev_screenshot_path = screenshot_path

        # Build the window context the model will see for this turn.
        window_ctx = format_window_context(capture_bbox=capture_bbox)

        # Send screenshot back. Re-pass instructions every turn so the model
        # can't drift away from the rules. Also send the OS window list as a
        # separate user message so the model knows what is open/focused.
        t0 = time.time()
        response = client.responses.create(
            model=model,
            tools=[computer_tool],
            previous_response_id=response.id,
            instructions=SYSTEM_INSTRUCTIONS,
            truncation="auto",
            input=[
                {
                    "type": "computer_call_output",
                    "call_id": computer_call.call_id,
                    "output": {
                        "type": "computer_screenshot",
                        "image_url": f"data:image/png;base64,{screenshot_b64}",
                        "detail": "original",
                    },
                },
                {"role": "user", "content": window_ctx},
            ],
        )
        duration = time.time() - t0
        rec = tracker.record(
            response, "computer_use", model, duration, f"step_{step}_screenshot"
        )
        tracker.print_live_step(rec)

    # ── Extract final output ─────────────────────────────────────────
    final_text = ""
    for item in response.output:
        if getattr(item, "type", None) == "message":
            content = getattr(item, "content", [])
            if isinstance(content, list):
                for part in content:
                    text = getattr(part, "text", None)
                    if text:
                        final_text += text
            elif isinstance(content, str):
                final_text += content

    console.print(f"\n  [bold green]✓ Completed in {step} steps[/bold green]")
    if final_text:
        console.print(f"  [green]Answer:[/green] {final_text[:500]}")

    return {
        "final_output": final_text,
        "steps": step,
        "tracker": tracker,
    }
