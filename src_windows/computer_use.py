"""
OpenAI Computer Use (CUA) loop targeting the native Windows Desktop.

Implements Option 1 from the docs: screenshot → model → execute actions → screenshot → repeat.
Uses pynput for native mouse/keyboard control and PIL.ImageGrab for visuals.

Multi-monitor: captures the entire virtual desktop (all monitors) so the model
can see every display. Model coords are relative to the screenshot's top-left;
pynput's SetCursorPos uses primary-monitor-origin coords (which extend to
negative x/y for monitors left of / above the primary). We translate every
emitted coord by the virtual-screen offset before clicking.

DPI: the process is marked per-monitor DPI-aware at module load. This forces
GetSystemMetrics, PIL.ImageGrab.grab, and SetCursorPos to all operate in
physical pixels, so the model's screenshot dimensions, the model's click
coordinates, and pynput's mouse calls are in a single consistent coordinate
space. Without this, ImageGrab silently flips the process to DPI-aware on its
first grab, causing GetSystemMetrics (queried earlier) to be in logical pixels
while the screenshot is in physical pixels — clicks on a HiDPI secondary
monitor would then land on the wrong position.
"""

from __future__ import annotations

import base64
import ctypes
import io
import time
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

from openai import OpenAI  # noqa: E402
from PIL import ImageGrab  # noqa: E402
from pynput.keyboard import Controller as KeyboardController  # noqa: E402
from pynput.keyboard import Key  # noqa: E402
from pynput.mouse import Button  # noqa: E402
from pynput.mouse import Controller as MouseController  # noqa: E402
from rich.console import Console  # noqa: E402

from .cost_tracker import CostTracker  # noqa: E402

console = Console()

# Controllers
mouse = MouseController()
keyboard = KeyboardController()


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


# ── Key normalization (Mapping standardized names to pynput Keys) ───────────


def get_pynput_key(key_name: str) -> Any:
    """Map model-emitted key names to pynput Key objects on Windows."""
    key_map = {
        "BACKSPACE": Key.backspace,
        "DELETE": Key.delete,
        "ENTER": Key.enter,
        "RETURN": Key.enter,
        "TAB": Key.tab,
        "ESC": Key.esc,
        "ESCAPE": Key.esc,
        "UP": Key.up,
        "DOWN": Key.down,
        "LEFT": Key.left,
        "RIGHT": Key.right,
        "PAGEUP": Key.page_up,
        "PAGEDOWN": Key.page_down,
        "HOME": Key.home,
        "END": Key.end,
        "CTRL": Key.ctrl,
        "CONTROL": Key.ctrl,
        "SHIFT": Key.shift,
        "ALT": Key.alt,
        "SPACE": Key.space,
        # pynput's Key.cmd resolves to the Windows key on Windows.
        "WIN": Key.cmd,
        "WINDOWS": Key.cmd,
        "META": Key.cmd,
        "SUPER": Key.cmd,
        "CMD": Key.cmd,
    }
    return key_map.get(key_name.upper(), key_name.lower())


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
    """Press modifier keys for the duration of an action."""
    modifiers = [get_pynput_key(k) for k in (keys or [])]
    # Filter for only actual modifiers in case the model sends garbage
    actual_mods = [m for m in modifiers if isinstance(m, Key)]

    try:
        for m in actual_mods:
            keyboard.press(m)
        action_fn()
    finally:
        for m in reversed(actual_mods):
            keyboard.release(m)


# ── Action handler ───────────────────────────────────────────────────────────


