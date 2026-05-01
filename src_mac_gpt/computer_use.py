"""
OpenAI Computer Use (CUA) loop targeting the Native macOS Desktop.

Implements Option 1 from the docs: screenshot → model → execute actions → screenshot → repeat.
Uses pynput for native mouse/keyboard control and 'screencapture' for visuals.
"""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import time
from typing import Any

from AppKit import NSScreen
from openai import OpenAI
from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import Key
from pynput.mouse import Button
from pynput.mouse import Controller as MouseController
from rich.console import Console

from .cost_tracker import CostTracker

console = Console()

# Controllers
mouse = MouseController()
keyboard = KeyboardController()


# ── Key normalization (Mapping standardized names to pynput Keys) ────────────


def get_pynput_key(key_name: str) -> Any:
    """Map model-emitted key names to pynput Key objects."""
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
        "OPTION": Key.alt,
        "META": Key.cmd,
        "CMD": Key.cmd,
        "COMMAND": Key.cmd,
        "SPACE": Key.space,
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


def handle_computer_actions(actions: list[Any]) -> None:
    """Execute a batch of model-returned actions natively on macOS."""
    button_map = {"left": Button.left, "middle": Button.middle, "right": Button.right}

    for action in actions:
        action_type = getattr(action, "type", None) or action.get("type")
        action_keys = getattr(action, "keys", None)

        if action_type == "click":
            x, y = action.x, action.y
            btn = button_map.get(
                getattr(action, "button", "left") or "left", Button.left
            )

            def do_click():
                mouse.position = (x, y)
                mouse.click(btn, 1)

            with_modifiers(action_keys, do_click)

        elif action_type == "double_click":
            x, y = action.x, action.y
            btn = button_map.get(
                getattr(action, "button", "left") or "left", Button.left
            )

            def do_double_click():
                mouse.position = (x, y)
                mouse.click(btn, 2)

            with_modifiers(action_keys, do_double_click)

        elif action_type == "drag":
            path = normalize_drag_path(action.path)
            if len(path) < 2:
                raise ValueError("drag action requires at least two path points")

            def do_drag():
                sx, sy = path[0]
                mouse.position = (sx, sy)
                mouse.press(Button.left)
                for dx, dy in path[1:]:
                    mouse.position = (dx, dy)
                    time.sleep(0.01)  # Small delay for macOS to register drag
                mouse.release(Button.left)

            with_modifiers(action_keys, do_drag)

        elif action_type == "move":
            x, y = action.x, action.y

            def do_move():
                mouse.position = (x, y)

            with_modifiers(action_keys, do_move)

        elif action_type == "scroll":
            x, y = action.x, action.y
            dx = getattr(action, "scrollX", 0) or 0
            dy = getattr(action, "scrollY", 0) or 0

            # pynput scroll uses (dx, dy). OpenAI scroll is (x, y) plus deltas.
            def do_scroll():
                mouse.position = (x, y)
                mouse.scroll(dx, dy)

            with_modifiers(action_keys, do_scroll)

        elif action_type == "keypress":
            # Press all keys down to create a chord (e.g. Cmd + Space)
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


def capture_screenshot(width: int, height: int) -> str:
    """Capture a macOS desktop screenshot and resize it to logical dimensions."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        # -x: mute sound
        subprocess.run(["screencapture", "-x", tmp_path], check=True)
        # Resize the retina screenshot down to the logical bounds to align coordinates
        subprocess.run(
            ["sips", "-z", str(height), str(width), tmp_path],
            check=True,
            capture_output=True,
        )

        with open(tmp_path, "rb") as f:
            png_bytes = f.read()
        return base64.b64encode(png_bytes).decode("utf-8")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def get_screen_size() -> tuple[int, int]:
    """Get the logical dimensions of the main screen."""
    size = NSScreen.mainScreen().frame().size
    return int(size.width), int(size.height)


# ── Main CUA loop ───────────────────────────────────────────────────────────


def run_computer_use(
    task: str,
    model: str = "gpt-5.4-mini",
    tracker: CostTracker | None = None,
    max_steps: int = 30,
) -> dict[str, Any]:
    """
    Run the full computer-use loop natively on macOS.
    """
    if tracker is None:
        tracker = CostTracker()

    client = OpenAI()
    width, height = get_screen_size()

    computer_tool = {"type": "computer"}

    console.print("\n[bold cyan]🖥  Computer Use Agent (Native macOS)[/bold cyan]")
    console.print(f"  Task: [italic]{task}[/italic]")
    console.print(f"  Model: {model}  |  Max steps: {max_steps}\n")

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
            handle_computer_actions(actions)
        except Exception as e:
            console.print(f"  [red]⚠ Action execution failed: {e}[/red]")
            break

        # Capture screenshot and scale down to logical dimensions
        screenshot_b64 = capture_screenshot(width, height)

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
