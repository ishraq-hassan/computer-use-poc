"""
Gemini Computer Use loop targeting the native macOS Desktop.

Mirrors the OpenAI flavor in src_mac_gpt/, but uses google-genai with
gemini-3.5-flash. The model emits normalized
0-999 coordinates which we denormalize to logical pixels and execute
via pynput. Screenshots are captured with `screencapture` and resized
with `sips` to match logical bounds.

Docs: https://ai.google.dev/gemini-api/docs/computer-use
"""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import time
from typing import Any

from AppKit import NSScreen
from google import genai
from google.genai import types
from google.genai.types import Content, Part
from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import Key
from pynput.mouse import Button
from pynput.mouse import Controller as MouseController
from rich.console import Console

from .cost_tracker import CostTracker

console = Console()

mouse = MouseController()
keyboard = KeyboardController()


# ── Coordinate normalization (Gemini emits 0-999) ────────────────────────────


def denorm_x(x: float, screen_width: int) -> int:
    return int(float(x) / 1000.0 * screen_width)


def denorm_y(y: float, screen_height: int) -> int:
    return int(float(y) / 1000.0 * screen_height)


# ── Key normalization for key_combination ────────────────────────────────────

KEY_MAP: dict[str, Any] = {
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
    "SUPER": Key.cmd,
    "SPACE": Key.space,
}


def to_pynput_key(token: str) -> Any:
    """Map a single key token (e.g. 'Control', 'A', 'Enter') to a pynput key."""
    upper = token.strip().upper()
    if upper in KEY_MAP:
        return KEY_MAP[upper]
    return token.strip().lower()


def press_combo(combo: str) -> None:
    """
    Press a key combination like "Control+A" or "Cmd+Shift+T".

    On macOS the model often emits "Control+..." even though "Cmd+..." is the
    correct macOS shortcut. We swap Control->Cmd for letter-based shortcuts so
    that things like Select-All / Copy / Paste / Save actually work.
    """
    tokens = [t for t in combo.replace(" ", "").split("+") if t]
    if not tokens:
        return

    mods = {t.upper() for t in tokens[:-1]}
    if "CONTROL" in mods or "CTRL" in mods:
        mods.discard("CONTROL")
        mods.discard("CTRL")
        mods.add("CMD")

    keys = [to_pynput_key(m) for m in mods] + [to_pynput_key(tokens[-1])]

    for k in keys:
        if k is not None:
            keyboard.press(k)
    for k in reversed(keys):
        if k is not None:
            keyboard.release(k)


# ── Action handlers ──────────────────────────────────────────────────────────


def execute_action(name: str, args: dict[str, Any], width: int, height: int) -> None:
    """Execute one Gemini Computer Use action natively on macOS."""
    if name == "click_at":
        x, y = denorm_x(args["x"], width), denorm_y(args["y"], height)
        mouse.position = (x, y)
        mouse.click(Button.left, 1)

    elif name == "type_text_at":
        x, y = denorm_x(args["x"], width), denorm_y(args["y"], height)
        mouse.position = (x, y)
        mouse.click(Button.left, 1)
        time.sleep(0.15)
        if args.get("clear_before_typing", True):
            keyboard.press(Key.cmd)
            keyboard.press("a")
            keyboard.release("a")
            keyboard.release(Key.cmd)
            time.sleep(0.05)
            keyboard.press(Key.backspace)
            keyboard.release(Key.backspace)
            time.sleep(0.05)
        keyboard.type(args["text"])
        if args.get("press_enter", True):
            time.sleep(0.1)
            keyboard.press(Key.enter)
            keyboard.release(Key.enter)

    elif name == "hover_at":
        x, y = denorm_x(args["x"], width), denorm_y(args["y"], height)
        mouse.position = (x, y)

    elif name == "key_combination":
        press_combo(args.get("keys", ""))

    elif name == "scroll_document":
        direction = (args.get("direction") or "down").lower()
        dx, dy = 0, 0
        if direction == "down":
            dy = -10
        elif direction == "up":
            dy = 10
        elif direction == "left":
            dx = -10
        elif direction == "right":
            dx = 10
        mouse.scroll(dx, dy)

    elif name == "scroll_at":
        x, y = denorm_x(args["x"], width), denorm_y(args["y"], height)
        mouse.position = (x, y)
        magnitude = int(args.get("magnitude", 800)) // 100  # heuristic scaling
        magnitude = max(1, magnitude)
        direction = (args.get("direction") or "down").lower()
        dx, dy = 0, 0
        if direction == "down":
            dy = -magnitude
        elif direction == "up":
            dy = magnitude
        elif direction == "left":
            dx = -magnitude
        elif direction == "right":
            dx = magnitude
        mouse.scroll(dx, dy)

    elif name == "drag_and_drop":
        sx, sy = denorm_x(args["x"], width), denorm_y(args["y"], height)
        ex, ey = (
            denorm_x(args["destination_x"], width),
            denorm_y(args["destination_y"], height),
        )
        mouse.position = (sx, sy)
        mouse.press(Button.left)
        steps = 20
        for i in range(1, steps + 1):
            mouse.position = (
                int(sx + (ex - sx) * i / steps),
                int(sy + (ey - sy) * i / steps),
            )
            time.sleep(0.01)
        mouse.release(Button.left)

    elif name == "wait_5_seconds":
        time.sleep(5)

    elif name == "navigate":
        # Open the URL via the default macOS browser
        url = args.get("url", "")
        if url:
            subprocess.run(["open", url], check=False)
            time.sleep(2)

    elif name == "open_web_browser":
        # Spotlight-launch Safari
        keyboard.press(Key.cmd)
        keyboard.press(Key.space)
        keyboard.release(Key.space)
        keyboard.release(Key.cmd)
        time.sleep(0.4)
        keyboard.type("Safari")
        time.sleep(0.2)
        keyboard.press(Key.enter)
        keyboard.release(Key.enter)
        time.sleep(1.0)

    elif name in ("search", "go_back", "go_forward"):
        # Browser-only; the active app might be a browser, but we don't
        # assume one — skip with a warning so the agent can self-correct.
        console.print(f"  [yellow]⚠ Skipping browser-only action: {name}[/yellow]")

    else:
        console.print(f"  [yellow]⚠ Unsupported action: {name}[/yellow]")


