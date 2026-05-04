"""Unified Computer Use agent for Windows — supports Gemini and OpenAI."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except (AttributeError, OSError):
    pass

from dotenv import load_dotenv

from .providers import GeminiProvider, OpenAIProvider, Provider
from .screen import capture_screenshot, execute_actions, get_screen_size

log = logging.getLogger("cua")


def pick_provider(prefs: list[str], task: str, model: str | None) -> Provider:
    for name in prefs:
        name = name.strip().lower()
        if name == "gemini" and (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            return GeminiProvider(task, model) if model else GeminiProvider(task)
        if name == "openai" and os.environ.get("OPENAI_API_KEY"):
            return OpenAIProvider(task, model) if model else OpenAIProvider(task)
    raise RuntimeError(
        f"No API key found for any provider in {prefs}. "
        "Set GEMINI_API_KEY, GOOGLE_API_KEY, or OPENAI_API_KEY."
    )


def cli():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Computer Use agent (Windows)")
    parser.add_argument("--task", required=True, help="Task for the agent")
    parser.add_argument("--providers", default="gemini,openai",
        help="Comma-separated provider preference list (default: gemini,openai)")
    parser.add_argument("--model", default=None, help="Override the provider's default model")
    parser.add_argument("--max-steps", type=int, default=15, help="Max steps (default: 15)")
    parser.add_argument("--no-verbose", action="store_true", help="Suppress per-step logging")
    parser.add_argument("--debug", action="store_true", help="Enable full debug logging (includes HTTP)")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(format="%(message)s", level=logging.DEBUG)
    else:
        logging.basicConfig(format="%(message)s", level=logging.WARNING)
    log.setLevel(logging.DEBUG if not args.no_verbose else logging.INFO)

    prefs = [p.strip() for p in args.providers.split(",")]
    provider = pick_provider(prefs, args.task, args.model)
    screen_w, screen_h = get_screen_size()

    log.info("Provider: %s | Screen: %dx%d | Max steps: %d",
             provider.name, screen_w, screen_h, args.max_steps)
    log.info("Task: %s", args.task)
    time.sleep(0.5)

    t0 = time.time()
    screenshot = capture_screenshot()
    for step in range(1, args.max_steps + 1):
        try:
            actions = provider.get_actions(screenshot, (screen_w, screen_h))
        except KeyboardInterrupt:
            log.info("\nAborted.")
            return
        except Exception as e:
            log.error("Step %d: API error — %s", step, e)
            break

        if actions is None:
            break

        if provider.last_text:
            log.debug("Step %d reasoning: %s", step, provider.last_text.strip())
        for a in actions:
            if a.type in ("click", "double_click") and a.x is not None:
                log.debug("Step %d: %s(%d, %d)", step, a.type, a.x, a.y)
            elif a.type == "type":
                loc = f" at ({a.x}, {a.y})" if a.x is not None else ""
                log.debug("Step %d: type(%r%s)", step, a.text, loc)
            elif a.type == "keypress" and a.keys:
                log.debug("Step %d: keypress(%s)", step, "+".join(a.keys))
            elif a.type == "scroll" and a.x is not None:
                log.debug("Step %d: scroll(%d, %d, dy=%d)", step, a.x, a.y, a.scroll_y)
            elif a.type == "move" and a.x is not None:
                log.debug("Step %d: move(%d, %d)", step, a.x, a.y)
            elif a.type == "drag" and a.drag_path:
                pts = " -> ".join(f"({x},{y})" for x, y in a.drag_path)
                log.debug("Step %d: drag(%s)", step, pts)
            else:
                log.debug("Step %d: %s", step, a.type)

        execute_actions(actions)

        time.sleep(0.3)
        screenshot = capture_screenshot()
    else:
        step = args.max_steps

    elapsed = time.time() - t0
    log.info("\nDone in %d steps (%.1fs)", step, elapsed)
    if provider.last_text:
        log.info("Model: %s", provider.last_text)


if __name__ == "__main__":
    cli()
