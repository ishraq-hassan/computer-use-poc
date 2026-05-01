"""
Gemini Computer Use loop targeting the native Windows Desktop.

Mirrors src_mac_gemini/ but uses the Windows infrastructure from src_windows/:
raw Win32 mouse (multi-monitor correct), pyautogui keyboard (pynput's
SendInput-based keystrokes silently fail on Windows 11 in practice), and
PIL.ImageGrab bbox capture against a per-monitor region.

Gemini emits coordinates on a normalized 0–999 grid relative to the
captured region. We denormalize to physical pixels of the captured
monitor, then add the monitor's virtual-desktop offset before issuing
SetCursorPos so clicks land on the right physical pixel even when
operating on a non-primary monitor with mixed DPI.

DPI: the process is marked per-monitor DPI-aware at module load. This
forces GetSystemMetrics, PIL.ImageGrab.grab, and the cursor calls to
operate in physical pixels — keeping the screenshot, the model's
denormalized coords, and the cursor in one consistent space.

Docs: https://ai.google.dev/gemini-api/docs/computer-use
"""

from __future__ import annotations

import base64  # noqa: F401  (kept for parity / future debug dumps)
import ctypes
import io  # noqa: F401
import os
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any


def _set_dpi_aware() -> None:
    """Mark this process as per-monitor DPI-aware. Must run before geometry queries."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


_set_dpi_aware()

import pyautogui  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
from google.genai.types import Content, Part  # noqa: E402
from PIL import Image, ImageDraw, ImageGrab  # noqa: E402
from rich.console import Console  # noqa: E402

from .cost_tracker import CostTracker  # noqa: E402

console = Console()

# Move cursor to a screen corner to abort the agent.
pyautogui.FAILSAFE = True


# ── Raw Win32 mouse helpers (multi-monitor correct) ─────────────────────────

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
    ctypes.windll.user32.SetCursorPos(int(x), int(y))


def _get_cursor_pos() -> tuple[int, int]:
    point = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def _click_at(x: int, y: int, button: str = "left", clicks: int = 1) -> tuple[int, int]:
    """Move to (x, y) and click via Win32 (no MOUSEEVENTF_ABSOLUTE — that flag
    normalizes to the primary monitor and breaks multi-monitor clicks)."""
    _move_cursor(x, y)
    time.sleep(0.05)
    actual = _get_cursor_pos()
    down, up = _BUTTON_DOWN_UP.get(button, _BUTTON_DOWN_UP["left"])
    for i in range(clicks):
        if i > 0:
            time.sleep(0.08)
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


# ── Virtual-screen + per-monitor enumeration ────────────────────────────────

_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79


def get_virtual_screen_rect() -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    return (
        user32.GetSystemMetrics(_SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(_SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN),
    )


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
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.RECT),
    wintypes.LPARAM,
)


def list_monitors() -> list[dict]:
    """Enumerate every connected monitor.

    Returns a list of dicts with `index` (1 = primary), `left/top/right/bottom`
    in physical pixels on the virtual desktop, `width/height`, and `is_primary`.
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
    raw.sort(key=lambda m: (not m["is_primary"], m["top"], m["left"]))
    for i, m in enumerate(raw, start=1):
        m["index"] = i
    return raw


def monitor_for_foreground(monitors: list[dict]) -> dict:
    """Return the monitor dict that contains the current foreground window."""
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
    """Resolve a --monitor argument to (label, (left, top, right, bottom))."""
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
                f"--monitor must be 'all', 'primary', 'follow', or a 1-based index; got {arg!r}"
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


# ── Coordinate normalization (Gemini emits 0-999 relative to the screenshot) ─


def denorm_x(x: float, region_width: int) -> int:
    return int(float(x) / 1000.0 * region_width)


def denorm_y(y: float, region_height: int) -> int:
    return int(float(y) / 1000.0 * region_height)


# ── Key normalization for key_combination ────────────────────────────────────