# ── Screenshot helper ────────────────────────────────────────────────────────


def capture_screenshot_bytes(width: int, height: int) -> bytes:
    """Capture macOS desktop screenshot, resize to logical bounds, return PNG bytes."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(["screencapture", "-x", tmp_path], check=True)
        subprocess.run(
            ["sips", "-z", str(height), str(width), tmp_path],
            check=True,
            capture_output=True,
        )
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def get_screen_size() -> tuple[int, int]:
    size = NSScreen.mainScreen().frame().size
    return int(size.width), int(size.height)


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

    Without this, every prior screenshot is re-billed on every turn — input
    tokens (and latency) grow linearly with step count.
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
                    # Strip image but keep call metadata
                    new_parts.append(
                        Part(
                            function_response=types.FunctionResponse(
                                name=p.function_response.name,
                                response=p.function_response.response or {},
                            )
                        )
                    )
                # else: a pure inline-image Part — drop entirely
            else:
                new_parts.append(p)
        if new_parts:
            out.append(Content(role=c.role, parts=new_parts))
    return out


# ── Main agent loop ─────────────────────────────────────────────────────────


def run_computer_use(
    task: str,
    model: str = "gemini-3.5-flash",
    tracker: CostTracker | None = None,
    max_steps: int = 30,
    keep_screenshots: int = 1,
) -> dict[str, Any]:
    """Run the Gemini Computer Use loop natively on macOS."""
    if tracker is None:
        tracker = CostTracker()

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key) if api_key else genai.Client()

    width, height = get_screen_size()

    console.print("\n[bold cyan]🖥  Gemini Computer Use Agent (Native macOS)[/bold cyan]")
    console.print(f"  Task: [italic]{task}[/italic]")
    console.print(f"  Model: {model}  |  Max steps: {max_steps}")
    console.print(f"  Logical screen: {width}x{height}  |  Keep last {keep_screenshots} screenshot(s)\n")

    config = types.GenerateContentConfig(
        tools=[
            types.Tool(
                computer_use=types.ComputerUse(
                    environment=types.Environment.ENVIRONMENT_BROWSER,
                )
            )
        ],
        system_instruction=(
            "You are operating the user's native macOS desktop, not a web browser. "
            "Use Spotlight (Cmd+Space) to open apps. Coordinates you emit are on a "
            "0-999 normalized grid mapped to the full desktop. Prefer click_at, "
            "type_text_at, key_combination, and scroll_at. Avoid navigate/go_back/"
            "go_forward/search since there is no browser context."
        ),
    )

    initial_png = capture_screenshot_bytes(width, height)

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

    while step < max_steps:
        if not response.candidates:
            break
        candidate = response.candidates[0]
        if not candidate.content or not candidate.content.parts:
            break

        contents.append(candidate.content)

        function_calls = [p.function_call for p in candidate.content.parts if p.function_call]

        # Capture any model text on this turn
        for p in candidate.content.parts:
            if getattr(p, "text", None):
                final_text += p.text

        if not function_calls:
            # No more actions — agent is done
            break

        step += 1
        action_names = [fc.name for fc in function_calls]
        console.print(f"  [dim]Step {step}:[/dim] actions={action_names}")

        function_responses: list[types.FunctionResponse] = []

        for fc in function_calls:
            args = dict(fc.args or {})

            try:
                execute_action(fc.name, args, width, height)
            except Exception as e:
                console.print(f"  [red]⚠ Action {fc.name} failed: {e}[/red]")

            time.sleep(0.6)  # let the UI settle

            shot = capture_screenshot_bytes(width, height)

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
