"""
CLI entry point for the Gemini Computer Use POC (Windows).

Runs both a normal Gemini text baseline and the Gemini Computer Use agent
on the same task, then prints a side-by-side cost comparison.
"""

from __future__ import annotations

import argparse
import sys

# Force UTF-8 on stdout/stderr so rich's emoji/box-drawing in panels and
# tables doesn't crash the legacy Windows console (cp1252 default chokes on
# 🤖, 📊, 💰, etc.). Uses backslashreplace as a last-resort so we degrade
# instead of throwing if anything still doesn't fit.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except (AttributeError, OSError):
    pass

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from .computer_use import run_computer_use
from .cost_tracker import CostTracker
from .normal_llm import run_normal_llm

console = Console()

DEFAULT_TASK = (
    "Open chrome and go to google.com. "
    "Then search for  'the weather in Alexandria, Egypt'. "
    "After the search results load, copy the temperature shown on the page. "
    "Then open notepad. "
    "Paste the temperature into notepad. "
    "Finally, save the file as 'weather.txt' on your desktop and close notepad. "
    "Do not close chrome."
)

# The Computer Use tool only ships on this preview model today.
DEFAULT_CUA_MODEL = "gemini-2.5-computer-use-preview-10-2025"
# Cost-effective text baseline.
DEFAULT_LLM_MODEL = "gemini-2.5-flash"


def cli():
    """CLI entry point."""
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Gemini Computer Use POC (Windows) — compare CUA vs normal LLM costs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python -m src_windows_gemini.main --task "Open Notepad and type Hello"\n'
            "  python -m src_windows_gemini.main --llm-model gemini-2.5-flash-lite\n"
            "  python -m src_windows_gemini.main --monitor 2\n"
            "  python -m src_windows_gemini.main --cua-only\n"
            "  python -m src_windows_gemini.main --llm-only\n"
        ),
    )
    parser.add_argument(
        "--task",
        type=str,
        default=DEFAULT_TASK,
        help="Task description for the agent",
    )
    parser.add_argument(
        "--cua-model",
        type=str,
        default=DEFAULT_CUA_MODEL,
        help=f"Gemini computer-use model (default: {DEFAULT_CUA_MODEL})",
    )
    parser.add_argument(
        "--llm-model",
        type=str,
        default=DEFAULT_LLM_MODEL,
        help=f"Gemini text-baseline model (default: {DEFAULT_LLM_MODEL})",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=30,
        help="Max CUA loop iterations (default: 30)",
    )
    parser.add_argument(
        "--keep-screenshots",
        type=int,
        default=1,
        help="How many of the most recent screenshots to keep in context "
        "(older ones are redacted to save tokens; default: 1)",
    )
    parser.add_argument(
        "--monitor",
        type=str,
        default="primary",
        help=(
            "Which screen the agent operates on. 'primary' (default) or '1' = "
            "primary monitor; '2', '3', ... = additional monitors in detected "
            "order; 'all' = full virtual desktop (less reliable on multi-monitor); "
            "'follow' = re-capture the foreground window's monitor after every "
            "step (recommended for tasks that span multiple apps/monitors)"
        ),
    )
    parser.add_argument(
        "--cua-only",
        action="store_true",
        help="Run only the Computer Use agent (skip normal LLM baseline)",
    )
    parser.add_argument(
        "--llm-only",
        action="store_true",
        help="Run only the normal LLM baseline (skip Computer Use)",
    )

    args = parser.parse_args()

    tracker = CostTracker()

    console.print(
        Panel.fit(
            "[bold]Gemini Computer Use POC (Windows)[/bold]\n"
            f"CUA model: [cyan]{args.cua_model}[/cyan]\n"
            f"LLM baseline: [cyan]{args.llm_model}[/cyan]\n"
            f"Task: [italic]{args.task[:80]}{'...' if len(args.task) > 80 else ''}[/italic]",
            title="🤖 Gemini CUA Cost Comparison",
            border_style="bright_blue",
        )
    )

    if not args.cua_only:
        try:
            llm_result = run_normal_llm(
                task=args.task,
                model=args.llm_model,
                tracker=tracker,
            )
            console.print(llm_result)
        except Exception as e:
            console.print(f"\n[red]✗ Normal LLM failed: {e}[/red]")
            if args.llm_only:
                sys.exit(1)

    if not args.llm_only:
        try:
            cua_result = run_computer_use(
                task=args.task,
                model=args.cua_model,
                tracker=tracker,
                max_steps=args.max_steps,
                keep_screenshots=args.keep_screenshots,
                monitor=args.monitor,
            )
            console.print(cua_result)
        except Exception as e:
            console.print(f"\n[red]✗ Computer Use failed: {e}[/red]")
            import traceback

            traceback.print_exc()

    console.print()
    tracker.print_report()
    console.print()


if __name__ == "__main__":
    cli()