def handle_computer_actions(
    actions: list[Any], virtual_origin: tuple[int, int]
) -> None:
    """
    Execute a batch of model-returned actions on Windows.

    `virtual_origin` is the (left, top) of the captured virtual desktop. Model
    coords are screenshot-relative; pynput uses primary-monitor-origin coords,
    so we add the virtual origin before every mouse action.
    """
    button_map = {"left": Button.left, "middle": Button.middle, "right": Button.right}
    vx, vy = virtual_origin

    def to_screen(x: int, y: int) -> tuple[int, int]:
        return x + vx, y + vy

    for action in actions:
        action_type = getattr(action, "type", None) or action.get("type")
        action_keys = getattr(action, "keys", None)

        if action_type == "click":
            sx, sy = to_screen(action.x, action.y)
            btn = button_map.get(
                getattr(action, "button", "left") or "left", Button.left
            )

            def do_click():
                mouse.position = (sx, sy)
                mouse.click(btn, 1)

            with_modifiers(action_keys, do_click)

        elif action_type == "double_click":
            sx, sy = to_screen(action.x, action.y)
            btn = button_map.get(
                getattr(action, "button", "left") or "left", Button.left
            )

            def do_double_click():
                mouse.position = (sx, sy)
                mouse.click(btn, 2)

            with_modifiers(action_keys, do_double_click)

        elif action_type == "drag":
            path = normalize_drag_path(action.path)
            if len(path) < 2:
                raise ValueError("drag action requires at least two path points")

            screen_path = [to_screen(px, py) for px, py in path]

            def do_drag():
                start_x, start_y = screen_path[0]
                mouse.position = (start_x, start_y)
                mouse.press(Button.left)
                for dx, dy in screen_path[1:]:
                    mouse.position = (dx, dy)
                    time.sleep(0.01)  # Small delay for Windows to register drag
                mouse.release(Button.left)

            with_modifiers(action_keys, do_drag)

        elif action_type == "move":
            sx, sy = to_screen(action.x, action.y)

            def do_move():
                mouse.position = (sx, sy)

            with_modifiers(action_keys, do_move)

        elif action_type == "scroll":
            sx, sy = to_screen(action.x, action.y)
            dx = getattr(action, "scrollX", 0) or 0
            dy = getattr(action, "scrollY", 0) or 0

            # pynput scroll uses (dx, dy). OpenAI scroll is (x, y) plus deltas.
            def do_scroll():
                mouse.position = (sx, sy)
                mouse.scroll(dx, dy)

            with_modifiers(action_keys, do_scroll)

        elif action_type == "keypress":
            # Press all keys down to create a chord (e.g. Ctrl + C)
            keys = [get_pynput_key(k) for k in action.keys]
            for key in keys:
                if key:
                    keyboard.press(key)
            # Release all keys in reverse order
            for key in reversed(keys):
                if key:
                    keyboard.release(key)

        elif action_type == "type":
            keyboard.type(action.text)

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


def capture_screenshot() -> str:
    """Capture the entire virtual desktop (all monitors) as a base64 PNG."""
    img = ImageGrab.grab(all_screens=True)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ── Main CUA loop ───────────────────────────────────────────────────────────


def run_computer_use(
    task: str,
    model: str = "gpt-5.4-mini",
    tracker: CostTracker | None = None,
    max_steps: int = 30,
) -> dict[str, Any]:
    """
    Run the full computer-use loop natively on Windows.
    """
    if tracker is None:
        tracker = CostTracker()

    client = OpenAI()
    vleft, vtop, vwidth, vheight = get_virtual_screen_rect()
    virtual_origin = (vleft, vtop)

    computer_tool = {"type": "computer"}

    console.print("\n[bold cyan]🖥  Computer Use Agent (Native Windows)[/bold cyan]")
    console.print(f"  Task: [italic]{task}[/italic]")
    console.print(
        f"  Virtual desktop: {vwidth}x{vheight} @ ({vleft}, {vtop})  |  "
        f"Model: {model}  |  Max steps: {max_steps}\n"
    )

    # ── Step 1: Initial request ──────────────────────────────────────
    t0 = time.time()
    response = client.responses.create(
        model=model,
        tools=[computer_tool],
        input=task,
        truncation="auto",
    )
    duration = time.time() - t0
    rec = tracker.record(response, "computer_use", model, duration, "initial_request")
    tracker.print_live_step(rec)

    # ── Step 2+: Action loop ─────────────────────────────────────────
    step = 0
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

        try:
            handle_computer_actions(actions, virtual_origin)
        except Exception as e:
            console.print(f"  [red]⚠ Action execution failed: {e}[/red]")
            break

        # Capture full virtual-desktop screenshot
        screenshot_b64 = capture_screenshot()

        # Send screenshot back
        t0 = time.time()
        response = client.responses.create(
            model=model,
            tools=[computer_tool],
            previous_response_id=response.id,
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
                }
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
