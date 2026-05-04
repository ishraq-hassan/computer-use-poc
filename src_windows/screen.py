"""Win32 screen capture and input."""

from __future__ import annotations

import ctypes
import io
import logging
import os
import time
from dataclasses import dataclass

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass

import pyautogui
from PIL import ImageGrab

pyautogui.FAILSAFE = True


# ── Mouse ──────────────────────────────────────────────────────────────────────

_BUTTON_FLAGS = {
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}


def move_cursor(x: int, y: int):
    ctypes.windll.user32.SetCursorPos(int(x), int(y))


def click_at(x: int, y: int, button: str = "left", clicks: int = 1):
    move_cursor(x, y)
    time.sleep(0.05)
    down, up = _BUTTON_FLAGS.get(button, _BUTTON_FLAGS["left"])
    for i in range(clicks):
        if i:
            time.sleep(0.08)
        ctypes.windll.user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.02)
        ctypes.windll.user32.mouse_event(up, 0, 0, 0, 0)


def mouse_down(button: str = "left"):
    ctypes.windll.user32.mouse_event(_BUTTON_FLAGS.get(button, _BUTTON_FLAGS["left"])[0], 0, 0, 0, 0)


def mouse_up(button: str = "left"):
    ctypes.windll.user32.mouse_event(_BUTTON_FLAGS.get(button, _BUTTON_FLAGS["left"])[1], 0, 0, 0, 0)


def scroll(dy: int, dx: int = 0):
    if dy:
        ctypes.windll.user32.mouse_event(0x0800, 0, 0, int(dy * 120), 0)
    if dx:
        ctypes.windll.user32.mouse_event(0x1000, 0, 0, int(dx * 120), 0)


# ── Keyboard ───────────────────────────────────────────────────────────────────

_KEY_MAP = {
    "BACKSPACE": "backspace", "DELETE": "delete",
    "ENTER": "enter", "RETURN": "enter", "TAB": "tab",
    "ESC": "esc", "ESCAPE": "esc",
    "UP": "up", "DOWN": "down", "LEFT": "left", "RIGHT": "right",
    "PAGEUP": "pageup", "PAGEDOWN": "pagedown", "HOME": "home", "END": "end",
    "CTRL": "ctrl", "CONTROL": "ctrl", "SHIFT": "shift",
    "ALT": "alt", "OPTION": "alt", "SPACE": "space",
    "WIN": "winleft", "WINDOWS": "winleft", "META": "winleft",
    "SUPER": "winleft", "CMD": "winleft", "COMMAND": "winleft",
}
_MODIFIERS = {"ctrl", "shift", "alt", "winleft"}


def map_key(name: str) -> str:
    return _KEY_MAP.get(name.strip().upper(), name.strip().lower())


def press_keys(keys: list[str]):
    mapped = [map_key(k) for k in keys]
    if len(mapped) == 1:
        pyautogui.press(mapped[0])
    else:
        pyautogui.hotkey(*mapped)


# ── Screen ─────────────────────────────────────────────────────────────────────

def get_screen_size() -> tuple[int, int]:
    return (
        ctypes.windll.user32.GetSystemMetrics(0),
        ctypes.windll.user32.GetSystemMetrics(1),
    )


def capture_screenshot() -> bytes:
    img = ImageGrab.grab()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def get_foreground_window_title() -> str:
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return ""
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    if not length:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


# ── Unified actions ────────────────────────────────────────────────────────────

@dataclass
class Action:
    type: str
    x: int | None = None
    y: int | None = None
    text: str | None = None
    keys: list[str] | None = None
    modifiers: list[str] | None = None
    button: str = "left"
    scroll_x: int = 0
    scroll_y: int = 0
    drag_path: list[tuple[int, int]] | None = None


def _hold_modifiers(mods: list[str] | None, fn):
    held = [map_key(k) for k in (mods or []) if map_key(k) in _MODIFIERS]
    try:
        for m in held:
            pyautogui.keyDown(m)
        fn()
    finally:
        for m in reversed(held):
            pyautogui.keyUp(m)


def execute_actions(actions: list[Action]):
    for a in actions:
        try:
            _do(a)
        except Exception as e:
            logging.getLogger("cua").error("%s failed: %s", a.type, e)
        if a.type not in ("wait", "navigate"):
            time.sleep(0.5)


def _do(a: Action):
    if a.type in ("click", "double_click"):
        if a.x is None or a.y is None:
            return
        n = 2 if a.type == "double_click" else 1
        _hold_modifiers(a.modifiers, lambda: click_at(a.x, a.y, a.button, n))

    elif a.type == "type":
        if a.x is not None and a.y is not None:
            click_at(a.x, a.y)
            time.sleep(0.15)
        pyautogui.write(a.text or "", interval=0.01)

    elif a.type == "keypress":
        if a.keys:
            press_keys(a.keys)

    elif a.type == "scroll":
        if a.x is not None and a.y is not None:
            move_cursor(a.x, a.y)
            time.sleep(0.05)
        _hold_modifiers(a.modifiers, lambda: scroll(a.scroll_y, a.scroll_x))

    elif a.type == "move":
        if a.x is not None and a.y is not None:
            _hold_modifiers(a.modifiers, lambda: move_cursor(a.x, a.y))

    elif a.type == "drag":
        if a.drag_path and len(a.drag_path) >= 2:
            def do_drag():
                move_cursor(*a.drag_path[0])
                time.sleep(0.05)
                mouse_down("left")
                for p in a.drag_path[1:]:
                    move_cursor(*p)
                    time.sleep(0.02)
                mouse_up("left")
            _hold_modifiers(a.modifiers, do_drag)

    elif a.type == "wait":
        time.sleep(2)

    elif a.type == "navigate":
        if a.text:
            os.startfile(a.text)
            time.sleep(2)