# Gemini emits keys as text tokens like "Control+A" or "Win". Map each token
# to a pyautogui key string. On Windows, "Cmd"/"Meta"/"Super"/"Win" all mean
# the Windows key — and Control/Ctrl stay as ctrl (do NOT swap to win the way
# the macOS port swaps Control->Cmd; that's the macOS convention only).
KEY_MAP: dict[str, str] = {
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
    "OPTION": "alt",
    "SPACE": "space",
    # Windows key — "winleft" is more reliable than "win" for opening Start
    # via pyautogui.press on Windows 10/11.
    "WIN": "winleft",
    "WINDOWS": "winleft",
    "META": "winleft",
    "SUPER": "winleft",
    "CMD": "winleft",
    "COMMAND": "winleft",
}

_MODIFIER_KEYS = {"ctrl", "shift", "alt", "winleft", "winright", "win"}


def to_pyautogui_key(token: str) -> str:
    upper = token.strip().upper()
    if upper in KEY_MAP:
        return KEY_MAP[upper]
    return token.strip().lower()


def press_combo(combo: str) -> None:
    """Press a key combination like "Control+A" or "Ctrl+Shift+T"."""
    tokens = [t for t in combo.replace(" ", "").split("+") if t]
    if not tokens:
        return
    keys = [to_pyautogui_key(t) for t in tokens]
    if len(keys) == 1:
        pyautogui.press(keys[0])
    else:
        pyautogui.hotkey(*keys)


# ── Action handlers ──────────────────────────────────────────────────────────


def execute_action(
    name: str,
    args: dict[str, Any],
    region_width: int,
    region_height: int,
    virtual_origin: tuple[int, int],
) -> None:
    """Execute one Gemini Computer Use action natively on Windows.

    `region_width/height` are the captured monitor's pixel dimensions used to
    denormalize Gemini's 0-999 coords. `virtual_origin` is the (left, top) of
    the captured region in physical screen coords; we add it to every
    denormalized coord before issuing the Win32 cursor call so multi-monitor
    clicks land on the right physical pixel.
    """
    vx, vy = virtual_origin

    def to_screen(nx: float, ny: float) -> tuple[int, int]:
        return denorm_x(nx, region_width) + vx, denorm_y(ny, region_height) + vy

    if name == "click_at":
        sx, sy = to_screen(args["x"], args["y"])
        console.print(
            f"    [cyan]click_at[/cyan] norm=({args['x']}, {args['y']}) "
            f"-> region=({sx - vx}, {sy - vy}) of {region_width}x{region_height}"
            f" -> screen=({sx}, {sy})"
        )
        actual = _click_at(sx, sy, "left", 1)
        if actual != (sx, sy):
            console.print(
                f"    [yellow]⚠ cursor at {actual} after SetCursorPos"
                f"({sx}, {sy}) — coord mapping may be off[/yellow]"
            )

    elif name == "type_text_at":
        sx, sy = to_screen(args["x"], args["y"])
        console.print(
            f"    [cyan]type_text_at[/cyan] norm=({args['x']}, {args['y']}) "
            f"-> region=({sx - vx}, {sy - vy}) of {region_width}x{region_height}"
            f" -> screen=({sx}, {sy})"
        )
        _click_at(sx, sy, "left", 1)
        time.sleep(0.15)
        if args.get("clear_before_typing", True):
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.05)
            pyautogui.press("backspace")
            time.sleep(0.05)
        pyautogui.write(args["text"], interval=0.01)
        if args.get("press_enter", True):
            time.sleep(0.1)
            pyautogui.press("enter")

    elif name == "hover_at":
        sx, sy = to_screen(args["x"], args["y"])
        _move_cursor(sx, sy)

    elif name == "key_combination":
        press_combo(args.get("keys", ""))

    elif name == "scroll_document":
        direction = (args.get("direction") or "down").lower()
        # Default scroll magnitude (notches) to mirror the macOS port's "10".
        amt = 10
        if direction == "down":
            _scroll(-amt, 0)
        elif direction == "up":
            _scroll(amt, 0)
        elif direction == "left":
            _scroll(0, -amt)
        elif direction == "right":
            _scroll(0, amt)

    elif name == "scroll_at":
        sx, sy = to_screen(args["x"], args["y"])
        _move_cursor(sx, sy)
        time.sleep(0.05)
        magnitude = max(1, int(args.get("magnitude", 800)) // 100)
        direction = (args.get("direction") or "down").lower()
        if direction == "down":
            _scroll(-magnitude, 0)
        elif direction == "up":
            _scroll(magnitude, 0)
        elif direction == "left":
            _scroll(0, -magnitude)
        elif direction == "right":
            _scroll(0, magnitude)

    elif name == "drag_and_drop":
        sx, sy = to_screen(args["x"], args["y"])
        ex, ey = to_screen(args["destination_x"], args["destination_y"])
        _move_cursor(sx, sy)
        time.sleep(0.05)
        _mouse_down("left")
        steps = 20
        for i in range(1, steps + 1):
            _move_cursor(
                int(sx + (ex - sx) * i / steps),
                int(sy + (ey - sy) * i / steps),
            )
            time.sleep(0.01)
        _mouse_up("left")

    elif name == "wait_5_seconds":
        time.sleep(5)

    elif name == "navigate":
        # Open the URL via the user's default Windows browser.
        url = args.get("url", "")
        if url:
            try:
                os.startfile(url)
            except OSError as e:
                console.print(f"  [yellow]⚠ navigate failed: {e}[/yellow]")
            time.sleep(2)

    elif name == "open_web_browser":
        # Run dialog → "chrome" → Enter. Falls back to the user's default
        # browser association if Chrome isn't installed.
        pyautogui.hotkey("winleft", "r")
        time.sleep(0.5)
        pyautogui.write("chrome", interval=0.02)
        time.sleep(0.2)
        pyautogui.press("enter")
        time.sleep(1.5)

    elif name in ("search", "go_back", "go_forward"):
        # Browser-only; we don't assume one is in focus.
        console.print(f"  [yellow]⚠ Skipping browser-only action: {name}[/yellow]")

    else:
        console.print(f"  [yellow]⚠ Unsupported action: {name}[/yellow]")


# ── Screenshot helper ────────────────────────────────────────────────────────


def save_intent_overlay(
    src_path: Path,
    dst_path: Path,
    function_calls: list,
    region_width: int,
    region_height: int,
) -> None:
    """Copy `src_path` and draw markers at every action target in
    `function_calls`, denormalizing Gemini's 0-999 coords against the SAME
    region size that produced `src_path`.

    This is the single most useful debugging artifact when clicks are landing
    in the wrong place: open `step_NN_intent.png` and visually compare where
    the model intended to click against where the cursor actually went.
    """
    img = Image.open(src_path).convert("RGBA")
    draw = ImageDraw.Draw(img, "RGBA")

    for i, fc in enumerate(function_calls, start=1):
        name = fc.name
        args = dict(fc.args or {})

        if name in ("click_at", "type_text_at", "hover_at", "scroll_at"):
            x = args.get("x")
            y = args.get("y")
            if x is None or y is None:
                continue
            ax = denorm_x(x, region_width)
            ay = denorm_y(y, region_height)
            if name in ("click_at", "type_text_at"):
                color = "red"
            elif name == "hover_at":
                color = "yellow"
            else:  # scroll_at
                color = "green"
            r = 28
            draw.ellipse([ax - r, ay - r, ax + r, ay + r], outline=color, width=6)
            draw.ellipse([ax - 3, ay - 3, ax + 3, ay + 3], fill=color)
            label = f"{i}: {name} ({int(x)},{int(y)})"
            draw.text((ax + r + 6, ay - 14), label, fill=color)

        elif name == "drag_and_drop":
            sx_n = args.get("x")
            sy_n = args.get("y")
            ex_n = args.get("destination_x")
            ey_n = args.get("destination_y")
            if None in (sx_n, sy_n, ex_n, ey_n):
                continue
            sx = denorm_x(sx_n, region_width)
            sy = denorm_y(sy_n, region_height)
            ex = denorm_x(ex_n, region_width)
            ey = denorm_y(ey_n, region_height)
            color = "blue"
            draw.line([(sx, sy), (ex, ey)], fill=color, width=4)
            draw.ellipse([sx - 8, sy - 8, sx + 8, sy + 8], fill=color)
            draw.ellipse([ex - 8, ey - 8, ex + 8, ey + 8], fill=color)
            draw.text((sx + 12, sy - 14), f"{i}: drag start", fill=color)
            draw.text((ex + 12, ey - 14), f"{i}: drag end", fill=color)

    img.save(dst_path)


def capture_screenshot_bytes(
    bbox: tuple[int, int, int, int],
    save_path: Path | None = None,
) -> bytes:
    """Capture the configured region of the Windows desktop as PNG bytes.

    `bbox` is `(left, top, right, bottom)` in virtual-desktop physical pixels.
    If `save_path` is given, also write the PNG to disk for post-run inspection.
    """
    img = ImageGrab.grab(bbox=bbox, all_screens=True)
    if save_path is not None:
        img.save(save_path, format="PNG")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── History trimming ────────────────────────────────────────────────────────


def _redact_old_screenshots(
    contents: list[Content], keep_last_n: int = 1
) -> list[Content]:
    """
    Return a copy of `contents` with old screenshots removed to save tokens.

    Keeps the function_response *metadata* (name, response dict) so the
    conversation flow stays valid, but drops the inline image bytes from
    older turns. The most recent `keep_last_n` screenshots are preserved
    so the model can see the current screen state.
    """
    image_locs: list[tuple[int, int]] = []
    for ci, c in enumerate(contents):
        for pi, p in enumerate(c.parts or []):
            has_inline_img = (
                p.inline_data
                and (p.inline_data.mime_type or "").startswith("image/")
            )
            has_fr_img = p.function_response and p.function_response.parts
            if has_inline_img or has_fr_img:
                image_locs.append((ci, pi))

    redact = (
        set(image_locs[:-keep_last_n]) if keep_last_n > 0 else set(image_locs)
    )

    out: list[Content] = []
    for ci, c in enumerate(contents):
        new_parts: list[Part] = []
        for pi, p in enumerate(c.parts or []):
            if (ci, pi) in redact:
                if p.function_response is not None:
                    new_parts.append(
                        Part(
                            function_response=types.FunctionResponse(
                                name=p.function_response.name,
                                response=p.function_response.response or {},
                            )
                        )
                    )
            else:
                new_parts.append(p)
        if new_parts:
            out.append(Content(role=c.role, parts=new_parts))
    return out


# ── System instructions ─────────────────────────────────────────────────────

SYSTEM_INSTRUCTION = """\
You are operating the user's native Windows desktop. Coordinates you emit are
on a 0-999 normalized grid mapped to the captured region (a single monitor
unless --monitor=all was passed). Use Ctrl+ for shortcuts (never Cmd+).

HARD RULES — follow exactly, no exceptions:

1. IF THE TARGET APP ISN'T VISIBLE, LAUNCH IT — don't improvise on whatever
   is visible. Before clicking on anything, first verify the screenshot
   actually contains the app the task is about. If it doesn't, use
   key_combination "Win" to open Start, then type_text_at to type the app
   name ("Chrome", "Notepad", "Calculator") into the search box, then
   click_at the result. Do NOT click on a generic text editor and assume it
   is Notepad. Do NOT use Ctrl+L to "open" an app — Ctrl+L only focuses a
   browser's address bar.

2. AT MOST 1 ACTION PER TURN. Default to 1 action and let the next
   screenshot guide you. Only emit more when they are tightly coupled with
   no plausible failure point between them (e.g. type-then-Enter in a
   focused text field). If any action could fail or change the screen
   unexpectedly, stop after it and wait for the screenshot.

3. PREFER THE MOUSE for visible targets. If a button, link, menu item, tab,
   or text field is visible, click it with mouse coordinates. Use keyboard
   shortcuts only when there is no visible target — including the case in
   Rule 1 (launching a missing app).

4. VERIFY EVERY ACTION VISUALLY. After each action, examine the new
   screenshot. Confirm the click landed on the intended element, the typed
   text appeared in the intended field, the right window came forward. If
   anything looks wrong, recover (key_combination "Escape", click Close,
   click the right window) before continuing.

5. NO SHELL EXPANSION. Strings like %USERPROFILE%, $HOME, ~ are NOT expanded
   inside File Explorer, Save As dialogs, or Notepad. They appear as literal
   characters. Always type absolute paths, e.g. C:\\Users\\<name>\\Desktop.

6. APPS MAY ALREADY BE RUNNING. Notepad, Chrome, etc. may open with a stale
   document or tab. After launching an app, look at the screenshot and use
   File > New (or Ctrl+N) for a clean document if required by the task.

7. ASCII TEXT ONLY when typing. Do not emit non-ASCII characters (e.g. °, é,
   smart quotes) — they are typed as escape sequences and corrupt the input.
   Use plain ASCII substitutions ("28C" instead of "28°C").

8. Avoid navigate/go_back/go_forward/search since there is no browser context
   until you launch one yourself. Prefer click_at, type_text_at,
   key_combination, and scroll_at.
"""


# ── Main agent loop ─────────────────────────────────────────────────────────


def run_computer_use(
    task: str,
    model: str = "gemini-2.5-computer-use-preview-10-2025",
    tracker: CostTracker | None = None,
    max_steps: int = 30,
    keep_screenshots: int = 1,
    monitor: str | int = "primary",
) -> dict[str, Any]:
    """Run the Gemini Computer Use loop natively on Windows.

    `monitor` selects which screen the agent operates on. "primary" (default)
    or 1 picks the primary monitor; 2, 3, ... pick others (see list_monitors);
    "all" uses the entire virtual desktop; "follow" re-captures the foreground
    window's monitor after every step.
    """
    if tracker is None:
        tracker = CostTracker()

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key) if api_key else genai.Client()

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

    run_dir = Path("screenshots") / f"run_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_gemini"
    run_dir.mkdir(parents=True, exist_ok=True)

    console.print("\n[bold cyan]🖥  Gemini Computer Use Agent (Native Windows)[/bold cyan]")
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
        f"Model: {model}  |  Max steps: {max_steps}  |  "
        f"Keep last {keep_screenshots} screenshot(s)"
    )
    console.print(f"  Screenshots: [cyan]{run_dir}[/cyan]")
    console.print(
        "  [yellow]Switch focus off this terminal in the next 3 seconds "
        "(Alt+Tab to a target window, or click on the desktop)...[/yellow]"
    )
    time.sleep(3)
    console.print()

    config = types.GenerateContentConfig(
        tools=[
            types.Tool(
                computer_use=types.ComputerUse(
                    environment=types.Environment.ENVIRONMENT_BROWSER,
                )
            )
        ],
        system_instruction=SYSTEM_INSTRUCTION,
    )

    initial_path = run_dir / "step_00_initial.png"
    initial_png = capture_screenshot_bytes(capture_bbox, save_path=initial_path)

    contents: list[Content] = [
        Content(
            role="user",
            parts=[
                Part(text=task),
                Part.from_bytes(data=initial_png, mime_type="image/png"),
            ],
        )
    ]

    # ── First turn ───────────────────────────────────────────────────────
    t0 = time.time()
    response = client.models.generate_content(
        model=model,
        contents=_redact_old_screenshots(contents, keep_screenshots),
        config=config,
    )
    duration = time.time() - t0
    rec = tracker.record(response, "computer_use", model, duration, "initial_request")
    tracker.print_live_step(rec)

    final_text = ""
    step = 0

    # Track the screenshot the model used to plan the current turn's actions
    # (and the region size that screenshot was captured at — important under
    # follow mode where the region can change between sub-actions). This is
    # what we'll annotate with red intent circles.
    prev_input_screenshot_path: Path = initial_path
    prev_input_region_size: tuple[int, int] = (rwidth, rheight)

    while step < max_steps:
        if not response.candidates:
            break
        candidate = response.candidates[0]
        if not candidate.content or not candidate.content.parts:
            break

        contents.append(candidate.content)

        function_calls = [p.function_call for p in candidate.content.parts if p.function_call]

        for p in candidate.content.parts:
            if getattr(p, "text", None):
                final_text += p.text

        if not function_calls:
            break

        step += 1
        action_names = [fc.name for fc in function_calls]
        console.print(f"  [dim]Step {step}:[/dim] actions={action_names}")

        # Annotate the screenshot the model planned this turn against, so
        # mis-clicks can be diagnosed by eye: open step_NN_intent.png and
        # check whether the red circles land on the intended UI elements.
        try:
            save_intent_overlay(
                prev_input_screenshot_path,
                run_dir / f"step_{step:02d}_intent.png",
                function_calls,
                prev_input_region_size[0],
                prev_input_region_size[1],
            )
        except Exception as e:
            console.print(f"  [yellow]⚠ Could not save intent overlay: {e}[/yellow]")

        function_responses: list[types.FunctionResponse] = []

        # Track the LAST screenshot captured this turn — that's what the model
        # will plan against next turn, so it becomes the next intent overlay's
        # base image.
        last_shot_path: Path | None = None
        last_shot_region: tuple[int, int] | None = None

        for fc in function_calls:
            args = dict(fc.args or {})

            console.print(f"    [dim]-> {fc.name} {args}[/dim]")
            try:
                execute_action(fc.name, args, rwidth, rheight, virtual_origin)
            except Exception as e:
                console.print(f"  [red]⚠ Action {fc.name} failed: {e}[/red]")

            time.sleep(0.6)  # let the UI settle

            # In follow mode, re-check which monitor holds the foreground
            # window between sub-actions (e.g. when the agent just opened a
            # new app on a different monitor) so the next screenshot tracks it.
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

            shot_path = run_dir / f"step_{step:02d}.png"
            shot = capture_screenshot_bytes(capture_bbox, save_path=shot_path)
            last_shot_path = shot_path
            last_shot_region = (rwidth, rheight)

            response_payload: dict[str, Any] = {"url": ""}

            function_responses.append(
                types.FunctionResponse(
                    name=fc.name,
                    response=response_payload,
                    parts=[
                        types.FunctionResponsePart(
                            inline_data=types.FunctionResponseBlob(
                                mime_type="image/png",
                                data=shot,
                            )
                        )
                    ],
                )
            )

        contents.append(
            Content(
                role="user",
                parts=[Part(function_response=fr) for fr in function_responses],
            )
        )

        # Promote this turn's last screenshot to be the planning input the
        # model sees next turn — that's what the next intent overlay annotates.
        if last_shot_path is not None and last_shot_region is not None:
            prev_input_screenshot_path = last_shot_path
            prev_input_region_size = last_shot_region

        t0 = time.time()
        response = client.models.generate_content(
            model=model,
            contents=_redact_old_screenshots(contents, keep_screenshots),
            config=config,
        )
        duration = time.time() - t0
        rec = tracker.record(
            response, "computer_use", model, duration, f"step_{step}_screenshot"
        )
        tracker.print_live_step(rec)

    console.print(f"\n  [bold green]✓ Completed in {step} steps[/bold green]")
    if final_text:
        console.print(f"  [green]Answer:[/green] {final_text[:500]}")

    return {
        "final_output": final_text,
        "steps": step,
        "tracker": tracker,
    }
